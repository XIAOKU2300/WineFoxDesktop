"""Production desktop engine joining Agent 04/05/06 with Agent 07's API."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import mimetypes
import re
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from desktop_backend import VERSION
from desktop_backend.errors import AppError
from desktop_backend.models import HistoryEntry, ImageData, JobRecord
from winefox_core.comfyui_executor import ComfyUIExecutor
from winefox_core.comfyui_types import AuthMode, ComfyUIAuth, ComfyUIError, ComfyUIServerConfig, ExecutionSnapshot
from winefox_core.storage import Database, FavoriteStore, HistoryStore
from winefox_core.storage_models import utcnow_iso
from winefox_core.prompt_service import PromptService
from winefox_core.lora_service import LoraService, normalize_lora_key
from winefox_core.workflow_service import WorkflowEntry, WorkflowService

from .config_adapter import DesktopConfigAdapter
from .llm_client import OpenAICompatibleLLMClient, resolve_provider
from .lora_download import LoraDownloadService
from .lora_profiles import (
    cache_generated_lora_profiles,
    load_lora_profiles,
    lora_metadata_cache_key,
    merge_prompt_tags,
    profile_from_comfy_metadata,
    resolve_prompt_loras,
    resolve_selected_lora_triggers,
)
from .upload_store import FileUploadStore
from .workflow_builder import prepare_workflow

logger = logging.getLogger("winefox.runtime.engine")

TERMINAL_STATES = {"completed", "failed", "cancelled", "detached"}

_IMAGE_FORMATS = {
    "PNG": (".png", "image/png"),
    "JPEG": (".jpg", "image/jpeg"),
    "WEBP": (".webp", "image/webp"),
    "GIF": (".gif", "image/gif"),
    "BMP": (".bmp", "image/bmp"),
    "TIFF": (".tiff", "image/tiff"),
}


def _detect_image_output(data: bytes) -> tuple[str, str]:
    """Return a safe extension and MIME type from decoded image bytes."""
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as image:
            image_format = str(image.format or "").upper()
            image.verify()
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise ValueError("ComfyUI returned undecodable image bytes") from exc
    output = _IMAGE_FORMATS.get(image_format)
    if output is None:
        raise ValueError(f"ComfyUI returned unsupported image format: {image_format or 'unknown'}")
    return output

_WINDOWS_PATH_RE = re.compile(
    r"(?i)(?:\"[a-z]:[\\/][^\"\r\n]+\"|(?:[a-z]:[\\/]|\\\\)[^\s,;\"<>|?*]+)"
)
_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(authorization|api[_-]?key|token|secret|password|passwd|bearer)"
    r"(\s*[:=]\s*)[^\s,;]+"
)
_LONG_SECRET_RE = re.compile(r"\b(?:[0-9a-fA-F]{32,}|[A-Za-z0-9+/]{40,}={0,2})\b")


def _iso_to_epoch(value: str | None) -> float:
    if not value:
        return time.time()
    try:
        from datetime import datetime

        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return time.time()


def _redact_diagnostic_text(value: Any) -> str:
    """Remove secrets and local absolute paths from diagnostic text."""
    text = str(value or "")
    text = _SECRET_VALUE_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)
    text = _LONG_SECRET_RE.sub("[REDACTED]", text)
    return _WINDOWS_PATH_RE.sub("[LOCAL_PATH]", text)


def _comfyui_version(system_stats: Mapping[str, Any] | None) -> str:
    stats = system_stats if isinstance(system_stats, Mapping) else {}
    system = stats.get("system") if isinstance(stats.get("system"), Mapping) else {}
    for source in (stats, system):
        for key in ("comfyui_version", "version", "required_frontend_version"):
            value = source.get(key)
            if value:
                return str(value)
    return ""


class DesktopEngine:
    def __init__(
        self,
        *,
        database: Database,
        config: DesktopConfigAdapter,
        uploads: FileUploadStore,
        workflow_dir: Path,
        output_dir: Path,
        data_root: Path,
    ) -> None:
        self.db = database
        self.config = config
        self.uploads = uploads
        self.workflow_dir = Path(workflow_dir)
        self.output_dir = Path(output_dir)
        self.data_root = Path(data_root)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.workflows = WorkflowService(self.workflow_dir)
        self.workflows.scan()
        self._lora_metadata_cache_key = lora_metadata_cache_key(
            self._connection_config().server_address
        )
        self._reload_lora_profiles()
        self.history_store = HistoryStore(database)
        self.favorite_store = FavoriteStore(database)
        self._queue: asyncio.Queue[int] = asyncio.Queue(maxsize=20)
        self._worker: asyncio.Task | None = None
        self._accepting = False
        self._current_job_id: int | None = None
        self._cancel_requested: set[int] = set()
        self._last_probe: dict[str, Any] | None = None
        self._last_probe_at = 0.0
        self._probe_task: asyncio.Task[dict[str, Any]] | None = None
        self._lora_metadata_task: asyncio.Task[None] | None = None
        self._lora_metadata_attempted: set[str] = set()
        self.lora_download = LoraDownloadService(
            config=self.config,
            data_root=self.data_root,
            connection_factory=self._connection_config,
            on_installed=self._after_lora_installed,
        )

    def _reload_lora_profiles(self) -> None:
        loaded = load_lora_profiles(
            self.data_root,
            auto_cache_key=self._lora_metadata_cache_key,
        )
        service = LoraService(loaded.profiles)
        index: dict[str, list[Any]] = {}
        for family in ("anima", "illustrious"):
            for profile in service.profiles_for(family).values():
                index.setdefault(normalize_lora_key(profile.lora_name), []).append(profile)
        self.loaded_lora_profiles = loaded
        self.lora_service = service
        self._lora_profile_index = index

    async def start(self) -> None:
        if self._accepting:
            return
        self._accepting = True
        rows = self.db.connection.execute(
            "SELECT id, state, comfy_prompt_id FROM jobs WHERE state NOT IN ('completed','failed','cancelled','detached') ORDER BY id"
        ).fetchall()
        for row in rows:
            if row["state"] in {"running", "submitting", "downloading"} and not row["comfy_prompt_id"]:
                self._update_job(int(row["id"]), "failed", error="BACKEND_RESTARTED: 提交状态不明，请重新生成")
                continue
            self._update_job(int(row["id"]), "recovering")
            await self._queue.put(int(row["id"]))
        self._worker = asyncio.create_task(self._worker_loop(), name="winefox-job-worker")

    async def stop(self) -> None:
        self._accepting = False
        await self.lora_download.stop()
        metadata_task = self._lora_metadata_task
        self._lora_metadata_task = None
        if metadata_task is not None:
            metadata_task.cancel()
            await asyncio.gather(metadata_task, return_exceptions=True)
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        self._current_job_id = None

    def busy(self) -> bool:
        return self._current_job_id is not None or self._queue.qsize() > 0 or self.lora_download.active_count() > 0

    def task_count(self) -> int:
        return self._queue.qsize() + (1 if self._current_job_id is not None else 0) + self.lora_download.active_count()

    def _connection_config(self) -> ComfyUIServerConfig:
        settings = self.config.snapshot()
        comfy = settings.get("comfyui", {})
        auth_settings = settings.get("auth", {})
        mode_text = str(auth_settings.get("mode") or "none").lower()
        try:
            mode = AuthMode(mode_text)
        except ValueError:
            mode = AuthMode.NONE
        if mode is AuthMode.BEARER:
            auth = ComfyUIAuth.bearer(self.config.get_secret("comfyui_api_key") or "")
        elif mode is AuthMode.BASIC:
            auth = ComfyUIAuth.basic(
                self.config.get_secret("comfyui_username") or "",
                self.config.get_secret("comfyui_password") or "",
            )
        else:
            auth = ComfyUIAuth()
        return ComfyUIServerConfig(
            server_address=str(comfy.get("server_address") or comfy.get("address") or "127.0.0.1:8188"),
            auth=auth,
            verify_tls=bool(comfy.get("ssl_verify", True)),
        )

    async def probe_comfyui(self, payload: dict | None = None) -> dict[str, Any]:
        force = bool(isinstance(payload, Mapping) and payload.get("force"))
        if not force and self._last_probe is not None and time.monotonic() - self._last_probe_at < 5.0:
            return dict(self._last_probe)

        task = self._probe_task
        if task is None or task.done():
            task = asyncio.create_task(self._probe_comfyui_once())
            self._probe_task = task
        try:
            return await asyncio.shield(task)
        finally:
            if self._probe_task is task and task.done():
                self._probe_task = None

    async def _probe_comfyui_once(self) -> dict[str, Any]:
        connection: ComfyUIServerConfig | None = None
        try:
            connection = self._connection_config()
            async with ComfyUIExecutor(connection) as executor:
                probe = await executor.probe()
            installed_nodes = {str(name).lower() for name in probe.object_info}
            image_workflows = [
                entry for entry in self.workflows.all_entries()
                if entry.mode != "video" and entry.path
            ]
            required_nodes = {
                str(class_name).lower()
                for entry in image_workflows
                for class_name in entry.node_classes
            }
            missing_nodes = sorted(required_nodes - installed_nodes) if installed_nodes else []
            compatible_workflows = (
                sum(
                    1
                    for entry in image_workflows
                    if {str(name).lower() for name in entry.node_classes}.issubset(installed_nodes)
                )
                if installed_nodes
                else None
            )
            result = {
                "ok": probe.reachable,
                "online": probe.reachable,
                "server": connection.server_address,
                "version": _comfyui_version(probe.system_stats),
                "latency_ms": round(probe.latency_ms, 1),
                "node_count": probe.node_count,
                "queue": {"running": probe.queue.running, "pending": probe.queue.pending},
                "checkpoints": list(probe.models.checkpoints),
                "loras": list(probe.models.loras),
                "warnings": list(probe.warnings),
                "missing_nodes": missing_nodes,
                "workflow_total": len(image_workflows),
                "workflow_compatible": compatible_workflows,
                "result": "ok" if probe.reachable else "fail",
                "summary": "ComfyUI 连接正常" if probe.reachable else "无法连接 ComfyUI",
                "steps": [
                    {"status": "ok" if probe.reachable else "fail", "detail": "服务可访问" if probe.reachable else "连接失败"},
                    {"status": "ok" if probe.node_count else "warn", "detail": f"检测到 {probe.node_count} 个节点"},
                    {"status": "ok" if probe.models.checkpoints else "warn", "detail": f"检测到 {len(probe.models.checkpoints)} 个 checkpoint"},
                    {"status": "ok", "detail": f"检测到 {len(probe.models.loras)} 个 LoRA"},
                ],
            }
        except (AppError, ComfyUIError, ValueError) as exc:
            error_code = exc.code if isinstance(exc, AppError) else getattr(exc, "code", "COMFYUI_OFFLINE")
            result = {
                "ok": False,
                "online": False,
                "server": connection.server_address if connection is not None else "",
                "version": "",
                "latency_ms": None,
                "error": _redact_diagnostic_text(exc),
                "error_code": str(getattr(error_code, "value", error_code)),
                "result": "fail",
                "summary": _redact_diagnostic_text(exc),
                "steps": [{"status": "fail", "detail": _redact_diagnostic_text(exc)}],
                "checkpoints": [],
                "loras": [],
                "missing_nodes": [],
                "warnings": [],
                "node_count": None,
                "queue": {"running": 0, "pending": 0},
                "workflow_total": len([
                    entry for entry in self.workflows.all_entries()
                    if entry.mode != "video" and entry.path
                ]),
                "workflow_compatible": None,
            }
        self._last_probe = result
        self._last_probe_at = time.monotonic()
        if result.get("online"):
            self._schedule_lora_metadata_refresh(result.get("loras", []))
        return result

    async def get_catalog(self) -> dict[str, Any]:
        probe = await self.probe_comfyui()
        self._schedule_lora_metadata_refresh(probe.get("loras", []))
        metadata_task = self._lora_metadata_task
        if self.loaded_lora_profiles.count == 0 and metadata_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(metadata_task), timeout=2.2)
            except asyncio.TimeoutError:
                pass
        entries = self.workflows.all_entries()
        workflows = [
            {
                "id": entry.workflow_id,
                "name": entry.name,
                "label": Path(entry.name).stem.replace("_", " "),
                "mode": entry.mode,
                "family": entry.family,
                "valid": entry.valid,
                "error": entry.error,
            }
            for entry in entries
            if entry.mode != "video"
        ]
        checkpoints = [
            {"id": name, "name": name, "label": Path(name).name, "family": "illustrious", "compat": "ready"}
            for name in probe.get("checkpoints", [])
        ]
        loras = [self._catalog_lora(name) for name in probe.get("loras", [])]
        nodes = [
            {"name": name, "installed": False}
            for name in probe.get("missing_nodes", [])
        ]
        return {
            "families": [
                {"id": "anima", "label": "Anima", "description": "Anima 系列工作流"},
                {"id": "illustrious", "label": "Illustrious", "description": "Illustrious XL 系列工作流"},
            ],
            "model_families": ["anima", "illustrious"],
            "workflows": workflows,
            "checkpoints": checkpoints,
            "loras": loras,
            "nodes": nodes,
            "missing_nodes": probe.get("missing_nodes", []),
            "download": self.lora_download.capabilities(),
            "online": bool(probe.get("online")),
            "lora_profiles": {
                "loaded": self.loaded_lora_profiles.count,
                "sources": list(self.loaded_lora_profiles.sources),
            },
        }

    async def search_loras(self, query: str, *, family: str = "illustrious", limit: int = 12) -> dict[str, Any]:
        return await self.lora_download.search(query, family=family, limit=limit)

    async def start_lora_download(self, result_id: str, *, alias: str = "") -> dict[str, Any]:
        return self.lora_download.start_download(result_id, alias=alias)

    async def get_lora_download(self, job_id: str) -> dict[str, Any]:
        return self.lora_download.snapshot(job_id)

    async def probe_lora_bridge(self) -> dict[str, Any]:
        return await self.lora_download.bridge_status()

    async def refresh_after_env_setup(self) -> dict[str, Any]:
        """环境中心装完 Bridge 后调用：清 probe 缓存、强制重探、重载 profiles。"""
        await self._after_lora_installed()
        return {"ok": True, "bridge": await self.probe_lora_bridge()}

    async def _after_lora_installed(self) -> None:
        self._last_probe = None
        self._last_probe_at = 0.0
        self._lora_metadata_attempted.clear()
        self._reload_lora_profiles()
        try:
            await self.probe_comfyui({"force": True})
        except Exception as exc:
            logger.warning("LoRA installed but catalog refresh failed: %s", _redact_diagnostic_text(exc))

    def _schedule_lora_metadata_refresh(self, filenames: Any) -> None:
        if not isinstance(filenames, (list, tuple)):
            return
        active = self._lora_metadata_task
        if active is not None and not active.done():
            return
        if active is not None and active.done():
            self._lora_metadata_task = None
        current_cache_key = lora_metadata_cache_key(
            self._connection_config().server_address
        )
        if current_cache_key != self._lora_metadata_cache_key:
            self._lora_metadata_cache_key = current_cache_key
            self._lora_metadata_attempted.clear()
            self._reload_lora_profiles()
        existing = set(self._lora_profile_index)
        candidates: list[str] = []
        pending_markers: set[str] = set()
        for value in filenames:
            name = str(value or "").strip()
            marker = normalize_lora_key(name)
            if (
                not name.lower().endswith(".safetensors")
                or not marker
                or marker in existing
                or marker in self._lora_metadata_attempted
                or marker in pending_markers
            ):
                continue
            pending_markers.add(marker)
            candidates.append(name)
            if len(candidates) >= 500:
                break
        if candidates:
            self._lora_metadata_task = asyncio.create_task(
                self._refresh_lora_metadata_profiles(candidates),
                name="winefox-lora-metadata",
            )

    async def _refresh_lora_metadata_profiles(self, filenames: list[str]) -> None:
        try:
            settings = self.config.snapshot()
            defaults = settings.get("defaults") if isinstance(settings.get("defaults"), Mapping) else {}
            default_family = str(
                defaults.get("family")
                or settings.get("model_family")
                or "illustrious"
            ).strip().lower()
            deadline = time.monotonic() + 30.0
            generated_count = 0
            async with ComfyUIExecutor(self._connection_config()) as executor:
                for offset in range(0, len(filenames), 40):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0.1:
                        break
                    batch = filenames[offset : offset + 40]
                    self._lora_metadata_attempted.update(
                        normalize_lora_key(name) for name in batch
                    )
                    metadata_by_name = await executor.fetch_lora_metadata_batch(
                            batch,
                            max_items=40,
                            concurrency=2,
                            timeout_seconds=2.0,
                            total_timeout_seconds=min(8.0, remaining),
                            minimum_interval_seconds=0.05,
                        )
                    generated: dict[str, dict[str, dict[str, Any]]] = {
                        "anima": {},
                        "illustrious": {},
                    }
                    for filename, metadata in metadata_by_name.items():
                        parsed = profile_from_comfy_metadata(
                            filename,
                            metadata,
                            default_family=default_family,
                        )
                        if parsed is None:
                            continue
                        family, profile = parsed
                        generated[family][normalize_lora_key(filename)] = profile
                    if not any(generated.values()):
                        continue
                    await asyncio.to_thread(
                        cache_generated_lora_profiles,
                        self.data_root,
                        self._lora_metadata_cache_key,
                        generated,
                    )
                    generated_count += sum(len(items) for items in generated.values())
                    self._reload_lora_profiles()
            logger.info(
                "Loaded %d high-confidence LoRA profiles from ComfyUI metadata",
                generated_count,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # metadata enrichment must never affect probe/catalog
            logger.warning("LoRA metadata enrichment skipped: %s", _redact_diagnostic_text(exc))

    def _catalog_lora(self, name: str) -> dict[str, Any]:
        wanted = normalize_lora_key(name)
        matches = self._lora_profile_index.get(wanted, [])
        profile = matches[0] if matches else None
        return {
            "id": name,
            "name": name,
            "label": Path(name).stem,
            "family": profile.family if profile and len({item.family for item in matches}) == 1 else "any",
            "compat": "ready",
            "strength": profile.strength_model if profile and profile.strength_model is not None else 0.8,
            "profiled": bool(profile),
            "character": profile.character if profile else "",
            "aliases": list(profile.aliases) if profile else [],
            "trigger_words": list(profile.trigger_words) if profile else [],
            "tags": list(profile.aliases) if profile else [],
        }

    async def submit_job(self, payload: dict[str, Any]) -> JobRecord:
        if not self._accepting:
            raise AppError(503, "BACKEND_RESTARTED", "后端尚未就绪或正在退出")
        if self._queue.full():
            raise AppError(429, "QUEUE_FULL", "本地任务队列已满")
        client_id = str(payload.get("client_request_id") or uuid.uuid4().hex)
        existing = self.db.connection.execute(
            "SELECT * FROM jobs WHERE client_request_id = ?", (client_id,)
        ).fetchone()
        if existing is not None:
            return self._job_record(existing)
        now = utcnow_iso()
        with self.db.transaction():
            cur = self.db.connection.execute(
                "INSERT INTO jobs (client_request_id,mode,state,request_json,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (client_id, str(payload.get("mode") or "txt2img"), "queued", json.dumps(payload, ensure_ascii=False), now, now),
            )
            job_id = int(cur.lastrowid)
        await self._queue.put(job_id)
        return await self.get_job(str(job_id))  # type: ignore[return-value]

    async def get_job(self, job_id: str) -> JobRecord | None:
        try:
            numeric = int(job_id)
        except (TypeError, ValueError):
            return None
        row = self.db.connection.execute("SELECT * FROM jobs WHERE id = ?", (numeric,)).fetchone()
        return self._job_record(row) if row else None

    async def cancel_job(self, job_id: str) -> bool:
        try:
            numeric = int(job_id)
        except (TypeError, ValueError):
            return False
        row = self.db.connection.execute("SELECT state FROM jobs WHERE id = ?", (numeric,)).fetchone()
        if row is None or row["state"] in TERMINAL_STATES:
            return False
        self._cancel_requested.add(numeric)
        self._update_job(numeric, "cancelled", error="用户已取消本地等待；未中断 ComfyUI 全局队列")
        return True

    async def _worker_loop(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                if job_id in self._cancel_requested:
                    continue
                self._current_job_id = job_id
                await self._run_job(job_id)
            except asyncio.CancelledError:
                raise
            except ComfyUIError as exc:
                logger.warning("job %s ComfyUI failure: %s", job_id, exc.user_message)
                self._update_job(job_id, "failed", error=exc.user_message)
            except Exception as exc:  # noqa: BLE001
                logger.exception("job %s failed", job_id)
                self._update_job(job_id, "failed", error=str(exc)[:2000])
            finally:
                self._current_job_id = None
                self._queue.task_done()

    async def _run_job(self, job_id: int) -> None:
        row = self.db.connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None or row["state"] == "cancelled":
            return
        payload = json.loads(row["request_json"] or "{}")
        self._update_job(job_id, "preparing")
        raw_prompt = str(payload.get("prompt") or "")
        prompt_result = await self._prepare_prompt(payload)
        execution_payload = dict(payload)
        execution_payload["prompt"] = prompt_result.final_prompt
        vision_meta: dict[str, Any] = {"status": "not_requested"}
        if prompt_result.seed is not None and payload.get("seed") in (None, -1, "-1"):
            execution_payload["seed"] = prompt_result.seed
        entry = self._select_workflow(payload)
        source = json.loads(Path(entry.path).read_text(encoding="utf-8-sig"))
        resume_prompt_id = str(row["comfy_prompt_id"] or "").strip() or None

        async with ComfyUIExecutor(self._connection_config()) as executor:
            # Fail fast when the configured server is offline. The richer catalog
            # fallback intentionally retries, but a queued generation should not
            # sit in "preparing" through every optional endpoint.
            await executor.transport.get_json("/object_info", retries=1)
            inventory = await executor.fetch_models()
            auto_lora = None
            selected_loras = (
                [item for item in execution_payload.get("loras", []) if isinstance(item, dict)]
                if isinstance(execution_payload.get("loras"), list)
                else []
            )
            if selected_loras:
                selected_triggers = resolve_selected_lora_triggers(
                    self.lora_service,
                    str(payload.get("family") or "illustrious"),
                    selected_loras,
                )
                if selected_triggers:
                    prompt_result.final_prompt = merge_prompt_tags(
                        prompt_result.final_prompt, selected_triggers
                    )
                    execution_payload["prompt"] = prompt_result.final_prompt
            else:
                auto_lora = resolve_prompt_loras(
                    self.lora_service,
                    raw_prompt,
                    str(payload.get("family") or "illustrious"),
                    inventory.loras,
                )
                if auto_lora.loras:
                    execution_payload["loras"] = list(auto_lora.loras)
                    prompt_result.final_prompt = merge_prompt_tags(prompt_result.final_prompt, auto_lora.triggers)
                    execution_payload["prompt"] = prompt_result.final_prompt
            checkpoint = self._select_checkpoint(source, payload, list(inventory.checkpoints))
            uploaded_name = None
            source_bytes = await self._source_bytes(payload)
            if source_bytes is not None and resume_prompt_id is None and str(payload.get("mode") or "txt2img") == "img2img":
                vision_tags, vision_meta = await self._reverse_prompt_with_vision(
                    source_bytes,
                    raw_prompt=raw_prompt,
                    current_prompt=prompt_result.final_prompt,
                    family=str(payload.get("family") or "illustrious"),
                )
                if vision_tags:
                    prompt_result.final_prompt = merge_prompt_tags(prompt_result.final_prompt, vision_tags)
                    execution_payload["prompt"] = prompt_result.final_prompt
            if source_bytes is not None and resume_prompt_id is None:
                uploaded = await executor.upload_image(source_bytes, filename=f"winefox_{job_id}.png")
                uploaded_name = uploaded.name if not uploaded.subfolder else f"{uploaded.subfolder}/{uploaded.name}"
            workflow, seed = prepare_workflow(
                source,
                execution_payload,
                checkpoint=checkpoint,
                uploaded_name=uploaded_name,
            )
            self._update_job(job_id, "running", comfy_prompt_id=resume_prompt_id)
            result = await executor.run(
                ExecutionSnapshot(
                    workflow=workflow,
                    prompt=prompt_result.final_prompt,
                    negative_prompt=str(payload.get("negative_prompt") or ""),
                    output_node_id=self._output_node_id(workflow),
                    client_id=f"winefox_desktop_{job_id}",
                    resume_prompt_id=resume_prompt_id,
                )
            )
        if result.prompt_id:
            self._set_prompt_id(job_id, result.prompt_id)
        if job_id in self._cancel_requested:
            return
        if not result.ok or not result.images:
            message = result.error.user_message if result.error else "ComfyUI 未返回图片"
            self._update_job(job_id, "failed", error=message)
            return
        self._update_job(job_id, "downloading", comfy_prompt_id=result.prompt_id)
        image = result.images[0]
        extension, content_type = _detect_image_output(image)
        relative = f"outputs/{job_id}{extension}"
        target = self.data_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(image)
        digest = hashlib.sha256(image).hexdigest()
        history = self.history_store.add(
            job_id,
            relative,
            sha256=digest,
            raw_prompt=raw_prompt,
            final_prompt=prompt_result.final_prompt,
            seed=seed,
            width=int(payload.get("width") or 832),
            height=int(payload.get("height") or 1216),
            metadata={
                **payload,
                "prompt_id": result.prompt_id,
                "elapsed_seconds": result.meta.get("elapsed_seconds"),
                "prompt_source": prompt_result.source,
                "prompt_warnings": list(prompt_result.warnings),
                "vision_reverse": vision_meta,
                "auto_lora": auto_lora.metadata if auto_lora is not None else None,
                "image_content_type": content_type,
            },
        )
        self._update_job(job_id, "completed", comfy_prompt_id=result.prompt_id)
        logger.info("job %s completed history=%s", job_id, history.id)

    async def _prepare_prompt(self, payload: Mapping[str, Any]):
        settings = self.config.snapshot()
        provider = settings.get("ai_provider") if isinstance(settings.get("ai_provider"), Mapping) else {}
        llm_client = None
        if provider.get("enabled"):
            try:
                llm_client = OpenAICompatibleLLMClient(
                    str(provider.get("base_url") or ""),
                    self.config.get_secret("ai_api_key") or "",
                )
            except ValueError as exc:
                logger.warning("AI provider configuration ignored: %s", exc)
        service = PromptService(
            config={
                "family": str(payload.get("family") or "illustrious"),
                "llm": {
                    "enabled": bool(provider.get("enabled")),
                    "model": str(provider.get("model") or ""),
                    "max_tokens": 450,
                    "temperature": 0.1,
                },
            },
            llm_port=llm_client,
        )
        return await service.process(str(payload.get("prompt") or ""))

    async def _reverse_prompt_with_vision(
        self,
        source_bytes: bytes,
        *,
        raw_prompt: str,
        current_prompt: str,
        family: str,
    ) -> tuple[list[str], dict[str, Any]]:
        """Use the separately configured vision provider as an optional img2img aid."""
        settings = self.config.snapshot()
        provider = resolve_provider(settings, "vision")
        if not provider.enabled:
            return [], {"status": "disabled"}
        if not provider.base_url or not provider.model:
            return [], {"status": "not_configured"}
        api_key = self.config.get_secret(provider.secret_name) or ""
        if not api_key:
            return [], {"status": "missing_secret", "secret": provider.secret_name}
        try:
            mime = mimetypes.guess_type("reference.png")[0] or "image/png"
            data_url = f"data:{mime};base64,{base64.b64encode(source_bytes).decode('ascii')}"
            client = OpenAICompatibleLLMClient(
                provider.base_url,
                api_key,
                timeout_seconds=provider.timeout_seconds,
            )
            messages = [
                {
                    "role": "system",
                    "content": (
                        "Describe the attached anime reference image for image-to-image generation. "
                        "Output only English comma-separated visual tags. Describe pose, expression, gaze, "
                        "camera, framing, composition, background, lighting and visible props. Do not preserve "
                        "source identity, exact outfit, logos or text. Never output reasoning, headings, markdown "
                        "or first-person sentences."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "family": family,
                                    "user_request": raw_prompt.strip(),
                                    "current_prompt": current_prompt.strip(),
                                    "instruction": "Return at least four concise comma-separated tags.",
                                },
                                ensure_ascii=False,
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ]
            data = await client.complete(
                messages,
                model=provider.model,
                max_tokens=700,
                temperature=0.1,
            )
            content = self._extract_llm_text(data)
            tags = self._sanitize_vision_tags(content)
            if len(tags) < 4:
                return [], {"status": "invalid_output"}
            return tags, {"status": "completed", "count": len(tags), "provider": provider.source}
        except Exception as exc:  # Vision is an optional enhancement; generation still proceeds.
            logger.warning("vision reverse prompt skipped: %s", _redact_diagnostic_text(exc))
            return [], {"status": "failed", "message": _redact_diagnostic_text(exc)[:240]}

    @staticmethod
    def _extract_llm_text(data: Any) -> str:
        parts: list[str] = []

        def collect(value: Any) -> None:
            if isinstance(value, str) and value.strip():
                parts.append(value)
            elif isinstance(value, list):
                for item in value:
                    collect(item)
            elif isinstance(value, dict):
                # reasoning_content is intentionally ignored.
                for key in ("text", "content", "output_text"):
                    if key in value:
                        collect(value[key])

        if isinstance(data, dict):
            for choice in data.get("choices") or []:
                if isinstance(choice, dict):
                    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
                    collect(message.get("content"))
                    collect(choice.get("text"))
            collect(data.get("output_text"))
        return "\n".join(parts).strip()

    @staticmethod
    def _sanitize_vision_tags(value: str) -> list[str]:
        text = re.sub(r"<think>.*?</think>|<!--.*?-->|```[^`]*```", "", str(value or ""), flags=re.I | re.S)
        text = re.sub(r"[\r\n]+", ",", text)
        tags: list[str] = []
        for raw in text.split(","):
            tag = raw.strip(" `\"'\t")
            if not tag or len(tag.split()) > 16:
                continue
            if re.search(r"\b(?:analysis|reasoning|because|therefore|i am|i'm|the task|the request|policy|guidelines)\b", tag, re.I):
                continue
            if tag.casefold() not in {item.casefold() for item in tags}:
                tags.append(tag)
        return tags[:80]

    def _select_workflow(self, payload: Mapping[str, Any]) -> WorkflowEntry:
        requested = str(payload.get("workflow") or "").strip()
        entry = self.workflows.get(workflow_id=requested) or self.workflows.get(name=requested)
        if entry is not None and entry.path:
            return entry
        mode = str(payload.get("mode") or "txt2img")
        preferred = "img2img_illustrious_api.json" if mode == "img2img" else "workflow_api.json"
        entry = self.workflows.get(name=preferred)
        if entry is None:
            entry = next((item for item in self.workflows.all_entries() if item.mode == mode and item.path), None)
        if entry is None:
            raise AppError(422, "WORKFLOW_NOT_FOUND", f"找不到 {mode} 工作流")
        return entry

    @staticmethod
    def _select_checkpoint(source: Mapping[str, Any], payload: Mapping[str, Any], available: list[str]) -> str | None:
        requested = str(payload.get("checkpoint") or "").strip()
        if requested and (not available or requested in available):
            return requested
        configured = [
            str((node.get("inputs") or {}).get("ckpt_name"))
            for node in source.values()
            if isinstance(node, dict) and (node.get("inputs") or {}).get("ckpt_name")
        ]
        if available:
            for name in configured:
                if name in available:
                    return name
            return available[0]
        return configured[0] if configured else None

    async def _source_bytes(self, payload: Mapping[str, Any]) -> bytes | None:
        ref = payload.get("ref_image")
        upload_id = None
        if isinstance(ref, dict):
            url = str(ref.get("url") or "")
            if "/uploads/" in url:
                upload_id = url.rsplit("/", 1)[-1]
        ids = payload.get("upload_ids")
        if not upload_id and isinstance(ids, list) and ids:
            upload_id = str(ids[0])
        if not upload_id:
            return None
        image = await self.uploads.get(upload_id)
        if image is None:
            raise AppError(422, "INVALID_IMAGE", "引用图片不存在或已失效")
        return image.data

    @staticmethod
    def _output_node_id(workflow: Mapping[str, Any]) -> str | None:
        for node_id, node in workflow.items():
            if isinstance(node, dict) and str(node.get("class_type") or "").startswith("SaveImage"):
                return str(node_id)
        return None

    def _job_record(self, row) -> JobRecord:
        payload = json.loads(row["request_json"] or "{}")
        state = str(row["state"])
        count = self.db.connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE id < ? AND state IN ('queued','recovering')", (row["id"],)
        ).fetchone()[0]
        history = self.db.connection.execute("SELECT id, metadata FROM history WHERE job_id = ?", (row["id"],)).fetchone()
        image = None
        seed = payload.get("seed")
        result = None
        history_metadata: dict[str, Any] = {}
        if history is not None:
            history_metadata = json.loads(history["metadata"] or "{}")
            seed = history_metadata.get("seed", seed)
            image = {"id": str(history["id"]), "url": f"/api/v1/history/{history['id']}/image"}
            result = {"history_id": str(history["id"]), "image": image}
        job_meta = {"comfy_prompt_id": row["comfy_prompt_id"]}
        if history_metadata.get("auto_lora"):
            job_meta["auto_lora"] = history_metadata["auto_lora"]
        return JobRecord(
            id=str(row["id"]),
            status=state,
            created_at=_iso_to_epoch(row["created_at"]),
            updated_at=_iso_to_epoch(row["updated_at"]),
            prompt=str(payload.get("prompt") or ""),
            result=result,
            error=row["error"],
            meta=job_meta,
            progress=1.0 if state == "completed" else None,
            queue_position=int(count) + 1 if state in {"queued", "recovering"} else None,
            image=image,
            seed=seed,
        )

    def _update_job(self, job_id: int, state: str, *, error: str | None = None, comfy_prompt_id: str | None = None) -> None:
        now = utcnow_iso()
        fields = ["state = ?", "updated_at = ?"]
        values: list[Any] = [state, now]
        if error is not None:
            fields.append("error = ?")
            values.append(error)
        if comfy_prompt_id is not None:
            fields.append("comfy_prompt_id = ?")
            values.append(comfy_prompt_id)
        if state == "running":
            fields.append("started_at = COALESCE(started_at, ?)")
            values.append(now)
        if state in TERMINAL_STATES:
            fields.append("finished_at = ?")
            values.append(now)
        values.append(job_id)
        with self.db.transaction():
            self.db.connection.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?", values)

    def _set_prompt_id(self, job_id: int, prompt_id: str) -> None:
        with self.db.transaction():
            self.db.connection.execute(
                "UPDATE jobs SET comfy_prompt_id = ?, updated_at = ? WHERE id = ?",
                (prompt_id, utcnow_iso(), job_id),
            )

    async def list_history(self, limit: int = 50, offset: int = 0, favorite_only: bool = False) -> list[HistoryEntry]:
        page = await self.list_history_page(limit=limit, offset=offset, favorite_only=favorite_only)
        return [self._history_entry(item) for item in page["_rows"]]

    async def list_history_page(self, **query: Any) -> dict[str, Any]:
        limit = max(1, min(500, int(query.get("limit") or 50)))
        offset = max(0, int(query.get("offset") or 0))
        cursor = query.get("cursor")
        trash = str(query.get("trash") or "").lower() in {"1", "true", "yes"}
        favorite = bool(query.get("favorite_only"))
        search = str(query.get("q") or "").strip()
        where = ["h.deleted_at IS NOT NULL" if trash else "h.deleted_at IS NULL"]
        params: list[Any] = []
        if cursor:
            where.append("h.id < ?")
            params.append(int(cursor))
        if search:
            where.append("(h.raw_prompt LIKE ? OR h.final_prompt LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        if favorite:
            where.append("f.id IS NOT NULL")
        sql = (
            "SELECT h.*, j.request_json, f.id AS favorite_id, f.payload AS favorite_payload "
            "FROM history h JOIN jobs j ON j.id=h.job_id "
            "LEFT JOIN favorites f ON f.scope='history' AND f.ref_key=CAST(h.id AS TEXT) "
            f"WHERE {' AND '.join(where)} ORDER BY h.id DESC LIMIT ? OFFSET ?"
        )
        rows = self.db.connection.execute(sql, (*params, limit + 1, offset)).fetchall()
        page_rows = rows[:limit]
        items = [self._history_dict(row) for row in page_rows]
        next_cursor = str(page_rows[-1]["id"]) if len(rows) > limit and page_rows else None
        total = self.db.connection.execute(
            "SELECT COUNT(*) FROM history WHERE deleted_at IS " + ("NOT NULL" if trash else "NULL")
        ).fetchone()[0]
        return {"items": items, "total": int(total), "next_cursor": next_cursor, "_rows": page_rows}

    def _history_entry(self, row) -> HistoryEntry:
        data = self._history_dict(row)
        return HistoryEntry(
            id=data["id"],
            prompt=data["prompt"],
            created_at=data["created_at"],
            image_id=data["id"],
            favorite=data["favorite"],
            tags=[],
            meta=data,
        )

    def _history_dict(self, row) -> dict[str, Any]:
        request = json.loads(row["request_json"] or "{}") if "request_json" in row.keys() else {}
        metadata = json.loads(row["metadata"] or "{}")
        return {
            "id": str(row["id"]),
            "job_id": str(row["job_id"]),
            "prompt": row["raw_prompt"],
            "negative_prompt": request.get("negative_prompt"),
            "seed": row["seed"],
            "width": row["width"],
            "height": row["height"],
            "steps": request.get("steps"),
            "cfg": request.get("cfg"),
            "family": request.get("family"),
            "workflow": request.get("workflow"),
            "created_at": row["created_at"],
            "favorite": bool(row["favorite_id"]) if "favorite_id" in row.keys() else False,
            "deleted": row["deleted_at"] is not None,
            "image": {"url": f"/api/v1/history/{row['id']}/image"},
            "metadata": metadata,
        }

    def _history_row(self, history_id: str):
        try:
            numeric = int(history_id)
        except (TypeError, ValueError):
            return None
        return self.db.connection.execute(
            "SELECT h.*, j.request_json, f.id AS favorite_id FROM history h JOIN jobs j ON j.id=h.job_id "
            "LEFT JOIN favorites f ON f.scope='history' AND f.ref_key=CAST(h.id AS TEXT) WHERE h.id=?",
            (numeric,),
        ).fetchone()

    async def get_history_image(self, history_id: str) -> ImageData | None:
        row = self._history_row(history_id)
        if row is None:
            return None
        path = (self.data_root / str(row["image_relpath"])).resolve()
        try:
            path.relative_to(self.data_root.resolve())
        except ValueError:
            return None
        if not path.is_file():
            return None
        return ImageData(path.read_bytes(), mimetypes.guess_type(path.name)[0] or "image/png", path.name)

    async def set_favorite(self, history_id: str, favorite: bool) -> bool:
        if self._history_row(history_id) is None:
            return False
        if favorite:
            self.favorite_store.add("history", str(history_id), "image", {})
        else:
            self.favorite_store.remove("history", str(history_id))
        return True

    async def delete_history(self, history_id: str) -> bool:
        row = self._history_row(history_id)
        return bool(row and self.history_store.soft_delete(int(row["job_id"])))

    async def restore_history(self, history_id: str) -> bool:
        row = self._history_row(history_id)
        return bool(row and self.history_store.restore(int(row["job_id"])))

    async def purge_history(self, history_id: str) -> bool:
        row = self._history_row(history_id)
        if row is None:
            return False
        image_path = (self.data_root / str(row["image_relpath"])).resolve()
        try:
            image_path.relative_to(self.data_root.resolve())
            image_path.unlink(missing_ok=True)
        except ValueError:
            pass
        self.favorite_store.remove("history", str(history_id))
        return self.history_store.hard_delete(int(row["job_id"]))

    async def list_favorites(self, query: Mapping[str, Any] | None = None) -> dict[str, Any]:
        rows = self.db.connection.execute("SELECT * FROM favorites ORDER BY id DESC").fetchall()
        items = []
        for row in rows:
            payload = json.loads(row["payload"] or "{}")
            item = {"id": str(row["id"]), "category": payload.get("category", "默认"), "note": payload.get("note", ""), **payload}
            if row["scope"] == "history":
                history = self._history_row(row["ref_key"])
                if history is not None:
                    item.update(self._history_dict(history))
                    item["favorite_id"] = str(row["id"])
            items.append(item)
        return {"items": items, "total": len(items)}

    async def create_favorite(self, payload: dict[str, Any]) -> dict[str, Any]:
        ref = uuid.uuid4().hex
        record = self.favorite_store.add("note", ref, "prompt", payload)
        return {"id": str(record.id), **payload}

    async def update_favorite(self, favorite_id: str, payload: dict[str, Any]) -> bool:
        try:
            numeric = int(favorite_id)
        except ValueError:
            return False
        row = self.db.connection.execute("SELECT * FROM favorites WHERE id=?", (numeric,)).fetchone()
        if row is None:
            return False
        current = json.loads(row["payload"] or "{}")
        current.update(payload)
        self.favorite_store.upsert(row["scope"], row["ref_key"], row["kind"], current)
        return True

    async def delete_favorite(self, favorite_id: str) -> bool:
        try:
            numeric = int(favorite_id)
        except ValueError:
            return False
        with self.db.transaction():
            cur = self.db.connection.execute("DELETE FROM favorites WHERE id=?", (numeric,))
        return cur.rowcount > 0

    async def categories(self) -> dict[str, Any]:
        favorites = await self.list_favorites()
        counts: dict[str, int] = {}
        for item in favorites["items"]:
            category = str(item.get("category") or "默认")
            counts[category] = counts.get(category, 0) + 1
        return {"items": [{"name": name, "count": count} for name, count in sorted(counts.items())]}

    def _diagnostic_log_tail(self, *, max_lines: int = 80, max_bytes: int = 128 * 1024) -> dict[str, Any]:
        log_dir = self.data_root / "logs"
        try:
            candidates = sorted(
                (path for path in log_dir.glob("*.log") if path.is_file()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            candidates = []
        if not candidates:
            return {
                "available": False,
                "lines": [],
                "truncated": False,
                "detail": "当前还没有可读取的运行日志。",
            }
        path = candidates[0]
        try:
            with path.open("rb") as handle:
                size = path.stat().st_size
                start = max(0, size - max_bytes)
                handle.seek(start)
                raw = handle.read(max_bytes)
            lines = raw.decode("utf-8", errors="replace").splitlines()
            if start and lines:
                lines = lines[1:]
            was_truncated = start > 0 or len(lines) > max_lines
            safe_lines = [_redact_diagnostic_text(line) for line in lines[-max_lines:]]
            return {
                "available": True,
                "lines": safe_lines,
                "truncated": was_truncated,
                "detail": f"最近 {len(safe_lines)} 行脱敏日志。",
            }
        except OSError:
            return {
                "available": False,
                "lines": [],
                "truncated": False,
                "detail": "运行日志暂时无法读取。",
            }

    async def diagnostics(self) -> dict[str, Any]:
        probe = await self.probe_comfyui()
        connected = bool(probe.get("online"))
        failures: list[dict[str, Any]] = []
        config_health: list[dict[str, Any]] = []

        try:
            integrity = self.db.verify_integrity()
            database_ok = not integrity
            database_detail = "SQLite 数据库校验通过" if database_ok else "；".join(map(_redact_diagnostic_text, integrity))
        except Exception:
            logger.exception("diagnostic database integrity check failed")
            integrity = ["quick_check failed"]
            database_ok = False
            database_detail = "SQLite 数据库校验无法完成"
        config_health.append({
            "id": "database",
            "label": "本地数据库",
            "status": "ok" if database_ok else "fail",
            "detail": database_detail,
            "code": None if database_ok else "DATABASE_INTEGRITY_FAILED",
            "retryable": False,
            "action": "导出诊断信息并从备份恢复数据。" if not database_ok else "",
        })

        try:
            settings = self.config.snapshot()
            config_error = None
        except AppError as exc:
            settings = {}
            config_error = exc
            failures.append({
                "status": "fail",
                "code": exc.code,
                "message": _redact_diagnostic_text(exc.message),
                "retryable": False,
                "action": "打开设置检查配置文件，或从备份恢复设置。",
            })
        comfy_settings = settings.get("comfyui") if isinstance(settings.get("comfyui"), Mapping) else {}
        server = str(probe.get("server") or comfy_settings.get("server_address") or "")
        tls_verified = bool(comfy_settings.get("ssl_verify", True))
        config_health.append({
            "id": "comfyui_connection",
            "label": "ComfyUI 连接",
            "status": "ok" if connected else "fail",
            "detail": _redact_diagnostic_text(probe.get("summary") or ("连接正常" if connected else "连接失败")),
            "code": None if connected else str(probe.get("error_code") or "COMFYUI_OFFLINE"),
            "retryable": not connected,
            "action": "检查 ComfyUI 地址、认证信息和服务状态后重新检查。" if not connected else "",
        })
        config_health.append({
            "id": "tls_verification",
            "label": "TLS 校验",
            "status": "ok" if tls_verified else "warn",
            "detail": "证书校验已启用" if tls_verified else "证书校验已关闭，仅应在可信网络临时使用",
            "code": None if tls_verified else "TLS_VERIFICATION_DISABLED",
            "retryable": False,
            "action": "在设置中重新启用 TLS 证书校验。" if not tls_verified else "",
        })

        image_workflows = [entry for entry in self.workflows.all_entries() if entry.mode != "video" and entry.path]
        valid_workflows = sum(1 for entry in image_workflows if entry.valid)
        compatible_workflows = probe.get("workflow_compatible")
        workflow_status = "ok"
        if not image_workflows:
            workflow_status = "fail"
        elif compatible_workflows == 0 or valid_workflows == 0:
            workflow_status = "warn"
        workflow_detail = f"已载入 {len(image_workflows)} 个图像工作流，{valid_workflows} 个结构校验通过"
        if compatible_workflows is not None:
            workflow_detail += f"，{compatible_workflows} 个与当前 ComfyUI 节点兼容"
        config_health.append({
            "id": "workflows",
            "label": "工作流",
            "status": workflow_status,
            "detail": workflow_detail,
            "code": "WORKFLOW_NOT_FOUND" if not image_workflows else ("WORKFLOW_NODE_MISSING" if compatible_workflows == 0 else None),
            "retryable": False,
            "action": "检查内置工作流资源和 ComfyUI 自定义节点。" if workflow_status != "ok" else "",
        })

        checkpoint_count = len(probe.get("checkpoints") or [])
        lora_count = len(probe.get("loras") or [])
        config_health.append({
            "id": "model_catalog",
            "label": "模型目录",
            "status": "ok" if connected and checkpoint_count else ("warn" if connected else "fail"),
            "detail": f"检测到 {checkpoint_count} 个 checkpoint、{lora_count} 个 LoRA",
            "code": None if checkpoint_count else ("CHECKPOINT_NOT_FOUND" if connected else "COMFYUI_OFFLINE"),
            "retryable": True,
            "action": "确认模型目录已在 ComfyUI 中配置并刷新目录。" if connected and not checkpoint_count else "",
        })

        provider = settings.get("ai_provider") if isinstance(settings.get("ai_provider"), Mapping) else {}
        provider_enabled = bool(provider.get("enabled"))
        provider_ready = bool(provider.get("base_url") and provider.get("model"))
        if provider_enabled:
            try:
                provider_ready = provider_ready and bool(self.config.get_secret("ai_api_key"))
            except AppError:
                provider_ready = False
        config_health.append({
            "id": "ai_provider",
            "label": "AI 提示词服务",
            "status": "ok" if provider_enabled and provider_ready else ("warn" if not provider_enabled else "fail"),
            "detail": "已配置并启用" if provider_enabled and provider_ready else ("未启用（不影响基础生成）" if not provider_enabled else "已启用，但地址、模型或密钥不完整"),
            "code": "AI_PROVIDER_INCOMPLETE" if provider_enabled and not provider_ready else None,
            "retryable": False,
            "action": "在设置中补全 AI 提供商地址、模型和密钥。" if provider_enabled and not provider_ready else "",
        })

        vision_provider = settings.get("vision_provider") if isinstance(settings.get("vision_provider"), Mapping) else {}
        vision_enabled = bool(vision_provider.get("enabled"))
        vision_follows_text = vision_provider.get("follow_text_provider", True) is not False
        vision_base = provider.get("base_url") if vision_follows_text else vision_provider.get("base_url")
        vision_model = provider.get("model") if vision_follows_text else vision_provider.get("model")
        vision_ready = bool(vision_base and vision_model)
        vision_secret_name = "ai_api_key" if vision_follows_text else "vision_api_key"
        if vision_enabled:
            try:
                vision_ready = vision_ready and bool(self.config.get_secret(vision_secret_name))
            except AppError:
                vision_ready = False
        config_health.append({
            "id": "vision_provider",
            "label": "视觉理解 AI",
            "status": "ok" if vision_enabled and vision_ready else ("warn" if not vision_enabled else "fail"),
            "detail": (
                "已启用，跟随提示词 AI"
                if vision_enabled and vision_ready and vision_follows_text
                else "已启用，使用独立视觉接口"
                if vision_enabled and vision_ready
                else "未启用（图生图仍可直接生成）"
                if not vision_enabled
                else "已启用，但地址、模型或密钥不完整"
            ),
            "code": "VISION_PROVIDER_INCOMPLETE" if vision_enabled and not vision_ready else None,
            "retryable": False,
            "action": "在设置中补全视觉提供商地址、模型和对应密钥，或启用跟随提示词 AI。" if vision_enabled and not vision_ready else "",
        })

        if not database_ok:
            failures.append({
                "status": "fail",
                "code": "DATABASE_INTEGRITY_FAILED",
                "message": database_detail,
                "retryable": False,
                "action": "导出诊断信息并从备份恢复数据。",
            })
        if not connected:
            failures.append({
                "status": "fail",
                "code": str(probe.get("error_code") or "COMFYUI_OFFLINE"),
                "message": _redact_diagnostic_text(probe.get("summary") or "无法连接 ComfyUI"),
                "retryable": True,
                "action": "检查 ComfyUI 地址、认证信息和服务状态后重新检查。",
            })
        missing_nodes = list(probe.get("missing_nodes") or [])
        if missing_nodes:
            failures.append({
                "status": "warn",
                "code": "WORKFLOW_NODE_MISSING",
                "message": f"当前工作流需要的 {len(missing_nodes)} 个节点未安装",
                "retryable": False,
                "action": "通过 ComfyUI Manager 安装缺失节点后重启 ComfyUI。",
            })
        warnings = [_redact_diagnostic_text(value) for value in probe.get("warnings") or []]
        if warnings:
            failures.append({
                "status": "warn",
                "code": "COMFYUI_PARTIAL_PROBE",
                "message": "；".join(warnings[:3]),
                "retryable": True,
                "action": "重新检查；若仍存在，查看日志摘要定位失败的接口。",
            })

        return {
            "version": VERSION,
            "app": {"name": "WineFox Desktop", "version": VERSION, "build": "desktop-integrated"},
            "plugin": {
                "name": "astrbot_plugin_comfyui_pro_UltraPlusMax",
                "version": VERSION,
                "runtime": "desktop adapter",
            },
            "backend": {"status": "ok", "database": "ok" if database_ok else "fail"},
            "comfyui": {
                "connected": connected,
                "status": "ok" if connected else "fail",
                "url": _redact_diagnostic_text(server),
                "version": str(probe.get("version") or ""),
                "latency_ms": probe.get("latency_ms"),
                "queue": dict(probe.get("queue") or {}),
                "error": None if connected else {
                    "code": str(probe.get("error_code") or "COMFYUI_OFFLINE"),
                    "message": _redact_diagnostic_text(probe.get("summary") or "无法连接 ComfyUI"),
                    "retryable": True,
                },
            },
            "nodes": {
                "total": probe.get("node_count"),
                "missing": missing_nodes,
                "status": "fail" if not connected else ("warn" if missing_nodes or warnings else "ok"),
                "detail": "节点信息不可用" if probe.get("node_count") is None else f"检测到 {probe.get('node_count')} 个节点",
            },
            "catalog": {
                "workflows": len(image_workflows),
                "valid_workflows": valid_workflows,
                "compatible_workflows": compatible_workflows,
                "checkpoints": checkpoint_count,
                "loras": lora_count,
                "lora_profiles": self.loaded_lora_profiles.count,
                "status": "ok" if connected and image_workflows and checkpoint_count else "warn",
            },
            "queue": {"busy": self.busy(), "tasks": self.task_count()},
            "config_health": config_health,
            "checks": config_health,
            "failures": failures,
            "log_tail": self._diagnostic_log_tail(),
            "data_root": "WineFox application data",
        }

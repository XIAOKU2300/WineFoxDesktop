"""Production desktop engine joining Agent 04/05/06 with Agent 07's API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

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
from .llm_client import OpenAICompatibleLLMClient
from .lora_profiles import load_lora_profiles, merge_prompt_tags, resolve_prompt_loras
from .upload_store import FileUploadStore
from .workflow_builder import prepare_workflow

logger = logging.getLogger("winefox.runtime.engine")

TERMINAL_STATES = {"completed", "failed", "cancelled", "detached"}


def _iso_to_epoch(value: str | None) -> float:
    if not value:
        return time.time()
    try:
        from datetime import datetime

        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return time.time()


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
        self.loaded_lora_profiles = load_lora_profiles(self.data_root)
        self.lora_service = LoraService(self.loaded_lora_profiles.profiles)
        self._lora_profile_index: dict[str, list[Any]] = {}
        for family in ("anima", "illustrious"):
            for profile in self.lora_service.profiles_for(family).values():
                self._lora_profile_index.setdefault(normalize_lora_key(profile.lora_name), []).append(profile)
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
        return self._current_job_id is not None or self._queue.qsize() > 0

    def task_count(self) -> int:
        return self._queue.qsize() + (1 if self._current_job_id is not None else 0)

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
        try:
            async with ComfyUIExecutor(self._connection_config()) as executor:
                probe = await executor.probe()
            result = {
                "ok": probe.reachable,
                "online": probe.reachable,
                "server": self._connection_config().server_address,
                "latency_ms": round(probe.latency_ms, 1),
                "node_count": probe.node_count,
                "queue": {"running": probe.queue.running, "pending": probe.queue.pending},
                "checkpoints": list(probe.models.checkpoints),
                "loras": list(probe.models.loras),
                "warnings": list(probe.warnings),
                "missing_nodes": [],
                "result": "ok" if probe.reachable else "fail",
                "summary": "ComfyUI 连接正常" if probe.reachable else "无法连接 ComfyUI",
                "steps": [
                    {"status": "ok" if probe.reachable else "fail", "detail": "服务可访问" if probe.reachable else "连接失败"},
                    {"status": "ok" if probe.node_count else "warn", "detail": f"检测到 {probe.node_count} 个节点"},
                    {"status": "ok" if probe.models.checkpoints else "warn", "detail": f"检测到 {len(probe.models.checkpoints)} 个 checkpoint"},
                    {"status": "ok", "detail": f"检测到 {len(probe.models.loras)} 个 LoRA"},
                ],
            }
        except (ComfyUIError, ValueError) as exc:
            result = {
                "ok": False,
                "online": False,
                "server": self._connection_config().server_address,
                "latency_ms": None,
                "error": str(exc),
                "result": "fail",
                "summary": str(exc),
                "steps": [{"status": "fail", "detail": str(exc)}],
                "checkpoints": [],
                "loras": [],
                "missing_nodes": [],
            }
        self._last_probe = result
        self._last_probe_at = time.monotonic()
        return result

    async def get_catalog(self) -> dict[str, Any]:
        probe = await self.probe_comfyui()
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
            "download": {"enabled": False},
            "online": bool(probe.get("online")),
            "lora_profiles": {
                "loaded": self.loaded_lora_profiles.count,
                "sources": list(self.loaded_lora_profiles.sources),
            },
        }

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
            if not execution_payload.get("loras"):
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
        relative = f"outputs/{job_id}.png"
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
                "auto_lora": auto_lora.metadata if auto_lora is not None else None,
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

    async def diagnostics(self) -> dict[str, Any]:
        probe = self._last_probe or await self.probe_comfyui()
        integrity = self.db.verify_integrity()
        return {
            "version": "8.5.9",
            "backend": {"status": "ok", "database": "ok" if not integrity else "fail"},
            "comfyui": {"status": "ok" if probe.get("online") else "warn", "server": probe.get("server")},
            "queue": {"busy": self.busy(), "tasks": self.task_count()},
            "checks": [
                {"name": "SQLite", "status": "ok" if not integrity else "fail", "detail": ", ".join(integrity)},
                {"name": "ComfyUI", "status": "ok" if probe.get("online") else "warn", "detail": probe.get("summary")},
            ],
            "data_root": "WineFox application data",
        }

"""Civitai LoRA discovery and safe installation for WineFox Desktop.

The service reuses the original plugin's mature Civitai search, version
selection, resilient download and profile extraction.  It deliberately keeps
download URLs server-side and exposes background-job snapshots to the UI so a
large model download never blocks navigation or the WebView UI thread.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urlparse

from civitai_api import CivitaiClient, LoraProfileStore, safe_filename
from desktop_backend.errors import AppError
from winefox_core.comfyui_transport import ComfyUITransport

logger = logging.getLogger("winefox.runtime.lora_download")

_SEARCH_CACHE_TTL = 30 * 60
_JOB_TTL = 6 * 60 * 60
_ALLOWED_SUFFIXES = {".safetensors", ".ckpt", ".pt"}
_ALLOWED_DOWNLOAD_HOSTS = {"civitai.com", "www.civitai.com", "civitai.red", "www.civitai.red"}
_FAMILY_BASE_MODELS = {
    "anima": ["Anima"],
    "illustrious": ["Illustrious"],
}
_TERMINAL = {"completed", "failed", "cancelled"}


def _bounded_text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:limit]


def _safe_error(value: Any) -> str:
    text = str(value or "LoRA 下载失败")
    text = re.sub(r"(?i)(token=)[^&\s]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(authorization|cookie)(\s*[:=]\s*)[^\s,;]+", r"\1\2[REDACTED]", text)
    text = re.sub(r"(?i)(?:[a-z]:[\\/]|\\\\)[^\r\n,;]+", "[LOCAL_PATH]", text)
    return _bounded_text(text, 600)


def _json_response(raw: bytes, *, label: str, max_bytes: int = 1024 * 1024) -> dict[str, Any]:
    if len(raw) > max_bytes:
        raise RuntimeError(f"{label} 响应过大")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} 返回了无效 JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} 返回格式错误")
    return value


class LoraDownloadService:
    def __init__(
        self,
        *,
        config: Any,
        data_root: Path,
        connection_factory: Callable[[], Any],
        on_installed: Callable[[], Awaitable[None] | None] | None = None,
        client_factory: Callable[..., Any] = CivitaiClient,
    ) -> None:
        self.config = config
        self.data_root = Path(data_root)
        self.connection_factory = connection_factory
        self.on_installed = on_installed
        self.client_factory = client_factory
        self._search_cache: dict[str, dict[str, Any]] = {}
        self._jobs: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._active_by_result: dict[str, str] = {}
        self._download_lock = asyncio.Lock()

    def _settings(self) -> dict[str, Any]:
        snapshot = self.config.snapshot()
        value = snapshot.get("lora_download")
        return dict(value) if isinstance(value, Mapping) else {}

    def _client(self):
        settings = self._settings()
        return self.client_factory(
            api_token=self.config.get_secret("civitai_api_token") or "",
            cookie=self.config.get_secret("civitai_cookie") or "",
            proxy=str(settings.get("proxy") or "").strip(),
            timeout=max(10, min(int(settings.get("request_timeout") or 60), 180)),
            downloader="multithread",
            split=max(1, min(int(settings.get("split") or 4), 8)),
            network_max_retries=2,
        )

    def capabilities(self) -> dict[str, Any]:
        settings = self._settings()
        mode = str(settings.get("mode") or "auto").strip().lower()
        if mode not in {"auto", "local", "comfyui_bridge"}:
            mode = "auto"
        return {
            "enabled": True,
            "sources": ["civitai"],
            "mode": mode,
            "local_configured": bool(str(settings.get("local_dir") or "").strip()),
            "bridge_configured": bool(str(settings.get("bridge_endpoint") or "/astrbot/lora").strip()),
            "civitai_token_configured": bool(self.config.get_secret("civitai_api_token")),
            "bridge_token_configured": bool(self.config.get_secret("comfyui_bridge_token")),
        }

    def active_count(self) -> int:
        return sum(1 for job in self._jobs.values() if job.get("status") not in _TERMINAL)

    def _cleanup(self) -> None:
        now = time.time()
        self._search_cache = {
            key: value
            for key, value in self._search_cache.items()
            if now - float(value.get("created_at") or 0) <= _SEARCH_CACHE_TTL
        }
        for job_id, value in list(self._jobs.items()):
            if value.get("status") in _TERMINAL and now - float(value.get("updated_at") or 0) > _JOB_TTL:
                self._jobs.pop(job_id, None)
                self._tasks.pop(job_id, None)

    async def search(self, query: str, *, family: str = "illustrious", limit: int = 12) -> dict[str, Any]:
        query = _bounded_text(query, 120)
        if not query:
            raise AppError(422, "LORA_SEARCH_INVALID", "请输入 LoRA 名称、角色名或中文别称")
        family = str(family or "illustrious").strip().lower()
        if family not in _FAMILY_BASE_MODELS:
            raise AppError(422, "LORA_SEARCH_INVALID", f"不支持的模型族: {family}")
        limit = max(1, min(int(limit or 12), 20))
        self._cleanup()
        try:
            results = await self._client().search_loras(
                query=query,
                limit=limit,
                base_models=_FAMILY_BASE_MODELS[family],
            )
        except Exception as exc:
            raise AppError(502, "LORA_SEARCH_FAILED", _safe_error(exc), {"retryable": True}) from exc

        public: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in results or []:
            if not isinstance(raw, Mapping):
                continue
            model_id = str(raw.get("model_id") or "").strip()
            version_id = str(raw.get("version_id") or "").strip()
            identity = f"{model_id}:{version_id}"
            if not model_id or identity in seen:
                continue
            seen.add(identity)
            result_id = uuid.uuid4().hex
            page_url = str(raw.get("page_url") or "").strip()
            self._search_cache[result_id] = {
                "created_at": time.time(),
                "query": query,
                "family": family,
                "reference": page_url or model_id,
                "raw": dict(raw),
            }
            public.append({
                "id": result_id,
                "source": "civitai",
                "model_id": model_id,
                "version_id": version_id,
                "name": _bounded_text(raw.get("name") or "Civitai LoRA", 160),
                "version_name": _bounded_text(raw.get("version_name"), 100),
                "base_model": _bounded_text(raw.get("base_model"), 80),
                "trained_words": [_bounded_text(item, 100) for item in (raw.get("trained_words") or [])[:12] if _bounded_text(item, 100)],
                "tags": [_bounded_text(item, 60) for item in (raw.get("tags") or [])[:12] if _bounded_text(item, 60)],
                "download_count": int(raw.get("download_count") or 0),
                "heart_count": int(raw.get("heart_count") or 0),
                "nsfw": bool(raw.get("nsfw")),
                "page_url": page_url if urlparse(page_url).hostname in _ALLOWED_DOWNLOAD_HOSTS else "",
            })
            if len(public) >= limit:
                break
        return {"query": query, "family": family, "items": public}

    def start_download(self, result_id: str, *, alias: str = "") -> dict[str, Any]:
        self._cleanup()
        result_id = str(result_id or "").strip()
        cached = self._search_cache.get(result_id)
        if cached is None:
            raise AppError(404, "LORA_SEARCH_RESULT_EXPIRED", "搜索结果已过期，请重新搜索")
        active_id = self._active_by_result.get(result_id)
        if active_id and active_id in self._jobs and self._jobs[active_id].get("status") not in _TERMINAL:
            return self.snapshot(active_id)
        job_id = uuid.uuid4().hex
        now = time.time()
        self._jobs[job_id] = {
            "id": job_id,
            "result_id": result_id,
            "status": "queued",
            "message": "等待下载",
            "progress": {"done": 0, "total": 0, "percent": None},
            "result": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
        self._active_by_result[result_id] = job_id
        task = asyncio.create_task(self._run_download(job_id, cached, _bounded_text(alias, 80)), name=f"winefox-lora-{job_id[:8]}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda _task, jid=job_id: self._tasks.pop(jid, None))
        return self.snapshot(job_id)

    def snapshot(self, job_id: str) -> dict[str, Any]:
        value = self._jobs.get(str(job_id or ""))
        if value is None:
            raise AppError(404, "LORA_DOWNLOAD_NOT_FOUND", "LoRA 下载任务不存在")
        return copy.deepcopy(value)

    def _update(self, job_id: str, **changes: Any) -> None:
        job = self._jobs[job_id]
        job.update(changes)
        job["updated_at"] = time.time()

    def _progress(self, job_id: str, done: int, total: int) -> None:
        done = max(int(done or 0), 0)
        total = max(int(total or 0), 0)
        percent = round(done * 100 / total, 1) if total else None
        self._update(
            job_id,
            status="downloading",
            message=f"正在下载 {percent:.1f}%" if percent is not None else "正在下载 LoRA",
            progress={"done": done, "total": total, "percent": percent},
        )

    def _mode(self) -> tuple[str, dict[str, Any]]:
        settings = self._settings()
        mode = str(settings.get("mode") or "auto").strip().lower()
        local_dir = str(settings.get("local_dir") or "").strip()
        if mode == "local" or (mode == "auto" and local_dir):
            if not local_dir:
                raise RuntimeError("本地下载模式尚未配置 ComfyUI models/loras 目录")
            return "local", settings
        if mode in {"auto", "comfyui_bridge"}:
            return "comfyui_bridge", settings
        raise RuntimeError(f"未知 LoRA 下载模式: {mode}")

    @staticmethod
    def _local_target(local_dir: str, filename: str) -> Path:
        expanded = os.path.expandvars(os.path.expanduser(str(local_dir or "").strip()))
        root = Path(expanded)
        if not root.is_absolute():
            raise RuntimeError("LoRA 本地目录必须是绝对路径")
        root.mkdir(parents=True, exist_ok=True)
        resolved_root = root.resolve(strict=True)
        if not resolved_root.is_dir():
            raise RuntimeError("LoRA 本地目录不可用")
        target = (resolved_root / safe_filename(filename)).resolve(strict=False)
        try:
            target.relative_to(resolved_root)
        except ValueError as exc:
            raise RuntimeError("LoRA 文件名试图逃逸目标目录") from exc
        return target

    async def _install_local(self, client: Any, summary: dict[str, Any], settings: dict[str, Any], job_id: str) -> dict[str, Any]:
        filename = safe_filename(summary.get("file_name"))
        target = self._local_target(str(settings.get("local_dir") or ""), filename)
        if target.suffix.lower() not in _ALLOWED_SUFFIXES:
            raise RuntimeError("只允许安装 safetensors/ckpt/pt 格式的 LoRA")
        if target.is_file() and target.stat().st_size > 1024:
            return {"filename": target.name, "mode": "local", "already_exists": True, "bytes": target.stat().st_size}
        free = shutil.disk_usage(target.parent).free
        if free < 512 * 1024 * 1024:
            raise RuntimeError("目标磁盘可用空间不足 512 MB")

        async def progress(done: int, total: int) -> None:
            self._progress(job_id, done, total)

        await client.download_file(str(summary.get("download_url") or ""), target, progress_cb=progress)
        if not target.is_file() or target.stat().st_size <= 1024:
            raise RuntimeError("下载完成但 LoRA 文件为空或异常")
        return {"filename": target.name, "mode": "local", "already_exists": False, "bytes": target.stat().st_size}

    @staticmethod
    def _bridge_endpoint(settings: Mapping[str, Any]) -> str:
        endpoint = str(settings.get("bridge_endpoint") or "/astrbot/lora").strip()
        if not endpoint.startswith("/") or ".." in endpoint or "?" in endpoint or "#" in endpoint:
            raise RuntimeError("Bridge endpoint 必须是安全的绝对 URL 路径")
        return endpoint.rstrip("/") or "/astrbot/lora"

    async def bridge_status(self) -> dict[str, Any]:
        settings = self._settings()
        endpoint = self._bridge_endpoint(settings)
        token = self.config.get_secret("comfyui_bridge_token") or ""
        try:
            async with ComfyUITransport(self.connection_factory()) as transport:
                headers = {"X-AstrBot-Bridge-Token": token} if token else {}
                async with transport.session.get(f"{transport.base_url}{endpoint}/status", headers=headers) as response:
                    raw = await response.content.read(1024 * 1024 + 1)
                    if response.status == 404:
                        return {"ok": False, "installed": False, "message": "ComfyUI LoRA Bridge 未安装或尚未重启"}
                    data = _json_response(raw, label="ComfyUI Bridge")
                    if response.status >= 400 or not data.get("ok"):
                        return {"ok": False, "installed": True, "message": _safe_error(data.get("error") or f"HTTP {response.status}")}
                    return {
                        "ok": True,
                        "installed": True,
                        "version": _bounded_text(data.get("version"), 40),
                        "resource_download": bool(data.get("request_resource_download_supported", True)),
                        "message": "ComfyUI LoRA Bridge 已就绪",
                    }
        except Exception as exc:
            return {"ok": False, "installed": False, "message": _safe_error(exc)}

    async def _install_bridge(self, client: Any, summary: dict[str, Any], settings: dict[str, Any], job_id: str) -> dict[str, Any]:
        download_url = str(summary.get("download_url") or "").strip()
        parsed = urlparse(download_url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in _ALLOWED_DOWNLOAD_HOSTS:
            raise RuntimeError("Civitai 返回了不受信任的下载地址")
        endpoint = self._bridge_endpoint(settings)
        bridge_token = self.config.get_secret("comfyui_bridge_token") or ""
        external_headers = {
            str(key): str(value)
            for key, value in client._headers().items()
            if str(key).lower() in {"authorization", "cookie", "referer", "user-agent"} and str(value).strip()
        }
        payload = {
            "url": download_url,
            "filename": safe_filename(summary.get("file_name")),
            "source": "Civitai",
            "headers": external_headers,
            "proxy": str(settings.get("bridge_proxy") or "").strip(),
            "target_type": "loras",
            "target_subdir": "",
        }
        headers = {"X-AstrBot-Bridge-Token": bridge_token} if bridge_token else {}
        deadline = time.monotonic() + max(60, min(int(settings.get("download_timeout") or 7200), 24 * 60 * 60))
        async with ComfyUITransport(self.connection_factory()) as transport:
            base = f"{transport.base_url}{endpoint}"
            async with transport.session.post(f"{base}/download", json=payload, headers=headers) as response:
                raw = await response.content.read(1024 * 1024 + 1)
                if response.status == 404:
                    raise RuntimeError("ComfyUI LoRA Bridge 未安装或未重启")
                data = _json_response(raw, label="ComfyUI Bridge")
                if response.status >= 400 or not data.get("ok"):
                    raise RuntimeError(data.get("error") or f"Bridge 下载请求失败 HTTP {response.status}")
            if str(data.get("status") or "").lower() == "done":
                return {"filename": safe_filename(data.get("filename") or payload["filename"]), "mode": "comfyui_bridge", "already_exists": bool(data.get("already_exists")), "bytes": int(data.get("total") or data.get("done") or 0)}
            task_id = str(data.get("task_id") or "").strip()
            if not task_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
                raise RuntimeError("ComfyUI Bridge 未返回有效任务 ID")
            while time.monotonic() < deadline:
                await asyncio.sleep(1.0)
                async with transport.session.get(f"{base}/task/{task_id}", headers=headers) as response:
                    raw = await response.content.read(1024 * 1024 + 1)
                    data = _json_response(raw, label="ComfyUI Bridge 任务")
                    if response.status >= 400 or not data.get("ok"):
                        raise RuntimeError(data.get("error") or f"Bridge 任务查询失败 HTTP {response.status}")
                status = str(data.get("status") or "").lower()
                self._progress(job_id, int(data.get("done") or 0), int(data.get("total") or 0))
                if status == "done":
                    return {"filename": safe_filename(data.get("filename") or payload["filename"]), "mode": "comfyui_bridge", "already_exists": bool(data.get("already_exists")), "bytes": int(data.get("total") or data.get("done") or 0)}
                if status in {"error", "failed", "cancelled"}:
                    raise RuntimeError(data.get("error") or "ComfyUI Bridge 下载失败")
            raise RuntimeError("ComfyUI Bridge 下载超时，远端任务可能仍在继续")

    async def _write_profile(self, summary: dict[str, Any], filename: str, family: str, alias: str) -> dict[str, Any]:
        summary = dict(summary)
        summary["file_name"] = safe_filename(filename)
        summary["search_query"] = _bounded_text(summary.get("search_query"), 120)
        profile_name = "lora_profiles.json" if family == "anima" else "lora_profiles_illustrious.json"
        path = self.data_root / "lora_profiles" / profile_name
        store = LoraProfileStore(path)
        profile = await asyncio.to_thread(store.upsert_from_civitai, summary, alias, 0.8, 0.8)
        return {
            "character": _bounded_text(profile.get("character"), 100),
            "aliases": [_bounded_text(item, 100) for item in (profile.get("aliases") or [])[:12]],
            "trigger_words": [_bounded_text(item, 100) for item in (profile.get("activation_tags") or [])[:12]],
        }

    async def _run_download(self, job_id: str, cached: dict[str, Any], alias: str) -> None:
        result_id = str(self._jobs[job_id]["result_id"])
        try:
            async with self._download_lock:
                self._update(job_id, status="resolving", message="正在读取模型版本与触发词")
                client = self._client()
                summary = await client.resolve_model(
                    str(cached.get("reference") or ""),
                    base_models=_FAMILY_BASE_MODELS[str(cached.get("family") or "illustrious")],
                )
                summary["search_query"] = str(cached.get("query") or "")
                mode, settings = self._mode()
                self._update(job_id, status="downloading", message="正在准备下载")
                if mode == "local":
                    installed = await self._install_local(client, summary, settings, job_id)
                else:
                    installed = await self._install_bridge(client, summary, settings, job_id)
                self._update(job_id, status="profiling", message="正在建立中文别名与触发词档案")
                family = str(cached.get("family") or "illustrious")
                profile = await self._write_profile(summary, installed["filename"], family, alias)
                if self.on_installed is not None:
                    value = self.on_installed()
                    if asyncio.iscoroutine(value):
                        await value
                result = {
                    **installed,
                    "model_name": _bounded_text(summary.get("model_name"), 160),
                    "base_model": _bounded_text(summary.get("base_model"), 80),
                    "profile": profile,
                }
                self._update(
                    job_id,
                    status="completed",
                    message="LoRA 已安装并建立触发词档案",
                    progress={"done": int(installed.get("bytes") or 0), "total": int(installed.get("bytes") or 0), "percent": 100.0},
                    result=result,
                    error=None,
                )
        except asyncio.CancelledError:
            self._update(job_id, status="cancelled", message="下载已取消", error={"code": "LORA_DOWNLOAD_CANCELLED", "message": "下载已取消", "retryable": True})
            raise
        except Exception as exc:
            message = _safe_error(exc)
            logger.warning("LoRA download failed job=%s: %s", job_id, message)
            self._update(
                job_id,
                status="failed",
                message="LoRA 下载失败",
                error={"code": "LORA_DOWNLOAD_FAILED", "message": message, "retryable": True},
            )
        finally:
            if self._active_by_result.get(result_id) == job_id:
                self._active_by_result.pop(result_id, None)

    async def stop(self) -> None:
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()


__all__ = ["LoraDownloadService"]

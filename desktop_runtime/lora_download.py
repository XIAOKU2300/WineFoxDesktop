"""Civitai LoRA discovery and safe installation for WineFox Desktop.

The service reuses the original plugin's mature Civitai resolve/version
selection, resilient download and profile extraction.  It delegates search to
:class:`LoraSearchService` (public-safe items) and persists download jobs in
SQLite via :class:`winefox_core.storage.LoraDownloadJobStore`, so a large model
download never blocks navigation or the WebView UI thread and survives app
restarts (non-terminal jobs are marked ``interrupted`` by the engine).

Download safety:
- Download URLs / cookies / tokens are kept server-side; snapshots only expose
  public-safe fields (``validate_public_json`` enforces this on the store side).
- Local downloads verify SHA256 against Civitai when a hash is available and
  rename on filename conflicts instead of silently skipping.
"""

from __future__ import annotations

import asyncio
import hashlib
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
from winefox_core.storage import Database, LoraAssetStore, LoraDownloadJobStore
from winefox_core.storage import LoraProfileStore as SqlLoraProfileStore
from winefox_core.storage_models import DOWNLOAD_TERMINAL_STATES, LoraDownloadState

logger = logging.getLogger("winefox.runtime.lora_download")

# LoraSearchService 由并行 agent 创建；import 失败时 search 回退 Civitai 直查。
try:  # pragma: no cover - 取决于并行 agent 是否已合入
    from .lora_search import LoraSearchService as _LoraSearchService

    _LORA_SEARCH_AVAILABLE = True
except Exception:  # pragma: no cover - 缺失时的回退路径
    _LoraSearchService = None
    _LORA_SEARCH_AVAILABLE = False

_SEARCH_CACHE_TTL = 30 * 60
_ALLOWED_SUFFIXES = {".safetensors", ".ckpt", ".pt"}
_ALLOWED_DOWNLOAD_HOSTS = {"civitai.com", "www.civitai.com", "civitai.red", "www.civitai.red"}
_FAMILY_BASE_MODELS = {
    "anima": ["Anima"],
    "illustrious": ["Illustrious"],
}
_DOWNLOAD_MESSAGES = {
    "queued": "等待下载",
    "resolving": "正在读取模型版本与触发词",
    "downloading": "正在下载 LoRA",
    "profiling": "正在建立中文别名与触发词档案",
    "refreshing": "正在刷新本地档案",
    "completed": "LoRA 已安装并建立触发词档案",
    "failed": "LoRA 下载失败",
    "cancelled": "下载已取消",
    "interrupted": "下载已中断，可重新下载",
}
_TERMINAL_STATES = frozenset(s.value for s in DOWNLOAD_TERMINAL_STATES)


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
    """持久化 LoRA 下载服务（SQLite 状态机 + 哈希校验 + 冲突改名 + bridge 探测）。"""

    def __init__(
        self,
        *,
        db: Database,
        config: Any,
        data_root: Path,
        connection_factory: Callable[[], Any],
        on_installed: Callable[[], Awaitable[None] | None] | None = None,
        client_factory: Callable[..., Any] = CivitaiClient,
        search_service_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._db = db
        self.config = config
        self.data_root = Path(data_root)
        self.connection_factory = connection_factory
        self.on_installed = on_installed
        self.client_factory = client_factory
        self._search_cache: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._download_lock = asyncio.Lock()
        self._store = LoraDownloadJobStore(db)
        self._search_service_factory = search_service_factory
        self._search_service: Any = None
        self._search_service_failed = False

    # -- 配置 ---------------------------------------------------------------

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
        return self._store.active_count()

    def _cleanup(self) -> None:
        now = time.time()
        self._search_cache = {
            key: value
            for key, value in self._search_cache.items()
            if now - float(value.get("created_at") or 0) <= _SEARCH_CACHE_TTL
        }

    # -- 搜索 ---------------------------------------------------------------

    async def search(self, query: str, *, family: str = "illustrious", limit: int = 12) -> dict[str, Any]:
        query = _bounded_text(query, 120)
        if not query:
            raise AppError(422, "LORA_SEARCH_INVALID", "请输入 LoRA 名称、角色名或中文别称")
        family = str(family or "illustrious").strip().lower()
        if family not in _FAMILY_BASE_MODELS:
            raise AppError(422, "LORA_SEARCH_INVALID", f"不支持的模型族: {family}")
        limit = max(1, min(int(limit or 12), 20))
        self._cleanup()
        service = self._search_service
        if _LORA_SEARCH_AVAILABLE and not self._search_service_failed:
            if service is None:
                service = self._build_search_service()
                self._search_service = service
            if service is not None:
                try:
                    result = await service.search(query, family=family, limit=limit)
                    raw_items = result.get("items") or []
                    items = list(raw_items) if isinstance(raw_items, list) else []
                    for item in items:
                        if isinstance(item, Mapping):
                            self._cache_result(
                                str(item.get("id") or "").strip(),
                                family=str(item.get("family") or family),
                                source=str(item.get("source") or "civitai"),
                                model_id=item.get("model_id"),
                                version_id=item.get("version_id"),
                                query=query,
                            )
                    return {"query": query, "family": family, "items": items}
                except AppError:
                    raise
                except Exception as exc:
                    logger.warning("LoraSearchService 搜索失败，回退 Civitai 直查: %s", _safe_error(exc))
        return await self._search_fallback(query, family, limit)

    def _build_search_service(self) -> Any:
        try:
            if self._search_service_factory is not None:
                return self._search_service_factory(config=self.config, data_root=self.data_root)
            return _LoraSearchService(config=self.config, data_root=self.data_root)
        except Exception as exc:
            logger.warning("LoraSearchService 构造失败，回退 Civitai 直查: %s", _safe_error(exc))
            self._search_service_failed = True
            return None

    def _cache_result(
        self,
        result_id: str,
        *,
        family: str,
        source: str,
        model_id: Any,
        version_id: Any,
        query: str,
    ) -> None:
        if not result_id:
            return
        self._search_cache[result_id] = {
            "created_at": time.time(),
            "query": query,
            "family": family,
            "source": source,
            "model_id": str(model_id or "").strip(),
            "version_id": str(version_id or "").strip(),
        }

    async def _search_fallback(self, query: str, family: str, limit: int) -> dict[str, Any]:
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
            self._cache_result(
                result_id,
                family=family,
                source="civitai",
                model_id=model_id,
                version_id=version_id,
                query=query,
            )
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

    # -- 任务调度 -----------------------------------------------------------

    def start_download(self, result_id: str, *, alias: str = "") -> dict[str, Any]:
        self._cleanup()
        result_id = str(result_id or "").strip()
        if not result_id:
            raise AppError(422, "LORA_DOWNLOAD_INVALID", "缺少下载任务标识")
        running = self._tasks.get(result_id)
        if running is not None and not running.done():
            return self.snapshot(result_id)
        existing = self._store.get(result_id)
        if existing is not None:
            if existing.state in _TERMINAL_STATES:
                # 终态：requeue 重试回 queued
                alias = _bounded_text(alias or existing.alias, 80)
                self._store.requeue(result_id)
            else:
                return self.snapshot(result_id)
        else:
            cached = self._search_cache.get(result_id)
            if cached is None:
                raise AppError(404, "LORA_SEARCH_RESULT_EXPIRED", "搜索结果已过期，请重新搜索")
            self._store.enqueue(
                result_id,
                family=str(cached.get("family") or "illustrious"),
                alias=_bounded_text(alias, 80),
                source=str(cached.get("source") or "civitai"),
                source_ref={
                    "source": str(cached.get("source") or "civitai"),
                    "family": str(cached.get("family") or "illustrious"),
                    "model_id": str(cached.get("model_id") or ""),
                    "version_id": str(cached.get("version_id") or ""),
                },
                request_timeout=max(10, min(int(self._settings().get("request_timeout") or 60), 180)),
            )
        task = asyncio.create_task(
            self._run_download(result_id, alias),
            name=f"winefox-lora-{result_id[:8]}",
        )
        self._tasks[result_id] = task
        task.add_done_callback(lambda _t, rid=result_id: self._tasks.pop(rid, None))
        return self.snapshot(result_id)

    def snapshot(self, job_id: str) -> dict[str, Any]:
        record = self._store.get(str(job_id or ""))
        if record is None:
            raise AppError(404, "LORA_DOWNLOAD_NOT_FOUND", "LoRA 下载任务不存在")
        return self._to_job_dict(record)

    def _to_job_dict(self, record: Any) -> dict[str, Any]:
        state = record.state
        progress = dict(record.progress or {})
        message = _DOWNLOAD_MESSAGES.get(state, "LoRA 下载")
        if state == "downloading" and progress.get("percent") is not None:
            message = f"正在下载 {progress['percent']:.1f}%"
        error = record.error
        if error:
            error = {"code": "LORA_DOWNLOAD_FAILED", "message": str(error), "retryable": True}
        return {
            "id": record.result_id,
            "result_id": record.result_id,
            "status": state,
            "message": message,
            "progress": progress,
            "result": dict(record.result or {}) or None,
            "error": error,
            "family": record.family,
            "alias": record.alias,
            "source": record.source,
            "source_ref": dict(record.source_ref or {}),
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
        }

    # -- 模式 / 安装 --------------------------------------------------------

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

    @staticmethod
    def _is_installed_file(path: Path) -> bool:
        try:
            return path.is_file() and path.stat().st_size > 1024
        except OSError:
            return False

    def _unique_local_target(self, local_dir: str, filename: str) -> tuple[Path, bool]:
        """同名已安装文件存在时改名为 ``name (N).safetensors``，避免覆盖。"""
        base = self._local_target(local_dir, filename)
        if not self._is_installed_file(base):
            return base, False
        stem = base.stem
        suffix = base.suffix or ".safetensors"
        for index in range(1, 1000):
            candidate = base.with_name(f"{stem} ({index}){suffix}")
            if not self._is_installed_file(candidate):
                return candidate, True
        raise RuntimeError("无法为同名 LoRA 生成唯一文件名")

    async def _install_local(self, client: Any, summary: dict[str, Any], settings: dict[str, Any], result_id: str) -> dict[str, Any]:
        filename = safe_filename(summary.get("file_name"))
        if Path(filename).suffix.lower() not in _ALLOWED_SUFFIXES:
            raise RuntimeError("只允许安装 safetensors/ckpt/pt 格式的 LoRA")
        target, conflict_renamed = self._unique_local_target(str(settings.get("local_dir") or ""), filename)
        free = shutil.disk_usage(target.parent).free
        if free < 512 * 1024 * 1024:
            raise RuntimeError("目标磁盘可用空间不足 512 MB")

        async def progress(done: int, total: int) -> None:
            self._set_progress(result_id, done, total)

        await client.download_file(str(summary.get("download_url") or ""), target, progress_cb=progress)
        if not target.is_file() or target.stat().st_size <= 1024:
            raise RuntimeError("下载完成但 LoRA 文件为空或异常")
        return {
            "filename": target.name,
            "mode": "local",
            "already_exists": False,
            "conflict_renamed": bool(conflict_renamed),
            "bytes": target.stat().st_size,
        }

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

    async def _install_bridge(self, client: Any, summary: dict[str, Any], settings: dict[str, Any], result_id: str) -> dict[str, Any]:
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
                return {"filename": safe_filename(data.get("filename") or payload["filename"]), "mode": "comfyui_bridge", "already_exists": bool(data.get("already_exists")), "conflict_renamed": False, "bytes": int(data.get("total") or data.get("done") or 0)}
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
                self._set_progress(result_id, int(data.get("done") or 0), int(data.get("total") or 0))
                if status == "done":
                    return {"filename": safe_filename(data.get("filename") or payload["filename"]), "mode": "comfyui_bridge", "already_exists": bool(data.get("already_exists")), "conflict_renamed": False, "bytes": int(data.get("total") or data.get("done") or 0)}
                if status in {"error", "failed", "cancelled"}:
                    raise RuntimeError(data.get("error") or "ComfyUI Bridge 下载失败")
            raise RuntimeError("ComfyUI Bridge 下载超时，远端任务可能仍在继续")

    def _set_progress(self, result_id: str, done: int, total: int) -> None:
        done = max(int(done or 0), 0)
        total = max(int(total or 0), 0)
        percent = round(done * 100 / total, 1) if total else None
        self._store.set_progress(
            result_id,
            {"done": done, "total": total, "percent": percent},
        )

    # -- 哈希校验 -----------------------------------------------------------

    @staticmethod
    def _build_reference(source_ref: Mapping[str, Any]) -> str:
        model_id = str(source_ref.get("model_id") or "").strip()
        version_id = str(source_ref.get("version_id") or "").strip()
        if model_id.isdigit() and version_id.isdigit():
            return f"https://civitai.com/models/{model_id}?modelVersionId={version_id}"
        if model_id.isdigit():
            return model_id
        if version_id.isdigit():
            return f"https://civitai.com/api/download/models/{version_id}"
        return ""

    @staticmethod
    def _extract_sha256(file_info: Mapping[str, Any]) -> str:
        hashes = file_info.get("hashes") or {}
        value = hashes.get("SHA256") or hashes.get("sha256") or ""
        return str(value or "").strip().lower() or ""

    async def _expected_hash(self, client: Any, summary: Mapping[str, Any]) -> str:
        """取 Civitai 版本 files 中的期望 SHA256；取不到返回空串（不硬失败）。"""
        try:
            version_id = summary.get("version_id")
            if version_id is None:
                return ""
            version = await client.get_model_version(int(version_id))
            files = [f for f in (version.get("files") or []) if isinstance(f, Mapping)]
            target = str(summary.get("file_name") or "").strip().lower()
            for file_info in files:
                if str(file_info.get("name") or "").strip().lower() == target:
                    value = self._extract_sha256(file_info)
                    if value:
                        return value
            for file_info in files:
                value = self._extract_sha256(file_info)
                if value:
                    return value
        except Exception as exc:
            logger.info("读取 Civitai 期望哈希失败（跳过校验）: %s", _safe_error(exc))
        return ""

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        try:
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            return ""
        return digest.hexdigest()

    async def _sha256_async(self, path: Path) -> str:
        return await asyncio.to_thread(self._sha256_file, path)

    @staticmethod
    def _installed_local_path(settings: Mapping[str, Any], installed: Mapping[str, Any]) -> Path | None:
        if str(installed.get("mode") or "") != "local":
            return None
        try:
            local_dir = str(settings.get("local_dir") or "").strip()
            root = Path(os.path.expandvars(os.path.expanduser(local_dir)))
            target = (root / safe_filename(str(installed.get("filename") or ""))).resolve(strict=False)
            return target if target.is_file() else None
        except Exception:
            return None

    # -- 档案 / 资产 --------------------------------------------------------

    async def _write_profile(self, summary: dict[str, Any], filename: str, family: str, alias: str) -> dict[str, Any]:
        summary = dict(summary)
        summary["file_name"] = safe_filename(filename)
        summary["search_query"] = _bounded_text(summary.get("search_query"), 120)
        profile_name = "lora_profiles.json" if family == "anima" else "lora_profiles_illustrious.json"
        path = self.data_root / "lora_profiles" / profile_name
        store = LoraProfileStore(path)
        profile = await asyncio.to_thread(store.upsert_from_civitai, summary, alias, 0.8, 0.8)
        self._save_sqlite_profile(family, profile)
        return {
            "character": _bounded_text(profile.get("character"), 100),
            "aliases": [_bounded_text(item, 100) for item in (profile.get("aliases") or [])[:12]],
            "trigger_words": [_bounded_text(item, 100) for item in (profile.get("activation_tags") or [])[:12]],
        }

    def _save_sqlite_profile(self, family: str, profile: dict[str, Any]) -> None:
        try:
            sql_store = SqlLoraProfileStore(self._db)
            key = str(profile.get("character") or "").strip() or Path(str(profile.get("lora") or "")).stem
            sql_store.save(family, key, dict(profile))
        except Exception as exc:
            logger.warning("SQLite LoRA 档案写入失败 family=%s: %s", family, _safe_error(exc))

    def _register_asset(
        self,
        summary: Mapping[str, Any],
        installed: Mapping[str, Any],
        family: str,
        sha256: str,
    ) -> int | None:
        try:
            model_id = summary.get("model_id")
            version_id = summary.get("version_id")
            record = LoraAssetStore(self._db).register(
                model_family=family,
                filename=str(installed.get("filename") or ""),
                comfy_key=str(installed.get("filename") or ""),
                display_name=_bounded_text(summary.get("model_name"), 160),
                source=str(installed.get("mode") or summary.get("source") or "civitai"),
                model_id=int(model_id) if model_id is not None else None,
                version_id=int(version_id) if version_id is not None else None,
                info={
                    "sha256": sha256 or "",
                    "size_bytes": int(installed.get("bytes") or 0),
                    "mode": str(installed.get("mode") or ""),
                },
            )
            return record.id
        except Exception as exc:
            logger.warning("LoRA 资产登记失败 filename=%s: %s", installed.get("filename"), _safe_error(exc))
            return None

    # -- 下载流程 -----------------------------------------------------------

    async def _run_download(self, result_id: str, alias: str) -> None:
        try:
            async with self._download_lock:
                record = self._store.get(result_id)
                if record is None:
                    return
                family = record.family
                source_ref = dict(record.source_ref or {})
                self._store.update_state(result_id, LoraDownloadState.RESOLVING)
                client = self._client()
                summary = await client.resolve_model(
                    self._build_reference(source_ref),
                    base_models=_FAMILY_BASE_MODELS.get(family, ["Illustrious"]),
                )
                summary["search_query"] = _bounded_text(alias or str(source_ref.get("query") or ""), 120)
                mode, settings = self._mode()
                if mode == "comfyui_bridge":
                    probe = await self.bridge_status()
                    if not probe.get("ok"):
                        configured = str(settings.get("mode") or "auto").strip().lower()
                        if configured == "auto" and str(settings.get("local_dir") or "").strip():
                            mode = "local"
                        else:
                            raise RuntimeError(f"ComfyUI LoRA Bridge 不可用：{probe.get('message') or '未就绪'}")

                self._store.update_state(result_id, LoraDownloadState.DOWNLOADING)
                if mode == "local":
                    installed = await self._install_local(client, summary, settings, result_id)
                else:
                    installed = await self._install_bridge(client, summary, settings, result_id)

                expected_hash = await self._expected_hash(client, summary)
                local_hash = ""
                if expected_hash:
                    local_path = self._installed_local_path(settings, installed)
                    if local_path is not None:
                        local_hash = await self._sha256_async(local_path)
                        if local_hash and local_hash.lower() != expected_hash.lower():
                            raise RuntimeError(
                                f"LoRA 哈希校验失败：Civitai 期望 {expected_hash[:12]}…，实际 {local_hash[:12]}…"
                            )

                self._store.update_state(result_id, LoraDownloadState.PROFILING)
                profile = await self._write_profile(summary, installed["filename"], family, alias)
                asset_id = self._register_asset(summary, installed, family, local_hash)
                result = {
                    **installed,
                    "model_name": _bounded_text(summary.get("model_name"), 160),
                    "base_model": _bounded_text(summary.get("base_model"), 80),
                    "profile": profile,
                    "hash": local_hash or None,
                    "expected_hash": expected_hash or None,
                    "conflict_renamed": bool(installed.get("conflict_renamed", False)),
                }
                if asset_id is not None:
                    result["asset_id"] = asset_id

                self._store.update_state(result_id, LoraDownloadState.REFRESHING)
                if self.on_installed is not None:
                    value = self.on_installed()
                    if asyncio.iscoroutine(value):
                        await value
                self._store.finish(result_id, result)
        except asyncio.CancelledError:
            try:
                self._store.update_state(result_id, LoraDownloadState.CANCELLED, error="下载已取消")
            except Exception:
                logger.info("写入取消状态失败 job=%s", result_id, exc_info=True)
            raise
        except Exception as exc:
            message = _safe_error(exc)
            logger.warning("LoRA download failed job=%s: %s", result_id, message)
            try:
                self._store.update_state(result_id, LoraDownloadState.FAILED, error=message)
            except Exception:
                logger.warning("写入失败状态失败 job=%s", result_id, exc_info=True)

    async def stop(self) -> None:
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()


__all__ = ["LoraDownloadService"]

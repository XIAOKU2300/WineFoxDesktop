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
import ipaddress
import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urlencode, urlparse

from civitai_api import (
    CivitaiClient,
    CivitaiRedClient,
    HuggingFaceClient,
    LoraProfileStore,
    safe_filename,
)
from desktop_backend.errors import AppError
from winefox_core.comfyui_transport import ComfyUITransport
from winefox_core.lora_service import normalize_lora_key
from winefox_core.storage import Database, LoraAssetStore, LoraDownloadJobStore
from winefox_core.storage import LoraProfileStore as SqlLoraProfileStore
from winefox_core.storage_models import DOWNLOAD_TERMINAL_STATES, LoraDownloadState

from . import net_env
from .setup_verify import VerifyFailure, verify_safetensors

logger = logging.getLogger("winefox.runtime.lora_download")

# LoraSearchService 由并行 agent 创建；import 失败时 search 回退 Civitai 直查。
try:  # pragma: no cover - 取决于并行 agent 是否已合入
    from .lora_search import LoraSearchService as _LoraSearchService

    _LORA_SEARCH_AVAILABLE = True
except Exception:  # pragma: no cover - 缺失时的回退路径
    _LoraSearchService = None
    _LORA_SEARCH_AVAILABLE = False

_SEARCH_CACHE_TTL = 30 * 60
_ALLOWED_SUFFIXES = {".safetensors"}
_ALLOWED_DOWNLOAD_HOSTS = {
    "civitai.com",
    "www.civitai.com",
    "civitai.red",
    "www.civitai.red",
    "huggingface.co",
    "www.huggingface.co",
    "hf.co",
    "www.hf.co",
}
#: 各下载来源允许的下载主机白名单（hostname 精确匹配，禁止 IP/前缀逃逸）。
_SOURCE_DOWNLOAD_HOSTS = {
    "civitai": {"civitai.com", "www.civitai.com"},
    "civitai_red": {"civitai.red", "www.civitai.red"},
    "huggingface": {"huggingface.co", "www.huggingface.co", "hf.co", "www.hf.co"},
}
_ADULT_SOURCES = frozenset({"civitai_red"})
#: Clash/mihomo TUN fake-ip 网段。开启 fake-ip 的本机 DNS 会把所有域名解析
#: 成保留段内的地址（IPv4 常见 198.18.0.0/15，IPv6 还有 ULA 段）；这不代表
#: 目标真是内网，而是由 TUN 代为转交真实主机。除该已知段外，再用对照探针
#: 兜底识别其他 fake-ip 实现（见 ``_local_dns_is_fake_ip``）。仅放行“DNS
#: 解析出来的”该类地址；URL 里直接写 IP 字面量仍然拒绝。
_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
_FAKE_IP_PROBE_HOST = "civitai.com"
_FAKE_IP_PROBE_TTL = 60.0
_fake_ip_probe_cache: dict[str, Any] = {"at": 0.0, "value": False}


async def _local_dns_is_fake_ip() -> bool:
    """探测本机 DNS 是否处于 fake-ip（TUN）模式。

    对照域名 civitai.com 永远不应解析到非公网地址；若发生，说明本机
    DNS 把所有域名映射进 fake-ip 段，由 TUN 转交真实主机。此时对重定向
    主机继续做 is_global 硬校验只会误杀合法下载。
    """
    now = time.monotonic()
    if now - float(_fake_ip_probe_cache.get("at") or 0) < _FAKE_IP_PROBE_TTL:
        return bool(_fake_ip_probe_cache.get("value"))
    value = False
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            _FAKE_IP_PROBE_HOST, 443, type=0
        )
        for info in infos:
            try:
                address = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if not address.is_global:
                value = True
                break
    except OSError:
        value = False
    _fake_ip_probe_cache.update(at=now, value=value)
    return value


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


def _public_text(value: Any, limit: int = 200) -> str:
    """脱敏公开文本：命中 URL/凭据/绝对路径/穿越/网络路径模式的字符串一律置空。"""
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return ""
    if re.search(r"(?i)https?://|www\.|ftp://|file://|token=|api[_-]?key|authorization|cookie", text):
        return ""
    if re.search(r"(?i)(?:[a-z]:[\\/]|\\\\|^[\\/]|\.\.|//)", text):
        return ""
    return text[:limit]


def _translate_network_error(text: str) -> str:
    """常见 aiohttp 英文错误翻译成可操作的中文，避免原文直进界面。"""
    text = re.sub(
        r"Cannot connect to host\s+([^:\s]+):(\d+)[^\n]*",
        lambda m: f"无法连接 {m.group(1)}:{m.group(2)}（目标或代理不可达，请检查代理设置）",
        text,
    )
    text = re.sub(
        r"(?i)Name or service not known",
        "域名无法解析（请检查网络或代理）",
        text,
    )
    text = re.sub(
        r"(?i)(Connection reset by peer|connection lost)",
        "连接被重置（网络不稳定，可重试）",
        text,
    )
    text = re.sub(
        r"(?i)ssl:\w+",
        "",
        text,
    )
    return text


def _safe_error(value: Any) -> str:
    text = str(value or "LoRA 下载失败")
    text = _translate_network_error(text)
    text = re.sub(r"(?i)(token=)[^&\s]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(authorization|cookie)(\s*[:=]\s*)[^\s,;]+", r"\1\2[REDACTED]", text)
    text = re.sub(r"(?i)(?<![a-z0-9])[a-z]:[\\/][^\r\n,;]+|\\\\[^\r\n,;]+", "[LOCAL_PATH]", text)
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


def _require_trusted_download_url(download_url: str, source: str) -> str:
    """校验下载 URL 属于来源白名单的主机且仅 https；返回原 URL。"""
    url = str(download_url or "").strip()
    if not url:
        raise RuntimeError("下载源未提供下载地址")
    parsed = urlparse(url)
    allowed = _SOURCE_DOWNLOAD_HOSTS.get(str(source or "civitai").strip().lower(), _ALLOWED_DOWNLOAD_HOSTS)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in allowed:
        raise RuntimeError("下载源返回了不受信任的下载地址")
    return url


async def _require_public_redirect_url(download_url: str, *, proxy: str = "") -> bool:
    """Validate every redirect target before aiohttp follows it.

    The first URL is source-host allowlisted separately. Redirects may use a
    vendor CDN, so they are accepted only when HTTPS, credential-free, on the
    default port, and every resolved address is public.

    When a proxy is configured the proxy performs DNS resolution itself; a
    local ``getaddrinfo`` check is both redundant and wrong behind Clash/TUN
    setups, where the local resolver may be unable to resolve (or may return
    poisoned answers for) the Civitai CDN host even though the proxy can fetch
    it. Structural checks above stay in force in proxy mode.
    """
    url = str(download_url or "").strip()
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").strip().lower()
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("下载重定向地址无效") from exc
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise RuntimeError("下载重定向到了不安全的地址")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        raise RuntimeError("下载重定向不允许使用 IP 地址")
    if str(proxy or "").strip():
        return True
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port or 443, type=0
        )
    except OSError as exc:
        raise RuntimeError("下载重定向主机无法解析") from exc
    if not infos:
        raise RuntimeError("下载重定向主机无法解析")
    # fake-ip/TUN 环境下本机解析结果全是保留段地址，交由 TUN 转发；
    # 否则保持严格校验：任一解析地址非公网即拒绝。
    fake_ip_dns = await _local_dns_is_fake_ip()
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError as exc:
            raise RuntimeError("下载重定向主机解析结果无效") from exc
        if (
            not address.is_global
            and address not in _FAKE_IP_NETWORK
            and not fake_ip_dns
        ):
            raise RuntimeError("下载重定向到了非公网地址")
    return True


def _optional_int(value: Any) -> int | None:
    text = str(value or "").strip()
    return int(text) if text.isdigit() else None


def _safe_hf_file_ref(value: Any) -> str:
    """保留 Hugging Face 仓库内相对路径，同时拒绝本地路径/穿越。"""
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or "\x00" in raw or len(raw) > 500:
        return ""
    parts = raw.split("/")
    if any(part in {"", ".", ".."} or ":" in part for part in parts):
        return ""
    return "/".join(parts)


def _safe_hf_repo_id(value: Any) -> str:
    raw = str(value or "").strip().strip("/")
    return raw if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", raw) else ""


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
        red_client_factory: Callable[..., Any] = CivitaiRedClient,
        hf_client_factory: Callable[..., Any] = HuggingFaceClient,
        search_service_factory: Callable[..., Any] | None = None,
        preview_service: Any = None,
    ) -> None:
        self._db = db
        self.config = config
        self.data_root = Path(data_root)
        self.connection_factory = connection_factory
        self.on_installed = on_installed
        self.client_factory = client_factory
        self.red_client_factory = red_client_factory
        self.hf_client_factory = hf_client_factory
        self._preview_service = preview_service
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

    def _client(self, source: str = "civitai"):
        settings = self._settings()
        # 显式代理优先；留空回退系统代理（环境变量/注册表）。
        proxy, _proxy_source = net_env.effective_proxy(str(settings.get("proxy") or "").strip())
        kwargs = dict(
            api_token=self.config.get_secret("civitai_api_token") or "",
            cookie=self.config.get_secret("civitai_cookie") or "",
            proxy=proxy,
            timeout=max(10, min(int(settings.get("request_timeout") or 60), 180)),
            downloader="multithread",
            split=max(1, min(int(settings.get("split") or 4), 8)),
            network_max_retries=2,
            # 重定向校验需要知道是否配置了代理：代理模式下 DNS 由代理解析，
            # 本地 getaddrinfo 在 DNS 污染环境下会误杀合法的 CDN 重定向。
            download_redirect_validator=lambda url, _proxy=proxy: _require_public_redirect_url(
                url, proxy=_proxy
            ),
        )
        source = str(source or "civitai").strip().lower()
        if source == "civitai_red":
            kwargs.pop("cookie", None)
            # 不向独立的 civitai.red 域发送 civitai.com Bearer token。
            kwargs["api_token"] = ""
            return self.red_client_factory(**kwargs)
        if source == "huggingface":
            kwargs.pop("cookie", None)
            kwargs["api_token"] = self.config.get_secret("huggingface_api_token") or ""
            return self.hf_client_factory(**kwargs)
        return self.client_factory(**kwargs)

    def capabilities(self) -> dict[str, Any]:
        settings = self._settings()
        mode = str(settings.get("mode") or "auto").strip().lower()
        if mode not in {"auto", "local", "comfyui_bridge"}:
            mode = "auto"
        return {
            "enabled": True,
            "sources": ["civitai", "huggingface"],
            "mode": mode,
            "local_configured": bool(self._effective_local_dir(settings)),
            "bridge_configured": bool(str(settings.get("bridge_endpoint") or "/astrbot/lora").strip()),
            "civitai_token_configured": bool(self.config.get_secret("civitai_api_token")),
            "huggingface_token_configured": bool(self.config.get_secret("huggingface_api_token")),
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

    async def search(
        self,
        query: str,
        *,
        family: str = "illustrious",
        limit: int = 20,
        page: int = 1,
        sources: list[str] | None = None,
        adult_consent: bool = False,
    ) -> dict[str, Any]:
        query = _bounded_text(query, 120)
        if not query:
            raise AppError(422, "LORA_SEARCH_INVALID", "请输入 LoRA 名称、角色名或中文别称")
        family = str(family or "illustrious").strip().lower()
        if family not in _FAMILY_BASE_MODELS:
            raise AppError(422, "LORA_SEARCH_INVALID", f"不支持的模型族: {family}")
        limit = max(1, min(int(limit or 20), 20))
        page = max(1, min(int(page or 1), 20))
        requested_sources = {
            str(source or "").strip().lower() for source in (sources or [])
        }
        if requested_sources & _ADULT_SOURCES and not adult_consent:
            raise AppError(
                403,
                "LORA_R18_REQUIRES_CONSENT",
                "搜索成人内容源前必须先确认年龄与内容提示",
            )
        self._cleanup()
        service = self._search_service
        if _LORA_SEARCH_AVAILABLE and not self._search_service_failed:
            if service is None:
                service = self._build_search_service()
                self._search_service = service
            if service is not None:
                try:
                    try:
                        result = await service.search(query, family=family, limit=limit, page=page, sources=sources)
                    except TypeError as exc:
                        # 允许旧版/第三方搜索服务继续工作；只有明确是不认识
                        # page 参数时才走兼容调用，不能吞掉服务内部 TypeError。
                        if "unexpected keyword argument 'page'" not in str(exc):
                            raise
                        result = await service.search(query, family=family, limit=limit, sources=sources)
                    raw_items = result.get("items") or []
                    items = list(raw_items) if isinstance(raw_items, list) else []
                    nsfw_hidden = 0
                    if not adult_consent:
                        nsfw_hidden = sum(
                            1 for item in items
                            if isinstance(item, Mapping) and bool(item.get("nsfw"))
                        )
                        items = [
                            item for item in items
                            if not (isinstance(item, Mapping) and bool(item.get("nsfw")))
                        ]
                    for item in items:
                        if isinstance(item, Mapping):
                            self._cache_result(
                                str(item.get("id") or "").strip(),
                                family=str(item.get("family") or family),
                                source=str(item.get("source") or "civitai"),
                                model_id=item.get("model_id"),
                                version_id=item.get("version_id"),
                                file_name=item.get("file_name"),
                                query=query,
                                nsfw=bool(item.get("nsfw")),
                            )
                    return {
                        "query": query,
                        "family": family,
                        "items": items,
                        "page": int(result.get("page") or page),
                        "page_size": int(result.get("page_size") or limit),
                        "total": max(0, int(result.get("total") or len(items))),
                        "has_more": bool(result.get("has_more")),
                        "nsfw_hidden": nsfw_hidden,
                        "searched_terms": list(result.get("searched_terms") or [])[:8],
                        "alias": _bounded_text(result.get("alias"), 120),
                        "errors": [
                            _bounded_text(value, 240)
                            for value in (result.get("errors") or [])[:5]
                            if _bounded_text(value, 240)
                        ],
                    }
                except AppError:
                    raise
                except Exception as exc:
                    logger.warning("LoraSearchService 搜索失败，回退 Civitai 直查: %s", _safe_error(exc))
        result = await self._search_fallback(query, family, limit)
        result.setdefault("page", page)
        result.setdefault("page_size", limit)
        result.setdefault("total", len(result.get("items") or []))
        result.setdefault("has_more", False)
        if not adult_consent:
            items = list(result.get("items") or [])
            result["nsfw_hidden"] = sum(
                1 for item in items if isinstance(item, Mapping) and bool(item.get("nsfw"))
            )
            result["items"] = [
                item for item in items
                if not (isinstance(item, Mapping) and bool(item.get("nsfw")))
            ]
        return result

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
        file_name: Any = None,
        nsfw: bool = False,
    ) -> None:
        if not result_id:
            return
        source = str(source or "civitai").strip().lower()
        cached_file_name = (
            _safe_hf_file_ref(file_name)
            if source == "huggingface"
            else (safe_filename(str(file_name or "")) if str(file_name or "").strip() else "")
        )
        self._search_cache[result_id] = {
            "created_at": time.time(),
            "query": query,
            "family": family,
            "source": source,
            "model_id": str(model_id or "").strip(),
            "version_id": str(version_id or "").strip(),
            "file_name": cached_file_name,
            "nsfw": bool(nsfw),
        }

    async def _search_fallback(self, query: str, family: str, limit: int) -> dict[str, Any]:
        try:
            results = await self._client().search_loras(
                query=query,
                limit=limit,
                base_models=_FAMILY_BASE_MODELS[family],
            )
        except Exception as exc:
            # 不要用笼统的 502 直接打挂整场搜索：把真实原因放回 errors 由前端展示，
            # 保持 200 响应，页面仍能看到"来源无法连接 + 具体原因"，而非 Bad Gateway。
            logger.warning("[ComfyUI] Civitai 直查失败: %s", _safe_error(exc))
            return {
                "query": query,
                "family": family,
                "items": [],
                "nsfw_hidden": 0,
                "searched_terms": [query],
                "alias": "",
                "errors": [_bounded_text(f"直连 Civitai 失败：{_safe_error(exc)}", 240)],
            }

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
                nsfw=bool(raw.get("nsfw")),
            )
            public.append({
                "id": result_id,
                "source": "civitai",
                "model_id": model_id,
                "version_id": version_id,
                "name": _public_text(raw.get("name") or "Civitai LoRA", 160),
                "version_name": _public_text(raw.get("version_name"), 100),
                "base_model": _public_text(raw.get("base_model"), 80),
                "trained_words": [_public_text(item, 100) for item in (raw.get("trained_words") or [])[:12] if _public_text(item, 100)],
                "tags": [_public_text(item, 60) for item in (raw.get("tags") or [])[:12] if _public_text(item, 60)],
                "download_count": int(raw.get("download_count") or 0),
                "heart_count": int(raw.get("heart_count") or 0),
                "nsfw": bool(raw.get("nsfw")),
                "page_url": page_url if urlparse(page_url).hostname in _ALLOWED_DOWNLOAD_HOSTS else "",
            })
            if len(public) >= limit:
                break
        return {
            "query": query,
            "family": family,
            "items": public,
            "page": 1,
            "page_size": limit,
            "total": len(public),
            "has_more": False,
        }

    # -- 任务调度 -----------------------------------------------------------

    def start_download(self, result_id: str, *, alias: str = "", adult_consent: bool = False) -> dict[str, Any]:
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
            if bool(cached.get("nsfw")) and not adult_consent:
                raise AppError(400, "LORA_R18_REQUIRES_CONSENT", "该 LoRA 含敏感内容，请先确认年龄")
            cached_source = str(cached.get("source") or "civitai").strip().lower()
            cached_file_name = (
                _safe_hf_file_ref(cached.get("file_name"))
                if cached_source == "huggingface"
                else safe_filename(str(cached.get("file_name") or ""))
            )
            self._store.enqueue(
                result_id,
                family=str(cached.get("family") or "illustrious"),
                alias=_bounded_text(alias, 80),
                source=cached_source,
                source_ref={
                    "source": cached_source,
                    "family": str(cached.get("family") or "illustrious"),
                    "model_id": str(cached.get("model_id") or ""),
                    "version_id": str(cached.get("version_id") or ""),
                    "file_name": cached_file_name,
                    # 搜索关键词（尤其中文）随任务下发：装完后同词可再搜到
                    "query": _bounded_text(str(cached.get("query") or ""), 120),
                    "adult_consent": bool(adult_consent),
                    "nsfw": bool(cached.get("nsfw")),
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

    def _effective_local_dir(self, settings: dict[str, Any]) -> str:
        """auto 模式下可实际使用的本机 models/loras 目录。

        显式 local_dir 优先；未配置时若已知 ComfyUI 根目录（环境中心选择或
        从零装机时自动记住）且 ComfyUI 跑在本机，则推导其 models/loras。
        此前 auto 无从推导本地目录就一律落 Bridge，是"选了自动却总在
        bridge"的根因。
        """
        explicit = str(settings.get("local_dir") or "").strip()
        if explicit:
            return explicit
        try:
            snapshot = self.config.snapshot()
        except Exception:  # noqa: BLE001 - 设置不可读时不推导
            return ""
        root_text = str(snapshot.get("comfyui_root") or "").strip()
        if not root_text:
            return ""
        address = str((snapshot.get("comfyui") or {}).get("server_address") or "").strip()
        host = address.split(":")[0].strip().lower() if address else ""
        if host not in {"127.0.0.1", "localhost", "0.0.0.0", ""}:
            return ""
        candidate = Path(os.path.expandvars(os.path.expanduser(root_text))) / "models" / "loras"
        try:
            if candidate.is_dir():
                return str(candidate)
        except OSError:
            pass
        return ""

    def _mode(self) -> tuple[str, dict[str, Any]]:
        settings = self._settings()
        mode = str(settings.get("mode") or "auto").strip().lower()
        local_dir = self._effective_local_dir(settings)
        if mode == "local" or (mode == "auto" and local_dir):
            if not local_dir:
                raise RuntimeError("本地下载模式尚未配置 ComfyUI models/loras 目录")
            merged = dict(settings)
            merged["local_dir"] = local_dir
            return "local", merged
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
        # 任何已存在路径都不得覆盖，包括小于 1 KiB 的损坏/未完成文件与目录。
        # `_is_installed_file` 只适合状态展示，不能作为防覆盖判断。
        if not base.exists():
            return base, False
        stem = base.stem
        suffix = base.suffix or ".safetensors"
        for index in range(1, 1000):
            candidate = base.with_name(f"{stem} ({index}){suffix}")
            if not candidate.exists():
                return candidate, True
        raise RuntimeError("无法为同名 LoRA 生成唯一文件名")

    async def _install_local(self, client: Any, summary: dict[str, Any], settings: dict[str, Any], result_id: str, *, source: str = "civitai") -> dict[str, Any]:
        filename = safe_filename(summary.get("file_name"))
        if Path(filename).suffix.lower() not in _ALLOWED_SUFFIXES:
            raise RuntimeError("只允许安装 safetensors 格式的 LoRA")
        download_url = _require_trusted_download_url(str(summary.get("download_url") or ""), source)
        target, conflict_renamed = self._unique_local_target(str(settings.get("local_dir") or ""), filename)
        free = shutil.disk_usage(target.parent).free
        if free < 512 * 1024 * 1024:
            raise RuntimeError("目标磁盘可用空间不足 512 MB")
        # 先下载到同目录临时文件，校验通过后由调用方原子改名落盘，失败不污染最终目录。
        tmp = target.with_name(f".{target.name}.part")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass

        async def progress(done: int, total: int) -> None:
            self._set_progress(result_id, done, total)

        try:
            await client.download_file(download_url, tmp, progress_cb=progress)
        except BaseException:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        if not tmp.is_file() or tmp.stat().st_size <= 1024:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeError("下载完成但 LoRA 文件为空或异常")
        try:
            await asyncio.to_thread(verify_safetensors, tmp)
        except VerifyFailure as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeError(f"下载文件不是有效的 safetensors：{exc}") from exc
        return {
            "filename": filename,
            "mode": "local",
            "already_exists": False,
            "conflict_renamed": bool(conflict_renamed),
            "bytes": tmp.stat().st_size,
            "tmp_path": tmp,
            "target_path": target,
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

    # -- 连接诊断 -----------------------------------------------------------

    async def test_connection(self) -> dict[str, Any]:
        """一键诊断搜索/下载链路：代理 → Civitai 连通性 → Token/Cookie。

        每一步都返回明确的结论与下一步操作提示，让用户在任何机器上都知道
        该改哪个设置，而不是只看到一串异常文本。
        """
        settings = self._settings()
        explicit = str(settings.get("proxy") or "").strip()
        try:
            await net_env.ensure_probed(explicit)
        except Exception:
            pass
        proxy, proxy_source = net_env.effective_proxy(explicit)
        proxy_report: dict[str, Any] = {
            "value": proxy,
            "source": proxy_source,
            "explicit": bool(explicit),
            "explicit_value": net_env.normalize_proxy_url(explicit),
            "scheme": urlparse(proxy).scheme if proxy else "",
            "reachable": None,
            "error": "",
        }
        guidance: list[str] = []
        explicit_normalized = net_env.normalize_proxy_url(explicit)
        if explicit_normalized and urlparse(explicit_normalized).scheme not in {"http", "https"}:
            guidance.append(
                f"当前代理协议 {urlparse(explicit_normalized).scheme}:// 不受支持，仅支持 HTTP(S) 代理；"
                "Clash/v2rayN 请填 Mixed/HTTP 端口，例如 http://127.0.0.1:7890。"
            )
        if explicit and proxy_source != "explicit":
            proxy_report["error"] = (
                f"配置的代理 {net_env.normalize_proxy_url(explicit)} 当前不可达"
                + (f"，已自动改用 {proxy}。" if proxy else "，已自动改为直连。")
            )
            guidance.append(
                "设置中的代理不可达：请检查该代理是否开机/在线、局域网代理需开启 Allow LAN；"
                "或清空代理栏改用本机系统代理。当前已自动回退，搜索/下载仍可继续。"
            )
        if proxy and urlparse(proxy).scheme in {"http", "https"}:
            try:
                parsed = urlparse(proxy)
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(parsed.hostname, port), timeout=4.0
                )
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                proxy_report["reachable"] = True
            except Exception as exc:
                proxy_report["reachable"] = False
                proxy_report["error"] = _safe_error(exc)
                guidance.append(
                    "代理端口连不上：确认代理软件正在运行、端口正确；使用局域网代理需开启 Allow LAN；"
                    "也可以清空代理栏，让程序自动使用系统代理。"
                )

        api_report: dict[str, Any] = {"ok": False, "error": ""}
        try:
            probe_client = self.client_factory(
                api_token="",
                cookie="",
                proxy=proxy,
                timeout=min(20, max(10, int(settings.get("request_timeout") or 20))),
                downloader="multithread",
            )
            await probe_client._get_json(f"{probe_client.api_base}/models?limit=1")
            api_report["ok"] = True
        except Exception as exc:
            api_report["error"] = _safe_error(exc)
            if not proxy:
                guidance.append(
                    "Civitai 直连失败：在 设置 → LoRA 下载 填写代理（如 http://127.0.0.1:7890），"
                    "或开启 Windows 系统代理后清空该栏由程序自动检测。"
                )
            elif proxy_report.get("reachable"):
                guidance.append(
                    "代理本身可达但访问 Civitai 失败：请更换支持访问 civitai.com 的代理节点。"
                )

        token_report: dict[str, Any] = {"configured": False, "ok": None, "error": ""}
        token = str(self.config.get_secret("civitai_api_token") or "").strip()
        if token:
            token_report["configured"] = True
            try:
                token_client = self.client_factory(
                    api_token=token,
                    cookie="",
                    proxy=proxy,
                    timeout=min(20, max(10, int(settings.get("request_timeout") or 20))),
                    downloader="multithread",
                )
                await token_client._get_json(f"{token_client.api_base}/models?limit=1")
                token_report["ok"] = True
            except Exception as exc:
                message = _safe_error(exc)
                token_report["ok"] = False
                token_report["error"] = message
                if "401" in message:
                    guidance.append(
                        "Civitai API Token 无效：到 civitai.com → 账户设置 → API Keys 重新生成，"
                        "然后在 设置 → 认证与密钥 更新。"
                    )

        cookie_report: dict[str, Any] = {"configured": False, "fields": 0, "looks_valid": None, "error": ""}
        cookie = str(self.config.get_secret("civitai_cookie") or "").strip()
        if cookie:
            cookie_report["configured"] = True
            fields = [part for part in cookie.split(";") if "=" in part]
            cookie_report["fields"] = len(fields)
            has_session = any(part.strip().lower().startswith("civitai_token=") for part in fields)
            cookie_report["looks_valid"] = bool(has_session)
            if not fields:
                cookie_report["error"] = "Cookie 内容不像键值对（缺少 name=value 结构）。"
                guidance.append(
                    "Civitai Cookie 格式可疑：请重新从已登录浏览器复制完整 Cookie 请求头。"
                )
            elif not has_session:
                guidance.append(
                    "Cookie 里没有 civitai_token 字段，可能不是登录态 Cookie；"
                    "受限资源若仍 401，请重新复制已登录浏览器的 Cookie。"
                )

        ok = bool(api_report.get("ok")) and proxy_report.get("reachable") is not False
        return {
            "ok": ok,
            "proxy": proxy_report,
            "api": api_report,
            "token": token_report,
            "cookie": cookie_report,
            "guidance": guidance[:6],
        }

    async def _install_bridge(self, client: Any, summary: dict[str, Any], settings: dict[str, Any], result_id: str, *, source: str = "civitai") -> dict[str, Any]:
        download_url = _require_trusted_download_url(str(summary.get("download_url") or ""), source)
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
            # The Bridge performs the actual external download on the ComfyUI
            # host.  Keep a dedicated override for remote deployments, but do
            # not silently fall back to a direct connection when the user has
            # already configured the LoRA proxy in the desktop settings.
            "proxy": str(settings.get("bridge_proxy") or settings.get("proxy") or "").strip(),
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
        source = str(source_ref.get("source") or "civitai").strip().lower()
        model_id = str(source_ref.get("model_id") or "").strip()
        version_id = str(source_ref.get("version_id") or "").strip()
        if source == "huggingface":
            repo_id = _safe_hf_repo_id(model_id)
            file_name = _safe_hf_file_ref(source_ref.get("file_name"))
            if not repo_id:
                return ""
            query = f"?{urlencode({'file': file_name})}" if file_name else ""
            return f"https://huggingface.co/{repo_id}{query}"
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
            numeric_version = _optional_int(version_id)
            if numeric_version is None:
                return ""
            version = await client.get_model_version(numeric_version)
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
        # 用户没手填中文别称时，把搜索用的中文关键词自动存为别名——
        # 否则装完后同一个中文词在已安装列表搜不到，发现页种子词也推导不出
        alias_text = str(alias or "").strip()
        if not alias_text:
            query_text = str(summary.get("search_query") or "")
            cjk = re.search(r"[㐀-鿿぀-ヿ][㐀-鿿぀-ヿ·\s\-']{0,30}", query_text)
            if cjk:
                alias_text = cjk.group(0).strip(" -'·")
        # 仅用临时文件承载档案计算（load 为 {}），正常写入路径只有 SQLite。
        scratch_dir = self.data_root / ".tmp"
        scratch = scratch_dir / f"lora_profile_{uuid.uuid4().hex}.json"
        try:
            scratch_dir.mkdir(parents=True, exist_ok=True)
            store = LoraProfileStore(scratch)
            profile = await asyncio.to_thread(store.upsert_from_civitai, summary, alias_text, 0.8, 0.8)
        finally:
            try:
                scratch.unlink(missing_ok=True)
            except OSError:
                pass
        profile = self._save_sqlite_profile(family, profile)
        return {
            "character": _bounded_text(profile.get("character"), 100),
            "aliases": [_bounded_text(item, 100) for item in (profile.get("aliases") or [])[:12]],
            "trigger_words": [_bounded_text(item, 100) for item in (profile.get("activation_tags") or [])[:12]],
        }

    def _save_sqlite_profile(self, family: str, profile: dict[str, Any]) -> dict[str, Any]:
        """只新增下载档案，不覆盖 SQLite 中已有的用户维护档案。

        同一 LoRA 文件已有档案时直接复用；角色名键冲突但文件不同则改用
        文件名键，避免一次重新下载抹掉用户编辑过的别名/触发词。
        """
        sql_store = SqlLoraProfileStore(self._db)
        incoming = dict(profile)
        incoming_lora = normalize_lora_key(
            incoming.get("lora") or incoming.get("lora_name") or ""
        )
        for record in sql_store.get_family(family):
            existing_profile = dict(record.profile or {})
            existing_lora = normalize_lora_key(
                existing_profile.get("lora") or existing_profile.get("lora_name") or ""
            )
            if incoming_lora and incoming_lora == existing_lora:
                return existing_profile
        key = str(profile.get("character") or "").strip() or Path(str(profile.get("lora") or "")).stem
        if sql_store.get(family, key) is not None:
            filename_key = Path(str(profile.get("lora") or "lora")).stem or "lora"
            key = filename_key
            if sql_store.get(family, key) is not None:
                digest = hashlib.sha256(
                    str(profile.get("lora") or key).encode("utf-8")
                ).hexdigest()[:8]
                key = f"{filename_key}-{digest}"
        return dict(sql_store.save(family, key, incoming).profile or incoming)

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
            preview_key = ""
            preview_url = str(summary.get("preview_url") or "").strip()
            if preview_url and self._preview_service is not None:
                accepted = self._preview_service.register(
                    "",
                    preview_url,
                    source=str(summary.get("source") or "civitai"),
                )
                if accepted:
                    preview_key = str(accepted)
                    # 尽快把预览落到稳定 token 对应的磁盘缓存，使应用重启后仍
                    # 能展示图片；失败不影响已经完成的 LoRA 安装。
                    try:
                        task = asyncio.create_task(self._preview_service.fetch(preview_key))
                        task.add_done_callback(
                            lambda done: done.exception() if not done.cancelled() else None
                        )
                    except RuntimeError:
                        pass
            source = str(summary.get("source") or "civitai").strip().lower()
            record = LoraAssetStore(self._db).register(
                model_family=family,
                filename=str(installed.get("filename") or ""),
                comfy_key=str(installed.get("filename") or ""),
                display_name=_bounded_text(summary.get("model_name"), 160),
                source=str(installed.get("mode") or "local"),
                model_id=_optional_int(model_id),
                version_id=_optional_int(version_id),
                preview_key=preview_key or None,
                info={
                    "sha256": sha256 or "",
                    "size_bytes": int(installed.get("bytes") or 0),
                    "mode": str(installed.get("mode") or ""),
                    "origin": source,
                    "remote_model_id": _public_text(model_id, 160),
                    "remote_version_id": _public_text(version_id, 160),
                    "nsfw": bool(summary.get("nsfw")),
                    "preview_key": preview_key,
                },
            )
            return record.id
        except Exception as exc:
            logger.warning("LoRA 资产登记失败 filename=%s: %s", installed.get("filename"), _safe_error(exc))
            raise RuntimeError("LoRA 已下载，但本地库登记失败") from exc

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
                source = str(source_ref.get("source") or "civitai").strip().lower()
                # 下载前探测显式代理；失效时自动回退系统代理而不是撞死端口。
                try:
                    await net_env.ensure_probed(
                        str(self._settings().get("proxy") or "").strip()
                    )
                except Exception:
                    pass
                client = self._client(source)
                summary = await client.resolve_model(
                    self._build_reference(source_ref),
                    base_models=_FAMILY_BASE_MODELS.get(family, ["Illustrious"]),
                )
                if bool(summary.get("nsfw")) and not bool(source_ref.get("adult_consent")):
                    raise AppError(
                        403,
                        "LORA_R18_REQUIRES_CONSENT",
                        "下载源解析出的内容为成人/敏感内容，请重新确认后下载",
                    )
                summary["search_query"] = _bounded_text(alias or str(source_ref.get("query") or ""), 120)
                mode, settings = self._mode()
                if mode == "comfyui_bridge":
                    probe = await self.bridge_status()
                    if not probe.get("ok"):
                        configured = str(settings.get("mode") or "auto").strip().lower()
                        if configured == "auto" and self._effective_local_dir(settings):
                            mode = "local"
                        else:
                            raise RuntimeError(f"ComfyUI LoRA Bridge 不可用：{probe.get('message') or '未就绪'}")

                self._store.update_state(result_id, LoraDownloadState.DOWNLOADING)
                pending_tmp = None
                if mode == "local":
                    installed = await self._install_local(client, summary, settings, result_id, source=source)
                    pending_tmp = installed.get("tmp_path")
                else:
                    installed = await self._install_bridge(client, summary, settings, result_id, source=source)

                expected_hash = await self._expected_hash(client, summary)
                local_hash = ""
                try:
                    if mode == "local":
                        tmp_path = installed.get("tmp_path")
                        target_path = installed.get("target_path")
                        if tmp_path is not None and target_path is not None:
                            local_hash = await self._sha256_async(tmp_path)
                            if local_hash and expected_hash and local_hash.lower() != expected_hash.lower():
                                raise RuntimeError(
                                    f"LoRA 哈希校验失败：Civitai 期望 {expected_hash[:12]}…，实际 {local_hash[:12]}…"
                                )
                            # 哈希/大小校验通过后原子落盘
                            os.replace(str(tmp_path), str(target_path))
                            pending_tmp = None
                except BaseException:
                    try:
                        if pending_tmp is not None:
                            pending_tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise

                self._store.update_state(result_id, LoraDownloadState.PROFILING)
                profile = await self._write_profile(summary, installed["filename"], family, alias)
                asset_id = self._register_asset(summary, installed, family, local_hash)
                result = {
                    key: value
                    for key, value in installed.items()
                    if key not in {"tmp_path", "target_path"}
                }
                result.update({
                    "model_name": _bounded_text(summary.get("model_name"), 160),
                    "base_model": _bounded_text(summary.get("base_model"), 80),
                    "profile": profile,
                    "hash": local_hash or None,
                    "expected_hash": expected_hash or None,
                    "conflict_renamed": bool(installed.get("conflict_renamed", False)),
                })
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

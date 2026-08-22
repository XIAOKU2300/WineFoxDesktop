"""Civitai 预览图安全拉取与校验服务。

预览图只允许通过同源 API + 一次性 token 获取：调用方把 (url, source)
登记到内存注册表，前端只能拿 token 换图。下载后做内容类型、文件魔数与
像素尺寸三重校验，任何一步失败都返回 None，绝不把外部 URL 直接交给浏览器。
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import re
import socket
import struct
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse

import aiohttp

logger = logging.getLogger("winefox.runtime.lora_preview")

_DEFAULT_MAX_BYTES = 5 * 1024 * 1024
_MAX_REGISTRY = 512
_MAX_MEMORY_CACHE = 128
_CACHE_DIR_NAME = "civitai_preview_cache"
_USER_AGENT = "WineFox/0.1"
_UA = {"User-Agent": _USER_AGENT}
_REFERERS = {"civitai_red": "https://civitai.red/", "civitai.red": "https://civitai.red/"}
_DEFAULT_REFERER = "https://civitai.com/"
_ALLOWED_PREVIEW_HOSTS = ("civitai.com", "civitai.red", "civitaired.com", "huggingface.co", "hf.co")
_ALLOWED_PREVIEW_PORTS = {80, 443}
#: Clash/mihomo TUN fake-ip：本机 DNS 会把白名单域名解析到保留段（IPv4 常见
#: 198.18.0.0/15，IPv6 还有 ULA 段），实际由 TUN 转交真实主机。已知段直接
#: 放行，其余用对照探针兜底识别其他 fake-ip 实现。
_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
_FAKE_IP_PROBE_HOST = "civitai.com"
_FAKE_IP_PROBE_TTL = 60.0
_fake_ip_probe_cache = {"at": 0.0, "value": False}


def _local_dns_is_fake_ip() -> bool:
    """对照域名解析到非公网地址即判定本机 DNS 处于 fake-ip（TUN）模式。"""
    now = time.monotonic()
    if now - float(_fake_ip_probe_cache.get("at") or 0) < _FAKE_IP_PROBE_TTL:
        return bool(_fake_ip_probe_cache.get("value"))
    value = False
    try:
        infos = socket.getaddrinfo(_FAKE_IP_PROBE_HOST, 443, proto=socket.IPPROTO_TCP)
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if not ip.is_global:
                value = True
                break
    except (socket.gaierror, OSError):
        value = False
    _fake_ip_probe_cache.update(at=now, value=value)
    return value


_PREVIEW_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_PREVIEW_RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}
_PREVIEW_MAX_REDIRECTS = 3
_PREVIEW_MAX_ATTEMPTS = 3
_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _is_safe_preview_url(url: str) -> bool:
    """预览图 URL 白名单校验：仅 http/https、域名白名单、端口仅 80/443。

    拒绝 IP 字面量、私网/回环/保留地址与任意端口，防止 SSRF 打到内网。
    """
    value = str(url or "").strip()
    if len(value) > 2048:
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    # Never forward credentials embedded in a preview URL or a redirect.
    if parsed.username is not None or parsed.password is not None:
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        return False
    if not any(host == allowed or host.endswith(f".{allowed}") for allowed in _ALLOWED_PREVIEW_HOSTS):
        return False
    if parsed.port is not None and parsed.port not in _ALLOWED_PREVIEW_PORTS:
        return False
    return True


def _resolve_public(host: str) -> bool:
    """解析 hostname 并确认所有解析 IP 均为公网地址；解析失败视为不安全。

    Clash/mihomo TUN fake-ip 会把域名解析到保留段（如 198.18.0.0/15 或
    ULA 段），由 TUN 转交给真实主机；该类环境视为可放行，其余非公网地址
    仍然拒绝。
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError):
        return False
    if not infos:
        return False
    fake_ip_dns = _local_dns_is_fake_ip()
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not ip.is_global and ip not in _FAKE_IP_NETWORK and not fake_ip_dns:
            return False
    return True


def _sha256_url(url: str) -> str:
    return hashlib.sha256(str(url or "").encode("utf-8")).hexdigest()[:24]


def _parse_image_dimensions(data: bytes) -> tuple[int, int] | None:
    """轻量解析图片头部得到 (width, height)，失败返回 None。"""
    if not data:
        return None
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            if len(data) < 24:
                return None
            width, height = struct.unpack(">II", data[16:24])
            return (width, height)
        if data[:3] == b"\xff\xd8\xff":
            offset = 2
            n = len(data)
            while offset + 9 < n:
                if data[offset] != 0xFF:
                    offset += 1
                    continue
                marker = data[offset + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    offset += 2
                    continue
                if marker in (0xDA, 0xD9):
                    break
                seg_len = struct.unpack(">H", data[offset + 2:offset + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    height, width = struct.unpack(">HH", data[offset + 5:offset + 9])
                    return (width, height)
                offset += 2 + seg_len
            return None
        if data[:6] in (b"GIF87a", b"GIF89a"):
            width, height = struct.unpack("<HH", data[6:10])
            return (width, height)
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            fourcc = data[12:16]
            if fourcc == b"VP8X" and len(data) >= 30:
                raw = data[24:30]
                width = 1 + int.from_bytes(raw[0:3], "little")
                height = 1 + int.from_bytes(raw[3:6], "little")
                return (width, height)
            if fourcc == b"VP8L" and len(data) >= 25:
                bits = int.from_bytes(data[21:25], "little")
                width = (bits & 0x3FFF) + 1
                height = ((bits >> 14) & 0x3FFF) + 1
                return (width, height)
            if fourcc == b"VP8 " and len(data) >= 30:
                width = int.from_bytes(data[26:28], "little") & 0x3FFF
                height = int.from_bytes(data[28:30], "little") & 0x3FFF
                return (width, height)
            return None
    except (struct.error, IndexError, ValueError):
        return None
    return None


def _magic_matches(data: bytes, mime: str) -> bool:
    mime = str(mime or "").lower()
    if mime == "image/png":
        return data[:8] == b"\x89PNG\r\n\x1a\n"
    if mime in {"image/jpeg", "image/jpg"}:
        return data[:3] == b"\xff\xd8\xff"
    if mime == "image/webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if mime == "image/gif":
        return data[:6] in (b"GIF87a", b"GIF89a")
    return False


class LoraPreviewService:
    """按 token 提供 Civitai 预览图，下载后做安全性校验。"""

    def __init__(
        self,
        *,
        data_root: Path,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        proxy: str | Callable[[], str] = "",
        proxy_probe: Callable[[], object] | None = None,
    ) -> None:
        self.data_root = Path(data_root)
        self._proxy_probe = proxy_probe
        self.max_bytes = max(64 * 1024, int(max_bytes or _DEFAULT_MAX_BYTES))
        self._registry: dict[str, tuple[str, str]] = {}
        self._memory_cache: dict[str, tuple[str, bytes]] = {}
        self._lock = asyncio.Lock()
        # 预览图属于独立 token 的网络资源：同一个 token 只允许一个下载任务，
        # 不同 token 通过有界并发并行，避免全局锁把整页缩略图排成队列。
        self._fetch_semaphore = asyncio.Semaphore(6)
        self._inflight: dict[str, asyncio.Task[tuple[str, bytes] | None]] = {}
        self._proxy = proxy

    def _current_proxy(self) -> str:
        value = self._proxy() if callable(self._proxy) else self._proxy
        value = str(value or "").strip()
        if value and "://" not in value:
            value = f"http://{value}"
        return value

    def register(self, token: str, url: str, source: str = "") -> str | None:
        """登记 token -> (url, source)。仅内存，进程退出即失效。

        返回登记成功时的 token；URL 不在白名单内返回 None（不登记）。
        """
        url = str(url or "").strip()
        token = str(token or "").strip() or _sha256_url(url)
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", token)
            or not _is_safe_preview_url(url)
        ):
            return None
        self._registry[token] = (url, str(source or "").strip())
        if len(self._registry) > _MAX_REGISTRY:
            for stale in list(self._registry.keys())[: max(1, len(self._registry) - _MAX_REGISTRY)]:
                self._registry.pop(stale, None)
                self._memory_cache.pop(stale, None)
        return token

    def _cache_path(self, url: str, mime: str) -> Path | None:
        try:
            directory = self.data_root / _CACHE_DIR_NAME
            ext = _EXT_BY_MIME.get(str(mime or "").lower(), ".img")
            directory.mkdir(parents=True, exist_ok=True)
            return directory / f"{_sha256_url(url)}{ext}"
        except OSError:
            return None

    def _read_disk_cache(self, url: str, mime: str) -> bytes | None:
        path = self._cache_path(url, mime)
        if path is None or not path.is_file():
            return None
        try:
            if path.stat().st_size > self.max_bytes:
                return None
            return path.read_bytes()
        except OSError:
            return None

    async def fetch(self, token: str) -> tuple[str, bytes] | None:
        """查 token -> 下载 -> 校验，返回 (content_type, bytes)；任何失败返回 None。"""
        token = str(token or "").strip()
        if not token:
            return None
        entry = self._registry.get(token)
        if not entry:
            # 下载资产使用 URL 哈希作为稳定 token；重启后内存映射消失，但已
            # 校验落盘的图片仍可按 token 读取，不需要把远端 URL 持久化到 DB。
            cached = self._read_token_cache(token)
            if cached is not None:
                self._memory_cache[token] = cached
                return cached
            logger.info("预览 token 未登记: %s", token[:12])
            return None
        async with self._lock:
            cached = self._memory_cache.get(token)
            if cached is not None:
                return cached
            task = self._inflight.get(token)
            if task is None:
                task = asyncio.create_task(self._fetch_registered(token, entry))
                self._inflight[token] = task
        # 调用方取消时不取消共享下载任务，后续卡片仍可复用其结果。
        return await asyncio.shield(task)

    async def _fetch_registered(
        self,
        token: str,
        entry: tuple[str, str],
    ) -> tuple[str, bytes] | None:
        try:
            async with self._fetch_semaphore:
                url, source = entry
                disk_result = self._read_valid_disk_cache(url)
                result = disk_result or await self._download_and_validate(url, source)
            if result is not None:
                async with self._lock:
                    self._memory_cache[token] = result
                    if len(self._memory_cache) > _MAX_MEMORY_CACHE:
                        oldest = next(iter(self._memory_cache), None)
                        if oldest is not None and oldest != token:
                            self._memory_cache.pop(oldest, None)
            return result
        finally:
            async with self._lock:
                current = self._inflight.get(token)
                if current is asyncio.current_task():
                    self._inflight.pop(token, None)

    def _read_valid_disk_cache(self, url: str) -> tuple[str, bytes] | None:
        for mime in _EXT_BY_MIME:
            data = self._read_disk_cache(url, mime)
            if not data or not _magic_matches(data, mime):
                continue
            dimensions = _parse_image_dimensions(data)
            if dimensions is None:
                continue
            width, height = dimensions
            if (
                0 < width < 8000
                and 0 < height < 8000
                and max(width, height) / min(width, height) <= 10
            ):
                return (mime, data)
        return None

    def _read_token_cache(self, token: str) -> tuple[str, bytes] | None:
        if not re.fullmatch(r"[0-9a-f]{24}", str(token or "")):
            return None
        directory = self.data_root / _CACHE_DIR_NAME
        for mime, ext in _EXT_BY_MIME.items():
            path = directory / f"{token}{ext}"
            try:
                if not path.is_file() or path.stat().st_size > self.max_bytes:
                    continue
                data = path.read_bytes()
            except OSError:
                continue
            if not data or not _magic_matches(data, mime):
                continue
            dimensions = _parse_image_dimensions(data)
            if dimensions is None:
                continue
            width, height = dimensions
            if (
                0 < width < 8000
                and 0 < height < 8000
                and max(width, height) / min(width, height) <= 10
            ):
                return (mime, data)
        return None

    async def _download_and_validate(self, url: str, source: str) -> tuple[str, bytes] | None:
        if not _is_safe_preview_url(url):
            logger.info("预览图 URL 不在白名单内，拒绝下载")
            return None
        try:
            host = (urlparse(url).hostname or "").strip().lower()
        except ValueError:
            return None
        # 先探测显式代理可达性：死代理自动回退系统代理，避免预览图
        # 反复撞一个拒绝连接的端口（用户侧表现为缩略图批量失败）。
        if self._proxy_probe is not None:
            try:
                await self._proxy_probe()
            except Exception:
                pass
        proxy = self._current_proxy()
        # When a proxy is configured, the proxy performs DNS resolution.  A
        # local socket.getaddrinfo() check is both redundant and wrong behind
        # Clash/TUN setups (it can return a blocked/empty answer even though
        # the proxy can fetch the same public hostname).  The hostname and
        # port allowlist above remains in force in proxy mode.
        if not host or (not proxy and not _resolve_public(host)):
            logger.info("预览图主机解析为非公网地址，拒绝下载: %s", host)
            return None
        referer = _REFERERS.get(str(source or "").strip().lower(), _DEFAULT_REFERER)
        headers = dict(_UA)
        headers["Referer"] = referer
        timeout = aiohttp.ClientTimeout(total=20, connect=8, sock_read=12)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for attempt in range(1, _PREVIEW_MAX_ATTEMPTS + 1):
                    current_url = url
                    retry = False
                    try:
                        for redirect_index in range(_PREVIEW_MAX_REDIRECTS + 1):
                            # Validate every hop.  Civitai's image host normally
                            # returns 301 to image-b2.civitai.com; refusing that
                            # hop was the reason the UI showed an empty card.
                            if not _is_safe_preview_url(current_url):
                                logger.info("预览图重定向 URL 不在白名单内，拒绝下载")
                                return None
                            request_kwargs = {"headers": headers, "allow_redirects": False}
                            if proxy:
                                request_kwargs["proxy"] = proxy
                            async with session.get(current_url, **request_kwargs) as resp:
                                if resp.status in _PREVIEW_REDIRECT_STATUSES:
                                    location = str(resp.headers.get("Location") or "").strip()
                                    if not location:
                                        logger.info("预览图重定向缺少 Location")
                                        return None
                                    next_url = urljoin(current_url, location)
                                    parsed_next = urlparse(next_url)
                                    # Do not allow a redirect to downgrade HTTPS
                                    # or to carry credentials, even if the host
                                    # happens to be on the source allowlist.
                                    if parsed_next.scheme != "https" or not _is_safe_preview_url(next_url):
                                        logger.info("预览图重定向到了不安全地址")
                                        return None
                                    current_url = next_url
                                    continue
                                if resp.status != 200:
                                    logger.info("预览图下载非 200: %s", resp.status)
                                    retry = resp.status in _PREVIEW_RETRY_STATUSES
                                    if not retry:
                                        return None
                                    break
                                content_type = str(resp.headers.get("Content-Type") or "").lower()
                                # Ignore an optional charset/parameter when
                                # checking the image magic bytes.
                                content_type = content_type.split(";", 1)[0].strip()
                                if content_type and "image" not in content_type:
                                    logger.info("预览图 Content-Type 非图片: %s", content_type)
                                    return None
                                chunks = []
                                total = 0
                                async for chunk in resp.content.iter_chunked(64 * 1024):
                                    total += len(chunk)
                                    if total > self.max_bytes:
                                        logger.info("预览图超过大小上限")
                                        return None
                                    chunks.append(chunk)
                                if not chunks:
                                    logger.info("预览图响应为空")
                                    return None
                                data = b"".join(chunks)
                                break
                        else:
                            logger.info("预览图重定向次数超过上限")
                            return None
                        if retry:
                            if attempt < _PREVIEW_MAX_ATTEMPTS:
                                await asyncio.sleep(min(0.6 * attempt, 1.5))
                                continue
                            return None
                        # A successful 200 response leaves data/content_type
                        # defined; validation below is shared by all attempts.
                        break
                    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                        if attempt >= _PREVIEW_MAX_ATTEMPTS:
                            logger.info("预览图下载失败（%s）: %s", type(exc).__name__, exc)
                            return None
                        logger.info("预览图下载抖动，重试 %s/%s（%s）", attempt + 1, _PREVIEW_MAX_ATTEMPTS, type(exc).__name__)
                        await asyncio.sleep(min(0.6 * attempt, 1.5))
                        continue
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            logger.info("预览图下载失败（%s）: %s", type(exc).__name__, exc)
            return None

        if not content_type:
            content_type = _infer_mime(data)
        if not content_type:
            return None
        if not _magic_matches(data, content_type):
            logger.info("预览图魔数校验失败")
            return None
        dimensions = _parse_image_dimensions(data)
        if dimensions is None:
            logger.info("预览图像素解析失败")
            return None
        width, height = dimensions
        if not (0 < width < 8000 and 0 < height < 8000):
            logger.info("预览图尺寸越界: %sx%s", width, height)
            return None
        if width <= 0 or height <= 0:
            return None
        if max(width, height) / min(width, height) > 10:
            logger.info("预览图长宽比异常: %sx%s", width, height)
            return None
        self._write_disk_cache(url, content_type, data)
        return (content_type, data)

    def _write_disk_cache(self, url: str, mime: str, data: bytes) -> None:
        path = self._cache_path(url, mime)
        if path is None:
            return
        try:
            path.write_bytes(data)
        except OSError:
            pass


def _infer_mime(data: bytes) -> str:
    if not data:
        return ""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return ""


__all__ = ["LoraPreviewService"]

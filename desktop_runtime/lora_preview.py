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
from pathlib import Path
from urllib.parse import urlparse

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
    """解析 hostname 并确认所有解析 IP 均为公网地址；解析失败视为不安全。"""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError):
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not ip.is_global:
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

    def __init__(self, *, data_root: Path, max_bytes: int = _DEFAULT_MAX_BYTES) -> None:
        self.data_root = Path(data_root)
        self.max_bytes = max(64 * 1024, int(max_bytes or _DEFAULT_MAX_BYTES))
        self._registry: dict[str, tuple[str, str]] = {}
        self._memory_cache: dict[str, tuple[str, bytes]] = {}
        self._lock = asyncio.Lock()

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
            if token in self._memory_cache:
                return self._memory_cache[token]
            url, source = entry
            disk_result = self._read_valid_disk_cache(url)
            result = disk_result or await self._download_and_validate(url, source)
            if result is not None:
                self._memory_cache[token] = result
                if len(self._memory_cache) > _MAX_MEMORY_CACHE:
                    oldest = next(iter(self._memory_cache), None)
                    if oldest is not None:
                        self._memory_cache.pop(oldest, None)
            return result

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
        if not host or not _resolve_public(host):
            logger.info("预览图主机解析为非公网地址，拒绝下载: %s", host)
            return None
        referer = _REFERERS.get(str(source or "").strip().lower(), _DEFAULT_REFERER)
        headers = dict(_UA)
        headers["Referer"] = referer
        try:
            timeout = aiohttp.ClientTimeout(total=30, connect=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers, allow_redirects=False) as resp:
                    if resp.status != 200:
                        logger.info("预览图下载非 200: %s", resp.status)
                        return None
                    content_type = str(resp.headers.get("Content-Type") or "").lower()
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
                        return None
                    data = b"".join(chunks)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            logger.info("预览图下载失败: %s", type(exc).__name__)
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

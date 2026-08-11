"""Tunnel URL 校验与可达性验证。

安全约束（规范 §9）：
- 只允许 https；拒绝 http://。
- host 非空、无 userinfo（user:pass@）。
- 拒绝 file / javascript / data 等伪 scheme。
- 设置后必须能通过 Tunnel 访问到本机房间网关的 /room/v1/info，
  且返回的 room.public_id 必须与当前房间一致（防错指到别的房间）。
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

log = logging.getLogger(__name__)

_SCHEMES_ALLOWED = {"https"}
_SCHEMES_BLOCKED = {"file", "javascript", "data", "about", "ftp", "gopher", "ws", "wss", "http"}


class TunnelValidationError(Exception):
    """Tunnel URL 不合法。message 为面向用户的提示。"""


def validate_tunnel_url_syntax(url: str) -> str:
    """同步校验 URL 语法；通过则返回规范化后的 https URL，否则抛 TunnelValidationError。"""
    if not url or not isinstance(url, str):
        raise TunnelValidationError("Tunnel 地址不能为空")
    url = url.strip()
    if len(url) > 2048:
        raise TunnelValidationError("Tunnel 地址过长")
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme in _SCHEMES_BLOCKED:
        raise TunnelValidationError("仅支持 https 地址")
    if scheme not in _SCHEMES_ALLOWED:
        raise TunnelValidationError("仅支持 https 地址")
    if not parsed.hostname:
        raise TunnelValidationError("Tunnel 地址缺少主机名")
    if parsed.username or parsed.password:
        raise TunnelValidationError("Tunnel 地址不能包含账号信息")
    if parsed.fragment:
        raise TunnelValidationError("Tunnel 地址不能包含片段(#)")
    # 去掉末尾斜杠和路径；房间网关只通过根路径访问
    return f"https://{parsed.netloc}"


async def verify_tunnel(url: str, expected_public_id: str, *, timeout: float = 10.0) -> bool:
    """异步验证 Tunnel 可达且指向本房间。失败返回 False（不抛异常）。"""
    import aiohttp

    base = url.rstrip("/")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/room/v1/info", timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                if response.status != 200:
                    return False
                data = await response.json()
        info = data.get("room") if isinstance(data, dict) else None
        public_id = info.get("id") if isinstance(info, dict) else None
        return public_id == expected_public_id
    except Exception as exc:  # aiohttp.ClientError / asyncio.TimeoutError / json
        log.info("tunnel verify failed for %s: %s", base, exc)
        return False

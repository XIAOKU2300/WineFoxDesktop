"""出网环境探测：系统代理自动检测，供 LoRA 搜索/下载/预览复用。

目标是在任何机器上尽量开箱即用：用户没有在设置里显式填写代理时，依次
回退到环境变量（HTTPS_PROXY 等）和 Windows 系统代理（Internet Settings
注册表，即"系统代理"开关指向的地址）。显式配置永远优先；检测结论带
TTL 缓存，避免每次构建客户端都查注册表。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from urllib.parse import urlparse

_CACHE_TTL_SECONDS = 10.0
_system_proxy_cache: dict[str, object] = {"at": 0.0, "value": ""}

#: 显式代理可达性探测结果的缓存时长与最长信任期。
_PROXY_PROBE_TTL = 30.0
_PROXY_PROBE_HARD_STALE = 120.0
_proxy_probe_cache: dict[str, tuple[float, bool]] = {}


def normalize_proxy_url(proxy: str) -> str:
    """把 ``host:port`` 形式补全为 ``http://host:port``；已是 URL 则原样返回。"""
    value = str(proxy or "").strip()
    if not value:
        return ""
    if re.match(r"^[a-z][a-z0-9+.-]*://", value, flags=re.I):
        return value.rstrip("/")
    if re.match(r"^(?:[^:/\s]+|\[[0-9a-f:]+\]):\d{1,5}$", value, flags=re.I):
        return f"http://{value}"
    return value


def _proxy_from_environment() -> str:
    # Civitai / Hugging Face 全部走 HTTPS，环境变量优先取 https 专用项。
    for name in (
        "HTTPS_PROXY", "https_proxy",
        "ALL_PROXY", "all_proxy",
        "HTTP_PROXY", "http_proxy",
    ):
        value = normalize_proxy_url(os.environ.get(name) or "")
        if value:
            return value
    return ""


def _parse_registry_proxy_server(server: str) -> str:
    """解析注册表 ProxyServer 字段："host:port" 或 "http=..;https=.."。"""
    server = str(server or "").strip()
    if not server:
        return ""
    if "=" in server:
        # 形如 "http=127.0.0.1:7890;https=127.0.0.1:7890;ftp=..."；只取
        # https/http 条目，其余协议（ftp/socks 单独条目）对 HTTP(S) 出网无用。
        parts = {}
        for chunk in server.split(";"):
            name, _, value = chunk.partition("=")
            if name and value:
                parts[name.strip().lower()] = value.strip()
        server = parts.get("https") or parts.get("http") or ""
    return normalize_proxy_url(server)


def _proxy_from_windows_registry() -> str:
    try:
        import winreg
    except ImportError:
        return ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled, _kind = winreg.QueryValueEx(key, "ProxyEnable")
            if not enabled:
                return ""
            try:
                server, _kind = winreg.QueryValueEx(key, "ProxyServer")
            except OSError:
                return ""
    except OSError:
        return ""
    return _parse_registry_proxy_server(server)


def _detect_system_proxy_once() -> str:
    value = _proxy_from_environment()
    if value:
        return value
    return _proxy_from_windows_registry()


def system_proxy() -> str:
    """带 TTL 缓存的系统代理检测；没有返回空串。"""
    now = time.monotonic()
    if now - float(_system_proxy_cache.get("at") or 0) > _CACHE_TTL_SECONDS:
        _system_proxy_cache.update(at=now, value=_detect_system_proxy_once())
    return str(_system_proxy_cache.get("value") or "")


def effective_proxy(explicit: str = "") -> tuple[str, str]:
    """解析实际生效的代理。

    返回 ``(proxy, source)``；source 为 ``"explicit"``（用户设置）、
    ``"system"``（环境变量/注册表自动检测）、``"system-fallback"``
    （显式代理近期探测不可达，自动回退系统代理）或 ``""``（直连）。
    """
    value = normalize_proxy_url(explicit)
    if value:
        probed_at, reachable = _proxy_probe_cache.get(value, (0.0, True))
        never_probed = probed_at <= 0
        stale_beyond_trust = probed_at > 0 and time.monotonic() - probed_at > _PROXY_PROBE_HARD_STALE
        if reachable or never_probed or stale_beyond_trust:
            return value, "explicit"
        # 显式代理近期被探测为不可达：不要让一次失效配置拖垮全部搜索/下载，
        # 自动回退系统代理；没有系统代理则直连（TUN 环境仍可用）。
        detected = system_proxy()
        if detected and detected != value:
            return detected, "system-fallback"
        return "", ""
    detected = system_proxy()
    if detected:
        return detected, "system"
    return "", ""


async def probe_proxy_reachable(proxy: str, *, timeout: float = 1.5) -> bool:
    """TCP 探测代理端口是否可连；非 HTTP(S) 协议一律视为不可用。"""
    value = normalize_proxy_url(proxy)
    if not value:
        return False
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(parsed.hostname, port), timeout=timeout
        )
    except Exception:
        return False
    try:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    except Exception:
        pass
    return True


async def ensure_probed(explicit: str = "") -> None:
    """在异步上下文里探测显式代理可达性，供 ``effective_proxy`` 决策。

    搜索/下载发起前调用一次；TTL 内重复调用是空操作。探测失败会记录，
    使后续 ``effective_proxy`` 自动回退系统代理而不是继续撞死端口。
    """
    value = normalize_proxy_url(explicit)
    if not value:
        return
    probed_at, _reachable = _proxy_probe_cache.get(value, (0.0, True))
    if time.monotonic() - probed_at <= _PROXY_PROBE_TTL:
        return
    reachable = await probe_proxy_reachable(value)
    _proxy_probe_cache[value] = (time.monotonic(), reachable)


def reset_cache() -> None:
    """测试与设置变更后立即重新检测。"""
    _system_proxy_cache.update(at=0.0, value="")
    _proxy_probe_cache.clear()

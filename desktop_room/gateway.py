"""房间网关 aiohttp 应用（独立 app，独立安全中间件，只监听 127.0.0.1）。

Task 3 提供骨架：/room/v1/info、/room/v1/catalog。会话/WS/上传/生图路由在后续任务扩展。

安全中间件 host_origin_middleware：
- Host 头必须是 127.0.0.1:<port> / localhost:<port>（防 DNS 重绑定）。
- 面板代理的请求必须带 X-WineFox-Room-Host == host_token。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from . import protocol
from .models import room_snapshot
from .security import constant_time_equal
from .storage import RoomStorage

log = logging.getLogger(__name__)

# 访客界面静态目录。生产约定：room_ui/ 与桌面 UI 独立，自包含（无外部依赖），
# 由网关在 / 与 /assets/ 提供。本地回环 http 与公网 https（Tunnel）都从这入口。
_ROOM_UI_DIRNAME = "room_ui"


def _resolve_room_ui_dir() -> Path:
    """在开发树与 PyInstaller onedir 产物中定位 room_ui 目录。"""
    here = Path(__file__).resolve().parent
    candidates = [here / _ROOM_UI_DIRNAME]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "desktop_room" / _ROOM_UI_DIRNAME)
    candidates.append(
        Path(sys.executable).resolve().parent / "_internal" / "desktop_room" / _ROOM_UI_DIRNAME
    )
    for cand in candidates:
        if (cand / "index.html").is_file():
            return cand
    return here / _ROOM_UI_DIRNAME


async def _index_handler(request: web.Request) -> web.Response:
    room_ui = request.app.get("room_ui_dir")
    index = Path(room_ui) / "index.html" if room_ui else None
    if index is None or not index.is_file():
        return web.json_response({"error": "访客界面不可用", "errorCode": "UI_UNAVAILABLE"}, status=503)
    try:
        body = index.read_bytes()
    except OSError:
        return web.json_response({"error": "访客界面读取失败", "errorCode": "UI_UNAVAILABLE"}, status=503)
    return web.Response(
        body=body,
        content_type="text/html",
        charset="utf-8",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )

_HAS_HOST_TOKEN = "X-WineFox-Room-Host"
_ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


def _host_header(request: web.Request) -> str:
    return (request.headers.get("Host") or "").lower()


def _hostname(host: str) -> str:
    return host.split(":", 1)[0].strip("[]")


def host_origin_middleware(host_token: str):
    @web.middleware
    async def middleware(request: web.Request, handler: Callable[[web.Request], Any]):
        hostname = _hostname(_host_header(request))
        # 1) 本机回环（127.0.0.1 / localhost）
        if hostname in _ALLOWED_HOSTS:
            return await handler(request)
        # 2) 面板代理注入的房主令牌（Rust 代理改写请求，Host 为 winefox-panel.localhost）
        header = request.headers.get(_HAS_HOST_TOKEN)
        if header is not None and constant_time_equal(header, host_token):
            return await handler(request)
        # 3) 用户手动配置的 Tunnel 公网域名（访客只能走这里进来）
        if hostname in request.app.get("allow_tunnel_hosts", ()):
            return await handler(request)
        return web.json_response({"error": "forbidden", "errorCode": "HOST_ORIGIN_REJECTED"}, status=403)

    return middleware


async def _info_handler(request: web.Request) -> web.Response:
    storage: RoomStorage = request.app["room_storage"]
    room = storage.get_room(request.app["room_id"])
    if room is None:
        return web.json_response({"error": "not found", "errorCode": "ROOM_NOT_FOUND"}, status=404)
    return web.json_response({
        "protocol": protocol.ROOM_PROTOCOL_VERSION,
        "room": {
            "id": room["public_id"],
            "name": room["name"],
            "ownerName": room["owner_name"],
            "state": room["state"],
            "createdAt": room["created_at"],
        },
    })


async def _catalog_handler(request: web.Request) -> web.Response:
    storage: RoomStorage = request.app["room_storage"]
    room_id: int = request.app["room_id"]
    room = storage.get_room(room_id)
    if room is None:
        return web.json_response({"error": "not found", "errorCode": "ROOM_NOT_FOUND"}, status=404)
    settings = storage.room_settings_map(room_id)
    capabilities = [protocol.CAP_TXT2IMG]
    if settings.get("allowImg2img"):
        capabilities.append(protocol.CAP_IMG2IMG)
    if settings.get("allowPromptEnhancement"):
        capabilities.append(protocol.CAP_PROMPT_ENHANCEMENT)
    if settings.get("allowUpload"):
        capabilities.append(protocol.CAP_UPLOADS)
    payload: dict[str, Any] = {
        "room": {
            "id": room["public_id"],
            "name": room["name"],
            "state": room["state"],
            "maxMembers": settings["maxMembers"],
        },
        "capabilities": capabilities,
        "settings": settings,
    }
    # 共享生图时，把房主桌面目录里可安全暴露给访客的模型/工作流/LoRA 一并下发，
    # 让访客生成控件与桌面生成页一致（白名单字段，绝不含本地绝对路径/工作流 JSON）。
    bridge = request.app.get("generation_bridge")
    if bridge is not None:
        try:
            cat = await bridge.catalog()
        except Exception:  # pragma: no cover - 目录不可用不应阻塞房间
            cat = None
        if isinstance(cat, dict):
            for key, allow in _GUEST_CATALOG_FIELDS.items():
                value = cat.get(key)
                if isinstance(value, list):
                    payload[key] = [_guest_catalog_item(allow, item) for item in value if isinstance(item, dict)]
    return web.json_response(payload)


# 访客目录白名单：字段名 -> 允许下发的键。
_GUEST_CATALOG_FIELDS: dict[str, tuple[str, ...]] = {
    "families": ("id", "label"),
    "workflows": ("id", "name", "label", "mode", "family"),
    "checkpoints": ("id", "name", "label", "family", "compat"),
    "loras": ("id", "name", "label", "family", "compat", "strength",
              "character", "aliases", "trigger_words", "tags"),
}


def _guest_catalog_item(allow: tuple[str, ...], item: dict[str, Any]) -> dict[str, Any]:
    return {k: item[k] for k in allow if k in item}


def create_room_app(*, storage: RoomStorage, room_id: int, host_token: str,
                    data_dir: Path | None = None, generation_backend: Any = None,
                    generation_poll_interval: float = 0.4) -> web.Application:
    from .rate_limit import LoginRateLimiter
    from .rooms import _ChatRateBucket, _install_room_routes
    from .uploads import install_upload_routes
    from .websocket import ChatHub, ws_handler
    from .generation_bridge import GenerationBridge, install_generation_routes
    from .host import install_host_routes

    app = web.Application(middlewares=[host_origin_middleware(host_token)],
                          client_max_size=32 * 1024 * 1024)
    app["room_storage"] = storage
    app["room_id"] = room_id
    app["host_token"] = host_token
    # 附件文件存放的绝对根目录（数据库只存相对路径，绝不外泄绝对路径）。
    app["data_dir"] = data_dir
    # 用户配置的 Tunnel 公网域名（只允许显式放行的 Host）。
    app["allow_tunnel_hosts"] = []
    # 会话/聊天/限流/实时推送
    app["chat_hub"] = ChatHub()
    app["rate_limiter"] = LoginRateLimiter()
    app["chat_limiter"] = _ChatRateBucket()
    # Cookie 默认按规范 Secure；仅本地测试可关闭。
    app["secure_cookies"] = True
    # 房主管理界面可订阅登录失败数更新（manager 注入回调）。
    app["notify_login_failure"] = None

    # 共享生图桥接：把访客请求翻译给房主桌面引擎。
    if generation_backend is not None:
        room = storage.get_room(room_id)
        app["generation_bridge"] = GenerationBridge(
            storage=storage,
            room_id=room_id,
            room_public_id=room["public_id"] if room else "",
            data_dir=data_dir,
            hub=app["chat_hub"],
            backend=generation_backend,
            poll_interval=generation_poll_interval,
        )
    else:
        app["generation_bridge"] = None

    app.router.add_get("/room/v1/info", _info_handler)
    app.router.add_get("/room/v1/catalog", _catalog_handler)
    _install_room_routes(app)
    install_upload_routes(app)
    install_generation_routes(app)
    install_host_routes(app)
    app.router.add_get("/room/v1/ws", ws_handler)

    # 访客界面（/ 与 /assets/*）。缺失时房间功能不受影响，只是访客入口不可用。
    room_ui_dir = _resolve_room_ui_dir()
    if (room_ui_dir / "index.html").is_file():
        app["room_ui_dir"] = room_ui_dir
        app.router.add_get("/", _index_handler)
        app.router.add_static("/assets/", room_ui_dir, name="room_assets")
    else:
        app["room_ui_dir"] = None
    return app

"""房主管理路由：仅房主（X-WineFox-Room-Host 面板代理或房主会话）可用。

提供：状态/成员查看、公告、踢人、暂停生图/入房、清空聊天、修改房间密码。
密码修改走与建房相同的 Argon2id 哈希流程，绝不以明文落库。
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from . import protocol
from .auth import error_response, resolve_member
from .models import member_public, message_payload
from .security import hash_password
from .storage import RoomStorage
from .websocket import notify_member_left


def _require_owner(request: web.Request) -> web.Response | None:
    member = resolve_member(request)
    if member is None:
        return error_response("未登录或会话已失效", "AUTH_REQUIRED", 401)
    if str(member["role"]) != protocol.ROLE_OWNER:
        return error_response("仅房主可执行该操作", "FORBIDDEN", 403)
    request["room_owner"] = member
    return None


async def _status_handler(request: web.Request) -> web.Response:
    blocked = _require_owner(request)
    if blocked is not None:
        return blocked
    storage: RoomStorage = request.app["room_storage"]
    room_id = int(request.app["room_id"])
    room = storage.get_room(room_id)
    if room is None:
        return error_response("房间不存在", "ROOM_NOT_FOUND", 404)
    settings = storage.room_settings_map(room_id)
    members = [member_public(m) for m in storage.active_members(room_id)]
    return web.json_response({
        "room": {
            "id": room["public_id"],
            "name": room["name"],
            "state": room["state"],
            "maxMembers": room["max_members"],
            "createdAt": room["created_at"],
        },
        "settings": settings,
        "flags": {
            "generationPaused": bool(request.app.get("generation_paused")),
            "membersJoinPaused": bool(request.app.get("members_join_paused")),
        },
        "members": members,
        "runningJobs": storage.room_running_jobs(room_id),
    })


async def _members_handler(request: web.Request) -> web.Response:
    blocked = _require_owner(request)
    if blocked is not None:
        return blocked
    storage: RoomStorage = request.app["room_storage"]
    members = [member_public(m) for m in storage.active_members(int(request.app["room_id"]))]
    return web.json_response({"members": members})


async def _announce_handler(request: web.Request) -> web.Response:
    blocked = _require_owner(request)
    if blocked is not None:
        return blocked
    try:
        body = await request.json()
    except Exception:
        return error_response("请求体必须是 JSON", "BAD_REQUEST", 400)
    if not isinstance(body, dict):
        return error_response("请求体必须是 JSON", "BAD_REQUEST", 400)
    text = str(body.get("text") or "").strip()
    if not text:
        return error_response("公告内容不能为空", "EMPTY_TEXT", 400)
    if len(text) > 2000:
        return error_response("公告过长（最多 2000 字）", "TEXT_TOO_LONG", 400)
    storage: RoomStorage = request.app["room_storage"]
    room_id = int(request.app["room_id"])
    message_id = storage.add_message(
        room_id=room_id, sender_id=None, msg_type=protocol.MSG_SYSTEM, text=text,
    )
    row = storage.get_message(message_id)
    hub = request.app.get("chat_hub")
    if hub is not None and row is not None:
        await hub.broadcast("message.created", message_payload(row, storage))
    return web.json_response({"message": message_payload(row, storage)}, status=201)


async def _kick_handler(request: web.Request) -> web.Response:
    blocked = _require_owner(request)
    if blocked is not None:
        return blocked
    try:
        body = await request.json()
    except Exception:
        return error_response("请求体必须是 JSON", "BAD_REQUEST", 400)
    if not isinstance(body, dict):
        return error_response("请求体必须是 JSON", "BAD_REQUEST", 400)
    try:
        member_id = int(body.get("memberId"))
    except (TypeError, ValueError):
        return error_response("memberId 无效", "BAD_REQUEST", 400)
    storage: RoomStorage = request.app["room_storage"]
    member = storage.get_member(member_id)
    if member is None or int(member["room_id"]) != int(request.app["room_id"]):
        return error_response("成员不存在", "MEMBER_NOT_FOUND", 404)
    if str(member["role"]) == protocol.ROLE_OWNER:
        return error_response("不能移除房主", "FORBIDDEN", 403)
    storage.kick_member(member_id)
    hub = request.app.get("chat_hub")
    if hub is not None:
        await hub.broadcast("member.left", member_public(member))
        for ws in hub.sockets_for(member_id):
            try:
                await ws.close(code=1001, message=b"kicked")
            except Exception:
                pass
    return web.json_response({"kicked": True})


async def _pause_handler(request: web.Request) -> web.Response:
    blocked = _require_owner(request)
    if blocked is not None:
        return blocked
    try:
        body = await request.json()
    except Exception:
        return error_response("请求体必须是 JSON", "BAD_REQUEST", 400)
    if not isinstance(body, dict):
        return error_response("请求体必须是 JSON", "BAD_REQUEST", 400)
    request.app["generation_paused"] = bool(body.get("generationPaused", request.app.get("generation_paused")))
    request.app["members_join_paused"] = bool(body.get("membersJoinPaused", request.app.get("members_join_paused")))
    hub = request.app.get("chat_hub")
    if hub is not None:
        await hub.broadcast("room.settings_changed", {
            "generationPaused": request.app["generation_paused"],
            "membersJoinPaused": request.app["members_join_paused"],
        })
    return web.json_response({
        "generationPaused": request.app["generation_paused"],
        "membersJoinPaused": request.app["members_join_paused"],
    })


async def _clear_handler(request: web.Request) -> web.Response:
    blocked = _require_owner(request)
    if blocked is not None:
        return blocked
    storage: RoomStorage = request.app["room_storage"]
    room_id = int(request.app["room_id"])
    storage.clear_messages(room_id)
    return web.json_response({"cleared": True})


async def _password_handler(request: web.Request) -> web.Response:
    blocked = _require_owner(request)
    if blocked is not None:
        return blocked
    try:
        body = await request.json()
    except Exception:
        return error_response("请求体必须是 JSON", "BAD_REQUEST", 400)
    if not isinstance(body, dict):
        return error_response("请求体必须是 JSON", "BAD_REQUEST", 400)
    password = str(body.get("password") or "")
    if len(password) < 4 or len(password) > 128:
        return error_response("密码长度需为 4-128 个字符", "INVALID_PASSWORD", 400)
    algorithm, params_json, salt_b64, hash_b64 = hash_password(password)
    storage: RoomStorage = request.app["room_storage"]
    storage.update_room_password(
        int(request.app["room_id"]),
        password_algorithm=algorithm,
        password_parameters_json=params_json,
        password_salt=salt_b64,
        password_hash=hash_b64,
    )
    return web.json_response({"changed": True})


def install_host_routes(app: web.Application) -> None:
    app["members_join_paused"] = False
    app.router.add_get("/room/v1/host/status", _status_handler)
    app.router.add_get("/room/v1/host/members", _members_handler)
    app.router.add_post("/room/v1/host/announce", _announce_handler)
    app.router.add_post("/room/v1/host/kick", _kick_handler)
    app.router.add_post("/room/v1/host/pause", _pause_handler)
    app.router.add_post("/room/v1/host/clear", _clear_handler)
    app.router.add_post("/room/v1/host/password", _password_handler)

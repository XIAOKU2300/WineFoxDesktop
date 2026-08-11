"""房间 REST 处理器：加入/退出、会话、聊天消息（发送/删除/分页）。

加入流程校验顺序（全失败路径均返回明确错误码，绝不静默改名）：
  名称合法性 → 房间在线 → 名称占用 → 成员上限 → 密码校验(限流+并发门禁)
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from aiohttp import web

from . import protocol
from .auth import clear_session_cookie, error_response, require_member, resolve_member, set_session_cookie
from .models import member_public, message_payload
from .rate_limit import LoginRateLimiter
from .security import (
    NameErrorDetail,
    contains_lone_surrogate,
    new_session_token,
    normalize_lookup,
    validate_name,
    verify_password,
)
from .storage import RoomStorage
from .websocket import (
    ChatHub,
    notify_member_joined,
    notify_message_created,
    notify_message_deleted,
)

MAX_TEXT_LENGTH = 2000
MAX_JOIN_IP_KEY_LENGTH = 64


def _client_ip(request: web.Request) -> str:
    """限流用客户端 IP 键。

    网关只监听 127.0.0.1，公网流量必经 Cloudflare 隧道，因此优先信任
    CF-Connecting-IP（由 CF 覆写，不可被访客伪造）；其次取 X-Forwarded-For 的
    **最后**一项（代理链把真实远端 IP 追加在尾部，取末项可抵抗伪造头）。
    直接连回环的请求退回 request.remote。
    """
    cf = request.headers.get("CF-Connecting-IP")
    if cf:
        return cf.strip()[:MAX_JOIN_IP_KEY_LENGTH] or "unknown"
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[-1].strip()[:MAX_JOIN_IP_KEY_LENGTH] or "unknown"
    return (request.remote or "unknown")[:MAX_JOIN_IP_KEY_LENGTH]


async def _join_handler(request: web.Request) -> web.Response:
    app = request.app
    storage: RoomStorage = app["room_storage"]
    limiter: LoginRateLimiter = app["rate_limiter"]
    room_id = int(app["room_id"])
    room = storage.get_room(room_id)
    if room is None:
        return error_response("房间不存在", "ROOM_NOT_FOUND", 404)
    if room["state"] != protocol.ROOM_STATE_ONLINE:
        return error_response("房间已关闭", "ROOM_CLOSED", 409)
    if app.get("members_join_paused"):
        return error_response("房主已暂停新成员加入", "JOIN_PAUSED", 409)

    try:
        body = await request.json()
    except Exception:
        return error_response("请求体必须是 JSON", "BAD_REQUEST", 400)
    if not isinstance(body, dict):
        return error_response("请求体必须是 JSON", "BAD_REQUEST", 400)

    ip = _client_ip(request)
    public_id = str(room["public_id"])
    if not limiter.ip_allows(ip):
        return error_response("尝试过于频繁，请稍后再试", "RATE_LIMITED_IP", 429,
                              headers={"Retry-After": str(int(limiter.backoff_seconds(ip)))})
    if not limiter.room_allows(public_id, ip):
        return error_response("尝试过于频繁，请稍后再试", "RATE_LIMITED_ROOM", 429,
                              headers={"Retry-After": str(int(limiter.backoff_seconds(ip)))})

    name = body.get("name")
    password = body.get("password")
    if not isinstance(name, str) or not isinstance(password, str):
        return error_response("名称和密码为必填项", "BAD_REQUEST", 400)
    if not password:
        return error_response("请输入房间密码", "PASSWORD_REQUIRED", 400)

    try:
        display_name = validate_name(name)
    except NameErrorDetail as exc:
        return error_response(str(exc), "INVALID_NAME", 400)

    lookup = normalize_lookup(display_name)
    if storage.name_taken(room_id, lookup):
        return error_response("该名称已被使用，请换一个", "NAME_TAKEN", 409)
    if storage.active_member_count(room_id) >= int(room["max_members"]):
        return error_response("房间人数已满", "ROOM_FULL", 409)

    # 密码校验在全局并发门禁内执行，且 Argon2id 是同步 CPU 密集调用，
    # 必须移到线程池，避免阻塞事件循环（单线程里 Semaphore 只排队不并行）。
    async with limiter.concurrent_guard():
        ok = await asyncio.to_thread(_verify, room, password)
    if not ok:
        limiter.record_failure(ip, public_id)
        notify = app.get("notify_login_failure")
        if notify is not None:
            notify(limiter.room_failed_count(public_id))
        return error_response("密码错误", "WRONG_PASSWORD", 401)
    limiter.record_success(ip, public_id)

    token, token_hash = new_session_token()
    member_id = storage.add_member(
        room_id=room_id,
        display_name=display_name,
        normalized_name=lookup,
        role=protocol.ROLE_GUEST,
        session_hash=token_hash,
    )
    member = storage.get_member(member_id)
    if member is None:
        return error_response("加入失败", "JOIN_FAILED", 500)

    secure = _secure_cookies(app, request)
    payload: dict[str, Any] = {"member": member_public(member), "joined": True}
    # 纯回环 http（无 TLS）：浏览器不接收 Secure Cookie，把会话令牌放回响应体，
    # 由访客页面脚本显式携带；公网 https（Tunnel）仍只走 Cookie，令牌不进页面内存。
    if request.scheme != "https" and request.headers.get("X-Forwarded-Proto") != "https":
        payload["sessionToken"] = token
    response = web.json_response(payload)
    set_session_cookie(response, token, secure=secure)
    await notify_member_joined(request, member)
    return response


def _verify(room, password: str) -> bool:
    """Argon2id 校验（同步阻塞调用，由调用方 asyncio.to_thread 执行）。"""
    return verify_password(
        password,
        room["password_salt"],
        room["password_hash"],
        room["password_parameters_json"],
    )


def _secure_cookies(app: web.Application, request: web.Request) -> bool:
    if app.get("secure_cookies") is False:
        return False
    if request.scheme == "https" or request.headers.get("X-Forwarded-Proto") == "https":
        return True
    # 规范：Cookie 必须带 Secure；本地 http 测试由客户端自行携带令牌。
    return True


async def _session_handler(request: web.Request) -> web.Response:
    member = resolve_member(request)
    if member is None:
        return error_response("未登录或会话已失效", "AUTH_REQUIRED", 401)
    storage: RoomStorage = request.app["room_storage"]
    room = storage.get_room(int(request.app["room_id"]))
    return web.json_response({
        "member": member_public(member),
        "room": {
            "id": room["public_id"] if room else None,
            "name": room["name"] if room else None,
            "state": room["state"] if room else None,
        },
    })


async def _leave_handler(request: web.Request) -> web.Response:
    member = resolve_member(request)
    response = web.json_response({"left": True})
    if member is None:
        clear_session_cookie(response)
        return response
    storage: RoomStorage = request.app["room_storage"]
    storage.kick_member(int(member["id"]))
    await notify_member_left(request, member)
    clear_session_cookie(response)
    return response


async def _messages_get_handler(request: web.Request) -> web.Response:
    member = resolve_member(request)
    if member is None:
        return error_response("未登录或会话已失效", "AUTH_REQUIRED", 401)
    storage: RoomStorage = request.app["room_storage"]
    room_id = int(request.app["room_id"])
    try:
        before = int(request.query.get("before", 0)) or None
        limit = int(request.query.get("limit", 50))
    except (TypeError, ValueError):
        before, limit = None, 50
    rows = storage.list_messages(room_id=room_id, before=before, limit=limit)
    messages = [message_payload(r, storage) for r in rows]
    return web.json_response({"messages": messages})


async def _messages_post_handler(request: web.Request) -> web.Response:
    member = resolve_member(request)
    if member is None:
        return error_response("未登录或会话已失效", "AUTH_REQUIRED", 401)
    app = request.app
    storage: RoomStorage = app["room_storage"]
    room_id = int(app["room_id"])
    room = storage.get_room(room_id)
    if room is None:
        return error_response("房间不存在", "ROOM_NOT_FOUND", 404)

    try:
        body = await request.json()
    except Exception:
        return error_response("请求体必须是 JSON", "BAD_REQUEST", 400)
    if not isinstance(body, dict):
        return error_response("请求体必须是 JSON", "BAD_REQUEST", 400)

    if body.get("action") == "delete":
        return await _delete_message(request, member, body)

    if not room["allow_chat"]:
        return error_response("房主已关闭聊天", "CHAT_DISABLED", 403)
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        return error_response("消息不能为空", "EMPTY_TEXT", 400)
    if len(text) > MAX_TEXT_LENGTH:
        return error_response(f"消息过长（最多 {MAX_TEXT_LENGTH} 字）", "TEXT_TOO_LONG", 400)
    if contains_lone_surrogate(text):
        return error_response("消息包含无法识别的字符，请重新输入", "INVALID_CHARACTERS", 400)
    text = text.strip()

    client_message_id = body.get("clientMessageId")
    if client_message_id is not None and (not isinstance(client_message_id, str)
                                          or len(client_message_id) > 128):
        return error_response("clientMessageId 不合法", "BAD_REQUEST", 400)
    reply_to_id = body.get("replyToId")
    if reply_to_id is not None:
        try:
            reply_to_id = int(reply_to_id)
        except (TypeError, ValueError):
            reply_to_id = None

    # 轻量聊天限速（20 条 / 10 秒 / 成员）
    now = time.monotonic()
    bucket = request.app["chat_limiter"]
    if bucket.try_acquire(int(member["id"]), now, limit=20, window=10.0):
        pass
    else:
        return error_response("发言过于频繁，请稍后再试", "CHAT_RATE_LIMITED", 429)

    message_id = storage.add_message(
        room_id=room_id,
        sender_id=int(member["id"]),
        msg_type=protocol.MSG_TEXT,
        text=text,
        client_message_id=client_message_id,
        reply_to_id=reply_to_id,
    )
    row = storage.get_message(message_id)
    await notify_message_created(request, row)
    return web.json_response({"message": message_payload(row, storage)}, status=201)


async def _delete_message(request: web.Request, member, body: dict[str, Any]) -> web.Response:
    storage: RoomStorage = request.app["room_storage"]
    try:
        message_id = int(body.get("messageId"))
    except (TypeError, ValueError):
        return error_response("messageId 无效", "BAD_REQUEST", 400)
    row = storage.get_message(message_id)
    if row is None:
        return error_response("消息不存在", "MESSAGE_NOT_FOUND", 404)
    is_owner = member["role"] == protocol.ROLE_OWNER
    is_sender = int(row["sender_id"] or -1) == int(member["id"])
    if not (is_owner or is_sender):
        return error_response("无权删除该消息", "FORBIDDEN", 403)
    storage.mark_message_deleted(message_id)
    await notify_message_deleted(request, message_id)
    return web.json_response({"deleted": True})


class _ChatRateBucket:
    """进程内成员聊天限速（固定窗口计数）。"""

    def __init__(self) -> None:
        self._hits: dict[int, list[float]] = {}

    def try_acquire(self, member_id: int, now: float, *, limit: int, window: float) -> bool:
        hits = self._hits.get(member_id)
        if hits is None:
            self._hits[member_id] = [now]
            return True
        hits = [t for t in hits if now - t < window]
        self._hits[member_id] = hits
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True


def _install_room_routes(app: web.Application) -> None:
    app.router.add_post("/room/v1/join", _join_handler)
    app.router.add_get("/room/v1/session", _session_handler)
    app.router.add_post("/room/v1/leave", _leave_handler)
    app.router.add_get("/room/v1/messages", _messages_get_handler)
    app.router.add_post("/room/v1/messages", _messages_post_handler)

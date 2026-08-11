"""房间网关认证：会话 Cookie（访客）+ 房主管理令牌（面板代理）。

访客会话：
- 登录成功 → 下发 `wf_room_session` Cookie，Secure/HttpOnly/SameSite=Lax。
- 数据库只存令牌的 SHA-256 哈希；Cookie 值即 256 位随机令牌原文。
- 校验时先哈希再查库（常量时间比较由哈希保证）。

房主路径：面板代理注入 `X-WineFox-Room-Host` == host_token，直接映射到房主成员。
主后端会话 Cookie 绝不转发到房间网关（面板代理已在协议层拦截）。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Awaitable, Callable

from aiohttp import web

from .security import hash_session_token

SESSION_COOKIE = "wf_room_session"
_HOST_TOKEN_HEADER = "X-WineFox-Room-Host"


def resolve_member(request: web.Request) -> sqlite3.Row | None:
    """解析当前请求对应的成员。返回 None 表示未认证。"""
    app = request.app
    storage = app["room_storage"]
    # 房主面板代理路径
    host_token = app.get("host_token")
    if host_token and request.headers.get(_HOST_TOKEN_HEADER) == host_token:
        room_id = int(app["room_id"])
        members = storage.active_members(room_id)
        for member in members:
            if member["role"] == "owner":
                return member
    # 访客会话 Cookie
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        session_hash = hash_session_token(raw)
    except Exception:
        return None
    member = storage.member_by_session(session_hash)
    if member is None or member["kicked_at"] is not None:
        return None
    if int(member["room_id"]) != int(app["room_id"]):
        return None
    return member


def set_session_cookie(response: web.Response, token: str, *, secure: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="Lax",
        secure=secure,
        max_age=86400 * 30,
    )


def clear_session_cookie(response: web.Response) -> None:
    response.del_cookie(SESSION_COOKIE)


def require_member(
    handler: Callable[[web.Request], Awaitable[web.Response]],
) -> Callable[[web.Request], Awaitable[web.Response]]:
    async def wrapped(request: web.Request) -> web.Response:
        request["room_member"] = resolve_member(request)
        if request["room_member"] is None:
            return web.json_response(
                {"error": "未登录或会话已失效", "errorCode": "AUTH_REQUIRED"}, status=401
            )
        return await handler(request)

    return wrapped


def error_response(message: str, code: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message, "errorCode": code}, status=status)

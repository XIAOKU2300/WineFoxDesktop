"""房间 WebSocket 层：实时事件推送（聊天 + 任务 + 成员进出）。

帧格式：``{"event": "<WS_EVENTS>", "data": {...}}``。
图片从不以 Base64 走 WS —— 只发附件 ID 与授权 URL。

ChatHub 维护 member_id -> 连接集合，广播时逐个发送；发送失败自动清理。
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from aiohttp import web

from . import protocol
from .auth import resolve_member
from .models import member_public, message_payload, room_snapshot
from .storage import RoomStorage

log = logging.getLogger(__name__)


def ws_packet(event: str, data: Any) -> dict[str, Any]:
    return {"event": event, "data": data}


class ChatHub:
    def __init__(self) -> None:
        # member_id -> set[web.WebSocketResponse]
        self._connections: dict[int, set[web.WebSocketResponse]] = {}

    def connect(self, member_id: int, ws: web.WebSocketResponse) -> None:
        self._connections.setdefault(member_id, set()).add(ws)

    def disconnect(self, member_id: int, ws: web.WebSocketResponse) -> None:
        bucket = self._connections.get(member_id)
        if not bucket:
            return
        bucket.discard(ws)
        if not bucket:
            self._connections.pop(member_id, None)

    def connected_member_ids(self) -> list[int]:
        return list(self._connections)

    def sockets_for(self, member_id: int) -> list[web.WebSocketResponse]:
        return list(self._connections.get(member_id, ()))

    def count(self) -> int:
        return len(self._connections)

    async def broadcast(self, event: str, data: Any) -> None:
        packet = ws_packet(event, data)
        payload = __import__("json").dumps(packet, ensure_ascii=False, separators=(",", ":"))
        dead: list[tuple[int, web.WebSocketResponse]] = []
        for member_id, sockets in self._connections.items():
            for ws in sockets:
                try:
                    await ws.send_str(payload)
                except Exception:
                    dead.append((member_id, ws))
        for member_id, ws in dead:
            self.disconnect(member_id, ws)

    async def broadcast_room_closing(self, reason: str = "host-closed") -> None:
        await self.broadcast("room.closing", {"reason": reason})

    async def close_all(self) -> None:
        for member_id in list(self._connections):
            for ws in list(self._connections.get(member_id, ())):
                try:
                    await ws.close(code=1001, message=b"room closing")
                except Exception:
                    pass
        self._connections.clear()


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    """GET /room/v1/ws —— 认证后建立连接，发 snapshot，订阅广播。"""
    storage: RoomStorage = request.app["room_storage"]
    hub: ChatHub = request.app["chat_hub"]
    member = resolve_member(request)
    if member is None:
        raise web.HTTPUnauthorized(text="未登录或会话已失效")
    room = storage.get_room(request.app["room_id"])
    if room is None:
        raise web.HTTPNotFound(text="房间不存在")

    ws = web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)
    hub.connect(int(member["id"]), ws)
    storage.touch_last_seen(int(member["id"]))

    # 接入即推全量快照（房间信息 + 成员 + 最近消息）
    snapshot = room_snapshot(storage, int(room["id"]), int(member["id"]))
    try:
        await ws.send_str(__import__("json").dumps(
            ws_packet("room.snapshot", snapshot), ensure_ascii=False, separators=(",", ":")
        ))
    except Exception:
        pass

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                data = msg.json() if isinstance(msg.data, str) and msg.data.strip() else {}
                if isinstance(data, dict) and data.get("action") == "ping":
                    await ws.send_str(__import__("json").dumps(
                        {"event": "pong", "data": {}}, separators=(",", ":")
                    ))
                # 其余动作一律走 REST（POST /room/v1/messages），WS 只读推送
            elif msg.type == web.WSMsgType.ERROR:
                log.warning("ws error for member %s", member["id"])
                break
    except Exception:
        log.info("ws connection dropped for member %s", member["id"], exc_info=True)
    finally:
        hub.disconnect(int(member["id"]), ws)
        storage.mark_disconnected(int(member["id"]))
        await hub.broadcast("member.left", member_public(member))
    return ws


async def notify_member_joined(request: web.Request, member_row: sqlite3.Row) -> None:
    hub: ChatHub = request.app.get("chat_hub")
    if hub is not None:
        await hub.broadcast("member.joined", member_public(member_row))


async def notify_member_left(request: web.Request, member_row: sqlite3.Row) -> None:
    hub: ChatHub = request.app.get("chat_hub")
    if hub is not None:
        await hub.broadcast("member.left", member_public(member_row))


async def notify_message_created(request: web.Request, message_row: sqlite3.Row) -> None:
    hub: ChatHub = request.app.get("chat_hub")
    storage: RoomStorage = request.app["room_storage"]
    if hub is not None:
        await hub.broadcast("message.created", message_payload(message_row, storage))


async def notify_message_deleted(request: web.Request, message_id: int) -> None:
    hub: ChatHub = request.app.get("chat_hub")
    if hub is not None:
        await hub.broadcast("message.deleted", {"id": message_id})

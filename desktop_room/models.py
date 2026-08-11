"""房间实体的安全序列化（对外只暴露白名单字段，绝不暴露哈希/路径细节）。

所有 URL 均为房间网关相对路径（/room/v1/...）；不暴露本地绝对路径。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import protocol
from .storage import RoomStorage


def member_public(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "displayName": row["display_name"],
        "role": row["role"],
        "joinedAt": row["joined_at"],
        "kicked": row["kicked_at"] is not None,
    }


def _attachment_public(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "mimeType": row["mime_type"],
        "size": row["size"],
        "width": row["width"],
        "height": row["height"],
        "url": f"/room/v1/attachments/{row['id']}",
        "thumbUrl": f"/room/v1/attachments/{row['id']}/thumb" if row["thumbnail_relpath"] else None,
        "createdAt": row["created_at"],
    }


def message_public(row: sqlite3.Row, attachments: list[sqlite3.Row] | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": row["id"],
        "type": row["type"],
        "senderId": row["sender_id"],
        "replyToId": row["reply_to_id"],
        "generationJobId": row["generation_job_id"],
        "createdAt": row["created_at"],
    }
    if row["text"] is not None:
        data["text"] = row["text"]
    if row["client_message_id"]:
        data["clientMessageId"] = row["client_message_id"]
    if attachments is not None:
        data["attachments"] = [_attachment_public(a) for a in attachments]
    return data


def job_public(row: sqlite3.Row) -> dict[str, Any]:
    from .protocol import loads_json
    data: dict[str, Any] = {
        "id": row["id"],
        "memberId": row["member_id"],
        "clientRequestId": row["client_request_id"],
        "state": row["state"],
        "queuePosition": row["queue_position"],
        "createdAt": row["created_at"],
        "completedAt": row["completed_at"],
    }
    snapshot = loads_json(row["request_snapshot_json"], None)
    if isinstance(snapshot, dict):
        data["request"] = snapshot
    return data


def room_snapshot(storage: RoomStorage, room_id: int, viewer_member_id: int) -> dict[str, Any]:
    """WS 事件 room.snapshot 的负载：房间信息 + 在线成员 + 最近消息。"""
    room = storage.get_room(room_id)
    settings = storage.room_settings_map(room_id)
    members = [member_public(m) for m in storage.active_members(room_id)]
    recent = storage.list_messages(room_id=room_id, before=None, limit=50)
    recent.reverse()
    messages = [message_public(m, storage.attachments_for_message(m["id"])) for m in recent]
    return {
        "room": {
            "id": room["public_id"],
            "name": room["name"],
            "state": room["state"],
            "ownerName": room["owner_name"],
            "createdAt": room["created_at"],
            "viewerMemberId": viewer_member_id,
        },
        "settings": settings,
        "members": members,
        "messages": messages,
        "capabilities": _capabilities(settings),
    }


def _capabilities(settings: dict[str, Any]) -> list[str]:
    caps = []
    if settings.get("allowGeneration"):
        caps.append(protocol.CAP_TXT2IMG)
    if settings.get("allowImg2img"):
        caps.append(protocol.CAP_IMG2IMG)
    if settings.get("allowPromptEnhancement"):
        caps.append(protocol.CAP_PROMPT_ENHANCEMENT)
    if settings.get("allowUpload"):
        caps.append(protocol.CAP_UPLOADS)
    return caps


def message_payload(row: sqlite3.Row, storage: RoomStorage) -> dict[str, Any]:
    return message_public(row, storage.attachments_for_message(row["id"]))

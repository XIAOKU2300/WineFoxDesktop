"""房间网关协议常量与 JSON 校验。

约定：
- 图片从不以 Base64 走 WebSocket，只发送附件 ID 与授权 URL。
- 客户端请求幂等 ID：``room:<room_public_id>:member:<member_id>:<uuid>``。
"""

from __future__ import annotations

import json
from typing import Any

# 房间网关内部协议版本（独立于主 sidecar 协议）。
ROOM_PROTOCOL_VERSION = 1

# ---------------------------------------------------------------------------
# WebSocket 事件（服务端 → 客户端）
# ---------------------------------------------------------------------------
WS_EVENTS = (
    "room.snapshot",
    "member.joined",
    "member.left",
    "message.created",
    "message.deleted",
    "job.queued",
    "job.progress",
    "job.completed",
    "job.failed",
    "room.catalog_changed",
    "room.settings_changed",
    "room.closing",
    "room.error",
)

# 消息类型
MSG_TEXT = "text"
MSG_IMAGE = "image"
MSG_GENERATION_REQUEST = "generation_request"
MSG_GENERATION_RESULT = "generation_result"
MSG_GENERATION_FAILED = "generation_failed"
MSG_MEMBER_JOINED = "member_joined"
MSG_MEMBER_LEFT = "member_left"
MSG_SYSTEM = "system"
MESSAGE_TYPES = {
    MSG_TEXT,
    MSG_IMAGE,
    MSG_GENERATION_REQUEST,
    MSG_GENERATION_RESULT,
    MSG_GENERATION_FAILED,
    MSG_MEMBER_JOINED,
    MSG_MEMBER_LEFT,
    MSG_SYSTEM,
}

# 角色
ROLE_OWNER = "owner"
ROLE_GUEST = "guest"

# 任务状态（房间内任务的生命周期）
JOB_STATE_QUEUED = "queued"
JOB_STATE_RUNNING = "running"
JOB_STATE_COMPLETED = "completed"
JOB_STATE_FAILED = "failed"
JOB_STATE_CANCELLED = "cancelled"
JOB_STATES = {JOB_STATE_QUEUED, JOB_STATE_RUNNING, JOB_STATE_COMPLETED, JOB_STATE_FAILED, JOB_STATE_CANCELLED}

# 房间状态（对外广告）
ROOM_STATE_CREATED = "created"
ROOM_STATE_STARTING = "starting"
ROOM_STATE_ONLINE = "online"
ROOM_STATE_CLOSING = "closing"
ROOM_STATE_CLOSED = "closed"
ROOM_STATE_FAILED = "failed"

# 能力标志（GET /room/v1/catalog 的 capabilities）
CAP_TXT2IMG = "txt2img"
CAP_IMG2IMG = "img2img"
CAP_PROMPT_ENHANCEMENT = "promptEnhancement"
CAP_VISION_ANALYSIS = "visionAnalysis"
CAP_UPLOADS = "uploads"


def validate_json(value: Any) -> str:
    """验证对象可 JSON 序列化并返回紧凑字符串（写入 request_snapshot_json 等列）。"""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:  # pragma: no cover - 防御
        raise ValueError(f"不可 JSON 序列化: {exc}")


def loads_json(raw: str | None, default: Any = None) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def make_client_request_id(room_public_id: str, member_id: int, uuid_hex: str) -> str:
    """任务幂等 ID：room:<room_public_id>:member:<member_id>:<uuid>。"""
    return f"room:{room_public_id}:member:{member_id}:{uuid_hex}"


def parse_client_request_id(client_request_id: str) -> tuple[str | None, int | None]:
    """解析幂等 ID，返回 (room_public_id, member_id)；格式不合法返回 (None, None)。"""
    try:
        parts = client_request_id.split(":")
        if len(parts) != 5 or parts[0] != "room" or parts[2] != "member":
            return None, None
        return parts[1], int(parts[3])
    except (ValueError, IndexError):
        return None, None

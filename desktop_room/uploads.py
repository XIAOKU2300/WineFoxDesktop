"""房间图片上传与附件下载（魔数校验、EXIF 清理、缩略图、会话授权）。

规范：
- 单个文件 ≤ 20MB；尺寸 ≤ 8192×8192；魔数校验 + Pillow 解码 + 防解压炸弹。
- EXIF 修正方向并清理；生成缩略图（最长边 512，PNG）。
- 附件文件相对路径保存（相对房间数据目录），对外只暴露附件 ID 与授权 URL，
  绝不通过 WebSocket 发送 Base64 图片。
- 上传后以一条消息（type=image）的多个附件形式落库并广播 message.created。
- 限速 10 次/分钟/成员。
"""

from __future__ import annotations

import hashlib
import io
import secrets
import time
from pathlib import Path
from typing import Any

from aiohttp import web
from PIL import Image, ImageOps

from . import protocol
from .auth import error_response, resolve_member
from .models import message_payload
from .storage import RoomStorage
from .websocket import notify_message_created

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MiB
MAX_IMAGE_DIMENSION = 8192
THUMB_MAX_SIDE = 512
# 防解压炸弹：Pillow 在像素数超过该阈值时抛 DecompressionBombError。
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_DIMENSION * MAX_IMAGE_DIMENSION * 2

_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_UPLOAD_LIMIT_PER_MIN = 10


class ImageErrorDetail(Exception):
    def __init__(self, message: str, error_code: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.status = status


def _sniff_mime(head: bytes) -> str | None:
    """按魔数识别真实 MIME，拒绝伪造 Content-Type。"""
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _process_image(raw: bytes) -> tuple[str, bytes, bytes, int, int]:
    """校验并清洗图片，返回 (mime, 主图字节, 缩略图字节, width, height)。

    非 JPEG 一律重编码为 PNG 存储（剥离元数据）；JPEG 以 quality=92 重编码
    并丢弃 EXIF；方向通过 exif_transpose 纠正。
    """
    mime = _sniff_mime(raw[:12])
    if mime is None:
        raise ImageErrorDetail("不支持的图片格式，仅支持 JPG/PNG/WebP/GIF",
                               "UNSUPPORTED_IMAGE")
    try:
        with Image.open(io.BytesIO(raw)) as img:
            if img.width > MAX_IMAGE_DIMENSION or img.height > MAX_IMAGE_DIMENSION:
                raise ImageErrorDetail(
                    f"图片尺寸不能超过 {MAX_IMAGE_DIMENSION}×{MAX_IMAGE_DIMENSION}",
                    "IMAGE_TOO_LARGE")
            img.load()
            transposed = ImageOps.exif_transpose(img)
            if transposed is not img:
                img = transposed
            width, height = img.size

            main_buf = io.BytesIO()
            if mime == "image/jpeg":
                img.convert("RGB").save(main_buf, "JPEG", quality=92)
            else:
                img.save(main_buf, "PNG")
            main_data = main_buf.getvalue()

            thumb = img.copy()
            thumb.thumbnail((THUMB_MAX_SIDE, THUMB_MAX_SIDE))
            thumb_buf = io.BytesIO()
            thumb.save(thumb_buf, "PNG")
            thumb_data = thumb_buf.getvalue()

            if mime != "image/jpeg":
                mime = "image/png"
            return mime, main_data, thumb_data, width, height
    except Image.DecompressionBombError:
        raise ImageErrorDetail("图片像素量异常，疑似解压炸弹", "IMAGE_PIXELS_TOO_LARGE")
    except ImageErrorDetail:
        raise
    except Exception:
        raise ImageErrorDetail("图片解析失败，请确认是有效的图片文件", "INVALID_IMAGE")


def _store_file(data_dir: Path, mime: str, data: bytes) -> str:
    """写入主图，返回相对路径（相对 data_dir）。文件名随机。"""
    name = f"{secrets.token_hex(16)}{_EXT[mime]}"
    rel = f"attachments/{name}"
    target = data_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return rel


def _store_thumb(data_dir: Path, data: bytes) -> str:
    name = f"{secrets.token_hex(16)}_thumb.png"
    rel = f"attachments/{name}"
    target = data_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return rel


class _UploadRateBucket:
    """进程内成员上传限速（固定窗口计数：10 次 / 60 秒）。"""

    def __init__(self) -> None:
        self._hits: dict[int, list[float]] = {}

    def try_acquire(self, member_id: int, now: float) -> bool:
        hits = self._hits.get(member_id)
        if hits is None:
            self._hits[member_id] = [now]
            return True
        hits = [t for t in hits if now - t < 60.0]
        self._hits[member_id] = hits
        if len(hits) >= _UPLOAD_LIMIT_PER_MIN:
            return False
        hits.append(now)
        return True


async def _read_part_limited(part: Any, limit: int) -> bytes | None:
    """分块读取文件部件，超过 limit 字节返回 None（调用方据此返回 413）。"""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await part.read_chunk(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def _upload_handler(request: web.Request) -> web.Response:
    app = request.app
    storage: RoomStorage = app["room_storage"]
    room_id = int(app["room_id"])
    member = resolve_member(request)
    if member is None:
        return error_response("未登录或会话已失效", "AUTH_REQUIRED", 401)
    room = storage.get_room(room_id)
    if room is None:
        return error_response("房间不存在", "ROOM_NOT_FOUND", 404)
    if room["state"] != protocol.ROOM_STATE_ONLINE:
        return error_response("房间已关闭", "ROOM_CLOSED", 409)
    if not storage.room_settings_map(room_id).get("allowUpload"):
        return error_response("房主已关闭图片上传", "UPLOAD_DISABLED", 403)

    member_id = int(member["id"])
    limiter: _UploadRateBucket = app["upload_limiter"]
    data_dir = Path(app["data_dir"])

    fields: dict[str, str] = {}
    files: list[tuple[str, bytes, bytes, int, int]] = []
    try:
        reader = await request.multipart()
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.filename is None:
                name = (part.name or "").strip()
                raw = await _read_part_limited(part, 2048)
                if raw is None:
                    return error_response("表单字段过长", "BAD_REQUEST", 400)
                try:
                    fields[name] = raw.decode("utf-8")
                except UnicodeDecodeError:
                    return error_response("表单字段编码无效", "BAD_REQUEST", 400)
                continue
            if not part.filename:
                continue
            now = time.monotonic()
            if not limiter.try_acquire(member_id, now):
                return error_response("上传过于频繁，请稍后再试", "UPLOAD_RATE_LIMITED", 429)
            raw = await _read_part_limited(part, MAX_UPLOAD_BYTES)
            if raw is None:
                return error_response("单个图片不能超过 20MB", "FILE_TOO_LARGE", 413)
            if not raw:
                return error_response("空文件", "EMPTY_FILE", 400)
            try:
                files.append(_process_image(raw))
            except ImageErrorDetail as exc:
                return error_response(exc.message, exc.error_code, exc.status)
    except (ValueError, TypeError):
        return error_response("multipart 解析失败", "BAD_REQUEST", 400)

    if not files:
        return error_response("未收到图片文件", "NO_FILE", 400)

    client_message_id = fields.get("clientMessageId") or None
    if client_message_id is not None and len(client_message_id) > 128:
        return error_response("clientMessageId 不合法", "BAD_REQUEST", 400)
    reply_to_id = fields.get("replyToId")
    if reply_to_id is not None:
        try:
            reply_to_id = int(reply_to_id)
        except (TypeError, ValueError):
            reply_to_id = None

    message_id = storage.add_message(
        room_id=room_id,
        sender_id=member_id,
        msg_type=protocol.MSG_IMAGE,
        text=None,
        client_message_id=client_message_id,
        reply_to_id=reply_to_id,
    )
    attachment_ids: list[int] = []
    for mime, main_data, thumb_data, width, height in files:
        rel = _store_file(data_dir, mime, main_data)
        thumb_rel = _store_thumb(data_dir, thumb_data)
        aid = storage.add_attachment(
            room_id=room_id,
            uploader_id=member_id,
            message_id=message_id,
            kind="image",
            relative_path=rel,
            thumbnail_relpath=thumb_rel,
            mime_type=mime,
            size=len(main_data),
            width=width,
            height=height,
            sha256=hashlib.sha256(main_data).hexdigest(),
        )
        attachment_ids.append(aid)

    row = storage.get_message(message_id)
    await notify_message_created(request, row)
    return web.json_response({"message": message_payload(row, storage)}, status=201)


async def _attachment_get_handler(request: web.Request, *, thumb: bool = False) -> web.Response:
    app = request.app
    storage: RoomStorage = app["room_storage"]
    member = resolve_member(request)
    if member is None:
        return error_response("未登录或会话已失效", "AUTH_REQUIRED", 401)
    try:
        attachment_id = int(request.match_info["id"])
    except (TypeError, ValueError, KeyError):
        return error_response("附件 ID 无效", "BAD_REQUEST", 400)
    row = storage.get_attachment(attachment_id)
    if row is None or int(row["room_id"]) != int(app["room_id"]):
        return error_response("附件不存在", "ATTACHMENT_NOT_FOUND", 404)
    rel = row["thumbnail_relpath"] if thumb else row["relative_path"]
    if not rel:
        return error_response("该附件没有缩略图", "NOT_FOUND", 404)
    path = Path(app["data_dir"]) / rel
    if not path.is_file():
        return error_response("附件文件缺失", "ATTACHMENT_NOT_FOUND", 404)
    content_type = "image/png" if thumb else row["mime_type"]
    return web.FileResponse(path, headers={
        "Content-Type": content_type,
        "X-Content-Type-Options": "nosniff",
    })


def install_upload_routes(app: web.Application) -> None:
    from functools import partial

    app["upload_limiter"] = _UploadRateBucket()
    app.router.add_post("/room/v1/uploads", _upload_handler)
    app.router.add_get("/room/v1/attachments/{id}",
                       partial(_attachment_get_handler, thumb=False))
    app.router.add_get("/room/v1/attachments/{id}/thumb",
                       partial(_attachment_get_handler, thumb=True))

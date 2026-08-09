"""上传处理：multipart 解析 + 大小限制 + 解码校验。

- 默认上限 20MB（可注入），Content-Length 与读取过程双重限制；
- 图片必须通过魔数 + PIL 解码校验（损坏/伪造直接 400）；
- 只返回 upload ID，绝不返回绝对路径。
"""

from __future__ import annotations

import io
import logging
import secrets
from typing import Optional

from aiohttp import web

from .errors import AppError, ValidationError

logger = logging.getLogger("winefox.uploads")

_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),  # 需要进一步校验 WEBP 标记
    (b"BM", "image/bmp"),
)

_VIDEO_MAGIC = (
    (b"\x00\x00\x00\x18ftyp", "video/mp4"),
    (b"\x00\x00\x00\x20ftyp", "video/mp4"),
    (b"ftyp", "video/mp4"),
    (b"\x1a\x45\xdf\xa3", "video/webm"),
)

SUPPORTED_KINDS = {"image", "video", "generic"}

CHUNK = 64 * 1024


def _sniff_content_type(data: bytes) -> Optional[str]:
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    for magic, ctype in _IMAGE_MAGIC:
        if data.startswith(magic):
            return ctype
    for magic, ctype in _VIDEO_MAGIC:
        if data.startswith(magic):
            return ctype
    return None


def _verify_image(data: bytes) -> None:
    """魔数 + PIL 解码校验。失败抛 AppError(400, upload_invalid)。"""
    ctype = _sniff_content_type(data)
    if ctype is None:
        raise AppError(400, "upload_invalid", "文件不是受支持的图片格式")
    try:
        from PIL import Image, UnidentifiedImageError

        with Image.open(io.BytesIO(data)) as img:
            img.verify()
        # verify() 之后 image 对象不可再用；重新打开确认可解码
        with Image.open(io.BytesIO(data)) as img:
            img.load()
    except ImportError:
        pass  # 无 PIL 时仅做魔数校验
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise AppError(400, "upload_invalid", "图片解码失败，文件可能已损坏") from exc


def _verify_video(data: bytes) -> None:
    if not any(data.startswith(m) for m, _ in _VIDEO_MAGIC):
        raise AppError(400, "upload_invalid", "文件不是受支持的视频格式")


async def _read_part_with_limit(part, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while not part.at_eof():
        chunk = await part.read_chunk(CHUNK)
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            raise AppError(413, "payload_too_large", "上传文件超过大小限制")
        chunks.append(chunk)
    return b"".join(chunks)


async def parse_upload(request: web.Request, max_upload_bytes: int) -> dict:
    """解析 multipart 上传，返回 {"uploadId", "filename", "contentType", "bytes", "kind"}。"""
    content_type = request.headers.get("Content-Type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise AppError(415, "unsupported_media_type", "上传需要 multipart/form-data")

    cl = request.headers.get("Content-Length")
    if cl and cl.isdigit() and int(cl) > max_upload_bytes:
        raise AppError(413, "payload_too_large", "上传文件超过大小限制")

    reader = await request.multipart()
    # 注意：aiohttp 的 multipart reader 前进时会丢弃当前 part 未读内容，
    # 因此 file 内容必须在循环内就地读走，不能保存 part 留到循环后再读。
    file_data: bytes | None = None
    file_name = "upload.bin"
    file_ct = ""
    kind = "image"
    async for part in reader:
        name = (part.name or "").strip()
        if name == "file" and file_data is None:
            file_data = await _read_part_with_limit(part, max_upload_bytes)
            file_name = part.filename or file_name
            file_ct = (part.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        elif name == "kind":
            try:
                kind = (await part.read()).decode("utf-8", "replace").strip().lower()
            except Exception:
                kind = "image"
            if kind not in SUPPORTED_KINDS:
                raise ValidationError("kind 仅支持 image/video/generic")
        else:
            try:
                await part.read()  # 丢弃未知字段（受 client_max_size 约束）
            except Exception:
                pass

    if file_data is None:
        raise AppError(400, "upload_no_file", "缺少 file 字段")
    if not file_data:
        raise AppError(400, "upload_empty", "上传内容为空")

    if kind == "image":
        _verify_image(file_data)
        sniffed = _sniff_content_type(file_data)
        if sniffed:
            file_ct = sniffed
    elif kind == "video":
        _verify_video(file_data)
        sniffed = _sniff_content_type(file_data)
        if sniffed:
            file_ct = sniffed
    else:
        file_ct = file_ct or "application/octet-stream"

    upload_id = secrets.token_hex(16)  # 128 位随机 ID，不暴露路径
    return {
        "uploadId": upload_id,
        "filename": file_name,
        "contentType": file_ct,
        "bytes": len(file_data),
        "kind": kind,
        "data": file_data,
    }

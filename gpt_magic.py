import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import mimetypes
import os
import random
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

import aiohttp
from aiohttp import web

from astrbot.api import logger
try:
    from .watermark import DEFAULT_WATERMARK_PATH, apply_watermark_to_image_file
except ImportError:
    from watermark import DEFAULT_WATERMARK_PATH, apply_watermark_to_image_file


PLUGIN_ROOT = Path(__file__).resolve().parent
WEBUI_ASSETS_DIR = PLUGIN_ROOT / "assets"
POSE_PRESET_MAX_BYTES = 180_000
GALLERY_IMPORT_MAX_BYTES = 30 * 1024 * 1024
GALLERY_DEFAULT_ADMIN_USERNAME = "admin"
GALLERY_PASSWORD_ALGO = "pbkdf2_sha256"
GALLERY_PASSWORD_ITERATIONS = 210_000
GALLERY_SESSION_MAX_MS = 24 * 60 * 60 * 1000
GALLERY_LOGIN_WINDOW_MS = 15 * 60 * 1000
GALLERY_LOGIN_LOCK_MS = 10 * 60 * 1000
GALLERY_LOGIN_LOCK_THRESHOLD = 5
GALLERY_REGISTRATION_TOKEN_TTL_MS = 15 * 60 * 1000
GALLERY_PUBLIC_URL = ""

# 视频额度（钱包）相关常量
GALLERY_DEFAULT_TRIAL_BALANCE = 5.0          # 新老用户激活后默认试用额度（元）
GALLERY_BALANCE_MAX = 1_000_000.0            # 单账户余额上限，防止溢出/误操作
GALLERY_BALANCE_LEDGER_LIMIT = 200           # 每个账户保留的最近消费/充值流水条数


def _safe_money(value: Any, default: float = 0.0, minimum: float = None, maximum: float = None) -> float:
    """把任意输入安全转换为两位小数的金额（元）。"""
    try:
        result = round(float(value), 2)
    except (TypeError, ValueError):
        result = round(float(default), 2)
    if minimum is not None and result < minimum:
        result = round(float(minimum), 2)
    if maximum is not None and result > maximum:
        result = round(float(maximum), 2)
    return result


def _now_ms() -> int:
    return int(time.time() * 1000)


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base or {})
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _safe_int(value: Any, default: int, minimum: int = None, maximum: int = None) -> int:
    try:
        num = int(value)
    except Exception:
        num = default
    if minimum is not None:
        num = max(minimum, num)
    if maximum is not None:
        num = min(maximum, num)
    return num


def _normalize_api_video_aspect_ratio(value: Any) -> str:
    text = str(value or "auto").strip().lower()
    if text in ("16:9", "landscape", "horizontal"):
        return "16:9"
    if text in ("9:16", "portrait", "vertical"):
        return "9:16"
    return "auto"


def _mask_secret(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}***{text[-4:]}"


def _html_escape(value: Any) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _looks_masked(value: str) -> bool:
    text = str(value or "")
    return "***" in text or (text and set(text) == {"*"})


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except Exception:
        return str(value)


def _format_template(text: str, **values) -> str:
    template = str(text or "").strip()
    if not template:
        return ""
    safe_values = {key: str(value) for key, value in values.items()}
    class SafeDict(dict):
        def __missing__(self, key):
            return ""
    try:
        return template.format_map(SafeDict(safe_values))
    except Exception:
        return template


def _default_config(schema_conf: dict = None) -> dict:
    schema_conf = schema_conf or {}
    draw_conf = schema_conf.get("draw", {}) if isinstance(schema_conf.get("draw"), dict) else {}
    cache_conf = schema_conf.get("cache", {}) if isinstance(schema_conf.get("cache"), dict) else {}
    webui_conf = schema_conf.get("webui", {}) if isinstance(schema_conf.get("webui"), dict) else {}
    gallery_conf = schema_conf.get("gallery", {}) if isinstance(schema_conf.get("gallery"), dict) else {}
    return {
        "enabled": bool(schema_conf.get("enabled", True)),
        "draw_enabled": bool(schema_conf.get("draw_enabled", True)),
        "draw_close_reason": str(schema_conf.get("draw_close_reason", "") or ""),
        "default_quota": _safe_int(schema_conf.get("default_quota", 2), 2, 0),
        "max_queue_size": _safe_int(schema_conf.get("max_queue_size", 100), 100, 1),
        "max_concurrent_tasks": _safe_int(schema_conf.get("max_concurrent_tasks", 2), 2, 1, 8),
        "history_limit": _safe_int(schema_conf.get("history_limit", 300), 300, 10),
        "result_image_in_forward": bool(schema_conf.get("result_image_in_forward", True)),
        "send_task_received_message": bool(schema_conf.get("send_task_received_message", True)),
        "task_received_message": str(
            schema_conf.get("task_received_message", "🪄 已收到 GPT 魔法任务，当前位置：{position}")
            or ""
        ),
        "send_processing_message": bool(schema_conf.get("send_processing_message", True)),
        "processing_message": str(
            schema_conf.get("processing_message", "🪄 GPT 魔法正在生成图片，请稍等...")
            or ""
        ),
        "edit_processing_message": str(
            schema_conf.get("edit_processing_message", "🪄 GPT 魔法正在编辑图片，请稍等...")
            or ""
        ),
        "webui": {
            "enabled": bool(webui_conf.get("enabled", True)),
            "host": str(webui_conf.get("host", "0.0.0.0") or "0.0.0.0"),
            "port": _safe_int(webui_conf.get("port", 1144), 1144, 1, 65535),
            "token": str(webui_conf.get("token", "") or ""),
        },
        "gallery": {
            "enabled": bool(gallery_conf.get("enabled", True)),
            "host": str(gallery_conf.get("host", "0.0.0.0") or "0.0.0.0"),
            "port": _safe_int(gallery_conf.get("port", 6666), 6666, 1, 65535),
            "name": str(gallery_conf.get("name", "WineFox") or "WineFox"),
            "public_url": str(gallery_conf.get("public_url", GALLERY_PUBLIC_URL) or GALLERY_PUBLIC_URL),
            "admin_username": str(gallery_conf.get("admin_username", GALLERY_DEFAULT_ADMIN_USERNAME) or GALLERY_DEFAULT_ADMIN_USERNAME),
            "admin_password": str(gallery_conf.get("admin_password", "") or ""),
            "limit": _safe_int(gallery_conf.get("limit", 900), 900, 1, 3000),
            "draw_history_ttl_hours": _safe_int(gallery_conf.get("draw_history_ttl_hours", 24), 24, 1, 720),
            "video_local_price": _safe_money(gallery_conf.get("video_local_price", 0.5), 0.5, 0.0, GALLERY_BALANCE_MAX),
            "video_api_price": _safe_money(gallery_conf.get("video_api_price", 0.5), 0.5, 0.0, GALLERY_BALANCE_MAX),
            "video_trial_balance": _safe_money(gallery_conf.get("video_trial_balance", 5.0), 5.0, 0.0, GALLERY_BALANCE_MAX),
        },
        "draw": {
            "base_url": str(draw_conf.get("base_url", "") or "").rstrip("/"),
            "api_url": str(draw_conf.get("api_url", "") or ""),
            "edit_api_url": str(draw_conf.get("edit_api_url", "") or ""),
            "api_key": str(draw_conf.get("api_key", "") or ""),
            "model": str(draw_conf.get("model", "gpt-image-2") or "gpt-image-2"),
            "model_2k": str(draw_conf.get("model_2k", "gpt-image-2-2k") or ""),
            "model_4k": str(draw_conf.get("model_4k", "gpt-image-2-4k") or ""),
            "auto_model_by_resolution": bool(draw_conf.get("auto_model_by_resolution", True)),
            "timeout_sec": _safe_int(draw_conf.get("timeout_sec", 600), 600, 1),
            "quality": str(draw_conf.get("quality", "high") or "high"),
            "background": str(draw_conf.get("background", "auto") or "auto"),
            "moderation": str(draw_conf.get("moderation", "auto") or "auto"),
            "output_format": str(draw_conf.get("output_format", "png") or "png"),
            "output_compression": _safe_int(draw_conf.get("output_compression", 100), 100, 0, 100),
            "stream": bool(draw_conf.get("stream", True)),
            "aspect_strategy": str(draw_conf.get("aspect_strategy", "auto") or "auto"),
            "default_resolution": _normalize_resolution(draw_conf.get("default_resolution", "4k"), "4k"),
            "enable_prompt_aspect_detect": bool(draw_conf.get("enable_prompt_aspect_detect", True)),
            "enable_custom_size": bool(draw_conf.get("enable_custom_size", False)),
            "custom_size": str(draw_conf.get("custom_size", "") or ""),
            "input_fidelity": str(draw_conf.get("input_fidelity", "high") or "high"),
            "extra_body": draw_conf.get("extra_body", {}) if isinstance(draw_conf.get("extra_body", {}), dict) else {},
        },
        "cache": {
            "enabled": bool(cache_conf.get("enabled", True)),
            "ttl_sec": _safe_int(cache_conf.get("ttl_sec", 604800), 604800, 0),
            "max_entries": _safe_int(cache_conf.get("max_entries", 500), 500, 1),
            "consume_quota_on_hit": bool(cache_conf.get("consume_quota_on_hit", True)),
        },
        "stats": {
            "total_users": 0,
            "total_images": 0,
            "total_failed": 0,
            "total_cache_hit": 0,
            "total_consumed_quota": 0,
            "total_redeemed": 0,
            "updated_at": _now_ms(),
        },
    }


def _parse_size(value: str) -> Optional[Tuple[int, int]]:
    match = re.match(r"^\s*(\d{2,5})\s*[xX]\s*(\d{2,5})\s*$", str(value or ""))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _normalize_size(value: str) -> str:
    parsed = _parse_size(value)
    if not parsed:
        return str(value or "").strip()
    return f"{parsed[0]}x{parsed[1]}"


GPT_IMAGE_SIZE_MAP = {
    "1k": {
        "1:1": "1024x1024",
        "3:2": "1536x1024",
        "2:3": "1024x1536",
        "4:3": "1024x768",
        "3:4": "768x1024",
        "5:4": "1280x1024",
        "4:5": "1024x1280",
        "16:9": "1536x864",
        "9:16": "864x1536",
        "2:1": "2048x1024",
        "1:2": "1024x2048",
        "3:1": "1536x512",
        "1:3": "512x1536",
        "21:9": "2016x864",
        "9:21": "864x2016",
    },
    "2k": {
        "1:1": "2048x2048",
        "3:2": "2048x1360",
        "2:3": "1360x2048",
        "4:3": "2048x1536",
        "3:4": "1536x2048",
        "5:4": "2560x2048",
        "4:5": "2048x2560",
        "16:9": "2048x1152",
        "9:16": "1152x2048",
        "2:1": "2688x1344",
        "1:2": "1344x2688",
        "3:1": "3072x1024",
        "1:3": "1024x3072",
        "21:9": "2688x1152",
        "9:21": "1152x2688",
    },
    "4k": {
        "1:1": "2880x2880",
        "3:2": "3520x2336",
        "2:3": "2336x3520",
        "4:3": "3312x2480",
        "3:4": "2480x3312",
        "5:4": "3216x2576",
        "4:5": "2576x3216",
        "16:9": "3840x2160",
        "9:16": "2160x3840",
        "2:1": "3840x1920",
        "1:2": "1920x3840",
        "3:1": "3840x1280",
        "1:3": "1280x3840",
        "21:9": "3840x1648",
        "9:21": "1648x3840",
    },
}

GPT_ASPECT_PRESETS = {
    "square": ("1:1", None),
    "portrait": ("2:3", None),
    "story": ("9:16", None),
    "landscape": ("3:2", None),
    "wide": ("16:9", None),
    "square_1k": ("1:1", "1k"),
    "portrait_1k": ("2:3", "1k"),
    "story_1k": ("9:16", "1k"),
    "landscape_1k": ("3:2", "1k"),
    "wide_1k": ("16:9", "1k"),
    "square_2k": ("1:1", "2k"),
    "portrait_2k": ("2:3", "2k"),
    "story_2k": ("9:16", "2k"),
    "landscape_2k": ("16:9", "2k"),
    "wide_2k": ("16:9", "2k"),
    "square_4k": ("1:1", "4k"),
    "square_max": ("1:1", "4k"),
    "uhd_square": ("1:1", "4k"),
    "portrait_4k": ("2:3", "4k"),
    "story_4k": ("9:16", "4k"),
    "landscape_4k": ("16:9", "4k"),
    "wide_4k": ("16:9", "4k"),
    "uhd_portrait": ("9:16", "4k"),
    "uhd_landscape": ("16:9", "4k"),
}


def _normalize_resolution(value: str, default: str = "4k") -> str:
    text = str(value or "").strip().lower().replace(" ", "")
    if text in GPT_IMAGE_SIZE_MAP:
        return text
    if text in {"max", "uhd", "ultra", "best", "highest", "最高", "最优"}:
        return "4k"
    return default if default in GPT_IMAGE_SIZE_MAP else "4k"


def _detect_prompt_resolution(prompt: str, default: str = "4k") -> str:
    text = str(prompt or "").lower()
    if re.search(r"(1\s*k|1k|1024\s*[xX×]\s*1024|普通分辨率|低分辨率)", text):
        return "1k"
    if re.search(r"(2\s*k|2k|2048|二\s*k|两\s*k)", text):
        return "2k"
    if re.search(r"(4\s*k|4k|uhd|ultra\s*hd|3840|2160|超清|超高清|高分辨率|高清大图|最高画质|最高清晰度)", text):
        return "4k"
    return _normalize_resolution(default, "4k")


def _normalize_ratio(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("：", ":").replace("×", "x").replace("X", "x")
    match = re.match(r"^\s*(\d{1,2})\s*[:x]\s*(\d{1,2})\s*$", text)
    if not match:
        return ""
    ratio = f"{int(match.group(1))}:{int(match.group(2))}"
    return ratio if ratio in GPT_IMAGE_SIZE_MAP["1k"] else ""


def _detect_prompt_ratio(prompt: str) -> tuple:
    text = str(prompt or "")
    lowered = text.lower()
    for match in re.finditer(r"(\d{1,2})\s*[:：xX×]\s*(\d{1,2})", text):
        ratio = _normalize_ratio(match.group(0))
        if ratio:
            return ratio, "explicit_ratio"
    if re.search(r"(方图|方形|方版|正方形|头像|封面头像|square)", lowered):
        return "1:1", "keyword_square"
    if re.search(r"(短视频|抖音|快手|小红书视频|reels|tiktok|story|手机全屏|竖屏壁纸|手机壁纸|9\s*[:：xX×]\s*16)", lowered):
        return "9:16", "keyword_story"
    if re.search(r"(竖图|竖版|竖屏|立绘|全身照|角色海报|人物海报|portrait|poster)", lowered):
        return "2:3", "keyword_portrait"
    if re.search(r"(小红书封面|杂志封面|封面竖图|3\s*[:：xX×]\s*4)", lowered):
        return "3:4", "keyword_cover"
    if re.search(r"(宽屏|宽图|电影感|横幅|banner|电脑壁纸|桌面壁纸|wide|cinematic|16\s*[:：xX×]\s*9)", lowered):
        return "16:9", "keyword_wide"
    if re.search(r"(横图|横版|横屏|横构图|风景横图|landscape)", lowered):
        return "16:9", "keyword_landscape"
    return "1:1", "default_square"


def _ratio_from_size(size: str) -> str:
    parsed = _parse_size(size)
    if not parsed:
        return ""
    width, height = parsed
    if not width or not height:
        return ""
    best_ratio = ""
    best_delta = 999.0
    actual = width / height
    for ratio in GPT_IMAGE_SIZE_MAP["1k"]:
        left, right = [int(x) for x in ratio.split(":")]
        delta = abs(actual - (left / right))
        if delta < best_delta:
            best_delta = delta
            best_ratio = ratio
    return best_ratio


def _resolution_from_size(size: str) -> str:
    parsed = _parse_size(size)
    if not parsed:
        return "custom"
    longest = max(parsed)
    if longest >= 2880:
        return "4k"
    if longest >= 1800:
        return "2k"
    return "1k"


def _keyword_aspect(prompt: str) -> str:
    ratio, _source = _detect_prompt_ratio(prompt)
    resolution = _detect_prompt_resolution(prompt, "4k")
    for preset, (preset_ratio, preset_resolution) in GPT_ASPECT_PRESETS.items():
        if preset_ratio == ratio and (preset_resolution or resolution) == resolution:
            return preset
    return "auto"


def _preset_to_size(name: str) -> str:
    value = str(name or "auto").lower()
    ratio, resolution = GPT_ASPECT_PRESETS.get(value, ("1:1", None))
    resolution = resolution or "4k"
    return GPT_IMAGE_SIZE_MAP.get(resolution, GPT_IMAGE_SIZE_MAP["4k"]).get(ratio, GPT_IMAGE_SIZE_MAP["4k"]["1:1"])


def _detect_image_meta(data: bytes) -> dict:
    meta = {"mime": "image/png", "ext": "png", "width": 0, "height": 0, "bytes": len(data or b"")}
    if not data or len(data) < 16:
        return meta
    if data.startswith(b"\x89PNG"):
        meta["mime"] = "image/png"
        meta["ext"] = "png"
        if len(data) >= 24:
            meta["width"] = int.from_bytes(data[16:20], "big")
            meta["height"] = int.from_bytes(data[20:24], "big")
        return meta
    if data.startswith(b"\xff\xd8"):
        meta["mime"] = "image/jpeg"
        meta["ext"] = "jpg"
        offset = 2
        while offset + 9 < len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            marker = data[offset + 1]
            if marker in (0xD9, 0xDA):
                break
            length = int.from_bytes(data[offset + 2:offset + 4], "big")
            if 0xC0 <= marker <= 0xC3 and length >= 7:
                meta["height"] = int.from_bytes(data[offset + 5:offset + 7], "big")
                meta["width"] = int.from_bytes(data[offset + 7:offset + 9], "big")
                break
            offset += 2 + max(length, 0)
        return meta
    if data.startswith(b"GIF"):
        meta["mime"] = "image/gif"
        meta["ext"] = "gif"
        if len(data) >= 10:
            meta["width"] = int.from_bytes(data[6:8], "little")
            meta["height"] = int.from_bytes(data[8:10], "little")
        return meta
    if len(data) >= 30 and data[8:12] == b"WEBP":
        meta["mime"] = "image/webp"
        meta["ext"] = "webp"
        if data[12:16] == b"VP8X":
            meta["width"] = 1 + int.from_bytes(data[24:27], "little")
            meta["height"] = 1 + int.from_bytes(data[27:30], "little")
    return meta


def _random_key() -> str:
    seed = secrets.token_hex(6).upper()
    return f"JHM-{seed[:4]}-{seed[4:8]}-{seed[8:12]}"


def _pick_image_object(data: Any) -> Any:
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for key in ("data", "output", "images", "results"):
            value = data.get(key)
            if isinstance(value, list) and value:
                for item in value:
                    picked = _pick_image_object(item)
                    if picked is not None:
                        return picked
            if isinstance(value, dict):
                picked = _pick_image_object(value)
                if picked is not None:
                    return picked
        if isinstance(data.get("content"), list):
            for item in data.get("content") or []:
                picked = _pick_image_object(item)
                if picked is not None:
                    return picked
        if any(
            k in data
            for k in (
                "b64_json",
                "base64_json",
                "base64",
                "image_base64",
                "image",
                "result",
                "url",
                "image_url",
            )
        ):
            return data
    if isinstance(data, list) and data:
        for item in data:
            picked = _pick_image_object(item)
            if picked is not None:
                return picked
    return None


def _try_decode_base64_image(value: Any) -> Optional[bytes]:
    text = str(value or "").strip()
    if not text or re.match(r"^https?://", text, re.I):
        return None
    raw = text.split(",", 1)[-1] if "," in text else text
    raw = re.sub(r"\s+", "", raw)
    if not raw:
        return None
    raw += "=" * (-len(raw) % 4)
    try:
        return base64.b64decode(raw, validate=True)
    except Exception:
        return None


def _decode_image_object(obj: Any) -> Optional[bytes]:
    if obj is None:
        return None
    if isinstance(obj, str):
        return _try_decode_base64_image(obj)
    if not isinstance(obj, dict):
        return None
    for key in ("b64_json", "base64_json", "base64", "image_base64", "image", "result"):
        if obj.get(key):
            value = obj.get(key)
            if isinstance(value, dict):
                nested = _decode_image_object(value)
                if nested:
                    return nested
                continue
            decoded = _try_decode_base64_image(value)
            if decoded:
                return decoded
    if isinstance(obj.get("image_url"), dict):
        return _decode_image_object(obj.get("image_url"))
    return None


def _stream_unsupported(status: int, text: str) -> bool:
    if int(status) not in {400, 404, 415, 422}:
        return False
    return bool(re.search(r"stream|partial_images|unsupported|unknown parameter|invalid.*parameter", text or "", re.I))


def _edit_retry_without_fidelity(status: int, text: str) -> bool:
    if int(status) not in {400, 404, 415, 422}:
        return False
    return bool(re.search(r"input_fidelity|fidelity|unsupported|unknown parameter|invalid.*parameter", text or "", re.I))


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _normalize_image_paths(paths) -> List[str]:
    if paths is None:
        return []
    if isinstance(paths, (str, Path)):
        raw = [str(paths)]
    elif isinstance(paths, (list, tuple, set)):
        raw = [str(x) for x in paths]
    else:
        raw = [str(paths)]
    result = []
    seen = set()
    for item in raw:
        item = str(item or "").strip()
        if not item:
            continue
        key = str(Path(item).expanduser())
        if key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def _guess_mime(path: str) -> str:
    mime = mimetypes.guess_type(str(path))[0]
    return mime or "image/png"


FAVORITE_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def _parse_sse_record(record: str) -> Optional[dict]:
    lines = []
    for line in str(record or "").splitlines():
        if line.startswith("data:"):
            lines.append(line[5:].strip())
    text = "\n".join(lines).strip()
    if not text or text == "[DONE]":
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


@dataclass
class GptMagicResult:
    image_bytes: bytes
    image_ext: str
    mime: str
    width: int
    height: int
    bytes_size: int
    raw: dict
    parameters: dict
    streamed: bool = False
    mode: str = "generation"


class GptMagicStore:
    def __init__(self, base_dir: Path, schema_conf: dict = None):
        self.base_dir = Path(base_dir) / "gpt_magic"
        self.cache_dir = self.base_dir / "cache"
        self.history_dir = self.base_dir / "history"
        self.file = self.base_dir / "db.json"
        self.builtin_defaults = _default_config({})
        self.schema_defaults = _default_config(schema_conf)
        self.data = self._load()
        self._sync_astrbot_config_overrides()
        self._ensure_token()
        self._ensure_gallery_admin_account()
        self.save()

    def ensure_dir(self):
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.history_dir.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict:
        self.ensure_dir()
        defaults = {
            "config": self.schema_defaults,
            "users": {},
            "keys": {},
            "history": [],
            "web_draw_history": [],
            "cache": {},
            "gallery_accounts": {},
            "gallery_access_log": [],
        }
        if not self.file.exists():
            return defaults
        try:
            loaded = json.loads(self.file.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                return defaults
            loaded["config"] = _deep_merge(self.schema_defaults, loaded.get("config", {}))
            loaded.setdefault("users", {})
            loaded.setdefault("keys", {})
            loaded.setdefault("history", [])
            loaded.setdefault("web_draw_history", [])
            loaded.setdefault("cache", {})
            loaded.setdefault("gallery_accounts", {})
            loaded.setdefault("gallery_access_log", [])
            loaded.setdefault("astrbot_config_snapshot", {})
            return loaded
        except Exception as e:
            logger.warning(f"[GPT Magic] 读取 db.json 失败，使用默认数据: {e}")
            return defaults

    def _iter_leaf_paths(self, value: Any, prefix: Tuple[str, ...] = ()):
        if isinstance(value, dict):
            for key, item in value.items():
                yield from self._iter_leaf_paths(item, prefix + (str(key),))
        else:
            yield prefix, value

    def _get_path(self, data: dict, path: Tuple[str, ...], default: Any = None) -> Any:
        current = data
        for key in path:
            if not isinstance(current, dict) or key not in current:
                return default
            current = current[key]
        return current

    def _set_path(self, data: dict, path: Tuple[str, ...], value: Any):
        current = data
        for key in path[:-1]:
            current = current.setdefault(key, {})
        current[path[-1]] = value

    def _should_sync_astrbot_leaf(self, path: Tuple[str, ...], current_value: Any, previous_snapshot: dict) -> bool:
        if not path or path[0] == "stats":
            return False
        if path in {("webui", "token"), ("draw", "api_key")} and _looks_masked(str(current_value or "")):
            return False

        has_snapshot = bool(previous_snapshot)
        if has_snapshot:
            previous_value = self._get_path(previous_snapshot, path)
            if current_value == previous_value:
                return False
            if path == ("webui", "token") and not str(current_value or "").strip():
                return False
            return True

        builtin_value = self._get_path(self.builtin_defaults, path)
        if current_value == builtin_value:
            return False
        if path == ("webui", "token") and not str(current_value or "").strip():
            return False
        return True

    def _sync_astrbot_config_overrides(self):
        previous_snapshot = self.data.get("astrbot_config_snapshot")
        if not isinstance(previous_snapshot, dict):
            previous_snapshot = {}

        changed_paths = []
        config = self.data.setdefault("config", {})
        for path, current_value in self._iter_leaf_paths(self.schema_defaults):
            if not self._should_sync_astrbot_leaf(path, current_value, previous_snapshot):
                continue
            self._set_path(config, path, current_value)
            changed_paths.append(".".join(path))

        self.data["astrbot_config_snapshot"] = json.loads(json.dumps(self.schema_defaults, ensure_ascii=False))
        if changed_paths:
            preview = ", ".join(changed_paths[:8])
            extra = f" 等 {len(changed_paths)} 项" if len(changed_paths) > 8 else ""
            logger.info(f"[GPT Magic] 已同步 AstrBot 配置覆盖到 GPT 魔法数据：{preview}{extra}")

    def _ensure_token(self):
        webui_conf = self.data.setdefault("config", {}).setdefault("webui", {})
        if not webui_conf.get("token"):
            webui_conf["token"] = secrets.token_urlsafe(24)
            logger.warning(f"[GPT Magic] WebUI token 已自动生成：{webui_conf['token']}")

    def save(self):
        self.ensure_dir()
        self.data.setdefault("config", {}).setdefault("stats", {})
        self.data["config"]["stats"]["updated_at"] = _now_ms()
        self.file.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def config(self) -> dict:
        return self.data.setdefault("config", self.schema_defaults)

    def users(self) -> dict:
        return self.data.setdefault("users", {})

    def keys(self) -> dict:
        return self.data.setdefault("keys", {})

    def history(self) -> list:
        return self.data.setdefault("history", [])

    def web_draw_history(self) -> list:
        return self.data.setdefault("web_draw_history", [])

    def cache(self) -> dict:
        return self.data.setdefault("cache", {})

    def gallery_accounts(self) -> dict:
        return self.data.setdefault("gallery_accounts", {})

    def gallery_access_log(self) -> list:
        return self.data.setdefault("gallery_access_log", [])

    def _gallery_admin_username(self) -> str:
        cfg = self.config().get("gallery", {}) if isinstance(self.config().get("gallery"), dict) else {}
        return self._clean_gallery_username(cfg.get("admin_username") or GALLERY_DEFAULT_ADMIN_USERNAME) or GALLERY_DEFAULT_ADMIN_USERNAME

    def _gallery_admin_password(self) -> str:
        cfg = self.config().get("gallery", {}) if isinstance(self.config().get("gallery"), dict) else {}
        return str(cfg.get("admin_password") or "")

    def _clean_gallery_username(self, username: str) -> str:
        text = re.sub(r"[\x00-\x1f\x7f]+", "", str(username or "")).strip()
        text = re.sub(r"\s+", "_", text)
        text = re.sub(r"[^\w.@\-\u4e00-\u9fff]", "", text, flags=re.UNICODE)
        return text[:32]

    def _gallery_account_key(self, username: str) -> str:
        return self._clean_gallery_username(username).casefold()

    def _new_gallery_password(self) -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
        return "".join(secrets.choice(alphabet) for _ in range(16))

    def _hash_gallery_password(self, password: str, salt: str = "") -> tuple:
        salt = str(salt or secrets.token_hex(16))
        try:
            salt_bytes = bytes.fromhex(salt)
        except Exception:
            salt_bytes = salt.encode("utf-8")
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            str(password or "").encode("utf-8"),
            salt_bytes,
            GALLERY_PASSWORD_ITERATIONS,
        ).hex()
        return salt, digest

    def _legacy_gallery_password_hash(self, password: str, salt: str) -> str:
        return hashlib.sha256(f"{str(salt or '')}:{str(password or '')}".encode("utf-8")).hexdigest()

    def _set_gallery_account_password(self, account: dict, password: str):
        salt, digest = self._hash_gallery_password(password)
        account["salt"] = salt
        account["password_hash"] = digest
        account["password_algo"] = GALLERY_PASSWORD_ALGO
        account["password_iterations"] = GALLERY_PASSWORD_ITERATIONS
        account["updated_at"] = _now_ms()

    def _verify_gallery_password(self, password: str, salt: str, digest: str, algo: str = "", iterations: int = 0) -> bool:
        if not salt or not digest:
            return False
        if str(algo or "") == GALLERY_PASSWORD_ALGO:
            try:
                salt_bytes = bytes.fromhex(str(salt))
            except Exception:
                salt_bytes = str(salt).encode("utf-8")
            candidate = hashlib.pbkdf2_hmac(
                "sha256",
                str(password or "").encode("utf-8"),
                salt_bytes,
                _safe_int(iterations, GALLERY_PASSWORD_ITERATIONS, 100_000, 1_000_000),
            ).hex()
        else:
            candidate = self._legacy_gallery_password_hash(password, salt)
        return hmac.compare_digest(str(candidate), str(digest))

    def _public_gallery_account(self, account: dict) -> dict:
        account = account or {}
        return {
            "username": str(account.get("username") or ""),
            "role": str(account.get("role") or "user"),
            "disabled": bool(account.get("disabled", False)),
            "activated": bool(account.get("activated", True)),
            "owner_user_id": str(account.get("owner_user_id") or ""),
            "owner_name": str(account.get("owner_name") or ""),
            "last_group_id": str(account.get("last_group_id") or ""),
            "created_by": str(account.get("created_by") or ""),
            "created_at": int(account.get("created_at") or 0),
            "updated_at": int(account.get("updated_at") or 0),
            "last_login_at": int(account.get("last_login_at") or 0),
            "login_count": int(account.get("login_count") or 0),
            "last_ip": str(account.get("last_ip") or ""),
            "balance": _safe_money(account.get("balance", 0.0), 0.0, 0.0),
            "trial_granted": bool(account.get("trial_granted", False)),
            "total_spent": _safe_money(account.get("total_spent", 0.0), 0.0, 0.0),
            "total_recharged": _safe_money(account.get("total_recharged", 0.0), 0.0, 0.0),
        }

    def _ensure_gallery_admin_account(self):
        username = self._gallery_admin_username()
        key = self._gallery_account_key(username)
        if not key:
            return
        accounts = self.gallery_accounts()
        existing = accounts.get(key)
        gallery = self.config().setdefault("gallery", {})
        target_password = self._gallery_admin_password()
        target_password = target_password.strip()
        insecure_passwords = ("114514", "admin")
        needs_epoch_rotation = bool(
            isinstance(existing, dict)
            and existing.get("builtin")
            and int(existing.get("credential_epoch") or 0) < 1
        )
        generated_password = target_password in ("", *insecure_passwords) or needs_epoch_rotation
        if generated_password:
            target_password = self._new_gallery_password()
            if isinstance(gallery, dict):
                gallery["admin_password"] = target_password
            logger.warning(
                f"[GPT Magic] Gallery 管理员初始密码已随机生成：{target_password}；请登录后立即修改"
            )
        if key in accounts:
            account = accounts[key]
            account["role"] = "admin"
            account["username"] = account.get("username") or username
            account["activated"] = True
            if account.get("builtin"):
                password_is_target = self._verify_gallery_password(
                    target_password,
                    account.get("salt"),
                    account.get("password_hash"),
                    account.get("password_algo"),
                    account.get("password_iterations"),
                )
                password_was_insecure = any(
                    self._verify_gallery_password(
                        old_password,
                        account.get("salt"),
                        account.get("password_hash"),
                        account.get("password_algo"),
                        account.get("password_iterations"),
                    )
                    for old_password in insecure_passwords
                )
                if (password_was_insecure or generated_password) and not password_is_target:
                    self._set_gallery_account_password(account, target_password)
                account.pop("default_password_migrated_to", None)
                account["credential_epoch"] = 1
            return
        now = _now_ms()
        account = {
            "username": username,
            "role": "admin",
            "disabled": False,
            "activated": True,
            "builtin": True,
            "owner_user_id": "",
            "owner_name": "WineFox Admin",
            "last_group_id": "",
            "created_by": "system",
            "created_at": now,
            "updated_at": now,
            "last_login_at": 0,
            "login_count": 0,
            "last_ip": "",
        }
        self._set_gallery_account_password(account, target_password)
        account["credential_epoch"] = 1
        accounts[key] = {
            **account,
        }

    def create_gallery_account(
        self,
        username: str,
        owner_user_id: str = "",
        owner_name: str = "",
        group_id: str = "",
        created_by: str = "bot",
        role: str = "user",
        password: str = "",
        activated: bool = True,
    ) -> Tuple[bool, dict, str]:
        username = self._clean_gallery_username(username)
        if len(username) < 2:
            raise ValueError("用户名至少 2 个字符，可用中文、英文、数字、下划线、点和短横线")
        role = "admin" if str(role or "").lower() == "admin" else "user"
        key = self._gallery_account_key(username)
        accounts = self.gallery_accounts()
        if key in accounts:
            raise ValueError("这个用户名已经被注册")
        owner_user_id = str(owner_user_id or "").strip()
        if owner_user_id:
            for account in accounts.values():
                if str(account.get("owner_user_id") or "") == owner_user_id and str(account.get("role") or "user") != "admin":
                    raise ValueError(f"你已经注册过账号：{account.get('username')}")
        password = str(password or "").strip() or self._new_gallery_password()
        now = _now_ms()
        account = {
            "username": username,
            "role": role,
            "disabled": False,
            "activated": bool(activated),
            "builtin": False,
            "owner_user_id": owner_user_id,
            "owner_name": str(owner_name or "")[:80],
            "last_group_id": str(group_id or ""),
            "created_by": str(created_by or "bot")[:80],
            "created_at": now,
            "updated_at": now,
            "last_login_at": 0,
            "login_count": 0,
            "last_ip": "",
        }
        self._set_gallery_account_password(account, password)
        accounts[key] = account
        self.save()
        return True, self._public_gallery_account(account), password

    def _gallery_registration_token_hash(self, token: str) -> str:
        return hashlib.sha256(str(token or "").strip().encode("utf-8")).hexdigest()

    def _purge_expired_gallery_registrations(self):
        now = _now_ms()
        accounts = self.gallery_accounts()
        removed = []
        for key, account in list(accounts.items()):
            if account.get("builtin") or bool(account.get("activated", True)):
                continue
            expires_at = int(account.get("activation_expires_at") or 0)
            if expires_at and expires_at < now:
                removed.append(key)
        for key in removed:
            accounts.pop(key, None)
        if removed:
            self.save()

    def create_pending_gallery_registration(self, username: str, password: str, owner_name: str = "") -> dict:
        self._purge_expired_gallery_registrations()
        username = self._clean_gallery_username(username)
        password = str(password or "")
        if len(username) < 2:
            raise ValueError("用户名至少 2 个字符")
        if len(password) < 8:
            raise ValueError("密码至少 8 位")
        key = self._gallery_account_key(username)
        existing = self.gallery_accounts().get(key)
        if existing:
            raise ValueError("这个用户名已经被注册或正在等待验证")
        _, account, _ = self.create_gallery_account(
            username,
            owner_user_id="",
            owner_name=owner_name,
            group_id="",
            created_by="web:register",
            role="user",
            password=password,
            activated=False,
        )
        token = "WFREG-" + secrets.token_urlsafe(32)
        raw_account = self.gallery_accounts()[key]
        now = _now_ms()
        raw_account["activation_token_hash"] = self._gallery_registration_token_hash(token)
        raw_account["activation_created_at"] = now
        raw_account["activation_expires_at"] = now + GALLERY_REGISTRATION_TOKEN_TTL_MS
        raw_account["updated_at"] = now
        self.save()
        account = self._public_gallery_account(raw_account)
        return {"account": account, "token": token, "expires_at": raw_account["activation_expires_at"]}

    def activate_gallery_registration_token(self, token: str, owner_user_id: str, owner_name: str = "", group_id: str = "") -> Tuple[Optional[dict], str]:
        token = str(token or "").strip()
        if not token:
            return None, "empty"
        owner_user_id = str(owner_user_id or "").strip()
        if not owner_user_id:
            return None, "missing_owner"
        existing_owner = self.gallery_account_by_owner(owner_user_id)
        if existing_owner:
            return existing_owner, "already_registered"
        digest = self._gallery_registration_token_hash(token)
        now = _now_ms()
        for key, account in list(self.gallery_accounts().items()):
            if str(account.get("activation_token_hash") or "") != digest:
                continue
            if bool(account.get("activated", True)):
                return self._public_gallery_account(account), "already_activated"
            if int(account.get("activation_expires_at") or 0) < now:
                self.gallery_accounts().pop(key, None)
                self.save()
                return None, "expired"
            account["activated"] = True
            account["disabled"] = False
            account["owner_user_id"] = owner_user_id
            if owner_name:
                account["owner_name"] = str(owner_name or "")[:80]
            account["last_group_id"] = str(group_id or "")
            account["activated_at"] = now
            account["updated_at"] = now
            account.pop("activation_token_hash", None)
            account.pop("activation_created_at", None)
            account.pop("activation_expires_at", None)
            self.save()
            # 激活后发放一次性试用额度（仅首次）
            self.grant_gallery_trial_if_needed(account.get("username"))
            return self._public_gallery_account(self.gallery_accounts().get(key) or account), "activated"
        return None, "invalid"

    def reset_gallery_account_password(self, username: str, password: str = "") -> Tuple[Optional[dict], str]:
        key = self._gallery_account_key(username)
        account = self.gallery_accounts().get(key)
        if not account:
            return None, ""
        password = str(password or "").strip() or self._new_gallery_password()
        self._set_gallery_account_password(account, password)
        self.save()
        return self._public_gallery_account(account), password

    def verify_gallery_account(self, username: str, password: str, ip: str = "") -> Optional[dict]:
        key = self._gallery_account_key(username)
        account = self.gallery_accounts().get(key)
        if not account or bool(account.get("disabled", False)):
            return None
        if not bool(account.get("activated", True)):
            return None
        if not self._verify_gallery_password(
            password,
            str(account.get("salt") or ""),
            str(account.get("password_hash") or ""),
            str(account.get("password_algo") or ""),
            int(account.get("password_iterations") or 0),
        ):
            return None
        if str(account.get("password_algo") or "") != GALLERY_PASSWORD_ALGO:
            self._set_gallery_account_password(account, password)
        account["last_login_at"] = _now_ms()
        account["login_count"] = int(account.get("login_count") or 0) + 1
        account["last_ip"] = str(ip or "")[:80]
        # 老用户首次登录时补发试用额度（新老用户都享有默认试用额度）
        if not bool(account.get("trial_granted", False)) and not account.get("builtin"):
            self.grant_gallery_trial_if_needed(account.get("username"))
            account = self.gallery_accounts().get(key) or account
        self.save()
        return self._public_gallery_account(account)

    def touch_gallery_account(self, username: str, ip: str = ""):
        key = self._gallery_account_key(username)
        account = self.gallery_accounts().get(key)
        if not account:
            return
        account["last_seen_at"] = _now_ms()
        if ip:
            account["last_ip"] = str(ip or "")[:80]

    def list_gallery_accounts(self) -> list:
        accounts = [self._public_gallery_account(account) for account in self.gallery_accounts().values()]
        accounts.sort(key=lambda item: (item.get("role") != "admin", str(item.get("username") or "").casefold()))
        return accounts

    def gallery_account_by_owner(self, owner_user_id: str) -> Optional[dict]:
        owner_user_id = str(owner_user_id or "").strip()
        if not owner_user_id:
            return None
        for account in self.gallery_accounts().values():
            if str(account.get("owner_user_id") or "") == owner_user_id and str(account.get("role") or "user") != "admin":
                return self._public_gallery_account(account)
        return None

    # ====== 视频额度 / 钱包 ======
    def _raw_gallery_account_by_owner(self, owner_user_id: str) -> Optional[dict]:
        """按绑定的 QQ(owner_user_id) 返回原始账户字典（含管理员），用于扣费/充值。"""
        owner_user_id = str(owner_user_id or "").strip()
        if not owner_user_id:
            return None
        for account in self.gallery_accounts().values():
            if str(account.get("owner_user_id") or "") == owner_user_id:
                return account
        return None

    def _append_balance_ledger(self, account: dict, kind: str, amount: float, reason: str, balance_after: float, by: str = ""):
        ledger = account.setdefault("balance_ledger", [])
        ledger.insert(0, {
            "at": _now_ms(),
            "kind": str(kind or "")[:24],          # consume / refund / recharge / set / trial
            "amount": _safe_money(amount, 0.0),
            "balance_after": _safe_money(balance_after, 0.0),
            "reason": str(reason or "")[:120],
            "by": str(by or "")[:80],
        })
        del ledger[GALLERY_BALANCE_LEDGER_LIMIT:]

    def grant_gallery_trial_if_needed(self, username: str) -> Optional[dict]:
        """给账户发放一次性试用额度（仅当从未发放过）。"""
        key = self._gallery_account_key(username)
        account = self.gallery_accounts().get(key)
        if not account:
            return None
        if bool(account.get("trial_granted", False)):
            return self._public_gallery_account(account)
        cfg = self.config()
        gallery = cfg.get("gallery", {}) if isinstance(cfg.get("gallery"), dict) else {}
        trial = _safe_money(gallery.get("video_trial_balance", cfg.get("video_trial_balance", GALLERY_DEFAULT_TRIAL_BALANCE)), GALLERY_DEFAULT_TRIAL_BALANCE, 0.0, GALLERY_BALANCE_MAX)
        balance = _safe_money(account.get("balance", 0.0), 0.0, 0.0) + trial
        balance = min(balance, GALLERY_BALANCE_MAX)
        account["balance"] = balance
        account["trial_granted"] = True
        account["total_recharged"] = _safe_money(account.get("total_recharged", 0.0), 0.0, 0.0) + trial
        account["updated_at"] = _now_ms()
        if trial > 0:
            self._append_balance_ledger(account, "trial", trial, "注册激活赠送试用额度", balance, by="system")
        self.save()
        return self._public_gallery_account(account)

    def gallery_balance_by_owner(self, owner_user_id: str) -> Optional[float]:
        account = self._raw_gallery_account_by_owner(owner_user_id)
        if account is None:
            return None
        return _safe_money(account.get("balance", 0.0), 0.0, 0.0)

    def consume_gallery_balance_by_owner(self, owner_user_id: str, amount: float, reason: str = "video") -> Tuple[bool, float, str]:
        """按 QQ 扣费。返回 (是否成功, 扣费后余额, 失败原因代码)。
        原因代码：not_registered / insufficient / ok"""
        account = self._raw_gallery_account_by_owner(owner_user_id)
        if account is None:
            return False, 0.0, "not_registered"
        amount = _safe_money(amount, 0.0, 0.0)
        balance = _safe_money(account.get("balance", 0.0), 0.0, 0.0)
        if amount <= 0:
            return True, balance, "ok"
        if balance + 1e-9 < amount:
            return False, balance, "insufficient"
        balance = _safe_money(balance - amount, 0.0, 0.0)
        account["balance"] = balance
        account["total_spent"] = _safe_money(account.get("total_spent", 0.0), 0.0, 0.0) + amount
        account["updated_at"] = _now_ms()
        self._append_balance_ledger(account, "consume", -amount, reason, balance, by=str(account.get("username") or ""))
        self.save()
        return True, balance, "ok"

    def refund_gallery_balance_by_owner(self, owner_user_id: str, amount: float, reason: str = "video_refund") -> Optional[float]:
        """生成失败时退回额度。返回退回后的余额，账户不存在返回 None。"""
        account = self._raw_gallery_account_by_owner(owner_user_id)
        if account is None:
            return None
        amount = _safe_money(amount, 0.0, 0.0)
        if amount <= 0:
            return _safe_money(account.get("balance", 0.0), 0.0, 0.0)
        balance = min(_safe_money(account.get("balance", 0.0), 0.0, 0.0) + amount, GALLERY_BALANCE_MAX)
        account["balance"] = balance
        # 退回时回冲已消费统计，避免统计虚高
        account["total_spent"] = max(0.0, _safe_money(account.get("total_spent", 0.0), 0.0, 0.0) - amount)
        account["updated_at"] = _now_ms()
        self._append_balance_ledger(account, "refund", amount, reason, balance, by="system")
        self.save()
        return balance

    def adjust_gallery_balance(self, username: str, amount: float, reason: str = "", by: str = "admin") -> Optional[dict]:
        """管理员按用户名增减余额（amount 可正可负）。"""
        key = self._gallery_account_key(username)
        account = self.gallery_accounts().get(key)
        if not account:
            return None
        amount = _safe_money(amount, 0.0)
        balance = _safe_money(account.get("balance", 0.0), 0.0, 0.0) + amount
        balance = _safe_money(min(max(balance, 0.0), GALLERY_BALANCE_MAX), 0.0, 0.0)
        account["balance"] = balance
        if amount >= 0:
            account["total_recharged"] = _safe_money(account.get("total_recharged", 0.0), 0.0, 0.0) + amount
        account["updated_at"] = _now_ms()
        self._append_balance_ledger(account, "recharge" if amount >= 0 else "deduct", amount, reason or ("管理员充值" if amount >= 0 else "管理员扣减"), balance, by=by)
        self.save()
        return self._public_gallery_account(account)

    def set_gallery_balance(self, username: str, value: float, reason: str = "", by: str = "admin") -> Optional[dict]:
        """管理员把余额设为绝对值。"""
        key = self._gallery_account_key(username)
        account = self.gallery_accounts().get(key)
        if not account:
            return None
        value = _safe_money(value, 0.0, 0.0, GALLERY_BALANCE_MAX)
        account["balance"] = value
        account["updated_at"] = _now_ms()
        self._append_balance_ledger(account, "set", value, reason or "管理员设置余额", value, by=by)
        self.save()
        return self._public_gallery_account(account)

    def gallery_balance_ledger(self, username: str, limit: int = 50) -> list:
        key = self._gallery_account_key(username)
        account = self.gallery_accounts().get(key)
        if not account:
            return []
        ledger = list(account.get("balance_ledger") or [])
        return ledger[:max(1, int(limit or 50))]

    # ====== API 视频配置（存放在 gallery.api_video，由超级管理员 WebUI 维护）======
    def get_api_video_config(self) -> dict:
        gallery = self.config().setdefault("gallery", {}) if isinstance(self.config().get("gallery"), dict) else self.config().setdefault("gallery", {})
        conf = gallery.get("api_video", {})
        if not isinstance(conf, dict):
            conf = {}
        return {
            "enable": bool(conf.get("enable", False)),
            "base_url": str(conf.get("base_url", "https://api.dealonhorizon.us") or "https://api.dealonhorizon.us"),
            "api_key": str(conf.get("api_key", "") or ""),
            "model": str(conf.get("model", "grok-imagine-video-10s-720p") or "grok-imagine-video-10s-720p"),
            "max_images": _safe_int(conf.get("max_images", 1), 1, 1, 7),
            "timeout": _safe_int(conf.get("timeout", 900), 900, 30, 3600),
            "duration": _safe_int(conf.get("duration", 6), 6, 1, 30),
            "aspect_ratio": _normalize_api_video_aspect_ratio(conf.get("aspect_ratio", "auto")),
            "proxy": str(conf.get("proxy", "") or ""),
            "prompt_enhance": str(conf.get("prompt_enhance", "light") or "light"),
            "default_prompt": str(conf.get("default_prompt", "") or ""),
        }

    def update_api_video_config(self, patch: dict) -> dict:
        gallery = self.config().setdefault("gallery", {})
        if not isinstance(gallery, dict):
            gallery = {}
            self.config()["gallery"] = gallery
        conf = gallery.get("api_video", {})
        if not isinstance(conf, dict):
            conf = {}
        patch = patch or {}
        if "enable" in patch:
            conf["enable"] = bool(patch.get("enable"))
        if "base_url" in patch:
            conf["base_url"] = str(patch.get("base_url") or "").strip()[:300]
        if "api_key" in patch:
            conf["api_key"] = str(patch.get("api_key") or "").strip()[:300]
        if "model" in patch:
            conf["model"] = str(patch.get("model") or "").strip()[:120] or "grok-imagine-video-10s-720p"
        if "max_images" in patch:
            conf["max_images"] = _safe_int(patch.get("max_images"), 1, 1, 7)
        if "timeout" in patch:
            conf["timeout"] = _safe_int(patch.get("timeout"), 900, 30, 3600)
        if "duration" in patch:
            conf["duration"] = _safe_int(patch.get("duration"), 6, 1, 30)
        if "aspect_ratio" in patch:
            conf["aspect_ratio"] = _normalize_api_video_aspect_ratio(patch.get("aspect_ratio"))
        if "proxy" in patch:
            conf["proxy"] = str(patch.get("proxy") or "").strip()[:300]
        if "prompt_enhance" in patch:
            mode = str(patch.get("prompt_enhance") or "light").strip().lower()
            conf["prompt_enhance"] = mode if mode in ("none", "light", "full") else "light"
        if "default_prompt" in patch:
            conf["default_prompt"] = str(patch.get("default_prompt") or "").strip()[:1000]
        gallery["api_video"] = conf
        self.save()
        return self.get_api_video_config()


    def update_gallery_account(self, username: str, patch: dict) -> Optional[dict]:
        key = self._gallery_account_key(username)
        account = self.gallery_accounts().get(key)
        if not account:
            return None
        patch = patch or {}
        if "role" in patch and not account.get("builtin"):
            account["role"] = "admin" if str(patch.get("role") or "").lower() == "admin" else "user"
        if "disabled" in patch and not account.get("builtin"):
            account["disabled"] = bool(patch.get("disabled"))
        if "owner_name" in patch:
            account["owner_name"] = str(patch.get("owner_name") or "")[:80]
        account["updated_at"] = _now_ms()
        self.save()
        return self._public_gallery_account(account)

    def delete_gallery_account(self, username: str) -> bool:
        key = self._gallery_account_key(username)
        account = self.gallery_accounts().get(key)
        if not account or account.get("builtin") or str(account.get("role") or "") == "admin":
            return False
        self.gallery_accounts().pop(key, None)
        self.save()
        return True

    def record_gallery_access(self, action: str, account: dict = None, ip: str = "", user_agent: str = "", ok: bool = True, detail: str = ""):
        log = self.gallery_access_log()
        account = account or {}
        log.insert(0, {
            "at": _now_ms(),
            "action": str(action or "")[:48],
            "ok": bool(ok),
            "username": str(account.get("username") or ""),
            "role": str(account.get("role") or "unknown"),
            "ip": str(ip or "")[:80],
            "user_agent": str(user_agent or "")[:180],
            "detail": str(detail or "")[:180],
        })
        del log[500:]
        self.save()

    def gallery_access_summary(self) -> dict:
        log = self.gallery_access_log()
        now = _now_ms()
        day = 24 * 60 * 60 * 1000
        visitors = {
            str(item.get("username") or item.get("ip") or "")
            for item in log
            if now - int(item.get("at") or 0) <= day and (item.get("username") or item.get("ip"))
        }
        return {
            "recent": list(log[:80]),
            "today_visitors": len(visitors),
            "today_requests": sum(1 for item in log if now - int(item.get("at") or 0) <= day),
            "failed_logins": sum(1 for item in log if str(item.get("action") or "") == "login" and not item.get("ok")),
        }

    def token(self) -> str:
        return str(self.config().get("webui", {}).get("token", "") or "")

    def pose_preset_file(self) -> Path:
        return self.base_dir / "pose_presets.json"

    def _read_pose_preset_data(self) -> dict:
        path = self.pose_preset_file()
        if not path.exists():
            return {"version": 1, "presets": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {"version": 1, "presets": []}
            presets = data.get("presets")
            if not isinstance(presets, list):
                presets = []
            return {"version": int(data.get("version") or 1), "presets": [p for p in presets if isinstance(p, dict)]}
        except Exception as e:
            logger.warning(f"[GPT Magic] 读取姿态预设失败: {e}")
            return {"version": 1, "presets": []}

    def _write_pose_preset_data(self, data: dict):
        self.ensure_dir()
        payload = {"version": 1, "presets": list((data or {}).get("presets") or [])}
        self.pose_preset_file().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _clean_pose_preset_name(self, name: str) -> str:
        text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(name or "")).strip()
        text = re.sub(r"\s+", " ", text)
        return text[:80]

    def list_pose_presets(self) -> list:
        data = self._read_pose_preset_data()
        presets = list(data.get("presets") or [])
        presets.sort(key=lambda p: int(p.get("updated_at") or p.get("created_at") or 0), reverse=True)
        return presets

    def save_pose_preset(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("payload 必须是对象")
        raw_size = len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
        if raw_size > POSE_PRESET_MAX_BYTES:
            raise ValueError("姿态预设太大，请减少数据量")
        name = self._clean_pose_preset_name(payload.get("name") or (payload.get("state") or {}).get("name") or "")
        if not name:
            raise ValueError("请填写预设名称")
        state = payload.get("state")
        if not isinstance(state, dict):
            raise ValueError("state 必须是对象")
        prompt = str(payload.get("prompt") or "")[:4000]
        now = _now_ms()
        data = self._read_pose_preset_data()
        presets = list(data.get("presets") or [])
        existing = next((p for p in presets if self._clean_pose_preset_name(p.get("name")) == name), None)
        if existing:
            preset_id = str(existing.get("id") or hashlib.sha256(f"{name}:{now}".encode("utf-8")).hexdigest()[:16])
            existing.update({
                "id": preset_id,
                "name": name,
                "state": _jsonable(state),
                "prompt": prompt,
                "updated_at": now,
            })
            preset = existing
        else:
            preset_id = hashlib.sha256(f"{name}:{now}:{random.random()}".encode("utf-8")).hexdigest()[:16]
            preset = {
                "id": preset_id,
                "name": name,
                "state": _jsonable(state),
                "prompt": prompt,
                "created_at": now,
                "updated_at": now,
            }
            presets.append(preset)
        data["presets"] = presets[-200:]
        self._write_pose_preset_data(data)
        return preset

    def delete_pose_preset(self, preset_id: str) -> bool:
        target = re.sub(r"[^a-zA-Z0-9_-]", "", str(preset_id or ""))[:64]
        if not target:
            return False
        data = self._read_pose_preset_data()
        presets = list(data.get("presets") or [])
        kept = [p for p in presets if str(p.get("id") or "") != target]
        if len(kept) == len(presets):
            return False
        data["presets"] = kept
        self._write_pose_preset_data(data)
        return True

    def set_config(self, patch: dict) -> dict:
        patch = dict(patch or {})
        current = self.config()

        draw_patch = patch.get("draw")
        if isinstance(draw_patch, dict):
            extra = draw_patch.get("extra_body")
            if isinstance(extra, str):
                try:
                    draw_patch["extra_body"] = json.loads(extra or "{}")
                except Exception:
                    raise ValueError("extra_body 必须是合法 JSON")
            if _looks_masked(draw_patch.get("api_key", "")):
                draw_patch.pop("api_key", None)

        webui_patch = patch.get("webui")
        if isinstance(webui_patch, dict) and _looks_masked(webui_patch.get("token", "")):
            webui_patch.pop("token", None)

        gallery_patch = patch.get("gallery")
        if isinstance(gallery_patch, dict) and _looks_masked(gallery_patch.get("password", "")):
            gallery_patch.pop("password", None)
        if isinstance(gallery_patch, dict) and _looks_masked(gallery_patch.get("admin_password", "")):
            gallery_patch.pop("admin_password", None)

        self.data["config"] = _deep_merge(current, patch)
        self._ensure_token()
        self.save()
        return self.config()

    def public_state(self, queue_snapshot: dict = None, comfy_status: dict = None) -> dict:
        cfg = json.loads(json.dumps(self.config(), ensure_ascii=False))
        cfg.setdefault("draw", {})
        cfg.setdefault("webui", {})
        cfg.setdefault("gallery", {})
        cfg["draw"]["api_key_masked"] = _mask_secret(cfg["draw"].get("api_key", ""))
        cfg["draw"]["api_key"] = cfg["draw"]["api_key_masked"]
        cfg["webui"]["token_masked"] = _mask_secret(cfg["webui"].get("token", ""))
        cfg["webui"]["token"] = cfg["webui"]["token_masked"]
        cfg["gallery"].pop("password", None)
        cfg["gallery"].pop("password_masked", None)
        cfg["gallery"]["admin_password_masked"] = _mask_secret(cfg["gallery"].get("admin_password", ""))
        cfg["gallery"]["admin_password"] = cfg["gallery"]["admin_password_masked"]

        history_items = []
        for item in self.history()[: int(cfg.get("history_limit", 300) or 300)]:
            copy_item = dict(item)
            if copy_item.get("image_path"):
                copy_item["image_url"] = f"/api/history/image/{copy_item.get('id')}"
            history_items.append(copy_item)

        return {
            "config": cfg,
            "users": self.users(),
            "keys": self.keys(),
            "history": history_items,
            "favorites": self.list_favorites(),
            "queue": queue_snapshot or {},
            "comfy": comfy_status or {},
            "analytics": self.analytics(),
        }

    def analytics(self) -> dict:
        now = _now_ms()
        day = 24 * 60 * 60 * 1000
        users = list(self.users().values())
        recent = self.history()
        return {
            "dau": sum(1 for u in users if now - int(u.get("last_seen_at", 0) or 0) <= day),
            "wau": sum(1 for u in users if now - int(u.get("last_seen_at", 0) or 0) <= day * 7),
            "recent7d": [
                {
                    "label": time.strftime("%m-%d", time.localtime((now - day * offset) / 1000)),
                    "count": sum(
                        1
                        for h in recent
                        if (now - day * (offset + 1)) < int(h.get("created_at", 0) or 0) <= (now - day * offset)
                    ),
                }
                for offset in range(6, -1, -1)
            ],
        }

    def user_name_from_event(self, event: Any) -> str:
        sender = getattr(event, "sender", None) or getattr(getattr(event, "message_obj", None), "sender", None)
        for attr in ("card", "nickname", "name"):
            value = getattr(sender, attr, None)
            if value:
                return str(value)
        return ""

    def touch_user(self, user_id: str, nickname: str = "", group_id: str = "", group_name: str = ""):
        user = self.users().get(str(user_id))
        if not user:
            return None
        user["last_seen_at"] = _now_ms()
        if nickname:
            user["nickname"] = nickname
        if group_id:
            user["last_group_id"] = str(group_id)
        if group_name:
            user["last_group_name"] = str(group_name)
        self.save()
        return user

    def register_user(self, user_id: str, nickname: str = "", group_id: str = "", group_name: str = "") -> Tuple[bool, dict]:
        uid = str(user_id)
        existing = self.users().get(uid)
        if existing:
            return False, self.touch_user(uid, nickname, group_id, group_name) or existing
        quota = _safe_int(self.config().get("default_quota", 2), 2, 0)
        user = {
            "user_id": uid,
            "nickname": nickname,
            "quota": quota,
            "total_drawn": 0,
            "total_success": 0,
            "total_failed": 0,
            "total_redeemed": 0,
            "total_recharge": 0,
            "blacklisted": False,
            "remark": "",
            "created_at": _now_ms(),
            "last_seen_at": _now_ms(),
            "last_draw_at": 0,
            "last_group_id": str(group_id or ""),
            "last_group_name": str(group_name or ""),
        }
        self.users()[uid] = user
        self.config().setdefault("stats", {})["total_users"] = len(self.users())
        self.save()
        return True, user

    def get_user(self, user_id: str) -> Optional[dict]:
        return self.users().get(str(user_id))

    def update_user(self, user_id: str, patch: dict) -> Optional[dict]:
        user = self.get_user(user_id)
        if not user:
            return None
        for key in ("quota", "blacklisted", "remark", "nickname"):
            if key in patch:
                user[key] = patch[key]
        if "quota" in user:
            user["quota"] = max(0, _safe_int(user.get("quota", 0), 0))
        self.save()
        return user

    def add_quota(self, user_id: str, amount: int, reason: str = "manual") -> Optional[dict]:
        user = self.get_user(user_id)
        if not user:
            return None
        amount = int(amount or 0)
        user["quota"] = max(0, int(user.get("quota", 0) or 0) + amount)
        if amount > 0 and reason in {"redeem", "manual", "recharge"}:
            user["total_recharge"] = int(user.get("total_recharge", 0) or 0) + amount
        if reason == "redeem" and amount > 0:
            user["total_redeemed"] = int(user.get("total_redeemed", 0) or 0) + amount
            self.config().setdefault("stats", {})["total_redeemed"] = (
                int(self.config().get("stats", {}).get("total_redeemed", 0) or 0) + amount
            )
        self.save()
        return user

    def consume_quota(self, user_id: str, bypass: bool = False) -> bool:
        if bypass:
            return True
        user = self.get_user(user_id)
        if not user or int(user.get("quota", 0) or 0) <= 0:
            return False
        user["quota"] = int(user.get("quota", 0) or 0) - 1
        user["total_drawn"] = int(user.get("total_drawn", 0) or 0) + 1
        user["last_draw_at"] = _now_ms()
        stats = self.config().setdefault("stats", {})
        stats["total_consumed_quota"] = int(stats.get("total_consumed_quota", 0) or 0) + 1
        self.save()
        return True

    def refund_quota(self, user_id: str, bypass: bool = False):
        if bypass:
            return
        user = self.get_user(user_id)
        if user:
            user["quota"] = int(user.get("quota", 0) or 0) + 1
            self.save()

    def mark_success(self, user_id: str, bypass: bool = False):
        user = self.get_user(user_id)
        if user:
            user["total_success"] = int(user.get("total_success", 0) or 0) + 1
        stats = self.config().setdefault("stats", {})
        stats["total_images"] = int(stats.get("total_images", 0) or 0) + 1
        self.save()

    def mark_fail(self, user_id: str):
        user = self.get_user(user_id)
        if user:
            user["total_failed"] = int(user.get("total_failed", 0) or 0) + 1
        stats = self.config().setdefault("stats", {})
        stats["total_failed"] = int(stats.get("total_failed", 0) or 0) + 1
        self.save()

    def create_keys(self, amount: int, count: int = 1, note: str = "") -> List[dict]:
        amount = max(1, _safe_int(amount, 1, 1))
        count = min(100, max(1, _safe_int(count, 1, 1)))
        created = []
        keys = self.keys()
        for _ in range(count):
            key = _random_key()
            while key in keys:
                key = _random_key()
            keys[key] = {
                "key": key,
                "amount": amount,
                "note": str(note or ""),
                "created_at": _now_ms(),
                "used_at": 0,
                "used_by": "",
            }
            created.append(keys[key])
        self.save()
        return created

    def delete_key(self, key: str) -> bool:
        if key not in self.keys():
            return False
        del self.keys()[key]
        self.save()
        return True

    def redeem_key(self, user_id: str, key: str) -> Optional[dict]:
        key = str(key or "").strip()
        record = self.keys().get(key)
        user = self.get_user(user_id)
        if not record or not user:
            return None
        amount = int(record.get("amount", 0) or 0)
        user["quota"] = int(user.get("quota", 0) or 0) + amount
        user["total_redeemed"] = int(user.get("total_redeemed", 0) or 0) + amount
        record["used_at"] = _now_ms()
        record["used_by"] = str(user_id)
        del self.keys()[key]
        stats = self.config().setdefault("stats", {})
        stats["total_redeemed"] = int(stats.get("total_redeemed", 0) or 0) + amount
        self.save()
        return {"amount": amount, "user": user}

    def save_image(self, data: bytes, ext: str, folder: str = "history") -> Path:
        safe_ext = re.sub(r"[^a-zA-Z0-9]", "", ext or "png").lower() or "png"
        root = self.history_dir if folder == "history" else self.cache_dir
        root.mkdir(parents=True, exist_ok=True)
        name = f"{int(time.time())}-{secrets.token_hex(4)}.{safe_ext}"
        path = root / name
        path.write_bytes(data)
        return path

    def add_history(self, item: dict) -> dict:
        item = {"id": f"{int(time.time())}-{secrets.token_hex(4)}", "created_at": _now_ms(), **item}
        self.history().insert(0, item)
        limit = max(10, _safe_int(self.config().get("history_limit", 300), 300))
        while len(self.history()) > limit:
            old = self.history().pop()
            old_path = old.get("image_path")
            if old_path and f"{os.sep}history{os.sep}" in str(old_path):
                try:
                    Path(old_path).unlink(missing_ok=True)
                except Exception:
                    pass
        self.save()
        return item

    def get_history_image_path(self, item_id: str) -> Optional[Path]:
        for item in self.history():
            if str(item.get("id")) == str(item_id):
                path = Path(str(item.get("image_path") or ""))
                if path.exists() and path.is_file():
                    return path
        return None

    def _web_draw_history_ttl_ms(self) -> int:
        gallery_conf = self.config().get("gallery", {}) if isinstance(self.config().get("gallery", {}), dict) else {}
        hours = _safe_int(gallery_conf.get("draw_history_ttl_hours", 24), 24, 1, 720)
        return hours * 60 * 60 * 1000

    def _safe_unlink_web_draw_image(self, image_path: str):
        path = Path(str(image_path or ""))
        if not path.exists() or not path.is_file():
            return
        try:
            data_root = self.base_dir.parent.resolve()
            resolved = path.resolve()
            if resolved == data_root or data_root not in resolved.parents:
                return
            path.unlink(missing_ok=True)
        except Exception:
            pass

    def cleanup_web_draw_history(self) -> bool:
        now = _now_ms()
        ttl_ms = self._web_draw_history_ttl_ms()
        changed = False
        kept = []
        for item in list(self.web_draw_history()):
            if not isinstance(item, dict):
                changed = True
                continue
            created_at = int(item.get("created_at") or 0)
            expires_at = int(item.get("expires_at") or (created_at + ttl_ms if created_at else now + ttl_ms))
            if expires_at <= now:
                self._safe_unlink_web_draw_image(item.get("image_path") or "")
                changed = True
                continue
            item["expires_at"] = expires_at
            kept.append(item)
        if changed:
            self.data["web_draw_history"] = kept
            self.save()
        return changed

    def add_web_draw_history(self, item: dict) -> dict:
        self.cleanup_web_draw_history()
        now = _now_ms()
        ttl_ms = self._web_draw_history_ttl_ms()
        payload = {
            "id": f"wd-{int(time.time())}-{secrets.token_hex(4)}",
            "source": "web_draw",
            "created_at": now,
            "expires_at": now + ttl_ms,
            **(item or {}),
        }
        self.web_draw_history().insert(0, payload)
        self.save()
        return payload

    def list_web_draw_history(self, limit: int = 24) -> list:
        self.cleanup_web_draw_history()
        limit = _safe_int(limit, 24, 1, 200)
        return [dict(item) for item in self.web_draw_history()[:limit] if isinstance(item, dict)]

    def _web_draw_history_allowed(self, item: dict, context: dict = None) -> bool:
        if not isinstance(item, dict):
            return False
        context = context or {}
        if str(context.get("role") or "user").lower() == "admin":
            return True
        username = self._clean_gallery_username(context.get("username") or "")
        account_key = str(context.get("account_key") or self._gallery_account_key(username)).strip()
        owner_user_id = str(context.get("owner_user_id") or "").strip()
        item_account_key = str(item.get("account_key") or "").strip()
        item_owner_user_id = str(item.get("owner_user_id") or "").strip()
        item_username = self._clean_gallery_username(item.get("username") or "")
        if account_key and item_account_key and account_key == item_account_key:
            return True
        if owner_user_id and item_owner_user_id and owner_user_id == item_owner_user_id:
            return True
        # Compatibility for records created before account_key/owner_user_id were stored.
        if username and item_username and self._gallery_account_key(username) == self._gallery_account_key(item_username):
            return True
        return False

    def list_web_draw_history_for_context(self, context: dict = None, limit: int = 24) -> list:
        self.cleanup_web_draw_history()
        limit = _safe_int(limit, 24, 1, 200)
        rows = []
        for item in self.web_draw_history():
            if not self._web_draw_history_allowed(item, context):
                continue
            rows.append(dict(item))
            if len(rows) >= limit:
                break
        return rows

    def get_web_draw_history_item(self, item_id: str, context: dict = None) -> Optional[dict]:
        self.cleanup_web_draw_history()
        for item in self.web_draw_history():
            if str(item.get("id")) == str(item_id) and self._web_draw_history_allowed(item, context):
                return dict(item)
        return None

    def get_web_draw_history_image_path(self, item_id: str, context: dict = None) -> Optional[Path]:
        item = self.get_web_draw_history_item(item_id, context=context)
        if not item:
            return None
        path = Path(str(item.get("image_path") or ""))
        if path.exists() and path.is_file():
            return path
        return None

    def favorite_dir(self) -> Path:
        return self.base_dir.parent / "favorites"

    def _favorite_sidecar_path(self, path: Path) -> Path:
        path = Path(path)
        return path.with_name(f"{path.stem}.favorite.json")

    def _favorite_item_id(self, path: Path) -> str:
        try:
            key = str(Path(path).resolve(strict=False))
        except Exception:
            key = str(path)
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]

    def _read_favorite_sidecar(self, path: Path) -> dict:
        sidecar = self._favorite_sidecar_path(path)
        if not sidecar.exists():
            return {}
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_favorite_sidecar(self, path: Path, meta: dict):
        sidecar = self._favorite_sidecar_path(path)
        sidecar.write_text(json.dumps(dict(meta or {}), ensure_ascii=False, indent=2), encoding="utf-8")

    def _clean_favorite_text(self, value: str, limit: int = 500) -> str:
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+", " ", str(value or "")).strip()
        text = re.sub(r"\s+", " ", text)
        return text[: max(1, int(limit or 500))]

    def _clean_favorite_category(self, value: str) -> str:
        text = self._clean_favorite_text(value, 80)
        text = re.sub(r'[<>:"/\\|?*]+', "_", text).strip(" ._")
        return text or "未分类"

    def _favorite_path_by_id(self, item_id: str) -> Optional[Path]:
        target = str(item_id or "").strip()
        if not target:
            return None
        root = self.favorite_dir()
        if not root.exists():
            return None
        try:
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in FAVORITE_IMAGE_EXTS:
                    continue
                if self._favorite_item_id(path) == target:
                    return path
        except Exception:
            return None
        return None

    def list_favorites(self, limit: int = 600) -> list:
        root = self.favorite_dir()
        if not root.exists():
            return []
        items = []
        try:
            paths = [
                path
                for path in root.rglob("*")
                if path.is_file() and path.suffix.lower() in FAVORITE_IMAGE_EXTS
            ]
            paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception as e:
            logger.warning(f"[GPT Magic] 读取收藏图库失败: {e}")
            return []
        max_items = max(1, int(limit or 600))
        seen_hashes = set()
        for path in paths:
            if len(items) >= max_items:
                break
            try:
                stat = path.stat()
                meta = self._read_favorite_sidecar(path)
                image_hash = str(meta.get("image_sha256") or "").strip().lower()
                if not image_hash:
                    try:
                        image_hash = _file_sha256(str(path)).lower()
                    except Exception:
                        image_hash = ""
                if image_hash:
                    if image_hash in seen_hashes:
                        continue
                    seen_hashes.add(image_hash)
                character = str(meta.get("favorite_character") or meta.get("character") or path.parent.name or "未分类")
                rel = path.relative_to(root).as_posix()
                item_id = self._favorite_item_id(path)
                collectors = meta.get("collectors") if isinstance(meta.get("collectors"), list) else []
                prompt_preview = str(
                    meta.get("prompt_preview")
                    or meta.get("prompt")
                    or meta.get("reply_text")
                    or ""
                ).strip()
                design_summary = str(
                    meta.get("design_summary")
                    or meta.get("summary")
                    or meta.get("description")
                    or ""
                ).strip()
                items.append({
                    "id": item_id,
                    "character": character,
                    "file_name": path.name,
                    "relative_path": rel,
                    "image_url": f"/api/favorites/image/{item_id}",
                    "created_at": meta.get("created_at") or time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
                    "mtime": int(stat.st_mtime * 1000),
                    "bytes": stat.st_size,
                    "sender_id": str(meta.get("sender_id") or ""),
                    "sender_name": str(meta.get("sender_name") or ""),
                    "maker_name": str(
                        meta.get("maker_name")
                        or meta.get("producer_name")
                        or meta.get("creator_name")
                        or meta.get("sender_name")
                        or ""
                    ),
                    "group_id": str(meta.get("group_id") or ""),
                    "source": str(meta.get("source") or ""),
                    "model": str(meta.get("model") or ""),
                    "workflow": str(meta.get("workflow") or ""),
                    "lora_name": str(meta.get("lora_name") or ""),
                    "image_sha256": image_hash,
                    "collector_count": int(meta.get("collector_count") or len(collectors) or (1 if meta.get("sender_id") or meta.get("sender_name") else 0)),
                    "prompt_preview": prompt_preview[:500],
                    "design_summary": design_summary[:1000],
                    "local_path": str(path),
                })
            except Exception:
                continue
        return items

    def get_favorite_image_path(self, item_id: str) -> Optional[Path]:
        return self._favorite_path_by_id(item_id)

    def find_favorite_by_sha256(self, image_sha256: str) -> Optional[dict]:
        target = str(image_sha256 or "").strip().lower()
        if not target:
            return None
        for item in self.list_favorites(limit=3000):
            if str(item.get("image_sha256") or "").strip().lower() == target:
                return item
        return None

    def add_favorite_collector(self, item_id: str, collector: dict) -> Optional[dict]:
        path = self._favorite_path_by_id(item_id)
        if not path:
            return None
        meta = self._read_favorite_sidecar(path)
        collector = dict(collector or {})
        collector_key = self._clean_favorite_text(
            collector.get("collector_key") or collector.get("sender_id") or collector.get("username") or "",
            100,
        )
        clean_collector = {
            "collector_key": collector_key,
            "sender_id": self._clean_favorite_text(collector.get("sender_id") or collector.get("username") or "", 80),
            "sender_name": self._clean_favorite_text(collector.get("sender_name") or collector.get("maker_name") or "", 80),
            "source": self._clean_favorite_text(collector.get("source") or "web_draw", 120),
            "created_at": collector.get("created_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        collectors = meta.get("collectors") if isinstance(meta.get("collectors"), list) else []
        exists = False
        for entry in collectors:
            if not isinstance(entry, dict):
                continue
            existing_key = str(entry.get("collector_key") or entry.get("sender_id") or entry.get("username") or "").strip()
            if collector_key and existing_key == collector_key:
                exists = True
                break
        if not exists:
            collectors.append(clean_collector)
        if collectors:
            meta["collectors"] = collectors[-100:]
        meta["collector_count"] = max(_safe_int(meta.get("collector_count"), 0, 0, 100000), len(collectors) or 1)
        if clean_collector["sender_id"] and not str(meta.get("sender_id") or "").strip():
            meta["sender_id"] = clean_collector["sender_id"]
        if clean_collector["sender_name"] and not str(meta.get("sender_name") or "").strip():
            meta["sender_name"] = clean_collector["sender_name"]
        meta["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._write_favorite_sidecar(path, meta)
        return next((item for item in self.list_favorites(limit=3000) if str(item.get("id") or "") == str(item_id)), None)

    def update_favorite_metadata(self, item_id: str, patch: dict) -> Optional[dict]:
        path = self._favorite_path_by_id(item_id)
        if not path:
            return None
        meta = self._read_favorite_sidecar(path)
        patch = patch or {}
        if "character" in patch or "category" in patch:
            meta["favorite_character"] = self._clean_favorite_category(patch.get("character", patch.get("category")))
        if "maker" in patch or "maker_name" in patch:
            meta["maker_name"] = self._clean_favorite_text(patch.get("maker", patch.get("maker_name")), 80) or "匿名制作人"
        if "prompt" in patch or "prompt_preview" in patch:
            meta["prompt_preview"] = self._clean_favorite_text(patch.get("prompt", patch.get("prompt_preview")), 4000)
        if "summary" in patch or "design_summary" in patch:
            meta["design_summary"] = self._clean_favorite_text(patch.get("summary", patch.get("design_summary")), 4000)
        meta["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._write_favorite_sidecar(path, meta)
        return next((item for item in self.list_favorites(limit=3000) if str(item.get("id") or "") == str(item_id)), None)

    def _normalize_favorite_batch_ids(self, item_ids, limit: int = 300) -> list:
        try:
            max_items = max(1, int(limit or 300))
        except Exception:
            max_items = 300
        if isinstance(item_ids, str):
            raw_ids = re.split(r"[\s,，;；]+", item_ids)
        elif isinstance(item_ids, (list, tuple, set)):
            raw_ids = list(item_ids)
        else:
            raw_ids = []
        seen = set()
        result = []
        for raw_id in raw_ids:
            item_id = str(raw_id or "").strip()
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            result.append(item_id)
            if len(result) >= max_items:
                break
        return result

    def update_favorite_metadata_batch(self, item_ids, patch: dict, limit: int = 300) -> dict:
        ids = self._normalize_favorite_batch_ids(item_ids, limit=limit)
        patch = patch if isinstance(patch, dict) else {}
        patch = {
            key: value
            for key, value in dict(patch or {}).items()
            if key in {"character", "category", "maker", "maker_name", "prompt", "prompt_preview", "summary", "design_summary"}
        }
        result = {
            "requested": len(ids),
            "updated": 0,
            "not_found": [],
            "failed": [],
            "items": [],
        }
        if not patch:
            return result
        for item_id in ids:
            try:
                item = self.update_favorite_metadata(item_id, patch)
                if not item:
                    result["not_found"].append(item_id)
                    continue
                result["updated"] += 1
                result["items"].append(item)
            except Exception as e:
                result["failed"].append({"id": item_id, "error": str(e)[:160]})
        return result

    def delete_favorite_items_batch(self, item_ids, limit: int = 300) -> dict:
        ids = self._normalize_favorite_batch_ids(item_ids, limit=limit)
        result = {
            "requested": len(ids),
            "deleted": 0,
            "not_found": [],
            "failed": [],
        }
        for item_id in ids:
            try:
                if self.delete_favorite_item(item_id):
                    result["deleted"] += 1
                else:
                    result["not_found"].append(item_id)
            except Exception as e:
                result["failed"].append({"id": item_id, "error": str(e)[:160]})
        return result

    def delete_favorite_item(self, item_id: str) -> bool:
        path = self._favorite_path_by_id(item_id)
        if not path:
            return False
        root = self.favorite_dir().resolve(strict=False)
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except Exception:
            return False
        sidecar = self._favorite_sidecar_path(path)
        try:
            path.unlink(missing_ok=True)
            sidecar.unlink(missing_ok=True)
            return True
        except Exception as e:
            logger.warning(f"[GPT Magic] 删除收藏图失败: {e}")
            return False

    def import_favorite_image(self, image_bytes: bytes, ext: str, meta: dict = None) -> dict:
        if not image_bytes:
            raise ValueError("图片内容为空")
        if len(image_bytes) > GALLERY_IMPORT_MAX_BYTES:
            raise ValueError("图片太大")
        safe_ext = "." + re.sub(r"[^a-zA-Z0-9]", "", str(ext or "png")).lower().lstrip(".")
        if safe_ext not in FAVORITE_IMAGE_EXTS:
            safe_ext = ".png"
        meta = dict(meta or {})
        character = self._clean_favorite_category(meta.get("character") or meta.get("favorite_character") or "导入")
        folder = self.favorite_dir() / character
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{int(time.time())}-{secrets.token_hex(4)}{safe_ext}"
        path = folder / name
        path.write_bytes(image_bytes)
        sidecar = {
            "favorite_character": character,
            "maker_name": self._clean_favorite_text(meta.get("maker") or meta.get("maker_name") or "后台导入", 80),
            "prompt_preview": self._clean_favorite_text(meta.get("prompt") or meta.get("prompt_preview") or "", 4000),
            "design_summary": self._clean_favorite_text(meta.get("summary") or meta.get("design_summary") or "", 4000),
            "source": self._clean_favorite_text(meta.get("source") or "gallery_admin_import", 300),
            "image_sha256": hashlib.sha256(image_bytes).hexdigest(),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        for key, limit in (
            ("sender_id", 80),
            ("sender_name", 80),
            ("group_id", 80),
            ("model", 200),
            ("workflow", 200),
            ("lora_name", 500),
            ("model_family", 80),
            ("web_draw_history_id", 80),
        ):
            if key in meta and str(meta.get(key) or "").strip():
                sidecar[key] = self._clean_favorite_text(meta.get(key), limit)
        for key in ("seed", "width", "height"):
            if meta.get(key) not in (None, ""):
                sidecar[key] = _jsonable(meta.get(key))
        collectors = meta.get("collectors")
        if isinstance(collectors, list):
            sidecar["collectors"] = _jsonable(collectors[-100:])
            sidecar["collector_count"] = _safe_int(meta.get("collector_count"), len(collectors) or 1, 0, 100000)
        elif meta.get("collector_count") is not None:
            sidecar["collector_count"] = _safe_int(meta.get("collector_count"), 0, 0, 100000)
        self._write_favorite_sidecar(path, sidecar)
        return next((item for item in self.list_favorites(limit=3000) if str(item.get("local_path") or "") == str(path)), {})

    def list_gallery_favorites(self, limit: int = 900) -> list:
        items = []
        for item in self.list_favorites(limit=max(1, _safe_int(limit, 900, 1, 3000))):
            maker = str(
                item.get("maker_name")
                or item.get("maker")
                or item.get("producer")
                or item.get("creator")
                or item.get("sender_name")
                or ""
            ).strip()
            if not maker:
                maker = "匿名制作人"
            prompt = str(item.get("prompt_preview") or "").strip()
            items.append({
                "id": str(item.get("id") or ""),
                "character": str(item.get("character") or "未分类").strip() or "未分类",
                "maker": maker[:80],
                "prompt": prompt[:500],
                "collector_count": int(item.get("collector_count") or 0),
                "image_url": f"/api/image/{item.get('id')}",
            })
        return items

    def generation_stats_file(self) -> Path:
        return self.base_dir.parent / "daily_generation_stats.json"

    def _read_generation_stats_data(self) -> dict:
        path = self.generation_stats_file()
        if not path.exists():
            return {"version": 1, "days": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {"version": 1, "days": {}}
            if not isinstance(data.get("days"), dict):
                data["days"] = {}
            return data
        except Exception as e:
            logger.warning(f"[GPT Magic] 读取排行榜统计失败: {e}")
            return {"version": 1, "days": {}}

    def _generation_stats_day_key(self, offset_days: int = 0) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400 * int(offset_days or 0)))

    def _generation_rank_scope(self, scope: str = "today") -> tuple:
        raw = str(scope or "today").strip().lower()
        if raw in {"all", "total", "overall", "总榜", "总排行", "全部"}:
            return "all", "总榜"
        if raw in {"yesterday", "昨日", "昨天"}:
            return self._generation_stats_day_key(-1), "昨日"
        if re.match(r"^20\d{2}-\d{1,2}-\d{1,2}$", raw):
            parts = raw.split("-")
            return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}", raw
        return self._generation_stats_day_key(), "今日"

    def generation_rank(self, scope: str = "today", limit: int = 12) -> dict:
        data = self._read_generation_stats_data()
        day_key, label = self._generation_rank_scope(scope)
        rows = {}
        total = 0
        if day_key == "all":
            for day in (data.get("days") or {}).values():
                if not isinstance(day, dict):
                    continue
                total += int(day.get("total") or 0)
                for user_id, item in (day.get("users") or {}).items():
                    if not isinstance(item, dict):
                        continue
                    row = rows.setdefault(str(user_id), {"count": 0, "name": str(item.get("name") or "匿名制作人")})
                    row["count"] += int(item.get("count") or 0)
                    if str(item.get("name") or "").strip():
                        row["name"] = str(item.get("name")).strip()
        else:
            day = (data.get("days") or {}).get(day_key) or {}
            total = int(day.get("total") or 0)
            for user_id, item in (day.get("users") or {}).items():
                if isinstance(item, dict):
                    rows[str(user_id)] = {
                        "count": int(item.get("count") or 0),
                        "name": str(item.get("name") or "匿名制作人").strip() or "匿名制作人",
                    }
        sorted_rows = sorted(rows.values(), key=lambda item: (int(item.get("count") or 0), str(item.get("name") or "")), reverse=True)
        max_count = max([int(item.get("count") or 0) for item in sorted_rows] or [0])
        top_rows = []
        for index, item in enumerate(sorted_rows[: max(1, _safe_int(limit, 12, 1, 50))], 1):
            count = int(item.get("count") or 0)
            top_rows.append({
                "rank": index,
                "name": str(item.get("name") or "匿名制作人").strip()[:80] or "匿名制作人",
                "count": count,
                "percent": round((count / max_count) * 100, 2) if max_count else 0,
            })
        return {
            "scope": day_key,
            "label": label,
            "total": int(total or 0),
            "total_users": len(sorted_rows),
            "top_rows": top_rows,
            "updated_at": str(data.get("updated_at") or ""),
        }

    def build_cache_key(self, prompt: str, body: dict) -> str:
        body_copy = {k: v for k, v in (body or {}).items() if k not in {"stream", "partial_images"}}
        seed = json.dumps({"prompt": prompt, "body": body_copy}, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()

    def get_cache(self, key: str) -> Optional[dict]:
        cfg = self.config().get("cache", {})
        if not cfg.get("enabled", True):
            return None
        item = self.cache().get(str(key))
        if not item:
            return None
        path = Path(str(item.get("image_path") or ""))
        if not path.exists():
            self.cache().pop(str(key), None)
            self.save()
            return None
        ttl = int(cfg.get("ttl_sec", 604800) or 0)
        if ttl > 0 and _now_ms() - int(item.get("created_at", 0) or 0) > ttl * 1000:
            self.cache().pop(str(key), None)
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            self.save()
            return None
        return item

    def put_cache(self, key: str, image_path: Path, meta: dict):
        cfg = self.config().get("cache", {})
        if not cfg.get("enabled", True):
            return
        cache_path = self.cache_dir / Path(image_path).name
        try:
            if Path(image_path).resolve(strict=False) != cache_path.resolve(strict=False):
                shutil.copy2(image_path, cache_path)
        except Exception:
            cache_path = Path(image_path)
        self.cache()[str(key)] = {
            "key": str(key),
            "created_at": _now_ms(),
            "image_path": str(cache_path),
            **{k: _jsonable(v) for k, v in (meta or {}).items()},
        }
        self.prune_cache()
        self.save()

    def mark_cache_hit(self):
        stats = self.config().setdefault("stats", {})
        stats["total_cache_hit"] = int(stats.get("total_cache_hit", 0) or 0) + 1
        self.save()

    def prune_cache(self):
        max_entries = max(1, int(self.config().get("cache", {}).get("max_entries", 500) or 500))
        items = sorted(self.cache().items(), key=lambda kv: int(kv[1].get("created_at", 0) or 0), reverse=True)
        for key, item in items[max_entries:]:
            path = Path(str(item.get("image_path") or ""))
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            self.cache().pop(key, None)


class OpenAIImageProvider:
    def __init__(self, store: GptMagicStore):
        self.store = store

    def resolve_size(self, prompt: str) -> dict:
        draw = self.store.config().get("draw", {})
        if draw.get("enable_custom_size") and draw.get("custom_size"):
            normalized = _normalize_size(draw.get("custom_size", ""))
            if normalized:
                return {
                    "size": normalized,
                    "aspect_preset": "custom",
                    "ratio": _ratio_from_size(normalized),
                    "resolution": _resolution_from_size(normalized),
                    "custom": True,
                    "source": "custom_size",
                }

        default_resolution = _normalize_resolution(draw.get("default_resolution", "4k"), "4k")
        preset = str(draw.get("aspect_strategy", "auto") or "auto")
        ratio = ""
        resolution = default_resolution
        source = "config"

        if preset != "auto":
            ratio, preset_resolution = GPT_ASPECT_PRESETS.get(preset.lower(), ("", None))
            resolution = _normalize_resolution(preset_resolution or default_resolution, default_resolution)

        if not ratio and draw.get("enable_prompt_aspect_detect", True):
            ratio, source = _detect_prompt_ratio(prompt)
            resolution = _detect_prompt_resolution(prompt, default_resolution)

        if not ratio:
            ratio = "1:1"
            source = "default"

        resolution = _normalize_resolution(resolution, default_resolution)
        size = GPT_IMAGE_SIZE_MAP.get(resolution, GPT_IMAGE_SIZE_MAP["4k"]).get(ratio)
        if not size:
            ratio = "1:1"
            size = GPT_IMAGE_SIZE_MAP[resolution]["1:1"]
        return {
            "size": size,
            "aspect_preset": preset if preset != "auto" else source,
            "ratio": ratio,
            "resolution": resolution,
            "custom": False,
            "source": source,
        }

    def resolve_model(self, size_info: dict) -> str:
        draw = self.store.config().get("draw", {})
        base_model = str(draw.get("model", "gpt-image-2") or "gpt-image-2").strip()
        if not bool(draw.get("auto_model_by_resolution", True)):
            return base_model
        resolution = str((size_info or {}).get("resolution") or "").lower()
        if resolution == "4k":
            return str(draw.get("model_4k") or base_model).strip() or base_model
        if resolution == "2k":
            return str(draw.get("model_2k") or base_model).strip() or base_model
        return base_model

    def endpoint(self) -> str:
        draw = self.store.config().get("draw", {})
        api_url = str(draw.get("api_url", "") or "").strip()
        if api_url:
            return api_url
        base_url = str(draw.get("base_url", "") or "").strip().rstrip("/")
        if not base_url:
            raise RuntimeError("未配置 GPT 生图接口地址，请在 WebUI 的 GPT 魔法区填写 API Base 或 API URL")
        suffix = "/images/generations" if base_url.endswith("/v1") else "/v1/images/generations"
        return f"{base_url}{suffix}"

    def edit_endpoint(self) -> str:
        draw = self.store.config().get("draw", {})
        edit_api_url = str(draw.get("edit_api_url", "") or "").strip()
        if edit_api_url:
            return edit_api_url
        api_url = str(draw.get("api_url", "") or "").strip()
        if api_url:
            if "/images/generations" in api_url:
                return api_url.replace("/images/generations", "/images/edits")
            return api_url.rstrip("/") + "/edits"
        base_url = str(draw.get("base_url", "") or "").strip().rstrip("/")
        if not base_url:
            raise RuntimeError("未配置 GPT 图生图接口地址，请在 WebUI 填写 API Base、API URL 或 Edit API URL")
        suffix = "/images/edits" if base_url.endswith("/v1") else "/v1/images/edits"
        return f"{base_url}{suffix}"

    def build_body(self, prompt: str, stream: Optional[bool] = None) -> dict:
        draw = self.store.config().get("draw", {})
        size_info = self.resolve_size(prompt)
        body = {
            "model": self.resolve_model(size_info),
            "prompt": prompt,
            "n": 1,
            "size": size_info["size"],
            "resolution": size_info.get("resolution") or "4k",
        }
        quality = str(draw.get("quality", "high") or "high").strip()
        if not quality or quality.lower() in {"standard", "normal", "auto"}:
            quality = "high"
        body["quality"] = quality
        if draw.get("background"):
            body["background"] = draw.get("background")
        if draw.get("moderation"):
            body["moderation"] = draw.get("moderation")
        extra = draw.get("extra_body", {})
        if isinstance(extra, dict):
            body.update(extra)

        model = str(body.get("model", ""))
        if re.match(r"^gpt-image", model, re.I):
            body.pop("response_format", None)
            body["quality"] = str(body.get("quality") or "high")
            body["resolution"] = str(body.get("resolution") or size_info.get("resolution") or "4k")
            body["output_format"] = body.get("output_format") or draw.get("output_format", "png") or "png"
            if body["output_format"] in {"jpeg", "webp"}:
                body["output_compression"] = _safe_int(
                    body.get("output_compression", draw.get("output_compression", 100)),
                    100,
                    0,
                    100,
                )
            else:
                body.pop("output_compression", None)
            use_stream = bool(draw.get("stream", True)) if stream is None else bool(stream)
            if use_stream:
                body["stream"] = True
                body.setdefault("partial_images", 1)
            else:
                body.pop("stream", None)
                body.pop("partial_images", None)
        else:
            body["response_format"] = body.get("response_format", "b64_json")

        body["__jhm_meta"] = size_info
        return body

    def build_edit_body(self, prompt: str, source_image_path: str = "", include_fidelity: bool = True, minimal: bool = False, source_image_paths=None) -> dict:
        draw = self.store.config().get("draw", {})
        body = self.build_body(prompt, stream=False)
        body.pop("stream", None)
        body.pop("partial_images", None)
        if minimal:
            keep = {"model", "prompt", "n", "size", "response_format", "__jhm_meta"}
            body = {key: value for key, value in body.items() if key in keep}
        body["__jhm_meta"]["mode"] = "edit"
        image_paths = _normalize_image_paths(source_image_paths or source_image_path)
        body["__jhm_meta"]["source_sha256"] = [_file_sha256(path) for path in image_paths]
        body["__jhm_meta"]["source_name"] = ", ".join([Path(path).name for path in image_paths])
        body["__jhm_meta"]["source_count"] = len(image_paths)
        body["__jhm_meta"]["minimal"] = bool(minimal)
        fidelity = str(draw.get("input_fidelity", "") or "").strip()
        if include_fidelity and fidelity and not minimal:
            body["input_fidelity"] = fidelity
        return body

    async def _read_json(self, resp: aiohttp.ClientResponse) -> dict:
        text = await resp.text()
        try:
            return json.loads(text) if text else {}
        except Exception:
            if not (200 <= int(resp.status) < 300):
                raise RuntimeError(f"HTTP {resp.status}: {text[:500]}")
            raise RuntimeError(f"接口返回了非 JSON 数据: {text[:300]}")

    async def _read_image_or_json(self, resp: aiohttp.ClientResponse) -> dict:
        content_type = str(resp.headers.get("content-type", "")).lower()
        if "image/" in content_type or "application/octet-stream" in content_type:
            image_bytes = await resp.read()
            if not image_bytes:
                raise RuntimeError("接口返回了空图片数据")
            return {"data": [{"b64_json": base64.b64encode(image_bytes).decode("ascii")}]}
        return await self._read_json(resp)

    async def _read_streaming(self, resp: aiohttp.ClientResponse) -> dict:
        completed = None
        latest_partial = None
        raw_events = []
        buffer = ""
        async for chunk in resp.content.iter_chunked(8192):
            buffer += chunk.decode("utf-8", errors="ignore")
            parts = re.split(r"\r?\n\r?\n", buffer)
            buffer = parts.pop() if parts else ""
            for record in parts:
                event = _parse_sse_record(record)
                if not event:
                    continue
                raw_events.append(event)
                if event.get("error"):
                    err = event.get("error")
                    raise RuntimeError(err.get("message") if isinstance(err, dict) else str(err))
                event_type = str(event.get("type", ""))
                if "completed" in event_type and event.get("b64_json"):
                    completed = event
                elif "partial_image" in event_type and event.get("b64_json"):
                    latest_partial = event
                elif event.get("b64_json") and not completed:
                    completed = event
        if buffer.strip():
            event = _parse_sse_record(buffer)
            if event:
                raw_events.append(event)
                if event.get("b64_json"):
                    completed = event
        final = completed or latest_partial
        if not final or not final.get("b64_json"):
            raise RuntimeError("流式图片接口没有返回最终图片")
        return {
            "created": final.get("created_at") or _now_ms(),
            "data": [{"b64_json": final.get("b64_json")}],
            "usage": final.get("usage"),
            "streamEvents": [
                {
                    "type": item.get("type"),
                    "created_at": item.get("created_at"),
                    "size": item.get("size"),
                    "quality": item.get("quality"),
                }
                for item in raw_events
            ],
        }

    async def _request_once(self, body: dict) -> Tuple[dict, bool, bool]:
        draw = self.store.config().get("draw", {})
        timeout = aiohttp.ClientTimeout(total=_safe_int(draw.get("timeout_sec", 600), 600, 1))
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if draw.get("api_key"):
            headers["Authorization"] = f"Bearer {draw.get('api_key')}"
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(self.endpoint(), headers=headers, json={k: v for k, v in body.items() if k != "__jhm_meta"}) as resp:
                content_type = str(resp.headers.get("content-type", "")).lower()
                if not (200 <= int(resp.status) < 300):
                    text = await resp.text()
                    if body.get("stream") and _stream_unsupported(resp.status, text):
                        return {"error": text}, False, True
                    raise RuntimeError(f"HTTP {resp.status}: {text[:500]}")
                if body.get("stream") and ("event-stream" in content_type or "stream" in content_type):
                    return await self._read_streaming(resp), True, False
                return await self._read_image_or_json(resp), False, False

    async def _request_edit_once(self, body: dict, source_image_paths) -> Tuple[dict, bool]:
        draw = self.store.config().get("draw", {})
        timeout = aiohttp.ClientTimeout(total=_safe_int(draw.get("timeout_sec", 600), 600, 1))
        headers = {"Accept": "application/json"}
        if draw.get("api_key"):
            headers["Authorization"] = f"Bearer {draw.get('api_key')}"
        form = aiohttp.FormData()
        image_paths = _normalize_image_paths(source_image_paths)
        for key, value in body.items():
            if key == "__jhm_meta":
                continue
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            form.add_field(key, str(value))
        files = []
        try:
            for source_image_path in image_paths:
                fp = open(source_image_path, "rb")
                files.append(fp)
                form.add_field(
                    "image",
                    fp,
                    filename=Path(source_image_path).name or "source.png",
                    content_type=_guess_mime(source_image_path),
                )
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self.edit_endpoint(), headers=headers, data=form) as resp:
                    if not (200 <= int(resp.status) < 300):
                        text = await resp.text()
                        if body.get("input_fidelity") and _edit_retry_without_fidelity(resp.status, text):
                            return {"error": text}, True
                        raise RuntimeError(f"HTTP {resp.status}: {text[:500]}")
                    return await self._read_image_or_json(resp), False
        finally:
            for fp in files:
                try:
                    fp.close()
                except Exception:
                    pass

    async def _download_url(self, url: str) -> bytes:
        draw = self.store.config().get("draw", {})
        timeout = aiohttp.ClientTimeout(total=_safe_int(draw.get("timeout_sec", 600), 600, 1))
        headers = {}
        if draw.get("api_key"):
            headers["Authorization"] = f"Bearer {draw.get('api_key')}"
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if not (200 <= int(resp.status) < 300):
                    text = await resp.text()
                    raise RuntimeError(f"下载图片失败 HTTP {resp.status}: {text[:300]}")
                return await resp.read()

    async def _build_result(self, data: dict, body: dict, streamed: bool, mode: str) -> GptMagicResult:
        first = _pick_image_object(data)
        if first is None:
            raise RuntimeError("生图接口没有返回图片数据")
        image_bytes = _decode_image_object(first)
        if not image_bytes and isinstance(first, str) and re.match(r"^https?://", first.strip(), re.I):
            image_bytes = await self._download_url(first.strip())
        if not image_bytes and isinstance(first, dict):
            image_url = first.get("url") or first.get("image_url")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            if image_url:
                image_bytes = await self._download_url(str(image_url))
        if not image_bytes:
            raise RuntimeError("接口返回里没有可用图片字段")

        meta = _detect_image_meta(image_bytes)
        return GptMagicResult(
            image_bytes=image_bytes,
            image_ext=meta["ext"],
            mime=meta["mime"],
            width=meta["width"],
            height=meta["height"],
            bytes_size=meta["bytes"],
            raw=data if isinstance(data, dict) else {"raw": data},
            parameters=body,
            streamed=streamed,
            mode=mode,
        )

    async def edit(self, prompt: str, source_image_path: str = "", source_image_paths=None) -> GptMagicResult:
        image_paths = _normalize_image_paths(source_image_paths or source_image_path)
        image_paths = [path for path in image_paths if Path(path).exists()]
        if not image_paths:
            raise RuntimeError("图生图源图不存在或无法读取")
        body = self.build_edit_body(prompt, include_fidelity=True, source_image_paths=image_paths)
        logger.info(
            "[GPT Magic] OpenAI 图片编辑请求: "
            f"model={body.get('model')} size={body.get('size')} images={len(image_paths)} "
            f"resolution={body.get('resolution')} quality={body.get('quality')} format={body.get('output_format')} "
            f"source={', '.join([Path(path).name for path in image_paths[:4]])}"
        )
        try:
            data, retry_without_fidelity = await self._request_edit_once(body, image_paths)
        except Exception as first_error:
            if re.search(r"HTTP\s+(401|403)", str(first_error), re.I):
                raise
            logger.warning(f"[GPT Magic] 图生图标准请求失败，尝试最小兼容参数重试: {first_error}")
            body = self.build_edit_body(prompt, include_fidelity=False, minimal=True, source_image_paths=image_paths)
            try:
                data, retry_without_fidelity = await self._request_edit_once(body, image_paths)
            except Exception as second_error:
                raise RuntimeError(
                    "GPT 图生图接口请求失败。请确认当前中转支持 /v1/images/edits，"
                    f"或在 WebUI 填写正确的 Edit API URL。原始错误：{second_error}"
                ) from second_error
        if retry_without_fidelity:
            logger.warning("[GPT Magic] 当前接口不支持 input_fidelity，已自动移除后重试")
            body = self.build_edit_body(prompt, include_fidelity=False, source_image_paths=image_paths)
            try:
                data, retry_without_fidelity = await self._request_edit_once(body, image_paths)
            except Exception as second_error:
                logger.warning(f"[GPT Magic] 移除 input_fidelity 后仍失败，尝试最小兼容参数重试: {second_error}")
                body = self.build_edit_body(prompt, include_fidelity=False, minimal=True, source_image_paths=image_paths)
                data, retry_without_fidelity = await self._request_edit_once(body, image_paths)
        return await self._build_result(data, body, streamed=False, mode="edit")

    async def generate(self, prompt: str, source_image_path: str = None, source_image_paths=None) -> GptMagicResult:
        image_paths = _normalize_image_paths(source_image_paths or source_image_path)
        if image_paths:
            return await self.edit(prompt, source_image_paths=image_paths)

        body = self.build_body(prompt)
        logger.info(
            "[GPT Magic] OpenAI 图片请求: "
            f"model={body.get('model')} size={body.get('size')} "
            f"resolution={body.get('resolution')} quality={body.get('quality')} "
            f"format={body.get('output_format')} stream={bool(body.get('stream'))}"
        )
        data, streamed, fallback = await self._request_once(body)
        if fallback:
            logger.warning("[GPT Magic] 当前中转不支持 stream 参数，已回退为非流式请求")
            body = self.build_body(prompt, stream=False)
            data, streamed, fallback = await self._request_once(body)

        return await self._build_result(data, body, streamed=streamed, mode="generation")


class GptMagicService:
    def __init__(self, plugin_config: dict, data_dir: Path, owner=None):
        self.plugin_config = plugin_config if isinstance(plugin_config, dict) else {}
        self.owner = owner
        schema_conf = {}
        if isinstance(plugin_config, dict):
            schema_conf = plugin_config.get("gpt_magic_settings", {}) or {}
        self.store = GptMagicStore(Path(data_dir), schema_conf=schema_conf)
        self.provider = OpenAIImageProvider(self.store)
        self.pending: List[dict] = []
        self.active_tasks: List[dict] = []
        self._running_tasks = set()
        self._queue_lock = asyncio.Lock()
        self.processing = False
        self.current_task: Optional[dict] = None
        self._pump_task: Optional[asyncio.Task] = None
        self.webui: Optional["GptMagicWebUI"] = None
        self.gallery_webui: Optional["FavoriteGalleryWebUI"] = None

    def _config_bool(self, value, default: bool = True) -> bool:
        if value is None:
            return bool(default)
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"0", "false", "off", "no", "none", "disable", "disabled", "关闭"}:
            return False
        if text in {"1", "true", "on", "yes", "enable", "enabled", "开启"}:
            return True
        return bool(default)

    def _watermark_options(self) -> dict:
        output_conf = self.plugin_config.get("output_settings", {}) if isinstance(self.plugin_config.get("output_settings", {}), dict) else {}
        watermark_conf = output_conf.get("watermark", {}) if isinstance(output_conf.get("watermark", {}), dict) else {}
        enabled = watermark_conf.get("enabled", output_conf.get("watermark_enabled", True))
        watermark_size = (
            watermark_conf.get("size")
            if watermark_conf.get("size") not in (None, "")
            else watermark_conf.get("watermark_size")
        )
        if watermark_size in (None, ""):
            watermark_size = output_conf.get("watermark_size", output_conf.get("watermark_width_ratio", 0.24))
        return {
            "enabled": self._config_bool(enabled, True),
            "watermark_path": str(
                watermark_conf.get("path")
                or output_conf.get("watermark_path")
                or DEFAULT_WATERMARK_PATH
            ),
            "position": str(watermark_conf.get("position") or output_conf.get("watermark_position") or "bottom_left"),
            "width_ratio": watermark_conf.get("width_ratio", watermark_size),
            "opacity": watermark_conf.get("opacity", output_conf.get("watermark_opacity", 0.76)),
            "margin_ratio": watermark_conf.get("margin_ratio", output_conf.get("watermark_margin_ratio", 0.035)),
        }

    def _apply_output_watermark(self, image_path: Path, source: str = "gpt_magic") -> dict:
        options = self._watermark_options()
        if not options.get("enabled", True):
            return {"applied": False, "reason": "disabled"}
        info = apply_watermark_to_image_file(
            image_path,
            watermark_path=options.get("watermark_path"),
            width_ratio=options.get("width_ratio", 0.24),
            opacity=options.get("opacity", 0.76),
            margin_ratio=options.get("margin_ratio", 0.035),
            position=options.get("position", "bottom_left"),
        )
        if info.get("applied"):
            logger.info(f"[GPT Magic] 已添加酒狐水印 | source={source} | image={Path(image_path).name}")
        else:
            logger.warning(f"[GPT Magic] 酒狐水印添加失败 | source={source} | {info.get('reason') or '-'}")
        return info

    def enabled(self) -> bool:
        return bool(self.store.config().get("enabled", True))

    def _max_concurrent_tasks(self) -> int:
        return _safe_int(self.store.config().get("max_concurrent_tasks", 2), 2, 1, 8)

    def _active_count(self) -> int:
        return len(self.active_tasks)

    def _sync_processing_state(self):
        self.processing = bool(self.active_tasks)
        self.current_task = self.active_tasks[0] if self.active_tasks else None

    def queue_snapshot(self) -> dict:
        active = [self._task_snapshot(task) for task in self.active_tasks]
        return {
            "processing": bool(active),
            "current": active[0] if active else None,
            "active": active,
            "running_count": len(active),
            "max_concurrent_tasks": self._max_concurrent_tasks(),
            "pending_count": len(self.pending),
            "pending": [
                {"index": idx + 1, **self._task_snapshot(task)}
                for idx, task in enumerate(self.pending)
            ],
        }

    def _task_snapshot(self, task: Optional[dict]) -> Optional[dict]:
        if not task:
            return None
        return {
            "user_id": task.get("user_id"),
            "user_name": task.get("user_name"),
            "prompt": task.get("prompt"),
            "created_at": task.get("created_at"),
            "started_at": task.get("started_at"),
        }

    def has_task_for_user(self, user_id: str) -> bool:
        uid = str(user_id)
        if any(task.get("user_id") == uid for task in self.active_tasks):
            return True
        return any(task.get("user_id") == uid for task in self.pending)

    async def enqueue(self, task: dict) -> int:
        if not self.enabled():
            raise RuntimeError("GPT 魔法生图已关闭")
        async with self._queue_lock:
            max_queue = max(1, int(self.store.config().get("max_queue_size", 100) or 100))
            if len(self.pending) + self._active_count() >= max_queue:
                raise RuntimeError("当前 GPT 魔法队列已满，请稍后再试")
            if self.has_task_for_user(task.get("user_id")):
                raise RuntimeError("你已经有一张 GPT 魔法图正在生成或排队，请等它完成后再发")
            task["created_at"] = _now_ms()
            self.pending.append(task)
            position = self._active_count() + len(self.pending)
            self._dispatch_locked()
            return position

    def _dispatch_locked(self):
        max_workers = self._max_concurrent_tasks()
        while self.pending and self._active_count() < max_workers:
            task = self.pending.pop(0)
            task["started_at"] = _now_ms()
            self.active_tasks.append(task)
            runner = asyncio.create_task(self._run_task(task))
            self._running_tasks.add(runner)
            runner.add_done_callback(lambda done, item=task: asyncio.create_task(self._task_done(item, done)))
        self._sync_processing_state()

    async def _run_task(self, task: dict):
        await self._handle_task(task)

    async def _task_done(self, task: dict, runner: asyncio.Task):
        try:
            runner.result()
        except asyncio.CancelledError:
            logger.warning("[GPT Magic] 队列任务被取消")
        except Exception as e:
            logger.error(f"[GPT Magic] 队列任务异常退出: {e}")
        finally:
            async with self._queue_lock:
                self._running_tasks.discard(runner)
                self.active_tasks = [item for item in self.active_tasks if item is not task]
                self._dispatch_locked()

    async def _pump(self):
        async with self._queue_lock:
            self._dispatch_locked()

    async def _handle_task(self, task: dict):
        send_plain = task["send_plain"]
        send_result = task["send_result"]
        user_id = str(task.get("user_id"))
        prompt = str(task.get("prompt") or "").strip()
        is_admin = bool(task.get("is_admin"))
        force_refresh = bool(task.get("force_refresh"))
        source_image_paths = _normalize_image_paths(task.get("source_image_paths") or task.get("source_image_path"))
        source_image_path = source_image_paths[0] if source_image_paths else ""
        source_image_names = ", ".join([Path(path).name for path in source_image_paths])
        is_edit = bool(source_image_paths)
        started = time.perf_counter()
        cfg = self.store.config()

        user = self.store.get_user(user_id)
        if not user and is_admin:
            _, user = self.store.register_user(
                user_id,
                task.get("user_name", ""),
                task.get("group_id", ""),
                task.get("group_name", ""),
            )
        if not user:
            await send_plain("⚠️ 你还没有注册 GPT 魔法，请先发送：酒狐魔法注册")
            return
        self.store.touch_user(user_id, task.get("user_name", ""), task.get("group_id", ""), task.get("group_name", ""))

        if user.get("blacklisted"):
            await send_plain("🚫 你当前无法使用 GPT 魔法生图，请联系管理员。")
            return
        if not cfg.get("draw_enabled", True):
            reason = cfg.get("draw_close_reason") or "当前 GPT 魔法生图已暂停，请稍后再试。"
            await send_plain(f"⚠️ {reason}")
            return
        missing_sources = [path for path in source_image_paths if not Path(path).exists()]
        if is_edit and missing_sources:
            await send_plain("❌ 没有找到可用的引用图/附带图，GPT 魔法图生图无法开始")
            return

        body_for_cache = (
            self.provider.build_edit_body(prompt, include_fidelity=True, source_image_paths=source_image_paths)
            if is_edit else self.provider.build_body(prompt, stream=False)
        )
        cache_meta = body_for_cache.get("__jhm_meta") if isinstance(body_for_cache.get("__jhm_meta"), dict) else {}
        cache_key = self.store.build_cache_key(prompt, body_for_cache)
        cache_item = None if force_refresh else self.store.get_cache(cache_key)
        cache_consumes = bool(cfg.get("cache", {}).get("consume_quota_on_hit", True))
        should_consume = not is_admin and (cache_consumes or cache_item is None)
        if should_consume and not self.store.consume_quota(user_id):
            await send_plain("❌ GPT 魔法额度不足，请联系管理员加额度或使用：酒狐魔法兑换 <卡密>")
            return

        try:
            if cache_item:
                cache_source = Path(str(cache_item.get("image_path") or ""))
                history_path = cache_source
                if cache_source.exists():
                    history_path = self.store.save_image(
                        cache_source.read_bytes(),
                        cache_source.suffix.lstrip(".") or "png",
                        folder="history",
                    )
                watermark_meta = cache_item.get("watermark") if isinstance(cache_item.get("watermark"), dict) else {}
                watermark_applied = bool(cache_item.get("watermark_applied"))
                if not watermark_applied:
                    watermark_meta = self._apply_output_watermark(history_path, source="gpt_magic_cache")
                    watermark_applied = bool(watermark_meta.get("applied"))
                image_width = watermark_meta.get("width") or cache_item.get("width", 0)
                image_height = watermark_meta.get("height") or cache_item.get("height", 0)
                image_bytes = watermark_meta.get("bytes") or cache_item.get("bytes", 0)
                self.store.mark_cache_hit()
                self.store.mark_success(user_id, bypass=is_admin)
                history_item = self.store.add_history({
                    "user_id": user_id,
                    "user_name": task.get("user_name", ""),
                    "group_id": task.get("group_id", ""),
                    "group_name": task.get("group_name", ""),
                    "prompt": prompt,
                    "mode": "edit" if is_edit else "generation",
                    "from_cache": True,
                    "image_path": str(history_path),
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "model": cache_item.get("model") or body_for_cache.get("model"),
                    "request_size": cache_item.get("request_size") or body_for_cache.get("size"),
                    "resolution": cache_item.get("resolution") or body_for_cache.get("resolution") or cache_meta.get("resolution"),
                    "ratio": cache_item.get("ratio") or cache_meta.get("ratio"),
                    "quality": cache_item.get("quality") or body_for_cache.get("quality"),
                    "output_format": cache_item.get("output_format") or body_for_cache.get("output_format"),
                    "aspect_preset": cache_item.get("aspect_preset") or cache_meta.get("aspect_preset"),
                    "width": image_width,
                    "height": image_height,
                    "bytes": image_bytes,
                    "parameters": cache_item.get("parameters", body_for_cache),
                    "watermark_applied": watermark_applied,
                    "watermark": watermark_meta,
                    "source_image_name": cache_item.get("source_image_name", ""),
                    "source_image_count": cache_item.get("source_image_count", 0),
                })
                await send_result({
                    "prompt": prompt,
                    "image_path": str(history_path),
                    "mode": "edit" if is_edit else "generation",
                    "from_cache": True,
                    "elapsed_seconds": time.perf_counter() - started,
                    "model": history_item.get("model"),
                    "request_size": history_item.get("request_size"),
                    "resolution": history_item.get("resolution"),
                    "ratio": history_item.get("ratio"),
                    "quality": history_item.get("quality"),
                    "output_format": history_item.get("output_format"),
                    "aspect_preset": history_item.get("aspect_preset"),
                    "width": history_item.get("width"),
                    "height": history_item.get("height"),
                    "bytes": history_item.get("bytes"),
                    "watermark_applied": watermark_applied,
                    "watermark": watermark_meta,
                    "quota": self.store.get_user(user_id).get("quota") if self.store.get_user(user_id) else 0,
                    "history_id": history_item.get("id"),
                    "source_image_name": history_item.get("source_image_name", ""),
                    "source_image_count": history_item.get("source_image_count", 0),
                })
                return

            if cfg.get("send_processing_message", True):
                processing_template = (
                    cfg.get("edit_processing_message")
                    if is_edit
                    else cfg.get("processing_message")
                )
                processing_message = _format_template(
                    processing_template,
                    prompt=prompt,
                    mode="图生图" if is_edit else "文生图",
                    user_id=user_id,
                    user_name=task.get("user_name", ""),
                    source_image=source_image_names,
                    source_image_count=len(source_image_paths),
                )
                if processing_message:
                    await send_plain(processing_message)
            result = await self.provider.generate(prompt, source_image_paths=source_image_paths or None)
            image_path = self.store.save_image(result.image_bytes, result.image_ext, folder="history")
            watermark_meta = self._apply_output_watermark(image_path, source="gpt_magic")
            watermark_applied = bool(watermark_meta.get("applied"))
            image_width = watermark_meta.get("width") or result.width
            image_height = watermark_meta.get("height") or result.height
            image_bytes = watermark_meta.get("bytes") or result.bytes_size
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            params = {k: _jsonable(v) for k, v in result.parameters.items()}
            result_meta = params.get("__jhm_meta") if isinstance(params.get("__jhm_meta"), dict) else {}
            history_item = self.store.add_history({
                "user_id": user_id,
                "user_name": task.get("user_name", ""),
                "group_id": task.get("group_id", ""),
                "group_name": task.get("group_name", ""),
                "prompt": prompt,
                "mode": result.mode,
                "from_cache": False,
                "image_path": str(image_path),
                "duration_ms": elapsed_ms,
                "width": image_width,
                "height": image_height,
                "bytes": image_bytes,
                "model": params.get("model"),
                "request_size": params.get("size"),
                "resolution": params.get("resolution") or result_meta.get("resolution"),
                "ratio": result_meta.get("ratio"),
                "quality": params.get("quality"),
                "output_format": params.get("output_format") or params.get("response_format"),
                "aspect_preset": result_meta.get("aspect_preset", ""),
                "custom_size": bool(result_meta.get("custom")),
                "streamed": result.streamed,
                "parameters": params,
                "watermark_applied": watermark_applied,
                "watermark": watermark_meta,
                "source_image_name": source_image_names,
                "source_image_count": len(source_image_paths),
            })
            self.store.put_cache(
                cache_key,
                image_path,
                {
                    "model": params.get("model"),
                    "request_size": params.get("size"),
                    "resolution": params.get("resolution") or result_meta.get("resolution"),
                    "ratio": result_meta.get("ratio"),
                    "quality": params.get("quality"),
                    "output_format": params.get("output_format") or params.get("response_format"),
                    "aspect_preset": result_meta.get("aspect_preset", ""),
                    "width": image_width,
                    "height": image_height,
                    "bytes": image_bytes,
                    "parameters": params,
                    "watermark_applied": watermark_applied,
                    "watermark": watermark_meta,
                    "source_image_name": source_image_names,
                    "source_image_count": len(source_image_paths),
                },
            )
            self.store.mark_success(user_id, bypass=is_admin)
            await send_result({
                "prompt": prompt,
                "image_path": str(image_path),
                "mode": result.mode,
                "from_cache": False,
                "elapsed_seconds": elapsed_ms / 1000,
                "model": params.get("model"),
                "request_size": params.get("size"),
                "resolution": params.get("resolution") or result_meta.get("resolution"),
                "ratio": result_meta.get("ratio"),
                "quality": params.get("quality"),
                "output_format": params.get("output_format") or params.get("response_format"),
                "aspect_preset": result_meta.get("aspect_preset", ""),
                "width": image_width,
                "height": image_height,
                "bytes": image_bytes,
                "streamed": result.streamed,
                "watermark_applied": watermark_applied,
                "watermark": watermark_meta,
                "quota": self.store.get_user(user_id).get("quota") if self.store.get_user(user_id) else 0,
                "history_id": history_item.get("id"),
                "source_image_name": history_item.get("source_image_name", ""),
                "source_image_count": history_item.get("source_image_count", 0),
            })
        except Exception as e:
            if should_consume:
                self.store.refund_quota(user_id)
            self.store.mark_fail(user_id)
            self.store.add_history({
                "user_id": user_id,
                "user_name": task.get("user_name", ""),
                "group_id": task.get("group_id", ""),
                "group_name": task.get("group_name", ""),
                "prompt": prompt,
                "mode": "edit" if is_edit else "generation",
                "success": False,
                "failed": True,
                "error_message": str(e),
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "model": body_for_cache.get("model"),
                "request_size": body_for_cache.get("size"),
                "resolution": body_for_cache.get("resolution") or cache_meta.get("resolution"),
                "ratio": cache_meta.get("ratio"),
                "quality": body_for_cache.get("quality"),
                "output_format": body_for_cache.get("output_format") or body_for_cache.get("response_format"),
                "source_image_name": source_image_names,
                "source_image_count": len(source_image_paths),
            })
            logger.error(f"[GPT Magic] 生图失败: {e}")
            await send_plain(f"❌ GPT 魔法生成失败：{str(e)[:160]}")

    async def start_webui(self, comfy_status_provider: Callable[[], dict] = None, comfy_action_handler: Callable = None):
        cfg = self.store.config().get("webui", {})
        if not cfg.get("enabled", True):
            return
        if self.webui:
            return
        self.webui = GptMagicWebUI(self, comfy_status_provider=comfy_status_provider, comfy_action_handler=comfy_action_handler)
        await self.webui.start(str(cfg.get("host", "0.0.0.0")), int(cfg.get("port", 1144) or 1144))

    async def stop_webui(self):
        if self.webui:
            await self.webui.stop()
            self.webui = None

    async def start_gallery_webui(self):
        cfg = self.store.config().get("gallery", {})
        if not cfg.get("enabled", True):
            return
        if self.gallery_webui:
            return
        self.gallery_webui = FavoriteGalleryWebUI(self)
        await self.gallery_webui.start(str(cfg.get("host", "0.0.0.0")), int(cfg.get("port", 6666) or 6666))

    async def stop_gallery_webui(self):
        if self.gallery_webui:
            await self.gallery_webui.stop()
            self.gallery_webui = None


class FavoriteGalleryWebUI:
    COOKIE_NAME = "wf_gallery_auth"

    def __init__(self, service: GptMagicService):
        self.service = service
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None
        self.active_host = ""
        self.active_port = 0
        self._secret = secrets.token_urlsafe(32)
        self._sessions = {}
        self._login_failures = {}
        self._draw_jobs = {}

    def _config(self) -> dict:
        return self.service.store.config().get("gallery", {}) or {}

    def _name(self) -> str:
        return str(self._config().get("name", "WineFox") or "WineFox").strip() or "WineFox"

    def _request_ip(self, request: web.Request) -> str:
        forwarded = str(request.headers.get("x-forwarded-for") or "").split(",", 1)[0].strip()
        return forwarded or str(request.remote or "")

    def _user_agent(self, request: web.Request) -> str:
        return str(request.headers.get("user-agent") or "")

    def _session_context(self, request: web.Request) -> Optional[dict]:
        provided = (
            request.cookies.get(self.COOKIE_NAME)
            or request.headers.get("x-winefox-gallery-token")
            or request.query.get("gallery_token")
            or ""
        )
        if not provided:
            return None
        provided = str(provided)
        session = self._sessions.get(provided)
        if not isinstance(session, dict):
            return None
        if _now_ms() - int(session.get("created_at") or 0) > GALLERY_SESSION_MAX_MS:
            self._sessions.pop(provided, None)
            return None
        account_key = self.service.store._gallery_account_key(session.get("username"))
        account = self.service.store.gallery_accounts().get(account_key)
        if not account or bool(account.get("disabled", False)) or not bool(account.get("activated", True)):
            self._sessions.pop(provided, None)
            return None
        session["username"] = str(account.get("username") or session.get("username") or "")
        session["role"] = str(account.get("role") or session.get("role") or "user")
        session["account_key"] = account_key
        session["owner_user_id"] = str(account.get("owner_user_id") or "")
        session["owner_name"] = str(account.get("owner_name") or "")
        return {
            "username": str(session.get("username") or ""),
            "role": str(session.get("role") or "user"),
            "account_key": str(session.get("account_key") or ""),
            "owner_user_id": str(session.get("owner_user_id") or ""),
            "owner_name": str(session.get("owner_name") or ""),
        }

    def _check_auth(self, request: web.Request) -> bool:
        return bool(self._session_context(request))

    def _require_auth(self, request: web.Request) -> dict:
        context = self._session_context(request)
        if not context:
            raise web.HTTPUnauthorized(
                text=json.dumps({"error": "账号登录已过期，请重新登录"}, ensure_ascii=False),
                content_type="application/json",
            )
        self.service.store.touch_gallery_account(context.get("username"), self._request_ip(request))
        return context

    def _require_admin(self, request: web.Request) -> dict:
        context = self._require_auth(request)
        if str(context.get("role") or "") != "admin":
            raise web.HTTPForbidden(
                text=json.dumps({"error": "需要管理员账号"}, ensure_ascii=False),
                content_type="application/json",
            )
        return context

    def _new_session_token(self, account: dict) -> str:
        token = secrets.token_urlsafe(32)
        self._sessions[token] = {
            "username": str(account.get("username") or ""),
            "role": str(account.get("role") or "user"),
            "account_key": self.service.store._gallery_account_key(account.get("username")),
            "owner_user_id": str(account.get("owner_user_id") or ""),
            "owner_name": str(account.get("owner_name") or ""),
            "created_at": _now_ms(),
        }
        return token

    def _login_failure_keys(self, username: str, ip: str) -> list:
        keys = []
        clean_username = self.service.store._gallery_account_key(username)
        if clean_username:
            keys.append(f"user:{clean_username}")
        if ip:
            keys.append(f"ip:{ip}")
        return keys or ["unknown"]

    def _login_lock_seconds(self, username: str, ip: str) -> int:
        now = _now_ms()
        locked_until = 0
        for key in self._login_failure_keys(username, ip):
            record = self._login_failures.get(key) or {}
            if now - int(record.get("first_at") or 0) > GALLERY_LOGIN_WINDOW_MS:
                self._login_failures.pop(key, None)
                continue
            locked_until = max(locked_until, int(record.get("locked_until") or 0))
        return max(0, int((locked_until - now + 999) // 1000))

    def _record_login_failure(self, username: str, ip: str) -> int:
        now = _now_ms()
        max_locked_until = 0
        for key in self._login_failure_keys(username, ip):
            record = self._login_failures.get(key) or {}
            if now - int(record.get("first_at") or 0) > GALLERY_LOGIN_WINDOW_MS:
                record = {"count": 0, "first_at": now, "locked_until": 0}
            count = int(record.get("count") or 0) + 1
            record["count"] = count
            record["first_at"] = int(record.get("first_at") or now)
            if count >= GALLERY_LOGIN_LOCK_THRESHOLD:
                extra = min(3, count - GALLERY_LOGIN_LOCK_THRESHOLD)
                record["locked_until"] = now + GALLERY_LOGIN_LOCK_MS * (2 ** extra)
            self._login_failures[key] = record
            max_locked_until = max(max_locked_until, int(record.get("locked_until") or 0))
        return max(0, int((max_locked_until - now + 999) // 1000))

    def _clear_login_failures(self, username: str, ip: str):
        for key in self._login_failure_keys(username, ip):
            self._login_failures.pop(key, None)

    def _api_manifest(self, request: web.Request = None, context: dict = None) -> dict:
        context = context if context is not None else (self._session_context(request) if request is not None else None)
        authenticated = bool(context)
        user = {
            "username": str((context or {}).get("username") or ""),
            "role": str((context or {}).get("role") or "user"),
            "is_admin": str((context or {}).get("role") or "") == "admin",
        } if context else None
        return {
            "ok": True,
            "name": self._name(),
            "public_url": str(self._config().get("public_url") or GALLERY_PUBLIC_URL),
            "authenticated": authenticated,
            "user": user,
            "version": 1,
            "features": {
                "gallery": True,
                "draw": True,
                "roles": True,
                "makers": True,
                "rank": True,
                "item_detail": True,
                "accounts": True,
                "web_register": True,
                "admin": bool(user and user.get("is_admin")),
            },
            "endpoints": {
                "register": {"method": "POST", "path": "/api/register", "body": {"username": "string", "password": "string"}},
                "login": {"method": "POST", "path": "/api/login", "body": {"username": "string", "password": "string"}},
                "logout": {"method": "POST", "path": "/api/logout"},
                "meta": {"method": "GET", "path": "/api/meta"},
                "gallery": {
                    "method": "GET",
                    "path": "/api/gallery",
                    "query": {
                        "limit": "1-3000",
                        "role": "角色名，可选",
                        "maker": "制作人，可选",
                        "q": "搜索角色/制作人/提示词，可选",
                        "sort": "default|oldest|character|maker",
                    },
                },
                "draw_page": {"method": "GET", "path": "/draw.html"},
                "draw": {"method": "POST", "path": "/api/draw", "body": {"prompt": "string"}},
                "draw_job": {"method": "GET", "path": "/api/draw/{job_id}"},
                "draw_image": {"method": "GET", "path": "/api/draw/image/{job_id}"},
                "draw_history": {"method": "GET", "path": "/api/draw/history"},
                "draw_history_image": {"method": "GET", "path": "/api/draw/history/{item_id}/image"},
                "draw_history_download": {"method": "GET", "path": "/api/draw/history/{item_id}/download"},
                "draw_history_favorite": {"method": "POST", "path": "/api/draw/history/{item_id}/favorite"},
                "draw_loras": {"method": "GET", "path": "/api/draw/loras"},
                "roles": {"method": "GET", "path": "/api/roles"},
                "makers": {"method": "GET", "path": "/api/makers"},
                "item": {"method": "GET", "path": "/api/gallery/{item_id}"},
                "rank": {"method": "GET", "path": "/api/rank", "query": {"scope": "today|yesterday|all", "limit": "1-50"}},
                "image": {"method": "GET", "path": "/api/image/{item_id}"},
                "admin_summary": {"method": "GET", "path": "/api/admin/summary"},
                "admin_accounts": {"method": "POST", "path": "/api/admin/accounts"},
                "admin_account": {"method": "PATCH|DELETE", "path": "/api/admin/accounts/{username}"},
                "admin_account_balance": {"method": "POST", "path": "/api/admin/accounts/{username}/balance", "body": {"mode": "add|set", "amount": "number", "reason": "string"}},
                "admin_account_ledger": {"method": "GET", "path": "/api/admin/accounts/{username}/ledger"},
                "admin_balance_batch": {"method": "POST", "path": "/api/admin/balance/batch", "body": {"usernames": ["string"], "mode": "add|set", "amount": "number", "reason": "string"}},
                "admin_billing": {"method": "POST", "path": "/api/admin/billing", "body": {"video_trial_balance": "number", "video_local_price": "number", "video_api_price": "number"}},
                "admin_video_config": {"method": "GET|POST", "path": "/api/admin/video-config"},
                "admin_gallery_item": {"method": "PATCH|DELETE", "path": "/api/admin/gallery/{item_id}"},
                "admin_gallery_batch": {"method": "POST", "path": "/api/admin/gallery/batch"},
                "admin_gallery_import": {"method": "POST", "path": "/api/admin/gallery/import"},
                "admin_lora_profiles": {"method": "GET", "path": "/api/admin/lora/profiles", "query": {"family": "current|anima|illustrious|all"}},
                "admin_lora_profile": {"method": "PATCH|DELETE", "path": "/api/admin/lora/profiles/{key}", "query": {"family": "anima|illustrious"}},
                "admin_lora_batch": {"method": "POST", "path": "/api/admin/lora/profiles/batch"},
            },
        }

    def _safe_gallery_limit(self, request: web.Request) -> int:
        cfg = self._config()
        default_limit = _safe_int(cfg.get("limit", 900), 900, 1, 3000)
        return _safe_int(request.query.get("limit", default_limit), default_limit, 1, 3000)

    def _gallery_items(self, request: web.Request) -> list:
        items = self.service.store.list_gallery_favorites(limit=self._safe_gallery_limit(request))
        role = str(request.query.get("role", "") or "").strip()
        maker = str(request.query.get("maker", "") or "").strip()
        query = str(request.query.get("q", "") or "").strip().lower()
        if role and role.lower() != "all":
            items = [item for item in items if str(item.get("character") or "未分类") == role]
        if maker and maker.lower() != "all":
            items = [item for item in items if str(item.get("maker") or "匿名制作人") == maker]
        if query:
            def _hit(item: dict) -> bool:
                values = (
                    item.get("character"),
                    item.get("maker"),
                    item.get("prompt"),
                )
                return any(query in str(value or "").lower() for value in values)
            items = [item for item in items if _hit(item)]
        sort = str(request.query.get("sort", "default") or "default").strip().lower()
        if sort == "oldest":
            items = list(reversed(items))
        elif sort == "character":
            items = sorted(items, key=lambda item: (str(item.get("character") or ""), str(item.get("maker") or "")))
        elif sort == "maker":
            items = sorted(items, key=lambda item: (str(item.get("maker") or ""), str(item.get("character") or "")))
        return items

    def _gallery_facets(self, items: list) -> dict:
        characters = {}
        makers = {}
        for item in items or []:
            character = str(item.get("character") or "未分类").strip() or "未分类"
            maker = str(item.get("maker") or "匿名制作人").strip() or "匿名制作人"
            characters[character] = characters.get(character, 0) + 1
            makers[maker] = makers.get(maker, 0) + 1
        return {
            "characters": [
                {"name": name, "count": count}
                for name, count in sorted(characters.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
            ],
            "makers": [
                {"name": name, "count": count}
                for name, count in sorted(makers.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
            ],
        }

    @web.middleware
    async def _security_headers_middleware(self, request: web.Request, handler):
        response = await handler(request)
        response.headers.setdefault("Cache-Control", "no-store, no-cache, must-revalidate, private")
        response.headers.setdefault("Pragma", "no-cache")
        response.headers.setdefault("Expires", "0")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "img-src 'self' data: blob: https:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        return response

    async def start(self, host: str, port: int):
        app = web.Application(middlewares=[self._security_headers_middleware])
        app.router.add_get("/", self.index)
        app.router.add_get("/gallery.html", self.index)
        app.router.add_get("/draw.html", self.draw_page)
        app.router.add_get("/api", self.meta)
        app.router.add_get("/api/meta", self.meta)
        app.router.add_post("/api/register", self.register)
        app.router.add_post("/api/login", self.login)
        app.router.add_post("/api/logout", self.logout)
        app.router.add_post("/api/draw", self.draw)
        app.router.add_get("/api/draw/history", self.draw_history)
        app.router.add_get("/api/draw/history/{item_id}/image", self.draw_history_image)
        app.router.add_get("/api/draw/history/{item_id}/download", self.draw_history_download)
        app.router.add_post("/api/draw/history/{item_id}/favorite", self.draw_history_favorite)
        app.router.add_get("/api/draw/loras", self.draw_loras)
        app.router.add_get("/api/draw/{job_id}", self.draw_status)
        app.router.add_get("/api/draw/image/{job_id}", self.draw_image)
        app.router.add_get("/api/gallery", self.gallery)
        app.router.add_get("/api/gallery/{item_id}", self.gallery_item)
        app.router.add_get("/api/roles", self.roles)
        app.router.add_get("/api/makers", self.makers)
        app.router.add_get("/api/rank", self.rank)
        app.router.add_get("/api/image/{item_id}", self.image)
        app.router.add_get("/api/admin/summary", self.admin_summary)
        app.router.add_post("/api/admin/accounts", self.admin_create_account)
        app.router.add_patch("/api/admin/accounts/{username}", self.admin_update_account)
        app.router.add_delete("/api/admin/accounts/{username}", self.admin_delete_account)
        app.router.add_post("/api/admin/accounts/{username}/balance", self.admin_adjust_balance)
        app.router.add_get("/api/admin/accounts/{username}/ledger", self.admin_balance_ledger)
        app.router.add_post("/api/admin/balance/batch", self.admin_batch_balance)
        app.router.add_post("/api/admin/billing", self.admin_save_billing)
        app.router.add_get("/api/admin/video-config", self.admin_get_video_config)
        app.router.add_post("/api/admin/video-config", self.admin_save_video_config)
        app.router.add_post("/api/admin/gallery/batch", self.admin_batch_gallery_items)
        app.router.add_patch("/api/admin/gallery/{item_id}", self.admin_update_gallery_item)
        app.router.add_delete("/api/admin/gallery/{item_id}", self.admin_delete_gallery_item)
        app.router.add_post("/api/admin/gallery/import", self.admin_import_gallery_item)
        app.router.add_get("/api/admin/lora/profiles", self.admin_lora_profiles)
        app.router.add_post("/api/admin/lora/profiles/batch", self.admin_batch_lora_profiles)
        app.router.add_patch("/api/admin/lora/profiles/{key}", self.admin_update_lora_profile)
        app.router.add_delete("/api/admin/lora/profiles/{key}", self.admin_delete_lora_profile)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, host=host, port=port)
        try:
            await self.site.start()
            self.active_host = host
            self.active_port = port
            logger.info(f"[GPT Magic] WineFox 收藏展示页已启动: http://{host}:{port}")
        except Exception as e:
            logger.error(f"[GPT Magic] WineFox 收藏展示页启动失败，已跳过: {e}")
            await self.stop()

    async def stop(self):
        if self.runner:
            try:
                await self.runner.cleanup()
            except Exception as e:
                logger.warning(f"[GPT Magic] WineFox 收藏展示页停止失败: {e}")
        self.runner = None
        self.site = None

    async def index(self, request: web.Request):
        html = (
            _load_favorite_gallery_html()
            .replace("__GALLERY_NAME__", _html_escape(self._name()))
            .replace("__GALLERY_NAME_JSON__", json.dumps(self._name(), ensure_ascii=False))
            .replace("__GALLERY_API_MANIFEST_JSON__", json.dumps(self._api_manifest(request), ensure_ascii=False))
        )
        if not html:
            raise web.HTTPInternalServerError(text="gallery template missing")
        return web.Response(text=html, content_type="text/html")

    async def draw_page(self, request: web.Request):
        html = (
            _load_winefox_draw_html()
            .replace("__GALLERY_NAME__", _html_escape(self._name()))
            .replace("__GALLERY_NAME_JSON__", json.dumps(self._name(), ensure_ascii=False))
            .replace("__GALLERY_API_MANIFEST_JSON__", json.dumps(self._api_manifest(request), ensure_ascii=False))
        )
        if not html:
            raise web.HTTPInternalServerError(text="draw template missing")
        return web.Response(text=html, content_type="text/html")

    async def meta(self, request: web.Request):
        return web.json_response(self._api_manifest(request), headers={"Cache-Control": "no-store"})

    async def register(self, request: web.Request):
        data = await request.json()
        try:
            result = self.service.store.create_pending_gallery_registration(
                data.get("username", ""),
                data.get("password", ""),
                owner_name=data.get("owner_name", ""),
            )
        except ValueError as e:
            self.service.store.record_gallery_access("register", {"username": str(data.get("username") or ""), "role": "pending"}, self._request_ip(request), self._user_agent(request), ok=False, detail=str(e))
            raise web.HTTPBadRequest(
                text=json.dumps({"error": str(e)}, ensure_ascii=False),
                content_type="application/json",
            )
        self.service.store.record_gallery_access("register", result.get("account") or {}, self._request_ip(request), self._user_agent(request), ok=True, detail="pending")
        return web.json_response(
            {
                "ok": True,
                "account": result.get("account"),
                "verify_token": result.get("token"),
                "expires_at": result.get("expires_at"),
                "command": f"酒狐注册 {result.get('token')}",
                "public_url": str(self._config().get("public_url") or GALLERY_PUBLIC_URL),
            },
            headers={"Cache-Control": "no-store"},
        )

    async def login(self, request: web.Request):
        data = await request.json()
        username = self.service.store._clean_gallery_username(data.get("username", "") or "")
        password = str(data.get("password", "") or "")
        account = None
        token = ""
        ip = self._request_ip(request)
        if not username or not password:
            self.service.store.record_gallery_access("login", {"username": username, "role": "unknown"}, ip, self._user_agent(request), ok=False, detail="missing_credentials")
            raise web.HTTPUnauthorized(
                text=json.dumps({"error": "请使用已注册账号登录"}, ensure_ascii=False),
                content_type="application/json",
            )
        lock_seconds = self._login_lock_seconds(username, ip)
        if lock_seconds > 0:
            self.service.store.record_gallery_access("login", {"username": username, "role": "unknown"}, ip, self._user_agent(request), ok=False, detail="locked")
            raise web.HTTPTooManyRequests(
                text=json.dumps({"error": f"登录尝试过多，请 {lock_seconds} 秒后再试", "retry_after": lock_seconds}, ensure_ascii=False),
                content_type="application/json",
                headers={"Retry-After": str(lock_seconds)},
            )
        account = self.service.store.verify_gallery_account(username, password, ip=ip)
        if not account:
            lock_seconds = self._record_login_failure(username, ip)
            self.service.store.record_gallery_access("login", {"username": username, "role": "unknown"}, ip, self._user_agent(request), ok=False, detail="account")
            if lock_seconds > 0:
                raise web.HTTPTooManyRequests(
                    text=json.dumps({"error": f"登录失败次数过多，请 {lock_seconds} 秒后再试", "retry_after": lock_seconds}, ensure_ascii=False),
                    content_type="application/json",
                    headers={"Retry-After": str(lock_seconds)},
                )
            raise web.HTTPUnauthorized(
                text=json.dumps({"error": "用户名或密码错误"}, ensure_ascii=False),
                content_type="application/json",
            )
        self._clear_login_failures(username, ip)
        token = self._new_session_token(account)
        self.service.store.record_gallery_access("login", account, ip, self._user_agent(request), ok=True)
        resp = web.json_response({"ok": True, "name": self._name(), "token": token, "user": account, "api": self._api_manifest(request, context=account)})
        resp.set_cookie(
            self.COOKIE_NAME,
            token,
            max_age=int(GALLERY_SESSION_MAX_MS / 1000),
            httponly=True,
            samesite="Lax",
            path="/",
        )
        return resp

    async def logout(self, request: web.Request):
        token = request.cookies.get(self.COOKIE_NAME) or request.headers.get("x-winefox-gallery-token") or ""
        if token:
            self._sessions.pop(str(token), None)
        resp = web.json_response({"ok": True})
        resp.del_cookie(self.COOKIE_NAME, path="/")
        return resp

    async def gallery(self, request: web.Request):
        context = self._require_auth(request)
        self.service.store.record_gallery_access("gallery", context, self._request_ip(request), self._user_agent(request), ok=True)
        all_items = self.service.store.list_gallery_favorites(limit=self._safe_gallery_limit(request))
        favorites = self._gallery_items(request)
        all_facets = self._gallery_facets(all_items)
        current_facets = self._gallery_facets(favorites)
        payload = {
            "ok": True,
            "name": self._name(),
            "favorites": favorites,
            "stats": {
                "total": len(favorites),
                "all_total": len(all_items),
                "characters": len(current_facets["characters"]),
                "makers": len(current_facets["makers"]),
                "character_counts": current_facets["characters"],
                "maker_counts": current_facets["makers"],
            },
            "facets": all_facets,
            "rank": self.service.store.generation_rank("today", limit=12),
            "api": self._api_manifest(request)["endpoints"],
        }
        return web.json_response(payload, headers={"Cache-Control": "no-store"})

    async def gallery_item(self, request: web.Request):
        self._require_auth(request)
        item_id = str(request.match_info.get("item_id") or "")
        for item in self.service.store.list_gallery_favorites(limit=self._safe_gallery_limit(request)):
            if str(item.get("id") or "") == item_id:
                return web.json_response({"ok": True, "item": item}, headers={"Cache-Control": "no-store"})
        raise web.HTTPNotFound(text=json.dumps({"error": "not found"}, ensure_ascii=False), content_type="application/json")

    async def roles(self, request: web.Request):
        self._require_auth(request)
        items = self.service.store.list_gallery_favorites(limit=self._safe_gallery_limit(request))
        return web.json_response(
            {"ok": True, "roles": self._gallery_facets(items)["characters"]},
            headers={"Cache-Control": "no-store"},
        )

    async def makers(self, request: web.Request):
        self._require_auth(request)
        items = self.service.store.list_gallery_favorites(limit=self._safe_gallery_limit(request))
        return web.json_response(
            {"ok": True, "makers": self._gallery_facets(items)["makers"]},
            headers={"Cache-Control": "no-store"},
        )

    async def rank(self, request: web.Request):
        context = self._require_auth(request)
        self.service.store.record_gallery_access("rank", context, self._request_ip(request), self._user_agent(request), ok=True)
        scope = str(request.query.get("scope", "today") or "today")
        limit = _safe_int(request.query.get("limit", 20), 20, 1, 50)
        return web.json_response(
            {"ok": True, "rank": self.service.store.generation_rank(scope, limit=limit)},
            headers={"Cache-Control": "no-store"},
        )

    async def image(self, request: web.Request):
        self._require_auth(request)
        path = self.service.store.get_favorite_image_path(request.match_info["item_id"])
        if not path:
            raise web.HTTPNotFound(text="not found")
        return web.FileResponse(path)

    def _draw_owner(self):
        owner = getattr(self.service, "owner", None)
        required = (
            "_register_webui_draw_queue_entry",
            "_handle_webui_draw_generate",
            "_format_elapsed",
        )
        if not owner or any(not hasattr(owner, name) for name in required):
            self._raise_json(web.HTTPServiceUnavailable, "当前 Web 绘图服务未连接 ComfyUI 插件")
        return owner

    def _draw_lora_owner(self):
        owner = getattr(self.service, "owner", None)
        required = (
            "_fetch_paintable_role_live_loras",
            "_build_paintable_role_rows",
            "_normalize_model_family",
            "_family_label",
        )
        if not owner or any(not hasattr(owner, name) for name in required):
            self._raise_json(web.HTTPServiceUnavailable, "当前 Web 绘图服务未连接 LoRA 列表")
        return owner

    def _draw_lora_payload(self, row: dict) -> dict:
        row = row if isinstance(row, dict) else {}
        return {
            "index": int(row.get("index") or 0),
            "key": str(row.get("key") or ""),
            "family": str(row.get("family") or ""),
            "character": str(row.get("character") or ""),
            "display_name": str(row.get("display_name") or row.get("character") or row.get("key") or ""),
            "lora": str(row.get("lora") or ""),
            "live_available": row.get("live_available"),
        }

    def _cleanup_draw_jobs(self):
        now = time.time()
        for job_id, job in list(self._draw_jobs.items()):
            created_at = float((job or {}).get("created_at") or now)
            status = str((job or {}).get("status") or "")
            if status in {"done", "error"} and now - created_at > 6 * 3600:
                self._draw_jobs.pop(job_id, None)
            elif now - created_at > 24 * 3600:
                self._draw_jobs.pop(job_id, None)

    def _draw_jsonable(self, value):
        try:
            return json.loads(json.dumps(value, ensure_ascii=False, default=str))
        except Exception:
            return str(value)

    def _draw_context_is_admin(self, context: dict = None) -> bool:
        return str((context or {}).get("role") or "user").lower() == "admin"

    def _draw_job_allowed(self, job: dict, context: dict = None) -> bool:
        if not isinstance(job, dict):
            return False
        if self._draw_context_is_admin(context):
            return True
        context = context or {}
        username = self.service.store._clean_gallery_username(context.get("username") or "")
        account_key = str(context.get("account_key") or self.service.store._gallery_account_key(username)).strip()
        owner_user_id = str(context.get("owner_user_id") or "").strip()
        job_account_key = str(job.get("account_key") or "").strip()
        job_owner_user_id = str(job.get("owner_user_id") or "").strip()
        job_username = self.service.store._clean_gallery_username(job.get("username") or "")
        if account_key and job_account_key and account_key == job_account_key:
            return True
        if owner_user_id and job_owner_user_id and owner_user_id == job_owner_user_id:
            return True
        if username and job_username and self.service.store._gallery_account_key(username) == self.service.store._gallery_account_key(job_username):
            return True
        return False

    def _draw_safety_payload(self, meta: dict = None) -> dict:
        meta = meta if isinstance(meta, dict) else {}
        status = str(meta.get("generated_output_audit") or "").strip().lower()
        sensitive = bool(meta.get("webui_draw_sensitive") or status in {"blocked", "error"})
        reveal_required = bool(meta.get("webui_draw_reveal_required") or sensitive)
        message = str(meta.get("generated_output_audit_message") or "").strip()
        reason = str(meta.get("generated_output_audit_reason") or meta.get("generated_output_audit_error") or "").strip()
        if meta.get("webui_draw_sensitive"):
            message = "出图图片审核失败，已默认隐藏。" if status == "error" else "出图图片审核未通过，已默认隐藏。"
        if not message:
            if status == "blocked":
                message = "出图图片审核未通过，已默认隐藏。"
            elif status == "error":
                message = "出图图片审核失败，已默认隐藏。"
        return {
            "sensitive": sensitive,
            "reveal_required": reveal_required,
            "audit_status": status,
            "risk": meta.get("generated_output_audit_risk"),
            "threshold": meta.get("generated_output_audit_threshold"),
            "backend": str(meta.get("generated_output_audit_backend") or ""),
            "reason": reason,
            "message": message,
        }

    def _draw_result_info(self, item: dict, meta: dict = None) -> dict:
        item = item or {}
        meta = meta if isinstance(meta, dict) else {}
        watermark = meta.get("watermark") if isinstance(meta.get("watermark"), dict) else {}
        width = meta.get("width") or watermark.get("width") or item.get("width") or 0
        height = meta.get("height") or watermark.get("height") or item.get("height") or 0
        size_text = f"{width}x{height}" if width and height else ""
        loras = meta.get("profile_multi_loras") or []
        if isinstance(loras, list):
            lora_text = "、".join(
                str((entry or {}).get("lora") or (entry or {}).get("profile_lora") or "").strip()
                for entry in loras
                if isinstance(entry, dict) and str((entry or {}).get("lora") or (entry or {}).get("profile_lora") or "").strip()
            )
        else:
            lora_text = ""
        if not lora_text:
            lora_text = str(meta.get("profile_lora") or meta.get("lora_name") or "").strip()
        return {
            "raw_prompt": str(item.get("raw_prompt") or meta.get("raw_prompt") or item.get("prompt") or ""),
            "final_prompt": str(item.get("final_prompt") or meta.get("translated_prompt") or ""),
            "character": str(meta.get("profile_character") or meta.get("character") or ""),
            "lora": lora_text,
            "seed": meta.get("seed") if meta.get("seed") is not None else meta.get("base_seed"),
            "workflow": str(meta.get("workflow") or meta.get("workflow_file") or meta.get("multi_lora_workflow") or ""),
            "model_family": str(meta.get("model_family") or meta.get("profile_model_family") or ""),
            "size": size_text,
            "width": width,
            "height": height,
            "bytes": meta.get("bytes") or watermark.get("bytes") or item.get("bytes") or 0,
            "elapsed_seconds": item.get("elapsed_seconds") or meta.get("webui_draw_elapsed_seconds") or meta.get("elapsed_seconds"),
            "watermark_applied": bool(meta.get("watermark_applied")),
            "watermark": watermark,
        }

    def _draw_history_payload(self, item: dict) -> dict:
        item = dict(item or {})
        item_id = str(item.get("id") or "")
        meta = self._draw_jsonable(item.get("meta") or {})
        if not isinstance(meta, dict):
            meta = {}
        safety = self._draw_safety_payload(meta)
        payload = {
            "id": item_id,
            "prompt": str(item.get("prompt") or ""),
            "raw_prompt": str(item.get("raw_prompt") or ""),
            "final_prompt": str(item.get("final_prompt") or ""),
            "created_at": item.get("created_at"),
            "expires_at": item.get("expires_at"),
            "elapsed_seconds": item.get("elapsed_seconds"),
            "image_url": f"/api/draw/history/{item_id}/image",
            "download_url": f"/api/draw/history/{item_id}/download",
            "favorite_url": f"/api/draw/history/{item_id}/favorite",
            "watermark_applied": bool(meta.get("watermark_applied")),
            "sensitive": bool(safety.get("sensitive")),
            "reveal_required": bool(safety.get("reveal_required")),
            "safety": safety,
            "info": self._draw_result_info(item, meta),
            "meta": meta,
        }
        return payload

    def _draw_job_payload(self, job_id: str, job: dict) -> dict:
        job = job or {}
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        history_item = job.get("history_item") if isinstance(job.get("history_item"), dict) else {}
        image_path = str(result.get("image_path") or job.get("image_path") or "")
        meta = self._draw_jsonable(result.get("meta") or {})
        if not isinstance(meta, dict):
            meta = {}
        status = str(job.get("status") or "queued")
        queue_status = ""
        if status not in {"done", "error"}:
            try:
                owner = getattr(self.service, "owner", None)
                entry_id = str(job.get("queue_entry_id") or "").strip()
                entry = (getattr(owner, "_generation_queue_entries", {}) or {}).get(entry_id) if owner and entry_id else None
                if isinstance(entry, dict):
                    queue_status = str(entry.get("status") or "").strip()
                    if queue_status:
                        status = queue_status
            except Exception:
                queue_status = ""
        payload = {
            "ok": True,
            "job_id": job_id,
            "status": status,
            "queue_status": queue_status or status,
            "prompt": str(job.get("prompt") or ""),
            "created_at": job.get("created_at"),
            "started_at": job.get("started_at"),
            "completed_at": job.get("completed_at"),
            "wait_seconds": float(job.get("wait_seconds") or 0),
            "wait_text": str(job.get("wait_text") or ""),
            "elapsed_seconds": job.get("elapsed_seconds"),
            "error": str(job.get("error") or ""),
        }
        if history_item:
            history_payload = self._draw_history_payload(history_item)
            payload["history"] = history_payload
            payload["sensitive"] = bool(history_payload.get("sensitive"))
            payload["reveal_required"] = bool(history_payload.get("reveal_required"))
            payload["safety"] = history_payload.get("safety") or {}
        if image_path:
            history_id = str((history_item or {}).get("id") or "")
            safety = self._draw_safety_payload(meta)
            payload.update({
                "image_url": f"/api/draw/history/{history_id}/image" if history_id else f"/api/draw/image/{job_id}",
                "download_url": f"/api/draw/history/{history_id}/download" if history_id else f"/api/draw/image/{job_id}?download=1",
                "image_path": image_path,
                "watermark_applied": bool(meta.get("watermark_applied")),
                "sensitive": bool(safety.get("sensitive")),
                "reveal_required": bool(safety.get("reveal_required")),
                "safety": safety,
                "info": self._draw_result_info(history_item or job, meta),
                "meta": meta,
            })
        return payload

    async def _run_draw_job(self, job_id: str, context: dict):
        job = self._draw_jobs.get(job_id)
        if not isinstance(job, dict):
            return
        job["status"] = "running"
        job["started_at"] = time.time()
        try:
            owner = self._draw_owner()
            result = await owner._handle_webui_draw_generate({
                "prompt": job.get("prompt") or "",
                "queue_entry_id": job.get("queue_entry_id") or "",
                "username": context.get("username") or "web",
            })
            if not isinstance(result, dict):
                raise RuntimeError("Web 绘图返回值异常")
            result.pop("image", None)
            image_path = str(result.get("image_path") or "")
            image_file = Path(image_path) if image_path else None
            meta = self._draw_jsonable(result.get("meta") or {})
            if not isinstance(meta, dict):
                meta = {}
            if image_file and image_file.exists():
                meta.setdefault("bytes", image_file.stat().st_size)
            history_item = self.service.store.add_web_draw_history({
                "prompt": job.get("prompt") or "",
                "raw_prompt": result.get("raw_prompt") or job.get("prompt") or "",
                "final_prompt": result.get("prompt") or meta.get("translated_prompt") or "",
                "image_path": image_path,
                "elapsed_seconds": float(result.get("elapsed_seconds") or 0),
                "meta": meta,
                "username": context.get("username") or "",
                "account_key": context.get("account_key") or self.service.store._gallery_account_key(context.get("username") or ""),
                "owner_user_id": context.get("owner_user_id") or "",
                "owner_name": context.get("owner_name") or "",
            })
            job["result"] = result
            job["history_item"] = history_item
            job["history_id"] = history_item.get("id")
            job["image_path"] = image_path
            job["elapsed_seconds"] = float(result.get("elapsed_seconds") or 0)
            job["status"] = "done"
            job["completed_at"] = time.time()
            self.service.store.record_gallery_access("draw_done", context, job.get("ip") or "", job.get("user_agent") or "", ok=True, detail=job_id)
        except Exception as e:
            job["status"] = "error"
            job["error"] = str(e)[:500]
            job["completed_at"] = time.time()
            logger.error(f"[GPT Magic] WineFox Web 绘图失败 job={job_id}: {e}")
            self.service.store.record_gallery_access("draw_error", context, job.get("ip") or "", job.get("user_agent") or "", ok=False, detail=job["error"][:160])

    def _draw_job_task_done(self, job_id: str, task: asyncio.Task):
        job = self._draw_jobs.get(job_id)
        try:
            task.result()
        except asyncio.CancelledError:
            if isinstance(job, dict) and str(job.get("status") or "") not in {"done", "error"}:
                job["status"] = "error"
                job["error"] = "绘图任务已取消"
                job["completed_at"] = time.time()
        except Exception as e:
            if isinstance(job, dict) and str(job.get("status") or "") not in {"done", "error"}:
                job["status"] = "error"
                job["error"] = str(e)[:500]
                job["completed_at"] = time.time()
            logger.error(f"[GPT Magic] WineFox Web 绘图后台任务异常 job={job_id}: {e}")
        finally:
            if isinstance(job, dict):
                queue_entry_id = str(job.get("queue_entry_id") or "").strip()
                owner = getattr(self.service, "owner", None)
                if queue_entry_id and owner and hasattr(owner, "_finish_generation_queue_entry"):
                    try:
                        owner._finish_generation_queue_entry(queue_entry_id)
                    except Exception:
                        pass

    async def draw(self, request: web.Request):
        context = self._require_auth(request)
        owner = self._draw_owner()
        data = await request.json()
        prompt = str(data.get("prompt") or "").strip()
        if not prompt:
            self._raise_json(web.HTTPBadRequest, "请填写画面提示词")
        if len(prompt) > 4000:
            self._raise_json(web.HTTPBadRequest, "提示词太长，请控制在 4000 字以内")
        self._cleanup_draw_jobs()
        try:
            queue_entry_id, queue_info = owner._register_webui_draw_queue_entry(prompt, username=context.get("username") or "web")
        except RuntimeError as e:
            self._raise_json(web.HTTPTooManyRequests, str(e))
        wait_seconds = float(queue_info.get("estimated_wait_seconds") or 0)
        wait_text = owner._format_elapsed(wait_seconds) if wait_seconds > 0 else "马上开始"
        job_id = secrets.token_urlsafe(18)
        job = {
            "id": job_id,
            "status": "preparing",
            "prompt": prompt,
            "queue_entry_id": queue_entry_id,
            "queue_info": queue_info,
            "wait_seconds": wait_seconds,
            "wait_text": wait_text,
            "created_at": time.time(),
            "ip": self._request_ip(request),
            "user_agent": self._user_agent(request),
            "username": context.get("username") or "",
            "account_key": context.get("account_key") or self.service.store._gallery_account_key(context.get("username") or ""),
            "owner_user_id": context.get("owner_user_id") or "",
            "owner_name": context.get("owner_name") or "",
        }
        self._draw_jobs[job_id] = job
        task = asyncio.create_task(self._run_draw_job(job_id, dict(context)))
        job["task"] = task
        if queue_entry_id and hasattr(owner, "_bind_generation_queue_task"):
            try:
                owner._bind_generation_queue_task(queue_entry_id, task, "web_draw")
            except Exception:
                pass
        task.add_done_callback(lambda done, jid=job_id: self._draw_job_task_done(jid, done))
        self.service.store.record_gallery_access("draw_submit", context, self._request_ip(request), self._user_agent(request), ok=True, detail=job_id)
        return web.json_response(self._draw_job_payload(job_id, job), headers={"Cache-Control": "no-store"})

    async def draw_status(self, request: web.Request):
        context = self._require_auth(request)
        self._cleanup_draw_jobs()
        job_id = str(request.match_info.get("job_id") or "")
        job = self._draw_jobs.get(job_id)
        if not isinstance(job, dict) or not self._draw_job_allowed(job, context):
            raise web.HTTPNotFound(text=json.dumps({"error": "draw job not found"}, ensure_ascii=False), content_type="application/json")
        return web.json_response(self._draw_job_payload(job_id, job), headers={"Cache-Control": "no-store"})

    async def draw_history(self, request: web.Request):
        context = self._require_auth(request)
        limit = _safe_int(request.query.get("limit", 24), 24, 1, 200)
        items = [
            self._draw_history_payload(item)
            for item in self.service.store.list_web_draw_history_for_context(context, limit=limit)
        ]
        self.service.store.record_gallery_access("draw_history", context, self._request_ip(request), self._user_agent(request), ok=True, detail=str(len(items)))
        return web.json_response(
            {
                "ok": True,
                "history": items,
                "ttl_hours": int(self.service.store._web_draw_history_ttl_ms() / (60 * 60 * 1000)),
            },
            headers={"Cache-Control": "no-store"},
        )

    async def draw_loras(self, request: web.Request):
        context = self._require_auth(request)
        owner = self._draw_lora_owner()
        limit = _safe_int(request.query.get("limit", 80), 80, 1, 300)
        family = owner._normalize_model_family(getattr(owner, "active_model_family", "anima"))
        live_loras, live_warn = await owner._fetch_paintable_role_live_loras()
        rows, skipped = owner._build_paintable_role_rows(family=family, available_loras=live_loras)
        live_checked = bool(live_loras) or not bool(live_warn)
        usable = [
            row
            for row in (rows or [])
            if not bool(row.get("disabled")) and (row.get("live_available") is not False or not live_checked)
        ]
        payload_rows = [self._draw_lora_payload(row) for row in usable[:limit]]
        detail = f"{family}:{len(payload_rows)}"
        if live_warn:
            detail += f":{str(live_warn)[:80]}"
        self.service.store.record_gallery_access("draw_loras", context, self._request_ip(request), self._user_agent(request), ok=True, detail=detail)
        return web.json_response(
            {
                "ok": True,
                "family": family,
                "family_label": owner._family_label(family),
                "live_checked": live_checked,
                "live_warning": str(live_warn or ""),
                "loras": payload_rows,
                "stats": {
                    "usable": len(usable),
                    "shown": len(payload_rows),
                    "skipped": int(skipped or 0),
                    "live_total": len(live_loras or []),
                },
            },
            headers={"Cache-Control": "no-store"},
        )

    def _draw_history_favorite_meta(self, item: dict, path: Path, context: dict, image_sha256: str) -> dict:
        item = dict(item or {})
        meta = self._draw_jsonable(item.get("meta") or {})
        if not isinstance(meta, dict):
            meta = {}
        info = self._draw_result_info(item, meta)
        username = str((context or {}).get("username") or "").strip()
        account_key = self.service.store._gallery_account_key(username)
        account = self.service.store.gallery_accounts().get(account_key) if account_key else {}
        maker = str((account or {}).get("owner_name") or username or "Web Draw").strip() or "Web Draw"
        character = str(
            info.get("character")
            or meta.get("profile_character")
            or meta.get("favorite_character")
            or meta.get("character")
            or "Web Draw"
        ).strip() or "Web Draw"
        prompt = str(info.get("raw_prompt") or item.get("raw_prompt") or item.get("prompt") or "").strip()
        final_prompt = str(info.get("final_prompt") or item.get("final_prompt") or "").strip()
        model = str(
            meta.get("model")
            or meta.get("checkpoint")
            or meta.get("checkpoint_name")
            or meta.get("sd_model")
            or meta.get("model_name")
            or info.get("model_family")
            or ""
        ).strip()
        summary_bits = ["Web Draw 生成"]
        for label, value in (
            ("尺寸", info.get("size")),
            ("模型族", info.get("model_family")),
            ("工作流", info.get("workflow")),
            ("LoRA", info.get("lora")),
            ("Seed", info.get("seed")),
        ):
            if value not in (None, ""):
                summary_bits.append(f"{label}: {value}")
        if final_prompt and final_prompt != prompt:
            summary_bits.append(f"最终提示词: {final_prompt}")
        collector = {
            "collector_key": f"web:{username}" if username else "web:anonymous",
            "sender_id": username,
            "sender_name": maker,
            "source": f"web_draw:{item.get('id') or ''}",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        return {
            "character": character,
            "maker": maker,
            "prompt": prompt,
            "summary": "；".join(str(bit) for bit in summary_bits if str(bit).strip()),
            "source": f"web_draw:{item.get('id') or ''}",
            "sender_id": username,
            "sender_name": maker,
            "model": model,
            "workflow": str(info.get("workflow") or ""),
            "lora_name": str(info.get("lora") or ""),
            "model_family": str(info.get("model_family") or ""),
            "seed": info.get("seed"),
            "width": info.get("width"),
            "height": info.get("height"),
            "web_draw_history_id": str(item.get("id") or ""),
            "image_sha256": str(image_sha256 or ""),
            "collectors": [collector],
            "collector_count": 1,
        }

    async def draw_history_favorite(self, request: web.Request):
        context = self._require_auth(request)
        item_id = str(request.match_info.get("item_id") or "")
        item = self.service.store.get_web_draw_history_item(item_id, context=context)
        path = self.service.store.get_web_draw_history_image_path(item_id, context=context)
        if not item or not path:
            self.service.store.record_gallery_access("draw_favorite", context, self._request_ip(request), self._user_agent(request), ok=False, detail=f"{item_id}:missing")
            raise web.HTTPNotFound(text=json.dumps({"error": "draw history item not found"}, ensure_ascii=False), content_type="application/json")
        try:
            if path.stat().st_size > GALLERY_IMPORT_MAX_BYTES:
                raise ValueError("图片太大，无法收藏")
            image_sha256 = _file_sha256(str(path)).lower()
            meta = self._draw_history_favorite_meta(item, path, context, image_sha256)
            existing = self.service.store.find_favorite_by_sha256(image_sha256)
            duplicate = bool(existing)
            if existing:
                favorite = self.service.store.add_favorite_collector(existing.get("id"), (meta.get("collectors") or [{}])[0]) or existing
            else:
                favorite = self.service.store.import_favorite_image(path.read_bytes(), path.suffix or ".png", meta)
            favorite_id = str((favorite or {}).get("id") or "")
            gallery_item = next(
                (row for row in self.service.store.list_gallery_favorites(limit=3000) if str(row.get("id") or "") == favorite_id),
                favorite or {},
            )
            self.service.store.record_gallery_access(
                "draw_favorite",
                context,
                self._request_ip(request),
                self._user_agent(request),
                ok=True,
                detail=f"{item_id}:{favorite_id}:{'duplicate' if duplicate else 'new'}",
            )
            return web.json_response(
                {"ok": True, "item": gallery_item, "favorite": favorite, "duplicate": duplicate},
                headers={"Cache-Control": "no-store"},
            )
        except ValueError as e:
            self.service.store.record_gallery_access("draw_favorite", context, self._request_ip(request), self._user_agent(request), ok=False, detail=f"{item_id}:{e}")
            raise web.HTTPBadRequest(text=json.dumps({"error": str(e)}, ensure_ascii=False), content_type="application/json")
        except Exception as e:
            self.service.store.record_gallery_access("draw_favorite", context, self._request_ip(request), self._user_agent(request), ok=False, detail=f"{item_id}:error")
            logger.warning(f"[GPT Magic] WineFox Web Draw 收藏失败 item={item_id}: {e}")
            raise web.HTTPInternalServerError(text=json.dumps({"error": "收藏失败"}, ensure_ascii=False), content_type="application/json")

    async def draw_history_image(self, request: web.Request):
        context = self._require_auth(request)
        item_id = str(request.match_info.get("item_id") or "")
        path = self.service.store.get_web_draw_history_image_path(item_id, context=context)
        if not path:
            raise web.HTTPNotFound(text="not found")
        return web.FileResponse(path)

    async def draw_history_download(self, request: web.Request):
        context = self._require_auth(request)
        item_id = str(request.match_info.get("item_id") or "")
        item = self.service.store.get_web_draw_history_item(item_id, context=context)
        path = self.service.store.get_web_draw_history_image_path(item_id, context=context)
        if not item or not path:
            raise web.HTTPNotFound(text="not found")
        suffix = Path(path).suffix or ".png"
        prompt_slug = re.sub(r"[^A-Za-z0-9._-]+", "_", str(item.get("raw_prompt") or item.get("prompt") or "winefox_draw")).strip("._-")
        prompt_slug = prompt_slug[:48] or "winefox_draw"
        filename = f"{prompt_slug}_{item_id}{suffix}"
        return web.FileResponse(
            path,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "private, max-age=60",
            },
        )

    async def draw_image(self, request: web.Request):
        context = self._require_auth(request)
        job_id = str(request.match_info.get("job_id") or "")
        job = self._draw_jobs.get(job_id)
        if not isinstance(job, dict) or not self._draw_job_allowed(job, context) or str(job.get("status") or "") != "done":
            raise web.HTTPNotFound(text="not found")
        path = Path(str(job.get("image_path") or ""))
        if not path.exists() or not path.is_file():
            raise web.HTTPNotFound(text="not found")
        if str(request.query.get("download") or "") in {"1", "true", "yes"}:
            filename = f"winefox_draw_{job_id}{path.suffix or '.png'}"
            return web.FileResponse(
                path,
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "Cache-Control": "private, max-age=60",
                },
            )
        return web.FileResponse(path)

    def _admin_gallery_items(self) -> list:
        items = []
        for item in self.service.store.list_favorites(limit=3000):
            items.append({
                "id": str(item.get("id") or ""),
                "character": str(item.get("character") or "未分类"),
                "maker": str(item.get("maker_name") or item.get("sender_name") or "匿名制作人"),
                "prompt": str(item.get("prompt_preview") or ""),
                "summary": str(item.get("design_summary") or ""),
                "created_at": item.get("created_at") or "",
                "bytes": int(item.get("bytes") or 0),
                "image_url": f"/api/image/{item.get('id')}",
            })
        return items

    def _raise_json(self, exc_cls, message: str, **extra):
        payload = {"error": str(message or "请求失败")}
        payload.update(extra or {})
        raise exc_cls(
            text=json.dumps(payload, ensure_ascii=False),
            content_type="application/json",
        )

    def _lora_owner(self):
        owner = getattr(self.service, "owner", None)
        required = (
            "_get_lora_profile_store",
            "_normalize_model_family",
            "_family_label",
            "_normalize_profile_match_key",
            "_normalize_lora_lookup_key",
        )
        if not owner or any(not hasattr(owner, name) for name in required):
            self._raise_json(web.HTTPServiceUnavailable, "当前 WebUI 未连接 LoRA 管理服务")
        return owner

    def _admin_lora_family(self, request: web.Request, owner, allow_all: bool = False) -> str:
        raw = str(request.query.get("family") or "current").strip().lower()
        if raw in {"", "current", "active", "当前"}:
            raw = str(getattr(owner, "active_model_family", "anima") or "anima")
        if allow_all and raw == "all":
            return "all"
        family = owner._normalize_model_family(raw)
        if not family:
            self._raise_json(web.HTTPBadRequest, "未知模型族")
        return family

    def _admin_lora_families(self, owner, family: str) -> list:
        if family == "all":
            families = ["anima", "illustrious"]
        else:
            families = [family]
        result = []
        seen = set()
        for item in families:
            normalized = owner._normalize_model_family(item)
            if normalized and normalized not in seen:
                seen.add(normalized)
                result.append(normalized)
        return result

    def _clean_admin_text(self, value, limit: int = 240) -> str:
        text = str(value or "").replace("\x00", "").strip()
        if limit and len(text) > limit:
            text = text[:limit].strip()
        return text

    def _split_admin_lora_values(self, value, limit: int = 64) -> list:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            raw_values = list(value)
        else:
            raw_values = re.split(r"[,，;；\n\r]+", str(value or ""))
        values = []
        seen = set()
        for raw in raw_values:
            item = self._clean_admin_text(raw, 120)
            marker = re.sub(r"\s+", " ", item).strip().lower()
            if not item or not marker or marker in seen:
                continue
            seen.add(marker)
            values.append(item)
            if len(values) >= limit:
                break
        return values

    def _admin_bool(self, value, default: bool = False) -> bool:
        if value is None:
            return bool(default)
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on", "enable", "enabled", "启用", "是"}:
            return True
        if text in {"0", "false", "no", "off", "disable", "disabled", "停用", "否"}:
            return False
        return bool(default)

    def _admin_lora_key_for_name(self, owner, lora_name: str) -> str:
        key = ""
        try:
            key = owner._normalize_lora_lookup_key(lora_name)
        except Exception:
            key = ""
        if not key:
            try:
                key = owner._normalize_profile_match_key(Path(str(lora_name or "")).stem)
            except Exception:
                key = re.sub(r"[^0-9a-z]+", "", Path(str(lora_name or "")).stem.lower())
        return key or hashlib.sha256(str(lora_name or "").encode("utf-8")).hexdigest()[:16]

    def _admin_lora_character_from_name(self, lora_name: str) -> str:
        name = Path(str(lora_name or "")).stem
        name = re.sub(r"[_\-]+", " ", name)
        name = re.sub(r"\b(?:lora|locon|lycoris|safetensors|ckpt|pt|bin)\b", " ", name, flags=re.I)
        name = re.sub(r"\s+", " ", name).strip(" ._-")
        return self._clean_admin_text(name or Path(str(lora_name or "")).stem or "未命名 LoRA", 120)

    def _admin_lora_virtual_profile(self, owner, family: str, lora_name: str) -> tuple:
        lora_name = str(lora_name or "").strip()
        key = self._admin_lora_key_for_name(owner, lora_name)
        return key, {
            "character": self._admin_lora_character_from_name(lora_name),
            "lora": lora_name,
            "aliases": [],
            "search_aliases": [],
            "trigger_prompt": "",
            "_web_unrecognized": True,
            "_web_profile_status": "unrecognized",
            "_web_family": family,
        }

    async def _admin_lora_live_loras(self, owner, force: bool = False) -> tuple:
        api = getattr(owner, "api", None)
        if not api or not hasattr(api, "fetch_lora_list"):
            return [], "ComfyUI API 未初始化，无法刷新物理 LoRA 列表"
        try:
            loras, warn = await api.fetch_lora_list(force=bool(force), allow_cache_fallback=True)
            return [str(x).strip() for x in (loras or []) if str(x).strip()], warn or ""
        except Exception as e:
            return [], f"读取 ComfyUI LoRA 列表失败：{e}"

    def _admin_lora_rows_for_family(self, owner, family: str, profiles: dict, live_loras: list = None, include_unrecognized: bool = True) -> list:
        rows = []
        profile_lora_keys = set()
        profiles = profiles or {}
        for key, profile in profiles.items():
            if not isinstance(profile, dict):
                continue
            payload = self._admin_lora_profile_payload(owner, family, str(key), profile, profiles)
            payload["recognized"] = True
            payload["profile_status"] = "profile"
            rows.append(payload)
            try:
                lora_key = owner._normalize_lora_lookup_key(profile.get("lora") or "")
            except Exception:
                lora_key = ""
            if lora_key:
                profile_lora_keys.add(lora_key)
        if not include_unrecognized:
            return rows
        all_profile_keys = set()
        try:
            all_profile_keys = set(owner._all_profile_lora_keys())
        except Exception:
            all_profile_keys = set(profile_lora_keys)
        seen = set(profile_lora_keys)
        for lora_name in live_loras or []:
            lora_name = str(lora_name or "").strip()
            if not lora_name:
                continue
            try:
                if owner._is_non_character_lora_model_name(lora_name):
                    continue
            except Exception:
                pass
            try:
                lora_key = owner._normalize_lora_lookup_key(lora_name)
            except Exception:
                lora_key = self._admin_lora_key_for_name(owner, lora_name)
            if not lora_key or lora_key in seen or lora_key in all_profile_keys:
                continue
            key, virtual_profile = self._admin_lora_virtual_profile(owner, family, lora_name)
            payload = self._admin_lora_profile_payload(owner, family, key, virtual_profile, profiles)
            payload.update({
                "recognized": False,
                "unrecognized": True,
                "profile_status": "unrecognized",
                "paintable": False,
                "status_label": "未识别",
            })
            rows.append(payload)
            seen.add(lora_key)
        return rows

    def _normalize_admin_lora_batch_items(self, owner, data: dict, limit: int = 200) -> list:
        try:
            max_items = max(1, int(limit or 200))
        except Exception:
            max_items = 200
        raw_items = []
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            raw_items = list(data.get("items") or [])
        elif isinstance(data, dict):
            raw_ids = data.get("ids") or data.get("keys") or []
            if isinstance(raw_ids, str):
                raw_items = re.split(r"[\s,，;；]+", raw_ids)
            elif isinstance(raw_ids, (list, tuple, set)):
                raw_items = list(raw_ids)
        seen = set()
        result = []
        for raw in raw_items:
            family = ""
            key = ""
            lora_name = ""
            if isinstance(raw, dict):
                family = str(raw.get("family") or data.get("family") or "").strip()
                key = str(raw.get("key") or raw.get("id") or "").strip()
                lora_name = str(raw.get("lora") or raw.get("lora_path") or "").strip()
            else:
                text = str(raw or "").strip()
                if "::" in text:
                    family, key = text.split("::", 1)
                else:
                    family = str(data.get("family") or "").strip() if isinstance(data, dict) else ""
                    key = text
            if not key:
                continue
            try:
                family = owner._normalize_model_family(family or getattr(owner, "active_model_family", "anima") or "anima")
            except Exception:
                family = str(family or "anima")
            marker = f"{family}::{key}"
            if marker in seen:
                continue
            seen.add(marker)
            result.append({"family": family, "key": key, "lora": lora_name})
            if len(result) >= max_items:
                break
        return result

    def _admin_lora_profile_payload(self, owner, family: str, key: str, profile: dict, all_profiles: dict = None, index: int = 0) -> dict:
        raw_profile = dict(profile or {})
        runtime = dict(raw_profile)
        try:
            runtime = owner._normalize_lora_profile_runtime(
                dict(raw_profile),
                family=family,
                all_profiles=all_profiles or {},
            )
        except Exception:
            runtime = dict(raw_profile)
        character = str(runtime.get("character") or raw_profile.get("character") or key or "").strip()
        display_name = character
        try:
            display_name = owner._paintable_role_cjk_display_name(str(key), runtime, character)
        except Exception:
            display_name = character
        conflict_reason = ""
        try:
            conflict_reason = str(owner._profile_identity_conflict_reason(str(key), runtime) or "")
        except Exception:
            conflict_reason = ""
        lora_name = str(runtime.get("lora") or raw_profile.get("lora") or "").strip()
        aliases = runtime.get("aliases") if isinstance(runtime.get("aliases"), list) else raw_profile.get("aliases")
        official_aliases = runtime.get("official_aliases") if isinstance(runtime.get("official_aliases"), list) else raw_profile.get("official_aliases")
        nickname_aliases = runtime.get("nickname_aliases") if isinstance(runtime.get("nickname_aliases"), list) else raw_profile.get("nickname_aliases")
        search_aliases = runtime.get("search_aliases") if isinstance(runtime.get("search_aliases"), list) else raw_profile.get("search_aliases")
        match_terms = runtime.get("match_terms") if isinstance(runtime.get("match_terms"), list) else raw_profile.get("match_terms")
        trigger_prompt = str(runtime.get("trigger_prompt") or raw_profile.get("trigger_prompt") or "").strip()
        sort_key = ""
        try:
            sort_key = owner._normalize_profile_match_key(display_name or character or key)
        except Exception:
            sort_key = str(display_name or character or key).lower()
        return {
            "index": int(index or 0),
            "key": str(key or ""),
            "family": family,
            "family_label": owner._family_label(family),
            "character": character,
            "display_name": display_name or character or str(key or ""),
            "lora": Path(lora_name).name if lora_name else "",
            "lora_path": lora_name,
            "aliases": [str(x).strip() for x in (aliases or []) if str(x or "").strip()],
            "official_aliases": [str(x).strip() for x in (official_aliases or []) if str(x or "").strip()],
            "nickname_aliases": [str(x).strip() for x in (nickname_aliases or []) if str(x or "").strip()],
            "search_aliases": [str(x).strip() for x in (search_aliases or []) if str(x or "").strip()],
            "match_terms": [str(x).strip() for x in (match_terms or []) if str(x or "").strip()],
            "trigger_prompt": trigger_prompt,
            "disabled": bool(raw_profile.get("disabled") or runtime.get("disabled")),
            "disabled_reason": str(raw_profile.get("disabled_reason") or runtime.get("disabled_reason") or ""),
            "conflict_reason": conflict_reason,
            "paintable": not bool(conflict_reason),
            "recognized": not bool(raw_profile.get("_web_unrecognized")),
            "unrecognized": bool(raw_profile.get("_web_unrecognized")),
            "profile_status": str(raw_profile.get("_web_profile_status") or ("unrecognized" if raw_profile.get("_web_unrecognized") else "profile")),
            "status_label": str(raw_profile.get("_web_profile_status") or ("未识别" if raw_profile.get("_web_unrecognized") else "")),
            "updated_at": raw_profile.get("updated_at") or 0,
            "sort_key": sort_key,
        }

    def _find_admin_lora_profile(self, owner, family: str, selector: str):
        selector = str(selector or "").strip()
        if not selector:
            return None, {}, "", {}
        store = owner._get_lora_profile_store(family)
        profiles = store.load()
        if not isinstance(profiles, dict):
            profiles = {}
        if selector in profiles and isinstance(profiles.get(selector), dict):
            return store, profiles, selector, profiles[selector]
        selector_norm = owner._normalize_profile_match_key(selector)
        selector_lora = owner._normalize_lora_lookup_key(selector)
        for key, profile in profiles.items():
            if not isinstance(profile, dict):
                continue
            candidates = [
                str(key),
                str(profile.get("character") or ""),
                str(profile.get("lora") or ""),
                Path(str(profile.get("lora") or "")).stem,
            ]
            for field in ("aliases", "official_aliases", "nickname_aliases", "match_terms", "search_aliases"):
                raw = profile.get(field) or []
                if isinstance(raw, str):
                    raw = re.split(r"[,，;；\n\r]+", raw)
                candidates.extend([str(x) for x in raw if str(x or "").strip()])
            norm_hits = {
                owner._normalize_profile_match_key(item)
                for item in candidates
                if str(item or "").strip()
            }
            lora_hits = {
                owner._normalize_lora_lookup_key(item)
                for item in candidates
                if str(item or "").strip()
            }
            if (selector_norm and selector_norm in norm_hits) or (selector_lora and selector_lora in lora_hits):
                return store, profiles, str(key), profile
        return store, profiles, "", {}

    def _apply_admin_lora_profile_patch(self, owner, family: str, selector: str, patch: dict, context: dict = None, lora_name: str = ""):
        patch = patch if isinstance(patch, dict) else {}
        store, profiles, old_key, old_profile = self._find_admin_lora_profile(owner, family, selector)
        created = False
        lora_name = self._clean_admin_text(lora_name or patch.get("lora") or patch.get("lora_path") or "", 500)
        if not old_key or not isinstance(old_profile, dict):
            if not lora_name:
                return None, "not_found"
            store = owner._get_lora_profile_store(family)
            profiles = store.load()
            if not isinstance(profiles, dict):
                profiles = {}
            old_key = selector or self._admin_lora_key_for_name(owner, lora_name)
            old_profile = {
                "character": self._admin_lora_character_from_name(lora_name),
                "lora": lora_name,
                "aliases": [],
                "search_aliases": [],
                "created_from": "web_admin_unrecognized",
                "created_at": int(time.time()),
            }
            created = True
        profile = dict(old_profile)
        old_profile_snapshot = dict(old_profile)
        old_character = str(profile.get("character") or old_key)
        if lora_name:
            profile["lora"] = lora_name
        if "character" in patch:
            character = self._clean_admin_text(patch.get("character"), 120)
            if character:
                profile["character"] = character
        if "aliases" in patch:
            aliases = self._split_admin_lora_values(patch.get("aliases"), limit=64)
            if aliases:
                profile["aliases"] = aliases
        if "search_aliases" in patch:
            search_aliases = self._split_admin_lora_values(patch.get("search_aliases"), limit=64)
            if search_aliases:
                profile["search_aliases"] = search_aliases
        if "trigger_prompt" in patch:
            trigger_prompt = self._clean_admin_text(patch.get("trigger_prompt"), 2000)
            if trigger_prompt:
                if hasattr(owner, "_sanitize_trigger_prompt"):
                    trigger_prompt = owner._sanitize_trigger_prompt(trigger_prompt)
                profile["trigger_prompt"] = trigger_prompt
                if hasattr(owner, "_profile_tag_values"):
                    profile["activation_tags"] = owner._profile_tag_values(trigger_prompt, limit=40)
        if "disabled" in patch:
            if self._admin_bool(patch.get("disabled")):
                profile["disabled"] = True
                profile["disabled_reason"] = self._clean_admin_text(patch.get("disabled_reason") or profile.get("disabled_reason") or "管理员手动停用", 240)
            else:
                profile.pop("disabled", None)
                profile.pop("disabled_reason", None)
        elif "disabled_reason" in patch and profile.get("disabled"):
            profile["disabled_reason"] = self._clean_admin_text(patch.get("disabled_reason") or "管理员手动停用", 240)
        profile["updated_at"] = int(time.time())
        profile["updated_by"] = f"gallery_admin:{(context or {}).get('username')}"
        new_key = old_key
        if "character" in patch and profile.get("character"):
            normalized_key = owner._normalize_profile_match_key(profile.get("character") or "")
            if normalized_key and owner._normalize_profile_match_key(old_character) != normalized_key:
                new_key = normalized_key
        if new_key != old_key and new_key in profiles:
            return None, f"conflict:{new_key}"
        if new_key != old_key:
            profiles.pop(old_key, None)
        profiles[new_key] = profile
        store.save(profiles)
        self._sync_admin_lora_aliases(owner, family, old_key, old_profile_snapshot, profile)
        if "trigger_prompt" in patch:
            self._sync_admin_lora_prompt(owner, family, profile.get("lora"), profile.get("trigger_prompt") or "")
        return {
            "old_key": old_key,
            "key": new_key,
            "created": created,
            "profile": self._admin_lora_profile_payload(owner, family, new_key, profile, profiles),
        }, ""

    def _sync_admin_lora_aliases(self, owner, family: str, old_key: str, old_profile: dict, profile: dict):
        lora_name = str((profile or {}).get("lora") or "").strip()
        if not lora_name:
            return
        alias_path = owner._lora_alias_store_for_family(family)
        data = owner._load_json_map_file(alias_path)
        old_terms = [old_key, old_profile.get("character")]
        for field in ("aliases", "official_aliases", "nickname_aliases", "match_terms", "search_aliases"):
            raw = old_profile.get(field) or []
            if isinstance(raw, str):
                raw = re.split(r"[,，;；\n\r]+", raw)
            old_terms.extend(raw)
        new_terms = [profile.get("character")]
        for field in ("aliases", "search_aliases"):
            new_terms.extend(profile.get(field) or [])
        new_markers = {
            owner._normalize_alias_key(item)
            for item in new_terms
            if str(item or "").strip()
        }
        old_markers = {
            owner._normalize_alias_key(item)
            for item in old_terms
            if str(item or "").strip()
        }
        lora_key = owner._normalize_lora_lookup_key(lora_name)
        changed = False
        for key, value in list(data.items()):
            if owner._normalize_lora_lookup_key(value) != lora_key:
                continue
            marker = owner._normalize_alias_key(key)
            if marker in old_markers and marker not in new_markers:
                data.pop(key, None)
                changed = True
        for alias in new_terms:
            alias = str(alias or "").strip()
            if not alias:
                continue
            if data.get(alias) != lora_name:
                data[alias] = lora_name
                changed = True
        if changed:
            owner._save_json_map_file(alias_path, data)

    def _sync_admin_lora_prompt(self, owner, family: str, lora_name: str, trigger_prompt: str):
        lora_name = str(lora_name or "").strip()
        if not lora_name:
            return
        prompt_path = owner._lora_prompt_store_for_family(family)
        data = owner._load_json_map_file(prompt_path)
        target = owner._normalize_lora_lookup_key(lora_name)
        changed = False
        for key in list(data.keys()):
            if owner._normalize_lora_lookup_key(key) == target:
                data.pop(key, None)
                changed = True
        trigger_prompt = str(trigger_prompt or "").strip()
        if trigger_prompt:
            data[lora_name] = trigger_prompt
            changed = True
        if changed:
            owner._save_json_map_file(prompt_path, data)

    def _refresh_admin_lora_runtime(self, owner, family: str):
        try:
            owner._lora_profiles_runtime_cache = {}
            owner._profile_blocked_markers_cache = {}
            owner._lora_conflict_warning_cache = {}
        except Exception:
            pass
        try:
            owner._refresh_api_lora_family_guard(family)
        except Exception:
            pass
        try:
            if getattr(owner, "api", None) and hasattr(owner.api, "_load_runtime_config"):
                owner.api._load_runtime_config()
        except Exception:
            pass

    def _admin_lora_summary(self) -> dict:
        owner = self._lora_owner()
        active_family = owner._normalize_model_family(getattr(owner, "active_model_family", "anima"))
        families = [
            {"id": family, "label": owner._family_label(family)}
            for family in self._admin_lora_families(owner, "all")
        ]
        counts = {}
        for family in self._admin_lora_families(owner, "all"):
            try:
                profiles = owner._get_lora_profile_store(family).load()
                counts[family] = len([1 for item in (profiles or {}).values() if isinstance(item, dict)])
            except Exception:
                counts[family] = 0
        return {
            "active_family": active_family,
            "active_family_label": owner._family_label(active_family),
            "families": families,
            "counts": counts,
        }

    async def admin_lora_profiles(self, request: web.Request):
        context = self._require_admin(request)
        owner = self._lora_owner()
        family = self._admin_lora_family(request, owner, allow_all=True)
        refresh_live = self._admin_bool(request.query.get("refresh"), default=False)
        live_loras, live_warn = await self._admin_lora_live_loras(owner, force=refresh_live)
        rows = []
        families = self._admin_lora_families(owner, family)
        active_family = owner._normalize_model_family(getattr(owner, "active_model_family", "anima") or "anima")
        assigned_live_keys = set()

        def family_live_loras(fam: str) -> list:
            if family != "all":
                return live_loras
            result = []
            for lora_name in live_loras:
                try:
                    lora_key = owner._normalize_lora_lookup_key(lora_name)
                except Exception:
                    lora_key = self._admin_lora_key_for_name(owner, lora_name)
                if not lora_key or lora_key in assigned_live_keys:
                    continue
                visible_for_fam = False
                visible_any = False
                for candidate in families:
                    try:
                        visible = owner._is_lora_visible_for_family(lora_name, family=candidate, profile_keys=set())
                    except Exception:
                        visible = False
                    visible_any = visible_any or visible
                    if candidate == fam and visible:
                        visible_for_fam = True
                if visible_for_fam or (fam == active_family and not visible_any):
                    result.append(lora_name)
                    assigned_live_keys.add(lora_key)
            return result

        for fam in families:
            try:
                profiles = owner._get_lora_profile_store(fam).load()
            except Exception as e:
                self._raise_json(web.HTTPInternalServerError, f"读取 LoRA 档案失败：{e}")
            rows.extend(self._admin_lora_rows_for_family(owner, fam, profiles, live_loras=family_live_loras(fam), include_unrecognized=True))
        rows.sort(key=lambda row: (
            str(row.get("family") or ""),
            1 if row.get("unrecognized") else 0,
            str(row.get("sort_key") or ""),
            str(row.get("display_name") or "").lower(),
        ))
        for idx, row in enumerate(rows, 1):
            row["index"] = idx
        self.service.store.record_gallery_access("admin_lora_profiles", context, self._request_ip(request), self._user_agent(request), ok=True, detail=family)
        return web.json_response(
            {
                "ok": True,
                "family": family,
                "summary": self._admin_lora_summary(),
                "profiles": rows,
                "stats": {
                    "profiles": len(rows),
                    "recognized": len([row for row in rows if not row.get("unrecognized")]),
                    "unrecognized": len([row for row in rows if row.get("unrecognized")]),
                    "live_loras": len(live_loras),
                },
                "warning": live_warn,
            },
            headers={"Cache-Control": "no-store"},
        )

    async def admin_update_lora_profile(self, request: web.Request):
        context = self._require_admin(request)
        owner = self._lora_owner()
        family = self._admin_lora_family(request, owner, allow_all=False)
        selector = str(request.match_info.get("key") or "").strip()
        data = await request.json()
        if not isinstance(data, dict):
            self._raise_json(web.HTTPBadRequest, "请求体必须是 JSON 对象")
        store, profiles, old_key, old_profile = self._find_admin_lora_profile(owner, family, selector)
        if not old_key or not isinstance(old_profile, dict):
            lora_name = self._clean_admin_text(data.get("lora") or data.get("lora_path") or selector, 500)
            if not lora_name:
                self._raise_json(web.HTTPNotFound, "LoRA 角色档案不存在")
            store = owner._get_lora_profile_store(family)
            profiles = store.load()
            if not isinstance(profiles, dict):
                profiles = {}
            old_key = selector or self._admin_lora_key_for_name(owner, lora_name)
            if old_key in profiles and isinstance(profiles.get(old_key), dict):
                old_profile = profiles[old_key]
            else:
                old_profile = {
                    "character": self._admin_lora_character_from_name(lora_name),
                    "lora": lora_name,
                    "aliases": [],
                    "search_aliases": [],
                    "created_from": "web_admin_unrecognized",
                    "created_at": int(time.time()),
                }
        profile = dict(old_profile)
        old_profile_snapshot = dict(old_profile)
        old_character = str(profile.get("character") or old_key)
        if "lora" in data or "lora_path" in data:
            lora_name = self._clean_admin_text(data.get("lora") or data.get("lora_path"), 500)
            if lora_name:
                profile["lora"] = lora_name
        if "character" in data:
            character = self._clean_admin_text(data.get("character"), 120)
            if not character:
                self._raise_json(web.HTTPBadRequest, "角色名不能为空")
            profile["character"] = character
        else:
            character = old_character
        if "aliases" in data:
            profile["aliases"] = self._split_admin_lora_values(data.get("aliases"), limit=64)
        if "search_aliases" in data:
            profile["search_aliases"] = self._split_admin_lora_values(data.get("search_aliases"), limit=64)
        if "trigger_prompt" in data:
            trigger_prompt = self._clean_admin_text(data.get("trigger_prompt"), 2000)
            if hasattr(owner, "_sanitize_trigger_prompt"):
                trigger_prompt = owner._sanitize_trigger_prompt(trigger_prompt)
            if trigger_prompt:
                profile["trigger_prompt"] = trigger_prompt
                if hasattr(owner, "_profile_tag_values"):
                    profile["activation_tags"] = owner._profile_tag_values(trigger_prompt, limit=40)
            else:
                profile.pop("trigger_prompt", None)
                profile.pop("activation_tags", None)
        if "disabled" in data:
            if self._admin_bool(data.get("disabled")):
                profile["disabled"] = True
                profile["disabled_reason"] = self._clean_admin_text(data.get("disabled_reason") or profile.get("disabled_reason") or "管理员手动停用", 240)
            else:
                profile.pop("disabled", None)
                profile.pop("disabled_reason", None)
        elif "disabled_reason" in data and profile.get("disabled"):
            profile["disabled_reason"] = self._clean_admin_text(data.get("disabled_reason") or "管理员手动停用", 240)
        profile["updated_at"] = int(time.time())
        profile["updated_by"] = f"gallery_admin:{context.get('username')}"
        new_key = old_key
        if "character" in data:
            normalized_key = owner._normalize_profile_match_key(profile.get("character") or "")
            if normalized_key and owner._normalize_profile_match_key(old_character) != normalized_key:
                new_key = normalized_key
        if new_key != old_key and new_key in profiles:
            self._raise_json(web.HTTPConflict, f"角色档案已存在：{new_key}")
        if new_key != old_key:
            profiles.pop(old_key, None)
        profiles[new_key] = profile
        store.save(profiles)
        self._sync_admin_lora_aliases(owner, family, old_key, old_profile_snapshot, profile)
        if "trigger_prompt" in data:
            self._sync_admin_lora_prompt(owner, family, profile.get("lora"), profile.get("trigger_prompt") or "")
        self._refresh_admin_lora_runtime(owner, family)
        payload = self._admin_lora_profile_payload(owner, family, new_key, profile, profiles)
        self.service.store.record_gallery_access("admin_lora_update", context, self._request_ip(request), self._user_agent(request), ok=True, detail=f"{family}:{new_key}")
        return web.json_response({"ok": True, "profile": payload, "old_key": old_key, "key": new_key}, headers={"Cache-Control": "no-store"})

    async def admin_delete_lora_profile(self, request: web.Request):
        context = self._require_admin(request)
        owner = self._lora_owner()
        family = self._admin_lora_family(request, owner, allow_all=False)
        selector = str(request.match_info.get("key") or "").strip()
        _store, profiles, key, profile = self._find_admin_lora_profile(owner, family, selector)
        if not key or not isinstance(profile, dict):
            lora_name = self._clean_admin_text(request.query.get("lora") or selector, 500)
            if not lora_name:
                self._raise_json(web.HTTPNotFound, "LoRA 角色档案不存在")
            delete_files = self._admin_bool(request.query.get("delete_files", "1"), default=True)
            result = owner._delete_role_everything(lora_name, families=[family], delete_files=delete_files)
            self._refresh_admin_lora_runtime(owner, family)
            ok = any(result.get(name) for name in ("profiles", "aliases", "prompts", "files")) or result.get("runtime_cleared")
            if not ok:
                self._raise_json(web.HTTPNotFound, "LoRA 角色档案不存在")
            self.service.store.record_gallery_access("admin_lora_delete", context, self._request_ip(request), self._user_agent(request), ok=True, detail=f"{family}:{lora_name}")
            return web.json_response(
                {
                    "ok": True,
                    "profile": {"family": family, "key": selector, "lora": lora_name, "unrecognized": True},
                    "result": result,
                },
                headers={"Cache-Control": "no-store"},
            )
        delete_files = self._admin_bool(request.query.get("delete_files", "1"), default=True)
        target = (
            self._clean_admin_text(request.query.get("lora") or "", 500)
            or str(profile.get("lora") or "").strip()
            or key
            or str(profile.get("character") or selector)
        )
        result = owner._delete_role_everything(target, families=[family], delete_files=delete_files)
        self._refresh_admin_lora_runtime(owner, family)
        removed = self._admin_lora_profile_payload(owner, family, key, profile, profiles)
        self.service.store.record_gallery_access("admin_lora_delete", context, self._request_ip(request), self._user_agent(request), ok=True, detail=f"{family}:{target}")
        return web.json_response(
            {
                "ok": True,
                "profile": removed,
                "result": result,
            },
            headers={"Cache-Control": "no-store"},
        )

    async def admin_batch_lora_profiles(self, request: web.Request):
        context = self._require_admin(request)
        owner = self._lora_owner()
        try:
            data = await request.json()
        except Exception:
            self._raise_json(web.HTTPBadRequest, "请求体必须是合法 JSON")
        if not isinstance(data, dict):
            self._raise_json(web.HTTPBadRequest, "请求体必须是 JSON 对象")
        items = self._normalize_admin_lora_batch_items(owner, data, limit=200)
        if not items:
            self._raise_json(web.HTTPBadRequest, "请选择至少一个 LoRA 角色")
        action = str(data.get("action") or "update").strip().lower()
        result = {
            "requested": len(items),
            "updated": 0,
            "deleted": 0,
            "created": 0,
            "not_found": [],
            "failed": [],
            "items": [],
        }
        if action in {"delete", "remove"}:
            delete_files = self._admin_bool(data.get("delete_files"), default=True)
            for item in items:
                family = item.get("family") or owner._normalize_model_family(getattr(owner, "active_model_family", "anima"))
                selector = str(item.get("key") or "").strip()
                lora_name = str(item.get("lora") or "").strip()
                try:
                    _store, _profiles, key, profile = self._find_admin_lora_profile(owner, family, selector)
                    target = lora_name or str((profile or {}).get("lora") or "").strip() or key or str((profile or {}).get("character") or "") or selector
                    delete_result = owner._delete_role_everything(target, families=[family], delete_files=delete_files)
                    if any(delete_result.get(name) for name in ("profiles", "aliases", "prompts", "files")) or delete_result.get("runtime_cleared"):
                        result["deleted"] += 1
                        result["items"].append({"family": family, "key": selector, "result": delete_result})
                    else:
                        result["not_found"].append({"family": family, "key": selector})
                except Exception as e:
                    result["failed"].append({"family": family, "key": selector, "error": str(e)[:160]})
            for family in sorted({str(item.get("family") or "") for item in items if item.get("family")}):
                self._refresh_admin_lora_runtime(owner, family)
            ok = result.get("deleted", 0) > 0 and not result.get("failed")
            self.service.store.record_gallery_access(
                "admin_lora_batch_delete",
                context,
                self._request_ip(request),
                self._user_agent(request),
                ok=ok,
                detail=f"{result.get('deleted', 0)}/{result.get('requested', 0)}",
            )
            return web.json_response({"ok": True, "action": "delete", "result": result}, headers={"Cache-Control": "no-store"})
        if action not in {"update", "patch", "edit"}:
            self._raise_json(web.HTTPBadRequest, "未知批量操作")
        raw_patch = data.get("patch") if isinstance(data.get("patch"), dict) else data
        patch = {}
        for key in ("character", "aliases", "search_aliases", "trigger_prompt", "disabled_reason"):
            if key not in raw_patch:
                continue
            value = raw_patch.get(key)
            if isinstance(value, str) and not value.strip():
                continue
            patch[key] = value
        if "disabled" in raw_patch and str(raw_patch.get("disabled")).strip().lower() not in {"", "keep", "保持", "none", "null"}:
            patch["disabled"] = self._admin_bool(raw_patch.get("disabled"))
        if not patch:
            self._raise_json(web.HTTPBadRequest, "请至少填写一个要批量修改的字段")
        for item in items:
            family = item.get("family") or owner._normalize_model_family(getattr(owner, "active_model_family", "anima"))
            selector = str(item.get("key") or "").strip()
            try:
                updated, error = self._apply_admin_lora_profile_patch(
                    owner,
                    family,
                    selector,
                    patch,
                    context=context,
                    lora_name=str(item.get("lora") or ""),
                )
                if not updated:
                    if error == "not_found":
                        result["not_found"].append({"family": family, "key": selector})
                    else:
                        result["failed"].append({"family": family, "key": selector, "error": error or "update_failed"})
                    continue
                result["updated"] += 1
                if updated.get("created"):
                    result["created"] += 1
                result["items"].append(updated.get("profile") or {})
            except Exception as e:
                result["failed"].append({"family": family, "key": selector, "error": str(e)[:160]})
        for family in sorted({str(item.get("family") or "") for item in items if item.get("family")}):
            self._refresh_admin_lora_runtime(owner, family)
        ok = result.get("updated", 0) > 0 and not result.get("failed")
        self.service.store.record_gallery_access(
            "admin_lora_batch_update",
            context,
            self._request_ip(request),
            self._user_agent(request),
            ok=ok,
            detail=f"{result.get('updated', 0)}/{result.get('requested', 0)}",
        )
        return web.json_response({"ok": True, "action": "update", "result": result}, headers={"Cache-Control": "no-store"})

    def _admin_billing_payload(self) -> dict:
        cfg = self.service.store.config()
        gallery = cfg.get("gallery", {}) if isinstance(cfg.get("gallery"), dict) else {}
        return {
            "video_local_price": _safe_money(gallery.get("video_local_price", 0.5), 0.5, 0.0, GALLERY_BALANCE_MAX),
            "video_api_price": _safe_money(gallery.get("video_api_price", 0.5), 0.5, 0.0, GALLERY_BALANCE_MAX),
            "trial_balance": _safe_money(gallery.get("video_trial_balance", cfg.get("video_trial_balance", GALLERY_DEFAULT_TRIAL_BALANCE)), GALLERY_DEFAULT_TRIAL_BALANCE, 0.0, GALLERY_BALANCE_MAX),
        }

    async def admin_summary(self, request: web.Request):
        context = self._require_admin(request)
        self.service.store.record_gallery_access("admin_summary", context, self._request_ip(request), self._user_agent(request), ok=True)
        gallery_items = self._admin_gallery_items()
        accounts = self.service.store.list_gallery_accounts()
        total_balance = round(sum(float(a.get("balance") or 0.0) for a in accounts), 2)
        total_spent = round(sum(float(a.get("total_spent") or 0.0) for a in accounts), 2)
        billing = self._admin_billing_payload()
        payload = {
            "ok": True,
            "accounts": accounts,
            "access": self.service.store.gallery_access_summary(),
            "gallery": gallery_items,
            "lora": self._admin_lora_summary(),
            "billing": billing,
            "stats": {
                "accounts": len(accounts),
                "gallery": len(gallery_items),
                "characters": len({item.get("character") for item in gallery_items}),
                "makers": len({item.get("maker") for item in gallery_items}),
                "total_balance": total_balance,
                "total_spent": total_spent,
            },
        }
        return web.json_response(payload, headers={"Cache-Control": "no-store"})

    async def admin_create_account(self, request: web.Request):
        context = self._require_admin(request)
        data = await request.json()
        try:
            created, account, password = self.service.store.create_gallery_account(
                data.get("username"),
                owner_user_id=data.get("owner_user_id", ""),
                owner_name=data.get("owner_name", ""),
                group_id=data.get("group_id", ""),
                created_by=f"admin:{context.get('username')}",
                role=data.get("role", "user"),
                password=data.get("password", ""),
            )
        except ValueError as e:
            raise web.HTTPBadRequest(text=json.dumps({"error": str(e)}, ensure_ascii=False), content_type="application/json")
        self.service.store.record_gallery_access("admin_account_create", context, self._request_ip(request), self._user_agent(request), ok=True, detail=account.get("username"))
        return web.json_response({"ok": bool(created), "account": account, "password": password}, headers={"Cache-Control": "no-store"})

    async def admin_update_account(self, request: web.Request):
        context = self._require_admin(request)
        username = request.match_info.get("username", "")
        data = await request.json()
        account = None
        generated_password = ""
        if data.get("reset_password") or str(data.get("password") or "").strip():
            account, generated_password = self.service.store.reset_gallery_account_password(username, str(data.get("password") or ""))
            if not account:
                raise web.HTTPNotFound(text=json.dumps({"error": "account not found"}, ensure_ascii=False), content_type="application/json")
        account = self.service.store.update_gallery_account(username, data) or account
        if not account:
            raise web.HTTPNotFound(text=json.dumps({"error": "account not found"}, ensure_ascii=False), content_type="application/json")
        self.service.store.record_gallery_access("admin_account_update", context, self._request_ip(request), self._user_agent(request), ok=True, detail=account.get("username"))
        return web.json_response({"ok": True, "account": account, "password": generated_password}, headers={"Cache-Control": "no-store"})

    async def admin_delete_account(self, request: web.Request):
        context = self._require_admin(request)
        username = request.match_info.get("username", "")
        ok = self.service.store.delete_gallery_account(username)
        self.service.store.record_gallery_access("admin_account_delete", context, self._request_ip(request), self._user_agent(request), ok=ok, detail=username)
        if not ok:
            raise web.HTTPBadRequest(text=json.dumps({"error": "不能删除该账号"}, ensure_ascii=False), content_type="application/json")
        return web.json_response({"ok": True}, headers={"Cache-Control": "no-store"})

    async def admin_adjust_balance(self, request: web.Request):
        context = self._require_admin(request)
        username = request.match_info.get("username", "")
        data = await request.json()
        mode = str(data.get("mode") or "add").strip().lower()
        try:
            amount = float(data.get("amount"))
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text=json.dumps({"error": "金额必须是数字"}, ensure_ascii=False), content_type="application/json")
        reason = str(data.get("reason") or "")
        by = f"admin:{context.get('username')}"
        if mode == "set":
            account = self.service.store.set_gallery_balance(username, amount, reason=reason, by=by)
        else:
            account = self.service.store.adjust_gallery_balance(username, amount, reason=reason, by=by)
        if not account:
            raise web.HTTPNotFound(text=json.dumps({"error": "account not found"}, ensure_ascii=False), content_type="application/json")
        self.service.store.record_gallery_access(
            "admin_balance_adjust", context, self._request_ip(request), self._user_agent(request),
            ok=True, detail=f"{username} {mode} {amount}",
        )
        return web.json_response({"ok": True, "account": account}, headers={"Cache-Control": "no-store"})

    async def admin_batch_balance(self, request: web.Request):
        context = self._require_admin(request)
        data = await request.json()
        raw_names = data.get("usernames") or data.get("users") or []
        if isinstance(raw_names, str):
            raw_names = re.split(r"[\s,，;；]+", raw_names)
        if not isinstance(raw_names, (list, tuple, set)):
            raise web.HTTPBadRequest(text=json.dumps({"error": "请选择要批量修改的账号"}, ensure_ascii=False), content_type="application/json")
        usernames = []
        seen = set()
        for name in raw_names:
            clean = self.service.store._clean_gallery_username(name)
            key = self.service.store._gallery_account_key(clean)
            if clean and key not in seen:
                usernames.append(clean)
                seen.add(key)
            if len(usernames) >= 300:
                break
        if not usernames:
            raise web.HTTPBadRequest(text=json.dumps({"error": "请选择要批量修改的账号"}, ensure_ascii=False), content_type="application/json")
        mode = str(data.get("mode") or "add").strip().lower()
        if mode not in {"add", "set"}:
            raise web.HTTPBadRequest(text=json.dumps({"error": "操作类型必须是 add 或 set"}, ensure_ascii=False), content_type="application/json")
        try:
            amount = float(data.get("amount"))
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text=json.dumps({"error": "金额必须是数字"}, ensure_ascii=False), content_type="application/json")
        if mode == "set" and amount < 0:
            raise web.HTTPBadRequest(text=json.dumps({"error": "设额度不能小于 0"}, ensure_ascii=False), content_type="application/json")
        if mode == "add" and amount == 0:
            raise web.HTTPBadRequest(text=json.dumps({"error": "批量增减额度不能为 0"}, ensure_ascii=False), content_type="application/json")
        reason = str(data.get("reason") or ("批量设额度" if mode == "set" else "批量调整额度"))
        by = f"admin:{context.get('username')}"
        result = {"requested": len(usernames), "updated": 0, "failed": []}
        for username in usernames:
            account = self.service.store.set_gallery_balance(username, amount, reason=reason, by=by) if mode == "set" else self.service.store.adjust_gallery_balance(username, amount, reason=reason, by=by)
            if account:
                result["updated"] += 1
            else:
                result["failed"].append(username)
        self.service.store.record_gallery_access(
            "admin_balance_batch", context, self._request_ip(request), self._user_agent(request),
            ok=result["updated"] > 0 and not result["failed"], detail=f"{mode} {amount} {result['updated']}/{result['requested']}",
        )
        return web.json_response({"ok": True, "result": result, "accounts": self.service.store.list_gallery_accounts()}, headers={"Cache-Control": "no-store"})

    async def admin_balance_ledger(self, request: web.Request):
        context = self._require_admin(request)
        username = request.match_info.get("username", "")
        limit = _safe_int(request.query.get("limit", 50), 50, 1, GALLERY_BALANCE_LEDGER_LIMIT)
        ledger = self.service.store.gallery_balance_ledger(username, limit=limit)
        return web.json_response({"ok": True, "username": username, "ledger": ledger}, headers={"Cache-Control": "no-store"})

    async def admin_save_billing(self, request: web.Request):
        context = self._require_admin(request)
        data = await request.json()
        patch = {
            "gallery": {
                "video_trial_balance": _safe_money(data.get("video_trial_balance", data.get("trial_balance", GALLERY_DEFAULT_TRIAL_BALANCE)), GALLERY_DEFAULT_TRIAL_BALANCE, 0.0, GALLERY_BALANCE_MAX),
                "video_local_price": _safe_money(data.get("video_local_price", 0.5), 0.5, 0.0, GALLERY_BALANCE_MAX),
                "video_api_price": _safe_money(data.get("video_api_price", 0.5), 0.5, 0.0, GALLERY_BALANCE_MAX),
            }
        }
        self.service.store.set_config(patch)
        self.service.store.record_gallery_access(
            "admin_billing", context, self._request_ip(request), self._user_agent(request),
            ok=True, detail=json.dumps(patch["gallery"], ensure_ascii=False),
        )
        return web.json_response({"ok": True, "billing": self._admin_billing_payload()}, headers={"Cache-Control": "no-store"})

    def _masked_video_config(self) -> dict:
        conf = self.service.store.get_api_video_config()
        key = str(conf.get("api_key") or "")
        masked = ("•" * 6 + key[-4:]) if len(key) > 4 else ("已设置" if key else "")
        out = dict(conf)
        out["api_key"] = ""              # 不回传明文
        out["api_key_masked"] = masked
        out["api_key_set"] = bool(key)
        return out

    async def admin_get_video_config(self, request: web.Request):
        self._require_admin(request)
        return web.json_response({"ok": True, "config": self._masked_video_config()}, headers={"Cache-Control": "no-store"})

    async def admin_save_video_config(self, request: web.Request):
        context = self._require_admin(request)
        data = await request.json()
        patch = {}
        for field in ("enable", "base_url", "model", "max_images", "timeout", "duration", "aspect_ratio", "proxy", "prompt_enhance", "default_prompt"):
            if field in data:
                patch[field] = data.get(field)
        # 仅当传了非空 api_key 才更新，留空表示保持原值
        if "api_key" in data and str(data.get("api_key") or "").strip():
            patch["api_key"] = data.get("api_key")
        self.service.store.update_api_video_config(patch)
        self.service.store.record_gallery_access(
            "admin_video_config", context, self._request_ip(request), self._user_agent(request), ok=True,
        )
        return web.json_response({"ok": True, "config": self._masked_video_config()}, headers={"Cache-Control": "no-store"})

    async def admin_update_gallery_item(self, request: web.Request):
        context = self._require_admin(request)
        item_id = str(request.match_info.get("item_id") or "")
        data = await request.json()
        item = self.service.store.update_favorite_metadata(item_id, data)
        if not item:
            raise web.HTTPNotFound(text=json.dumps({"error": "gallery item not found"}, ensure_ascii=False), content_type="application/json")
        self.service.store.record_gallery_access("admin_gallery_update", context, self._request_ip(request), self._user_agent(request), ok=True, detail=item_id)
        return web.json_response({"ok": True, "item": next((x for x in self._admin_gallery_items() if x.get("id") == item_id), {})}, headers={"Cache-Control": "no-store"})

    async def admin_delete_gallery_item(self, request: web.Request):
        context = self._require_admin(request)
        item_id = str(request.match_info.get("item_id") or "")
        ok = self.service.store.delete_favorite_item(item_id)
        self.service.store.record_gallery_access("admin_gallery_delete", context, self._request_ip(request), self._user_agent(request), ok=ok, detail=item_id)
        if not ok:
            raise web.HTTPNotFound(text=json.dumps({"error": "gallery item not found"}, ensure_ascii=False), content_type="application/json")
        return web.json_response({"ok": True}, headers={"Cache-Control": "no-store"})

    async def admin_batch_gallery_items(self, request: web.Request):
        context = self._require_admin(request)
        try:
            data = await request.json()
        except Exception:
            self._raise_json(web.HTTPBadRequest, "请求体必须是合法 JSON")
        if not isinstance(data, dict):
            self._raise_json(web.HTTPBadRequest, "请求体必须是 JSON 对象")
        ids = self.service.store._normalize_favorite_batch_ids(data.get("ids") or data.get("item_ids"), limit=300)
        if not ids:
            self._raise_json(web.HTTPBadRequest, "请选择至少一张收藏图")
        action = str(data.get("action") or "update").strip().lower()
        if action in {"delete", "remove"}:
            result = self.service.store.delete_favorite_items_batch(ids, limit=300)
            ok = result.get("deleted", 0) > 0 and not result.get("failed")
            self.service.store.record_gallery_access(
                "admin_gallery_batch_delete",
                context,
                self._request_ip(request),
                self._user_agent(request),
                ok=ok,
                detail=f"{result.get('deleted', 0)}/{result.get('requested', 0)}",
            )
            return web.json_response({"ok": True, "action": "delete", "result": result}, headers={"Cache-Control": "no-store"})
        if action not in {"update", "patch", "edit"}:
            self._raise_json(web.HTTPBadRequest, "未知批量操作")
        raw_patch = data.get("patch") if isinstance(data.get("patch"), dict) else data
        patch = {}
        for key, aliases in {
            "character": ("character", "category"),
            "maker": ("maker", "maker_name"),
            "prompt": ("prompt", "prompt_preview"),
            "summary": ("summary", "design_summary"),
        }.items():
            for alias in aliases:
                if alias not in raw_patch:
                    continue
                value = str(raw_patch.get(alias) or "").strip()
                if value:
                    patch[key] = value
                break
        if not patch:
            self._raise_json(web.HTTPBadRequest, "请至少填写一个要批量修改的字段")
        result = self.service.store.update_favorite_metadata_batch(ids, patch, limit=300)
        ok = result.get("updated", 0) > 0 and not result.get("failed")
        self.service.store.record_gallery_access(
            "admin_gallery_batch_update",
            context,
            self._request_ip(request),
            self._user_agent(request),
            ok=ok,
            detail=f"{result.get('updated', 0)}/{result.get('requested', 0)}",
        )
        return web.json_response({"ok": True, "action": "update", "result": result}, headers={"Cache-Control": "no-store"})

    def _decode_gallery_data_url(self, value: str) -> tuple:
        match = re.match(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$", str(value or ""), flags=re.S)
        if not match:
            raise ValueError("data_url 格式不正确")
        mime = match.group(1)
        try:
            data = base64.b64decode(match.group(2), validate=True)
        except binascii.Error:
            raise ValueError("data_url base64 无效")
        ext = mimetypes.guess_extension(mime) or ".png"
        return data, ext

    async def _download_gallery_import_image(self, url: str) -> tuple:
        url = str(url or "").strip()
        if not re.match(r"^https?://", url, flags=re.I):
            raise ValueError("只支持 http/https 图片地址")
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"User-Agent": "WineFox-Gallery-Admin/1.0"}) as resp:
                if resp.status >= 400:
                    raise ValueError(f"图片下载失败：HTTP {resp.status}")
                mime = str(resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                chunks = []
                size = 0
                async for chunk in resp.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > GALLERY_IMPORT_MAX_BYTES:
                        raise ValueError("图片太大")
                    chunks.append(chunk)
                ext = mimetypes.guess_extension(mime) or Path(url.split("?", 1)[0]).suffix or ".png"
                return b"".join(chunks), ext

    async def admin_import_gallery_item(self, request: web.Request):
        context = self._require_admin(request)
        data = await request.json()
        try:
            if data.get("data_url"):
                image_bytes, ext = self._decode_gallery_data_url(data.get("data_url"))
            else:
                image_bytes, ext = await self._download_gallery_import_image(data.get("image_url") or data.get("url") or "")
            item = self.service.store.import_favorite_image(image_bytes, ext, {
                "character": data.get("character") or data.get("category") or "导入",
                "maker": data.get("maker") or data.get("maker_name") or f"admin:{context.get('username')}",
                "prompt": data.get("prompt") or "",
                "summary": data.get("summary") or data.get("design_summary") or "",
                "source": data.get("image_url") or data.get("url") or "data_url",
            })
        except ValueError as e:
            raise web.HTTPBadRequest(text=json.dumps({"error": str(e)}, ensure_ascii=False), content_type="application/json")
        self.service.store.record_gallery_access("admin_gallery_import", context, self._request_ip(request), self._user_agent(request), ok=True, detail=item.get("id"))
        return web.json_response({"ok": True, "item": next((x for x in self._admin_gallery_items() if x.get("id") == item.get("id")), item)}, headers={"Cache-Control": "no-store"})


class GptMagicWebUI:
    def __init__(self, service: GptMagicService, comfy_status_provider: Callable[[], dict] = None, comfy_action_handler: Callable = None):
        self.service = service
        self.comfy_status_provider = comfy_status_provider
        self.comfy_action_handler = comfy_action_handler
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None
        self.active_host = ""
        self.active_port = 0

    def _check_auth(self, request: web.Request) -> bool:
        token = self.service.store.token()
        provided = (
            request.headers.get("x-gpt-magic-token")
            or request.headers.get("x-qp-token")
            or request.query.get("token")
            or ""
        )
        return bool(token) and secrets.compare_digest(str(provided), str(token))

    def _require_auth(self, request: web.Request):
        if not self._check_auth(request):
            raise web.HTTPUnauthorized(text=json.dumps({"error": "未登录或令牌错误"}, ensure_ascii=False), content_type="application/json")

    async def start(self, host: str, port: int):
        app = web.Application()
        app.router.add_get("/", self.index)
        app.router.add_get("/assets/{path:.*}", self.static_asset)
        app.router.add_post("/api/login", self.login)
        app.router.add_get("/api/state", self.state)
        app.router.add_post("/api/config", self.save_config)
        app.router.add_post("/api/users/quota", self.user_quota)
        app.router.add_post("/api/users/update", self.user_update)
        app.router.add_post("/api/keys/create", self.key_create)
        app.router.add_post("/api/keys/delete", self.key_delete)
        app.router.add_get("/api/history/image/{item_id}", self.history_image)
        app.router.add_get("/api/favorites/image/{item_id}", self.favorite_image)
        app.router.add_get("/api/pose/presets", self.pose_presets)
        app.router.add_post("/api/pose/presets", self.pose_preset_save)
        app.router.add_delete("/api/pose/presets/{preset_id}", self.pose_preset_delete)
        app.router.add_post("/api/pose/generate", self.pose_generate)
        app.router.add_post("/api/comfy/model/{action}", self.comfy_model_action)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, host=host, port=port)
        try:
            await self.site.start()
            self.active_host = host
            self.active_port = port
            logger.info(f"[GPT Magic] WebUI 已启动: http://{host}:{port}")
        except Exception as e:
            logger.error(f"[GPT Magic] WebUI 启动失败，已跳过，不影响 ComfyUI: {e}")
            await self.stop()

    async def stop(self):
        if self.runner:
            try:
                await self.runner.cleanup()
            except Exception as e:
                logger.warning(f"[GPT Magic] WebUI 停止失败: {e}")
        self.runner = None
        self.site = None

    async def index(self, request: web.Request):
        return web.Response(text=WEBUI_HTML.replace("__TOKEN_HINT__", _mask_secret(self.service.store.token())), content_type="text/html")

    async def static_asset(self, request: web.Request):
        rel = str(request.match_info.get("path") or "").replace("\\", "/").lstrip("/")
        root = WEBUI_ASSETS_DIR.resolve()
        target = (root / rel).resolve()
        if target != root and root not in target.parents:
            raise web.HTTPNotFound(text="not found")
        if not target.exists() or not target.is_file():
            raise web.HTTPNotFound(text="not found")
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.suffix.lower() == ".js":
            content_type = "application/javascript"
        return web.FileResponse(target, headers={"Content-Type": content_type})

    async def login(self, request: web.Request):
        data = await request.json()
        token = str(data.get("token", "") or "")
        if not token or not secrets.compare_digest(token, self.service.store.token()):
            raise web.HTTPUnauthorized(text=json.dumps({"error": "令牌错误"}, ensure_ascii=False), content_type="application/json")
        return web.json_response({"ok": True})

    async def state(self, request: web.Request):
        self._require_auth(request)
        comfy = {}
        if self.comfy_status_provider:
            try:
                comfy = self.comfy_status_provider() or {}
            except Exception as e:
                comfy = {"error": str(e)}
        data = self.service.store.public_state(queue_snapshot=self.service.queue_snapshot(), comfy_status=comfy)
        data["webui_runtime"] = {"host": self.active_host, "port": self.active_port}
        return web.json_response(data)

    async def save_config(self, request: web.Request):
        self._require_auth(request)
        payload = await request.json()
        before = self.service.store.config().get("webui", {})
        cfg = self.service.store.set_config(payload)
        after = cfg.get("webui", {})
        restart_required = (
            str(before.get("host")) != str(after.get("host"))
            or int(before.get("port", 0) or 0) != int(after.get("port", 0) or 0)
        )
        return web.json_response({"ok": True, "restart_required": restart_required})

    async def user_quota(self, request: web.Request):
        self._require_auth(request)
        data = await request.json()
        user = self.service.store.add_quota(str(data.get("user_id", "")), int(data.get("amount", 0) or 0))
        if not user:
            raise web.HTTPNotFound(text=json.dumps({"error": "用户不存在"}, ensure_ascii=False), content_type="application/json")
        return web.json_response({"ok": True, "user": user})

    async def user_update(self, request: web.Request):
        self._require_auth(request)
        data = await request.json()
        user_id = str(data.get("user_id", "") or "")
        patch = {}
        for key in ("quota", "blacklisted", "remark", "nickname"):
            if key in data:
                patch[key] = data[key]
        user = self.service.store.update_user(user_id, patch)
        if not user:
            raise web.HTTPNotFound(text=json.dumps({"error": "用户不存在"}, ensure_ascii=False), content_type="application/json")
        return web.json_response({"ok": True, "user": user})

    async def key_create(self, request: web.Request):
        self._require_auth(request)
        data = await request.json()
        keys = self.service.store.create_keys(data.get("amount", 1), data.get("count", 1), data.get("note", ""))
        return web.json_response({"ok": True, "keys": keys})

    async def key_delete(self, request: web.Request):
        self._require_auth(request)
        data = await request.json()
        ok = self.service.store.delete_key(str(data.get("key", "") or ""))
        return web.json_response({"ok": ok})

    async def comfy_model_action(self, request: web.Request):
        self._require_auth(request)
        if not self.comfy_action_handler:
            raise web.HTTPNotFound(text=json.dumps({"error": "ComfyUI model manager is not available"}, ensure_ascii=False), content_type="application/json")
        payload = await request.json()
        action = str(request.match_info.get("action") or "")
        try:
            result = await self.comfy_action_handler(action, payload)
            return web.json_response(result)
        except Exception as e:
            raise web.HTTPBadRequest(text=json.dumps({"error": str(e)}, ensure_ascii=False), content_type="application/json")

    async def history_image(self, request: web.Request):
        self._require_auth(request)
        path = self.service.store.get_history_image_path(request.match_info["item_id"])
        if not path:
            raise web.HTTPNotFound(text="not found")
        return web.FileResponse(path)

    async def favorite_image(self, request: web.Request):
        self._require_auth(request)
        path = self.service.store.get_favorite_image_path(request.match_info["item_id"])
        if not path:
            raise web.HTTPNotFound(text="not found")
        return web.FileResponse(path)

    async def pose_presets(self, request: web.Request):
        self._require_auth(request)
        return web.json_response({"ok": True, "presets": self.service.store.list_pose_presets()})

    async def pose_preset_save(self, request: web.Request):
        self._require_auth(request)
        data = await request.json()
        try:
            preset = self.service.store.save_pose_preset(data)
        except Exception as e:
            raise web.HTTPBadRequest(text=json.dumps({"error": str(e)}, ensure_ascii=False), content_type="application/json")
        return web.json_response({"ok": True, "preset": preset, "presets": self.service.store.list_pose_presets()})

    async def pose_preset_delete(self, request: web.Request):
        self._require_auth(request)
        ok = self.service.store.delete_pose_preset(request.match_info.get("preset_id"))
        return web.json_response({"ok": ok, "presets": self.service.store.list_pose_presets()})

    async def pose_generate(self, request: web.Request):
        self._require_auth(request)
        if not self.comfy_action_handler:
            raise web.HTTPNotFound(text=json.dumps({"error": "ComfyUI is not available"}, ensure_ascii=False), content_type="application/json")
        payload = await request.json()
        try:
            result = await self.comfy_action_handler("pose_generate", payload)
            return web.json_response(result)
        except Exception as e:
            raise web.HTTPBadRequest(text=json.dumps({"error": str(e)}, ensure_ascii=False), content_type="application/json")


FAVORITE_GALLERY_HTML_PATH = PLUGIN_ROOT / "templates" / "favorite_gallery.html"
WINEFOX_DRAW_HTML_PATH = PLUGIN_ROOT / "templates" / "winefox_draw.html"


def _load_favorite_gallery_html() -> str:
    try:
        return FAVORITE_GALLERY_HTML_PATH.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"[GPT Magic] WineFox gallery template load failed: {e}")
        return ""


def _load_winefox_draw_html() -> str:
    try:
        return WINEFOX_DRAW_HTML_PATH.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"[GPT Magic] WineFox draw template load failed: {e}")
        return ""


WEBUI_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>酒狐魔法后台</title>
  <style>
    :root { color-scheme: light; --bg:#f4f7fb; --paper:#ffffff; --paper-soft:#f8fafc; --paper-strong:#ffffff; --ink:#172033; --muted:#657083; --line:#d9e2ee; --line-strong:#bac8dc; --sumi:#22304a; --ai:#315efb; --aka:#d64545; --matcha:#0f9f77; --warn:#b7791f; --danger:#c73642; --shadow:0 10px 24px rgba(31,44,67,.08); --shadow-soft:0 4px 14px rgba(31,44,67,.06); }
    * { box-sizing:border-box; }
    body { margin:0; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:linear-gradient(180deg,#f9fbff 0%,var(--bg) 46%,#eef3f9 100%); color:var(--ink); }
    button, input, select, textarea { font:inherit; }
    .login { min-height:100vh; display:grid; place-items:center; padding:24px; }
    .login-card, .card, .panel, .surface { background:var(--paper); border:1px solid var(--line); border-radius:8px; box-shadow:var(--shadow-soft); }
    .login-card { width:min(440px, 100%); padding:26px; }
    .muted { color:var(--muted); }
    .app { display:grid; grid-template-columns:252px 1fr; min-height:100vh; }
    aside { border-right:1px solid var(--line); padding:18px; background:#ffffff; }
    .brand { font-size:20px; font-weight:750; margin-bottom:8px; letter-spacing:0; }
    .brand::before { content:""; display:inline-block; width:22px; height:2px; margin-right:8px; vertical-align:middle; background:var(--ai); }
    .nav { display:grid; gap:8px; margin-top:20px; }
    .nav button { text-align:left; border:1px solid transparent; background:transparent; color:var(--muted); padding:11px 12px; border-radius:8px; cursor:pointer; }
    .nav button.active, .nav button:hover { color:var(--ink); border-color:#dbe5f3; background:#f4f7fb; box-shadow:inset 3px 0 0 var(--ai); }
    main { padding:22px; overflow:auto; min-width:0; }
    .top { display:flex; justify-content:space-between; gap:16px; align-items:flex-start; margin:0 auto 18px; max-width:1440px; }
    h1,h2,h3 { margin:0; }
    .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(210px,1fr)); gap:12px; }
    .grid.two { grid-template-columns:repeat(auto-fit,minmax(340px,1fr)); }
    .card { padding:16px; }
    .hero, .page-hero { border-color:#dbe5f3; background:linear-gradient(135deg,#ffffff,#f6f9fd); }
    .page-hero { padding:18px; border:1px solid var(--line); border-radius:8px; box-shadow:var(--shadow-soft); }
    .pill { display:inline-flex; align-items:center; gap:6px; padding:4px 9px; border-radius:999px; background:#eef3ff; color:var(--ai); font-size:12px; border:1px solid #d7e2ff; white-space:nowrap; }
    .split { display:grid; grid-template-columns:1.1fr .9fr; gap:12px; }
    .kpi { position:relative; overflow:hidden; }
    .kpi::before { content:""; position:absolute; inset:0 auto 0 0; width:3px; background:var(--aka); opacity:.76; }
    .kpi .value { font-size:28px; font-weight:800; margin-top:8px; }
    .stack { display:grid; gap:14px; max-width:1440px; margin:0 auto; }
    .hidden { display:none !important; }
    .row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
    .input, .select, .textarea { width:100%; border:1px solid var(--line); border-radius:8px; background:#fff; color:var(--ink); padding:10px; outline:none; min-width:0; }
    .input:focus, .select:focus, .textarea:focus { border-color:#8aa5ff; box-shadow:0 0 0 3px rgba(49,94,251,.12); }
    .textarea { min-height:96px; resize:vertical; }
    label { display:block; margin-bottom:6px; color:var(--muted); font-size:13px; }
    .form { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:12px; }
    .wide { grid-column:1 / -1; }
    .btn { border:1px solid var(--line); border-radius:8px; background:#fff; color:var(--ink); padding:10px 13px; cursor:pointer; }
    .btn:hover { border-color:var(--line-strong); transform:translateY(-1px); }
    .btn.primary { background:var(--ai); border-color:var(--ai); color:#fff; font-weight:700; }
    .btn.danger { background:rgba(185,74,74,.10); border-color:rgba(185,74,74,.28); color:var(--danger); }
    table { width:100%; border-collapse:collapse; }
    th,td { border-bottom:1px solid var(--line); padding:10px; text-align:left; vertical-align:top; }
    th { color:var(--muted); font-weight:600; }
    .tag { display:inline-flex; padding:3px 8px; border-radius:999px; font-size:12px; background:rgba(43,48,55,.08); color:var(--sumi); border:1px solid rgba(43,48,55,.10); }
    .tag.ok { background:rgba(76,114,92,.10); color:var(--matcha); border-color:rgba(76,114,92,.18); }
    .tag.warn { background:rgba(181,121,35,.10); color:var(--warn); border-color:rgba(181,121,35,.18); }
    .tag.danger { background:rgba(185,74,74,.10); color:var(--danger); border-color:rgba(185,74,74,.18); }
    .gallery { display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:14px; min-width:0; }
    .thumb { width:100%; aspect-ratio:1/1; object-fit:cover; border-radius:8px; background:rgba(43,48,55,.08); border:1px solid rgba(43,37,32,.12); display:block; }
    .history-card { display:grid; gap:10px; padding:10px; }
    .history-preview { position:relative; padding:0; border:0; background:transparent; cursor:zoom-in; border-radius:8px; overflow:hidden; }
    .history-preview::after { content:"预览"; position:absolute; right:9px; bottom:9px; padding:3px 8px; border-radius:999px; color:#fff; background:rgba(36,31,27,.72); font-size:12px; opacity:0; transition:opacity .15s; }
    .history-preview:hover::after { opacity:1; }
    .history-meta { display:grid; gap:6px; font-size:13px; line-height:1.55; }
    .history-prompt { color:var(--ink); background:var(--paper-soft); border:1px solid var(--line); border-radius:8px; padding:8px; min-height:42px; }
    .help { line-height:1.8; color:#475569; }
    .toolbar { display:grid; grid-template-columns:minmax(240px,1fr) 180px; gap:12px; align-items:end; }
    .section-head { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; margin-bottom:12px; }
    .favorite-layout { display:grid; grid-template-columns:270px minmax(0,1fr); gap:16px; align-items:start; }
    .role-panel { padding:12px; position:sticky; top:14px; max-height:calc(100vh - 120px); overflow:auto; }
    .panel-title { font-weight:750; margin-bottom:10px; }
    .role-list { display:grid; gap:7px; }
    .role-button { width:100%; display:grid; grid-template-columns:40px minmax(0,1fr) auto; gap:10px; align-items:center; padding:8px; border:1px solid transparent; border-radius:8px; background:transparent; color:var(--ink); text-align:left; cursor:pointer; }
    .role-button:hover, .role-button.active { background:#f2f6ff; border-color:#d7e2ff; }
    .role-avatar { width:40px; height:40px; border-radius:8px; object-fit:cover; background:#e8eef7; border:1px solid var(--line); }
    .role-name { font-weight:680; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .role-sub { color:var(--muted); font-size:12px; margin-top:2px; }
    .role-count { color:var(--muted); font-size:12px; }
    .favorite-main { min-width:0; }
    .favorite-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(184px,1fr)); gap:14px; align-items:start; }
    .favorite-card { display:grid; gap:9px; min-width:0; padding:10px; background:#fff; border:1px solid var(--line); border-radius:8px; box-shadow:var(--shadow-soft); }
    .favorite-preview { width:100%; aspect-ratio:3/4; padding:0; border:0; border-radius:8px; overflow:hidden; background:#e8eef7; cursor:zoom-in; }
    .favorite-img { width:100%; height:100%; object-fit:cover; display:block; }
    .favorite-title { font-weight:720; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .favorite-meta { display:grid; gap:4px; min-width:0; font-size:12px; line-height:1.45; color:var(--muted); }
    .clamp { display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
    .empty-state { padding:30px; text-align:center; color:var(--muted); border:1px dashed var(--line-strong); border-radius:8px; background:#fff; }
    .comfy-shell { display:grid; grid-template-columns:240px minmax(0,1fr); gap:16px; align-items:start; }
    .comfy-sidebar { padding:12px; position:sticky; top:14px; }
    .comfy-nav { display:grid; gap:7px; }
    .comfy-nav-btn { width:100%; padding:10px 12px; border:1px solid transparent; border-radius:8px; background:transparent; color:var(--muted); text-align:left; cursor:pointer; }
    .comfy-nav-btn:hover, .comfy-nav-btn.active { color:var(--ink); background:#f2f6ff; border-color:#d7e2ff; box-shadow:inset 3px 0 0 var(--ai); }
    .comfy-main { display:grid; gap:14px; min-width:0; }
    .comfy-page { display:grid; gap:14px; min-width:0; }
    .section-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; }
    .settings-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }
    .settings-actions { display:flex; gap:10px; align-items:center; justify-content:flex-end; padding:12px; background:#fff; border:1px solid var(--line); border-radius:8px; box-shadow:var(--shadow-soft); }
    .kv-list { display:grid; gap:8px; min-width:0; }
    .kv-row { display:grid; grid-template-columns:150px minmax(0,1fr); gap:10px; padding:8px 0; border-bottom:1px solid var(--line); }
    .kv-row:last-child { border-bottom:0; }
    .kv-key { color:var(--muted); }
    .kv-value { min-width:0; overflow-wrap:anywhere; }
    .check-line { display:flex; align-items:center; min-height:42px; gap:8px; padding:10px; border:1px solid var(--line); border-radius:8px; background:#fff; }
    .pose-layout { display:grid; grid-template-columns:minmax(620px,1fr) 420px; gap:16px; align-items:start; }
    .pose-stage { min-width:0; padding:12px; display:grid; gap:12px; }
    .pose-viewport { position:relative; width:100%; min-height:640px; border:1px solid var(--line); border-radius:8px; overflow:hidden; background:#101827; }
    .pose-viewport canvas { display:block; width:100%; height:100%; }
    .pose-overlay { position:absolute; left:12px; bottom:12px; display:flex; gap:8px; flex-wrap:wrap; pointer-events:none; }
    .pose-panel { display:grid; gap:12px; min-width:0; }
    .pose-controls { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }
    .pose-controls input[type="range"] { width:100%; accent-color:var(--ai); }
    .pose-quick-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px; margin-top:12px; }
    .pose-quick-grid .btn { min-height:38px; padding:8px; }
    .pose-selected { display:grid; gap:4px; padding:10px; margin-top:12px; border:1px solid var(--line); border-radius:8px; background:#f8fafc; }
    .pose-hint-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; }
    .pose-hint { padding:8px; border:1px solid var(--line); border-radius:8px; background:#fff; color:var(--muted); font-size:12px; text-align:center; }
    .pose-output { min-height:190px; font-family:ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace; }
    .pose-generate-output { display:grid; gap:10px; margin-top:12px; }
    .pose-preview-row { display:grid; grid-template-columns:150px minmax(0,1fr); gap:10px; align-items:start; }
    .pose-control-preview { width:150px; aspect-ratio:2/3; object-fit:contain; border:1px solid var(--line); border-radius:8px; background:#05070c; }
    .pose-result-img { width:100%; max-height:620px; object-fit:contain; border:1px solid var(--line); border-radius:8px; background:#fff; }
    .pose-meta { max-height:130px; overflow:auto; font-size:12px; color:var(--muted); overflow-wrap:anywhere; }
    .pose-preset-list { display:grid; gap:8px; max-height:260px; overflow:auto; }
    .pose-preset { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:8px; align-items:center; padding:9px; border:1px solid var(--line); border-radius:8px; background:#fff; }
    .pose-preset-name { font-weight:680; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .pose-preset-meta { color:var(--muted); font-size:12px; margin-top:2px; }
    .modal { position:fixed; inset:0; z-index:50; display:grid; place-items:center; padding:24px; background:rgba(35,31,28,.45); backdrop-filter:blur(12px); }
    .modal-panel { width:min(1120px,100%); max-height:92vh; display:grid; gap:12px; padding:14px; background:#fff; border:1px solid var(--line); border-radius:8px; box-shadow:0 22px 70px rgba(22,18,15,.26); }
    .modal-img-wrap { min-height:220px; max-height:72vh; display:grid; place-items:center; overflow:auto; border:1px solid var(--line); border-radius:8px; background:#f8fafc; }
    .modal-img-wrap img { max-width:100%; max-height:72vh; object-fit:contain; display:block; }
    .modal-bar { display:flex; justify-content:space-between; gap:10px; align-items:center; }
    .modal-meta { max-height:120px; overflow:auto; font-size:13px; }
    @media (max-width: 1000px) { .favorite-layout,.comfy-shell,.section-grid,.pose-layout { grid-template-columns:1fr; } .role-panel,.comfy-sidebar { position:static; max-height:none; } }
    @media (max-width: 760px) { .app { grid-template-columns:1fr; } aside { border-right:0; border-bottom:1px solid var(--line); } .grid.two,.split,.settings-grid,.toolbar { grid-template-columns:1fr; } main { padding:14px; } .favorite-grid { grid-template-columns:repeat(auto-fill,minmax(142px,1fr)); } .kv-row { grid-template-columns:1fr; gap:4px; } }
  </style>
</head>
<body>
  <div id="login" class="login">
    <div class="login-card">
      <h1>酒狐魔法后台</h1>
      <p class="muted">请输入 WebUI 令牌。首次自动生成的令牌提示：<b>__TOKEN_HINT__</b></p>
      <label>令牌</label>
      <input id="token" class="input" type="password" autocomplete="current-password">
      <div style="height:12px"></div>
      <button class="btn primary" onclick="login()">进入后台</button>
      <p id="loginMsg" class="muted"></p>
    </div>
  </div>
  <div id="app" class="app hidden">
    <aside>
      <div class="brand">酒狐魔法后台</div>
      <div class="muted">GPT 魔法、ComfyUI 和收藏图库集中管理。</div>
      <div class="nav">
        <button class="active" data-tab="dashboard">总览</button>
        <button data-tab="gpt">GPT 魔法配置</button>
        <button data-tab="users">用户额度</button>
        <button data-tab="keys">卡密管理</button>
        <button data-tab="queue">队列状态</button>
        <button data-tab="history">历史图片</button>
        <button data-tab="favorites">收藏图库</button>
        <button data-tab="pose">姿态控制</button>
        <button data-tab="comfy">ComfyUI</button>
        <button data-tab="help">指令说明</button>
      </div>
    </aside>
    <main>
      <div class="top">
        <div>
          <h2 id="title">总览</h2>
          <div id="desc" class="muted">查看 GPT 魔法运行状态。</div>
        </div>
        <div class="row">
          <button class="btn" onclick="refresh(true)">刷新</button>
          <button class="btn danger" onclick="logout()">退出</button>
        </div>
      </div>
      <section id="tab-dashboard" class="stack">
        <div id="kpis" class="grid"></div>
        <div class="grid two">
          <div class="card"><h3>当前 GPT 配置</h3><div id="summary" class="help"></div></div>
          <div class="card"><h3>最近 7 天</h3><div id="trend" class="help"></div></div>
        </div>
      </section>
      <section id="tab-gpt" class="stack hidden">
        <div class="card">
          <h3>后台与开关</h3>
          <p class="muted">Host/端口改动后需要重启 AstrBot 或重载插件才会切换监听地址。</p>
          <div class="form">
            <div><label>WebUI Host</label><input id="webHost" class="input"></div>
            <div><label>WebUI 端口</label><input id="webPort" type="number" class="input"></div>
            <div><label>WebUI 令牌</label><input id="webToken" class="input"></div>
            <div><label>默认注册额度</label><input id="defaultQuota" type="number" class="input"></div>
            <div><label>队列上限</label><input id="maxQueue" type="number" class="input"></div>
            <div><label>并发任务数</label><input id="maxConcurrentTasks" type="number" min="1" max="8" class="input"></div>
            <div><label>历史条数</label><input id="historyLimit" type="number" class="input"></div>
            <div><label><input id="enabled" type="checkbox"> 启用 GPT 魔法</label></div>
            <div><label><input id="drawEnabled" type="checkbox"> 允许用户生图</label></div>
            <div><label><input id="resultImageInForward" type="checkbox"> 结果图片放入聊天记录</label></div>
            <div><label><input id="sendTaskReceived" type="checkbox"> 发送收到任务提示</label></div>
            <div><label><input id="sendProcessing" type="checkbox"> 发送开始生成提示</label></div>
            <div class="wide"><label>关闭原因</label><input id="closeReason" class="input"></div>
            <div class="wide"><label>收到任务提示</label><input id="taskReceivedMessage" class="input" placeholder="支持 {position} {mode} {prompt}"></div>
            <div class="wide"><label>文生图开始提示</label><input id="processingMessage" class="input"></div>
            <div class="wide"><label>图生图开始提示</label><input id="editProcessingMessage" class="input"></div>
          </div>
        </div>
        <div class="card">
          <h3>OpenAI 兼容图片接口</h3>
        <p class="muted">无图走 `generations`；引用/附带一张或多张图片会自动走 `edits`，支持“酒狐魔法 要求 / 酒狐魔法改图 要求 / 酒狐魔法图生图 要求”。`edit_api_url` 可留空，系统会自动拼接或替换为 `/v1/images/edits`。提示词会原样传入，不走 ComfyUI 的 LLM 转译；如果中转不认识 `input_fidelity/output_format/background` 等参数，会自动用最小兼容参数重试。</p>
        <p class="muted">4K 预设会通过 `size` 字段传给 OpenAI 兼容接口；官方接口不一定开放所有高分辨率，若你的中转另有参数名，可以继续在“额外请求 JSON”里透传。</p>
          <div class="form">
            <div><label>基础模型</label><input id="model" class="input" placeholder="gpt-image-2"></div>
            <div><label>2K 模型</label><input id="model2k" class="input" placeholder="gpt-image-2-2k"></div>
            <div><label>4K 模型</label><input id="model4k" class="input" placeholder="gpt-image-2-4k"></div>
            <div><label><input id="autoModelByResolution" type="checkbox"> 按分辨率自动换模型</label></div>
            <div><label>超时秒数</label><input id="timeout" type="number" class="input"></div>
            <div class="wide"><label>API Base</label><input id="baseUrl" class="input" placeholder="https://api.openai.com"></div>
            <div class="wide"><label>API URL</label><input id="apiUrl" class="input" placeholder="https://.../v1/images/generations"></div>
            <div class="wide"><label>Edit API URL（图生图，可留空）</label><input id="editApiUrl" class="input" placeholder="https://.../v1/images/edits"></div>
            <div class="wide"><label>API Key</label><input id="apiKey" class="input"></div>
            <div><label>质量</label><input id="quality" class="input"></div>
            <div><label>背景</label><input id="background" class="input"></div>
            <div><label>审核</label><input id="moderation" class="input" placeholder="auto"></div>
            <div><label>输出格式</label><select id="format" class="select"><option>png</option><option>jpeg</option><option>webp</option></select></div>
            <div><label>压缩率</label><input id="compression" type="number" min="0" max="100" class="input"></div>
            <div><label>默认分辨率</label><select id="defaultResolution" class="select"><option value="4k">4K / 最高</option><option value="2k">2K</option><option value="1k">1K</option></select></div>
            <div><label>比例 / 尺寸策略</label><select id="aspect" class="select"><option value="auto">自动识别</option><option value="square">方图</option><option value="portrait">竖图 2:3</option><option value="story">故事/手机 9:16</option><option value="landscape">横图 3:2</option><option value="wide">宽屏 16:9</option><option value="square_2k">2K 方图 2048x2048</option><option value="portrait_2k">2K 竖图 1360x2048</option><option value="story_2k">2K 手机 1152x2048</option><option value="landscape_2k">2K 横图 2048x1152</option><option value="square_4k">4K 方图 2880x2880</option><option value="portrait_4k">4K 竖图 2336x3520</option><option value="story_4k">4K 手机 2160x3840</option><option value="landscape_4k">4K 横图 3840x2160</option><option value="wide_4k">4K 宽屏 3840x2160</option></select></div>
            <div><label><input id="aspectDetect" type="checkbox"> 从提示词识别横图/竖图/方图</label></div>
            <div><label><input id="customSizeOn" type="checkbox"> 启用自定义尺寸</label></div>
            <div><label>自定义尺寸</label><input id="customSize" class="input" placeholder="1024x1024"></div>
            <div><label>图生图源图保真度</label><select id="inputFidelity" class="select"><option value="high">high</option><option value="low">low</option><option value="">不发送</option></select></div>
            <div><label><input id="stream" type="checkbox"> gpt-image 流式请求，失败自动回退</label></div>
            <div class="wide"><label>额外请求 JSON</label><textarea id="extraBody" class="textarea"></textarea></div>
          </div>
        </div>
        <div class="card">
          <h3>缓存</h3>
          <div class="form">
            <div><label><input id="cacheEnabled" type="checkbox"> 开启缓存</label></div>
            <div><label>缓存保留秒数</label><input id="cacheTtl" type="number" class="input"></div>
            <div><label>缓存最大条数</label><input id="cacheMax" type="number" class="input"></div>
            <div><label><input id="cacheConsume" type="checkbox"> 命中缓存也扣额度</label></div>
          </div>
          <div style="height:12px"></div>
          <button class="btn primary" onclick="saveConfig()">保存配置</button>
          <span id="saveMsg" class="muted"></span>
        </div>
      </section>
      <section id="tab-users" class="stack hidden">
        <div class="card"><input id="userSearch" class="input" placeholder="搜索 QQ / 昵称" oninput="renderUsers()"></div>
        <div class="card"><table><thead><tr><th>QQ</th><th>昵称</th><th>额度</th><th>成功/失败</th><th>黑名单</th><th>备注</th><th>操作</th></tr></thead><tbody id="users"></tbody></table></div>
      </section>
      <section id="tab-keys" class="stack hidden">
        <div class="card">
          <div class="form">
            <div><label>每张卡密额度</label><input id="keyAmount" class="input" type="number" value="5"></div>
            <div><label>生成数量</label><input id="keyCount" class="input" type="number" value="1"></div>
            <div><label>备注</label><input id="keyNote" class="input"></div>
          </div>
          <div style="height:12px"></div>
          <button class="btn primary" onclick="createKeys()">生成卡密</button>
          <pre id="keyResult" class="muted"></pre>
        </div>
        <div class="card"><table><thead><tr><th>卡密</th><th>额度</th><th>备注</th><th>创建时间</th><th>操作</th></tr></thead><tbody id="keys"></tbody></table></div>
      </section>
      <section id="tab-queue" class="stack hidden"><div id="queueCards" class="grid"></div><div class="card"><table><thead><tr><th>序号</th><th>QQ</th><th>提示词</th><th>入队时间</th></tr></thead><tbody id="queue"></tbody></table></div></section>
      <section id="tab-history" class="stack hidden"><div id="history" class="gallery"></div></section>
      <section id="tab-favorites" class="stack hidden">
        <div class="page-hero">
          <div class="row"><span class="pill">酒狐收藏</span><span class="pill">按角色分类</span><span class="pill">本地图库</span></div>
          <h3 style="margin-top:12px">收藏图库</h3>
          <p class="muted">左侧是角色目录，右侧只显示当前角色的收藏图。旧收藏按目录名归类，新收藏优先读取 sidecar 里的角色、收藏人、来源、模型和提示词。</p>
        </div>
        <div class="toolbar">
          <div><label>搜索角色 / 文件名 / 提示词 / QQ</label><input id="favoriteSearch" class="input" placeholder="输入角色名、QQ、文件名或 prompt 片段" oninput="renderFavorites()"></div>
          <div><label>排序</label><select id="favoriteSort" class="select" onchange="renderFavorites()"><option value="latest">最新收藏</option><option value="role">角色名称</option><option value="file">文件名</option></select></div>
        </div>
        <div class="favorite-layout">
          <div class="surface role-panel">
            <div class="panel-title">角色目录</div>
            <div id="favoriteRoles" class="role-list"></div>
          </div>
          <div class="favorite-main">
            <div class="section-head">
              <div>
                <h3 id="favoriteRoleTitle">全部角色</h3>
                <div id="favoriteRoleMeta" class="muted"></div>
              </div>
              <div id="favoriteStats" class="row"></div>
            </div>
            <div id="favorites" class="favorite-grid"></div>
          </div>
        </div>
      </section>
      <section id="tab-pose" class="stack hidden">
        <div class="page-hero">
          <div class="row"><span class="pill">IK 骨架</span><span class="pill">手脸脚细节</span><span class="pill">姿态提示词</span><span class="pill">本地预设</span></div>
          <h3 style="margin-top:12px">姿态控制</h3>
          <p class="muted">拖动身体控制点、手脚末端和相机视角，生成只包含姿态与镜头的英文提示词。</p>
        </div>
        <div class="pose-layout">
          <div class="surface pose-stage">
            <div class="section-head">
              <div>
                <h3>人体控制器</h3>
                <div id="poseStatus" class="muted">等待初始化</div>
              </div>
              <div class="row">
                <button class="btn" onclick="resetPoseState()">重置</button>
                <button class="btn" onclick="mirrorPoseState()">左右镜像</button>
                <button class="btn" onclick="copyPosePrompt()">复制提示词</button>
              </div>
            </div>
            <div id="poseViewport" class="pose-viewport">
              <div class="pose-overlay"><span class="pill">Orbit</span><span class="pill">XYZ Gizmo</span><span class="pill">IK limbs</span><span class="pill">Body / Hands / Feet / Face</span></div>
            </div>
            <div class="pose-hint-grid">
              <div class="pose-hint">拖手腕/脚踝：整条手臂/腿跟随</div>
              <div class="pose-hint">拖骨盆：移动全身</div>
              <div class="pose-hint">拖胸/肩/髋：带动身体段落</div>
              <div class="pose-hint">手指/脚尖可单独细调</div>
            </div>
          </div>
          <div class="pose-panel">
            <div class="surface card">
              <h3>人体控制</h3>
              <div class="pose-controls">
                <div><label>控制层级</label><select id="poseFocus" class="select" onchange="updatePoseVisibility()"><option value="all">全部</option><option value="body">身体</option><option value="hands">手部</option><option value="feet">脚部</option><option value="face">脸/头</option></select></div>
                <div><label>控制点大小 <span id="poseHandleValue">1.0</span></label><input id="poseHandleSize" type="range" min="0.7" max="1.8" step="0.1" value="1" oninput="updatePoseHandleScale()"></div>
                <label class="check-line"><input id="poseHumanLimits" type="checkbox" checked onchange="syncPoseOptions()"> 人体约束</label>
                <label class="check-line"><input id="poseLockBones" type="checkbox" checked onchange="syncPoseOptions()"> 锁定骨长</label>
                <label class="check-line"><input id="poseShowHands" type="checkbox" checked onchange="syncPoseOptions()"> 手指</label>
                <label class="check-line"><input id="poseShowFace" type="checkbox" checked onchange="syncPoseOptions()"> 头脸</label>
                <label class="check-line"><input id="poseShowFeet" type="checkbox" checked onchange="syncPoseOptions()"> 脚尖</label>
                <label class="check-line"><input id="poseShowGuides" type="checkbox" checked onchange="syncPoseOptions()"> 辅助线</label>
              </div>
              <div class="pose-quick-grid">
                <button class="btn" onclick="applyPoseTemplate('idle')">自然站立</button>
                <button class="btn" onclick="applyPoseTemplate('arms_up')">双手举高</button>
                <button class="btn" onclick="applyPoseTemplate('crossed_arms')">抱臂</button>
                <button class="btn" onclick="applyPoseTemplate('running')">奔跑</button>
                <button class="btn" onclick="applyPoseTemplate('sitting')">坐姿</button>
                <button class="btn" onclick="applyPoseTemplate('kneeling')">单膝跪</button>
              </div>
              <div id="poseSelected" class="pose-selected">
                <b>未选中控制点</b>
                <span class="muted">点击身体、手脚或脸部关键点后，可用 XYZ 轴精调。</span>
              </div>
            </div>
            <div class="surface card">
              <h3>镜头与构图</h3>
              <div class="pose-controls">
                <div><label>Yaw <span id="poseYawValue">0</span></label><input id="poseYaw" type="range" min="-180" max="180" step="1" value="0" oninput="updatePoseCameraFromControls()"></div>
                <div><label>Pitch <span id="posePitchValue">10</span></label><input id="posePitch" type="range" min="-45" max="60" step="1" value="10" oninput="updatePoseCameraFromControls()"></div>
                <div><label>Roll</label><input id="poseRoll" type="number" class="input" value="0" oninput="updatePoseCameraFromControls()"></div>
                <div><label>Distance</label><input id="poseDistance" type="number" class="input" value="4.2" step="0.1" oninput="updatePoseCameraFromControls()"></div>
                <div><label>景别</label><select id="poseShot" class="select" onchange="updatePosePrompt()"><option value="full_body">full body</option><option value="upper_body">upper body</option><option value="close_up">close-up</option></select></div>
                <div><label>构图</label><select id="poseFraming" class="select" onchange="updatePosePrompt()"><option value="centered">centered</option><option value="dynamic">dynamic diagonal</option><option value="side">side composition</option></select></div>
                <div><label>角色朝向</label><select id="poseFacing" class="select" onchange="updatePosePrompt()"><option value="auto">auto</option><option value="facing viewer">facing viewer</option><option value="looking to side">looking to side</option><option value="back view">back view</option></select></div>
                <div><label>动作强度</label><select id="poseIntensity" class="select" onchange="updatePosePrompt()"><option value="natural">natural</option><option value="dynamic">dynamic</option><option value="dramatic">dramatic</option></select></div>
              </div>
            </div>
            <div class="surface card">
              <h3>提示词</h3>
              <textarea id="posePrompt" class="textarea pose-output" readonly></textarea>
            </div>
            <div class="surface card">
              <h3>ComfyUI 生图</h3>
              <div class="settings-grid">
                <div class="wide"><label>画面提示词</label><textarea id="poseDrawPrompt" class="textarea" rows="5" placeholder="例如：1girl, blonde hair, blue eyes, white dress, simple background"></textarea></div>
                <div><label>姿态生成</label><input id="poseAutoSummary" class="input" value="auto full body / 1024x1440" readonly></div>
                <label class="check-line"><input id="poseFreeMemory" type="checkbox" checked> 生成前清显存</label>
                <details class="wide">
                  <summary>高级参数</summary>
                  <div class="settings-grid" style="margin-top:10px">
                    <div><label>OpenPose ControlNet</label><input id="poseControlNetName" class="input" value="illustriousXL_Openpose_v10.safetensors"></div>
                    <label class="check-line"><input id="poseManualControl" type="checkbox" onchange="syncPoseGenerateControls()"> 手动覆盖强度</label>
                    <div><label>姿态强度 <span id="poseControlStrengthValue">0.88</span></label><input id="poseControlStrength" type="range" min="0.2" max="1.4" step="0.02" value="0.88" oninput="syncPoseGenerateControls(true)"></div>
                    <div><label>结束比例 <span id="poseControlEndValue">0.78</span></label><input id="poseControlEnd" type="range" min="0.3" max="1" step="0.02" value="0.78" oninput="syncPoseGenerateControls(true)"></div>
                    <label class="check-line"><input id="poseIncludePrompt" type="checkbox"> 附带完整姿态提示词</label>
                  </div>
                </details>
              </div>
              <div class="row" style="margin-top:12px">
                <button id="poseGenerateBtn" class="btn primary" onclick="generatePoseImage()">用当前姿态生图</button>
                <button class="btn" onclick="previewPoseControlImage()">刷新控制图</button>
                <span id="poseGenerateMsg" class="muted"></span>
              </div>
              <div class="pose-generate-output">
                <div class="pose-preview-row">
                  <img id="poseControlPreview" class="pose-control-preview" alt="OpenPose 控制图预览">
                  <div>
                    <img id="poseResultImage" class="pose-result-img hidden" alt="ComfyUI 姿态生图结果">
                    <div id="poseGenerateMeta" class="pose-meta"></div>
                  </div>
                </div>
              </div>
            </div>
            <div class="surface card">
              <h3>预设</h3>
              <div class="settings-grid">
                <div class="wide"><label>预设名称</label><input id="posePresetName" class="input" placeholder="例如：侧身抬手"></div>
              </div>
              <div class="row" style="margin-top:12px">
                <button class="btn primary" onclick="savePosePreset()">保存预设</button>
                <button class="btn" onclick="loadPosePresets()">刷新</button>
                <span id="posePresetMsg" class="muted"></span>
              </div>
              <div id="posePresetList" class="pose-preset-list" style="margin-top:12px"></div>
            </div>
          </div>
        </div>
      </section>
      <section id="tab-comfy" class="stack hidden">
        <div class="page-hero">
          <div class="row"><span class="pill">ComfyUI</span><span class="pill">模型族</span><span class="pill">Anima 图生图</span><span class="pill">头像重绘</span></div>
          <h3 style="margin-top:12px">ComfyUI</h3>
          <p class="muted">基础连接、模型族、Anima 图生图和头像重绘统一在这里改。左侧切分区，右侧只放当前分区表单，避免横向挤成一排。</p>
        </div>
        <div class="comfy-shell">
          <div class="surface comfy-sidebar">
            <div class="panel-title">设置分区</div>
            <div class="comfy-nav">
              <button class="comfy-nav-btn active" data-comfy-page="overview" onclick="setComfyPage('overview')">概览</button>
              <button class="comfy-nav-btn" data-comfy-page="basic" onclick="setComfyPage('basic')">基础连接</button>
              <button class="comfy-nav-btn" data-comfy-page="models" onclick="setComfyPage('models')">模型族</button>
              <button class="comfy-nav-btn" data-comfy-page="img2img" onclick="setComfyPage('img2img')">图生图</button>
              <button class="comfy-nav-btn" data-comfy-page="avatar" onclick="setComfyPage('avatar')">头像重绘</button>
            </div>
          </div>
          <div class="comfy-main">
            <div id="comfy-page-overview" class="comfy-page">
              <div class="section-grid">
                <div class="surface card">
                  <h3>运行状态</h3>
                  <div id="comfyStatus" class="kv-list"></div>
                </div>
                <div class="surface card">
                  <h3>模型族</h3>
                  <div id="modelState" class="kv-list"></div>
                  <div class="row" style="margin-top:12px">
                    <button class="btn primary" onclick="switchModelFamily('anima')">切换 Anima</button>
                    <button class="btn primary" onclick="switchModelFamily('illustrious')">切换 Illustrious</button>
                    <button class="btn" onclick="loadModelManager(true)">刷新状态</button>
                  </div>
                </div>
                <div class="surface card">
                  <h3>Illustrious Checkpoint</h3>
                  <div id="illustriousState" class="kv-list"></div>
                  <div class="row" style="margin-top:12px">
                    <button class="btn primary" onclick="downloadIllustrious()">一键下载</button>
                  </div>
                </div>
                <div class="surface card help">
                  <h3>要点</h3>
                  <p>Anima 角色替换推荐使用参考图提供结构，目标角色 LoRA 和提示词负责重画身份。</p>
                  <p>切到 Illustrious 后，普通 `酒狐来点` 文生图会使用 Illustrious 工作流；引用/附带图片时走独立图生图工作流。</p>
                  <p>`/jh 找lora` 会按当前模型族过滤 Civitai。</p>
                </div>
              </div>
            </div>
            <div id="comfy-page-basic" class="comfy-page hidden">
              <div class="surface card">
                <h3>基础连接</h3>
                <div class="settings-grid">
                  <div class="wide"><label>ComfyUI 地址</label><input id="comfyServerAddress" class="input" placeholder="127.0.0.1:8188"></div>
                  <div><label>默认工作流文件</label><input id="comfyWorkflowFile" class="input" placeholder="finish_optimized_api.json"></div>
                  <div><label>正面节点 ID</label><input id="comfyInputNode" class="input"></div>
                  <div><label>负面节点 ID</label><input id="comfyNegNode" class="input"></div>
                  <div><label>输出节点 ID</label><input id="comfyOutputNode" class="input"></div>
                </div>
              </div>
              <div class="settings-actions"><button class="btn primary" onclick="saveComfySettings()">保存 ComfyUI 配置</button><span id="comfySaveMsg" class="muted"></span></div>
            </div>
            <div id="comfy-page-models" class="comfy-page hidden">
              <div class="section-grid">
                <div class="surface card">
                  <h3>模型族状态</h3>
                  <div id="modelStateCopy" class="kv-list"></div>
                </div>
                <div class="surface card">
                  <h3>Checkpoint 下载设置</h3>
                  <div class="settings-grid">
                    <div class="wide"><label>checkpoints 目录</label><input id="modelCheckpointDir" class="input" placeholder="F:/ComfyUI/models/checkpoints"></div>
                    <div class="wide"><label>下载代理</label><input id="modelDownloadProxy" class="input" placeholder="http://127.0.0.1:7899"></div>
                    <div><label>下载超时秒数</label><input id="modelDownloadTimeout" class="input" type="number" value="3600"></div>
                  </div>
                  <div class="row" style="margin-top:12px">
                    <button class="btn primary" onclick="saveModelDownloadSettings()">保存下载设置</button>
                    <button class="btn" onclick="downloadIllustrious()">下载 Illustrious</button>
                    <span id="modelSaveMsg" class="muted"></span>
                  </div>
                </div>
              </div>
            </div>
            <div id="comfy-page-img2img" class="comfy-page hidden">
              <div class="surface card">
                <h3>Anima 图生图 / 角色替换</h3>
                <div class="settings-grid">
                  <label class="check-line"><input id="img2imgEnable" type="checkbox"> 启用自动图生图</label>
                  <label class="check-line"><input id="characterReplaceEnable" type="checkbox"> 启用角色替换优化</label>
                  <label class="check-line"><input id="autoCharacterReplaceWorkflow" type="checkbox"> 角色替换时使用专用工作流</label>
                  <div><label>普通图生图重绘幅度</label><input id="img2imgDenoise" class="input" type="number" step="0.01"></div>
                  <div class="wide"><label>角色替换工作流</label><input id="characterReplaceWorkflow" class="input" placeholder="character_replace_anima_reference_api.json"></div>
                  <div><label>正面节点 ID</label><input id="characterReplaceInputNode" class="input"></div>
                  <div><label>负面节点 ID</label><input id="characterReplaceNegNode" class="input"></div>
                  <div><label>输出节点 ID</label><input id="characterReplaceOutputNode" class="input"></div>
                  <div><label>LoRA 节点 ID</label><input id="characterReplaceLoraNode" class="input"></div>
                  <div><label>角色替换重绘幅度</label><input id="characterReplaceDenoise" class="input" type="number" step="0.01"></div>
                  <div><label>LoRA model 强度</label><input id="characterReplaceLoraModel" class="input" type="number" step="0.01"></div>
                  <div><label>LoRA clip 强度</label><input id="characterReplaceLoraClip" class="input" type="number" step="0.01"></div>
                  <div><label>LLLite / Control 强度</label><input id="characterReplaceControlStrength" class="input" type="number" step="0.01"></div>
                  <div><label>控制结束比例</label><input id="characterReplaceControlEnd" class="input" type="number" step="0.01"></div>
                  <div><label>预处理器</label><input id="characterReplacePreprocessor" class="input" placeholder="DepthAnythingV2Preprocessor"></div>
                  <div><label>预处理分辨率</label><input id="characterReplacePreprocessorResolution" class="input" type="number"></div>
                  <div><label>源图长边缩放</label><input id="characterReplaceScaleToLength" class="input" type="number"></div>
                  <div class="wide"><label>角色替换提示词模板</label><textarea id="characterReplacePrompt" class="textarea"></textarea></div>
                  <div class="wide"><label>角色替换额外负面词</label><textarea id="characterReplaceNegative" class="textarea"></textarea></div>
                </div>
              </div>
              <div class="settings-actions"><button class="btn primary" onclick="saveComfySettings()">保存 ComfyUI 配置</button><span class="muted" data-comfy-save-mirror></span></div>
            </div>
            <div id="comfy-page-avatar" class="comfy-page hidden">
              <div class="surface card">
                <h3>@群友头像重绘</h3>
                <div class="settings-grid">
                  <label class="check-line"><input id="avatarRedrawEnable" type="checkbox"> 启用头像重绘</label>
                  <div><label>下载超时秒数</label><input id="avatarRedrawTimeout" class="input" type="number"></div>
                  <div class="wide"><label>头像重绘提示词</label><textarea id="avatarRedrawPrompt" class="textarea"></textarea></div>
                  <div class="wide"><label>头像 URL 模板</label><input id="avatarRedrawUrlTemplate" class="input" placeholder="https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"></div>
                </div>
              </div>
              <div class="settings-actions"><button class="btn primary" onclick="saveComfySettings()">保存 ComfyUI 配置</button><span class="muted" data-comfy-save-mirror></span></div>
            </div>
          </div>
        </div>
      </section>
      <section id="tab-help" class="stack hidden"><div class="card help">
        <h3>聊天指令</h3>
        <p><b>酒狐来点 提示词</b>：ComfyUI 常规画图，会走当前模型族和工作流。</p>
        <p><b>酒狐来点 @群友</b>：读取被 @ 用户头像做头像重绘，后台可在 ComfyUI / 头像重绘里改提示词和头像 URL 模板。</p>
        <p><b>酒狐收藏</b>：引用或附带图片收藏到图库；WebUI 收藏图库会按角色目录分组展示。</p>
        <p><b>酒狐记下来</b>：引用文字或直接附带提示词保存成本本模板；<b>酒狐看本本 [标题/触发词]</b> 可查看或直接调用已保存模板继续画图。</p>
        <p><b>酒狐排行 [今日|昨日|总榜]</b>：发送渲染图片排行榜，头像优先从 NapCat/OneBot 获取。</p>
        <p><b>酒狐看看</b>：引用/附带图片解析图片内容并生成提示。</p>
        <p><b>WebUI / 姿态控制</b>：拖动 3D 单人骨架和相机，可生成 OpenPose 控制图并直接提交 ComfyUI 出图，也可保存本地预设。</p>
        <p><b>酒狐魔法 提示词</b> / <b>酒狐魔法+提示词</b>：GPT 生图，提示词原样传入接口。</p>
        <p><b>引用/附带一张或多张图片 + 酒狐魔法 要求</b>：GPT 图生图/多图编辑，源图和要求一起传入 edits 接口。</p>
        <p><b>引用/附带图片 + 酒狐魔法改图 要求</b> / <b>酒狐魔法图生图 要求</b>：同样走 edits，只是更直观的中文写法；同消息多图会一起传入。</p>
        <p><b>酒狐魔法重画 提示词</b>：绕过缓存，强制重新生成。</p>
        <p><b>酒狐魔法注册</b>：注册并领取默认额度。<b>酒狐魔法余额</b>：查看额度。<b>酒狐魔法兑换 卡密</b>：兑换额度。</p>
        <p><b>/jh magic ...</b>：英文兼容入口，支持 help / balance / redeem。</p>
      </div></section>
    </main>
  </div>
  <div id="imageModal" class="modal hidden" onclick="modalBackdrop(event)">
    <div class="modal-panel" role="dialog" aria-modal="true" aria-label="图片预览">
      <div class="modal-bar">
        <div>
          <b id="modalTitle">图片预览</b>
          <div id="modalMeta" class="modal-meta muted"></div>
        </div>
        <button class="btn" onclick="hideImagePreview()">关闭</button>
      </div>
      <div class="modal-img-wrap"><img id="modalImg" alt="历史图片预览"></div>
    </div>
  </div>
<script type="importmap">
{"imports":{"three":"/assets/vendor/three/three.module.js","three/addons/":"/assets/vendor/three/"}}
</script>
<script>
const S={token:localStorage.getItem('jhm_token')||'',data:null,tab:'dashboard',modelManager:null,previewHistory:[],previewFavorites:[],favoriteRole:'all',comfyPage:'overview',pose:{initialized:false,loading:false,presets:[],modules:null,state:null,jointMeshes:{},boneLines:[],selected:null,defaultLengths:{}}};
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmt=t=>t?new Date(Number(t)).toLocaleString():'-';
const withToken=url=>url+(url.includes('?')?'&':'?')+'token='+encodeURIComponent(S.token);
async function api(url,method='GET',body){const r=await fetch(url,{method,headers:{'content-type':'application/json','x-gpt-magic-token':S.token},body:body?JSON.stringify(body):undefined});const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.error||'请求失败');return d}
async function login(){const token=$('token').value.trim();const r=await fetch('/api/login',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({token})});if(!r.ok){$('loginMsg').textContent='令牌错误';return}S.token=token;localStorage.setItem('jhm_token',token);$('login').classList.add('hidden');$('app').classList.remove('hidden');await refresh()}
function logout(){localStorage.removeItem('jhm_token');location.reload()}
async function refresh(toast=false){S.data=await api('/api/state');await loadModelManager(false);renderAll();if(toast)alert('已刷新')}
function showTab(tab){S.tab=tab;document.querySelectorAll('.nav button').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab));document.querySelectorAll('main section').forEach(s=>s.classList.add('hidden'));$('tab-'+tab).classList.remove('hidden');const nav=document.querySelector(`[data-tab="${tab}"]`);$('title').textContent=nav?nav.textContent:tab;const desc={dashboard:'查看运行数据和最近趋势。',gpt:'配置 GPT 魔法 OpenAI 兼容图片接口。',users:'管理用户额度、备注和黑名单。',keys:'生成和删除兑换卡密。',queue:'查看当前绘图队列。',history:'查看 GPT 魔法历史图片。',favorites:'按角色目录查看收藏图库。',pose:'拖动 3D 骨架生成姿态与镜头提示词。',comfy:'管理 ComfyUI 连接、模型族和图生图配置。',help:'查看常用聊天指令。'};$('desc').textContent=desc[tab]||'';if(tab==='comfy')setComfyPage(S.comfyPage||'overview');if(tab==='pose')initPoseEditor();}
document.querySelectorAll('.nav button').forEach(b=>b.onclick=()=>showTab(b.dataset.tab));
function renderAll(){renderDash();fillConfig();renderUsers();renderKeys();renderQueue();renderHistory();renderFavorites();renderModelManager();fillComfySettings();renderComfy();setComfyPage(S.comfyPage||'overview')}
function renderDash(){
  const d=S.data,c=d.config||{},st=c.stats||{},users=Object.keys(d.users||{}).length,q=d.queue||{},dr=c.draw||{};
  $('kpis').innerHTML=[['用户数',users],['成功出图',st.total_images||0],['失败',st.total_failed||0],['缓存命中',st.total_cache_hit||0],['运行中',`${q.running_count||0}/${q.max_concurrent_tasks||1}`],['等待中',q.pending_count||0]].map(x=>`<div class="card kpi"><div class="muted">${x[0]}</div><div class="value">${x[1]}</div></div>`).join('');
  $('summary').innerHTML=`模型：${esc(dr.model||'-')} / 2K ${esc(dr.model_2k||'-')} / 4K ${esc(dr.model_4k||'-')}<br>接口：${esc(dr.api_url||dr.base_url||'-')}<br>默认：${esc((dr.default_resolution||'4k').toUpperCase())} ${esc(dr.quality||'high')} ${esc(dr.output_format||'png')}<br>默认额度：${esc(c.default_quota)}<br>缓存：${c.cache?.enabled?'开启':'关闭'}<br>WebUI：${esc(d.webui_runtime?.host)}:${esc(d.webui_runtime?.port)}`;
  $('trend').innerHTML=(d.analytics?.recent7d||[]).map(i=>`${esc(i.label)}：${esc(i.count)}`).join('<br>');
}
function fillConfig(){
  const c=S.data.config||{},w=c.webui||{},dr=c.draw||{},ca=c.cache||{};
  $('enabled').checked=c.enabled!==false;$('drawEnabled').checked=c.draw_enabled!==false;$('resultImageInForward').checked=c.result_image_in_forward!==false;$('sendTaskReceived').checked=c.send_task_received_message!==false;$('sendProcessing').checked=c.send_processing_message!==false;
  $('closeReason').value=c.draw_close_reason||'';$('taskReceivedMessage').value=c.task_received_message||'🪄 已收到 GPT 魔法任务，当前位置：{position}';$('processingMessage').value=c.processing_message||'🪄 GPT 魔法正在生成图片，请稍等...';$('editProcessingMessage').value=c.edit_processing_message||'🪄 GPT 魔法正在编辑图片，请稍等...';
  $('webHost').value=w.host||'0.0.0.0';$('webPort').value=w.port||1144;$('webToken').value=w.token||'';$('defaultQuota').value=c.default_quota??2;$('maxQueue').value=c.max_queue_size??100;$('maxConcurrentTasks').value=c.max_concurrent_tasks??2;$('historyLimit').value=c.history_limit??300;
  $('model').value=dr.model||'gpt-image-2';$('model2k').value=dr.model_2k||'gpt-image-2-2k';$('model4k').value=dr.model_4k||'gpt-image-2-4k';$('autoModelByResolution').checked=dr.auto_model_by_resolution!==false;
  $('timeout').value=dr.timeout_sec||600;$('baseUrl').value=dr.base_url||'';$('apiUrl').value=dr.api_url||'';$('editApiUrl').value=dr.edit_api_url||'';$('apiKey').value=dr.api_key||'';
  $('quality').value=dr.quality||'high';$('background').value=dr.background||'auto';$('moderation').value=dr.moderation||'auto';$('format').value=dr.output_format||'png';$('compression').value=dr.output_compression??100;$('stream').checked=dr.stream!==false;
  $('defaultResolution').value=dr.default_resolution||'4k';$('aspect').value=dr.aspect_strategy||'auto';$('aspectDetect').checked=dr.enable_prompt_aspect_detect!==false;$('customSizeOn').checked=dr.enable_custom_size===true;$('customSize').value=dr.custom_size||'';$('inputFidelity').value=dr.input_fidelity??'high';
  $('extraBody').value=JSON.stringify(dr.extra_body||{},null,2);$('cacheEnabled').checked=ca.enabled!==false;$('cacheTtl').value=ca.ttl_sec??604800;$('cacheMax').value=ca.max_entries??500;$('cacheConsume').checked=ca.consume_quota_on_hit!==false;
}
async function saveConfig(){
  try{
    await api('/api/config','POST',{
      enabled:$('enabled').checked,draw_enabled:$('drawEnabled').checked,result_image_in_forward:$('resultImageInForward').checked,send_task_received_message:$('sendTaskReceived').checked,task_received_message:$('taskReceivedMessage').value,send_processing_message:$('sendProcessing').checked,processing_message:$('processingMessage').value,edit_processing_message:$('editProcessingMessage').value,draw_close_reason:$('closeReason').value,default_quota:Number($('defaultQuota').value||2),max_queue_size:Number($('maxQueue').value||100),max_concurrent_tasks:Number($('maxConcurrentTasks').value||2),history_limit:Number($('historyLimit').value||300),
      webui:{host:$('webHost').value,port:Number($('webPort').value||1144),token:$('webToken').value},
      draw:{model:$('model').value,model_2k:$('model2k').value,model_4k:$('model4k').value,auto_model_by_resolution:$('autoModelByResolution').checked,timeout_sec:Number($('timeout').value||600),base_url:$('baseUrl').value,api_url:$('apiUrl').value,edit_api_url:$('editApiUrl').value,api_key:$('apiKey').value,quality:$('quality').value||'high',background:$('background').value||'auto',moderation:$('moderation').value||'auto',output_format:$('format').value||'png',output_compression:Number($('compression').value||100),stream:$('stream').checked,default_resolution:$('defaultResolution').value||'4k',aspect_strategy:$('aspect').value,enable_prompt_aspect_detect:$('aspectDetect').checked,enable_custom_size:$('customSizeOn').checked,custom_size:$('customSize').value,input_fidelity:$('inputFidelity').value,extra_body:$('extraBody').value},
      cache:{enabled:$('cacheEnabled').checked,ttl_sec:Number($('cacheTtl').value||0),max_entries:Number($('cacheMax').value||500),consume_quota_on_hit:$('cacheConsume').checked}
    });
    $('saveMsg').textContent='已保存；Host/端口变更需重启生效';await refresh();
  }catch(e){$('saveMsg').textContent=e.message}
}
function renderUsers(){const kw=($('userSearch')?.value||'').trim();const arr=Object.values(S.data.users||{}).filter(u=>!kw||String(u.user_id).includes(kw)||String(u.nickname||'').includes(kw));$('users').innerHTML=arr.map(u=>`<tr><td>${esc(u.user_id)}</td><td>${esc(u.nickname||'-')}</td><td>${esc(u.quota)}</td><td>${esc(u.total_success||0)} / ${esc(u.total_failed||0)}</td><td>${u.blacklisted?'<span class="tag danger">是</span>':'<span class="tag ok">否</span>'}</td><td><input class="input" id="remark-${esc(u.user_id)}" value="${esc(u.remark||'')}"></td><td class="row"><button class="btn" onclick="quota('${esc(u.user_id)}',1)">+1</button><button class="btn" onclick="quota('${esc(u.user_id)}',5)">+5</button><button class="btn" onclick="quota('${esc(u.user_id)}',-1)">-1</button><button class="btn danger" onclick="black('${esc(u.user_id)}',${u.blacklisted?'false':'true'})">${u.blacklisted?'解除':'拉黑'}</button><button class="btn" onclick="remark('${esc(u.user_id)}')">备注</button></td></tr>`).join('')||'<tr><td colspan="7" class="muted">暂无用户</td></tr>'}
async function quota(id,n){await api('/api/users/quota','POST',{user_id:id,amount:n});await refresh()}
async function black(id,v){await api('/api/users/update','POST',{user_id:id,blacklisted:v});await refresh()}
async function remark(id){await api('/api/users/update','POST',{user_id:id,remark:$('remark-'+id).value});await refresh()}
function renderKeys(){$('keys').innerHTML=Object.values(S.data.keys||{}).map(k=>`<tr><td>${esc(k.key)}</td><td>${esc(k.amount)}</td><td>${esc(k.note||'-')}</td><td>${fmt(k.created_at)}</td><td><button class="btn danger" onclick="delKey('${esc(k.key)}')">删除</button></td></tr>`).join('')||'<tr><td colspan="5" class="muted">暂无卡密</td></tr>'}
async function createKeys(){const r=await api('/api/keys/create','POST',{amount:Number($('keyAmount').value||1),count:Number($('keyCount').value||1),note:$('keyNote').value});$('keyResult').textContent=r.keys.map(k=>k.key).join('\n');await refresh()}
async function delKey(k){if(confirm('确认删除卡密？')){await api('/api/keys/delete','POST',{key:k});await refresh()}}
function renderQueue(){const q=S.data.queue||{},active=q.active||[],pending=q.pending||[];$('queueCards').innerHTML=[['运行中',`${q.running_count||active.length}/${q.max_concurrent_tasks||1}`],['等待数量',q.pending_count||pending.length],['当前用户',active.map(i=>i.user_id).filter(Boolean).join(', ')||'-']].map(x=>`<div class="card kpi"><div class="muted">${x[0]}</div><div class="value">${esc(x[1])}</div></div>`).join('');const rows=[...active.map((i,n)=>({...i,index:'运行'+(n+1)})),...pending.map(i=>({...i,index:i.index}))];$('queue').innerHTML=rows.map(i=>`<tr><td>${esc(i.index)}</td><td>${esc(i.user_id)}</td><td>${esc(i.prompt)}</td><td>${fmt(i.started_at||i.created_at)}</td></tr>`).join('')||'<tr><td colspan="4" class="muted">没有排队任务</td></tr>'}
function gptParamLine(h){
  const bits=[];
  if(h.resolution)bits.push(String(h.resolution).toUpperCase());
  if(h.ratio)bits.push(h.ratio);
  if(h.quality)bits.push('quality='+h.quality);
  if(h.output_format)bits.push('format='+h.output_format);
  return bits.join(' / ');
}
const POSE_JOINTS=[
  ['nose','Nose'],['neck','Neck'],['r_shoulder','Right Shoulder'],['r_elbow','Right Elbow'],['r_wrist','Right Wrist'],['l_shoulder','Left Shoulder'],['l_elbow','Left Elbow'],['l_wrist','Left Wrist'],['r_hip','Right Hip'],['r_knee','Right Knee'],['r_ankle','Right Ankle'],['l_hip','Left Hip'],['l_knee','Left Knee'],['l_ankle','Left Ankle'],['r_eye','Right Eye'],['l_eye','Left Eye'],['r_ear','Right Ear'],['l_ear','Left Ear']
];
const POSE_BONES=[[0,1],[1,2],[2,3],[3,4],[1,5],[5,6],[6,7],[1,8],[8,9],[9,10],[1,11],[11,12],[12,13],[0,14],[14,16],[0,15],[15,17],[2,5],[8,11]];
const DEFAULT_POSE_POINTS=[
  [0,1.68,0],[0,1.48,0],[-.27,1.42,0],[-.52,1.08,.02],[-.62,.76,.04],[.27,1.42,0],[.52,1.08,.02],[.62,.76,.04],[-.18,.92,0],[-.2,.48,.02],[-.2,.05,.04],[.18,.92,0],[.2,.48,.02],[.2,.05,.04],[-.06,1.72,.03],[.06,1.72,.03],[-.13,1.68,.02],[.13,1.68,.02]
];
function newPoseState(){
  return {
    version:1,
    name:'',
    canvas:{width:896,height:1152},
    camera:{yaw:0,pitch:10,roll:0,distance:4.2,shot:'full_body',framing:'centered'},
    joints:POSE_JOINTS.map((j,i)=>({id:j[0],name:j[1],x:DEFAULT_POSE_POINTS[i][0],y:DEFAULT_POSE_POINTS[i][1],z:DEFAULT_POSE_POINTS[i][2],visible:true})),
    options:{lock_bone_length:true,style:'anime illustration pose',prompt_language:'en',facing:'auto',intensity:'natural'}
  };
}
const poseJoint=id=>S.pose.state?.joints?.[id]||{x:0,y:0,z:0};
const dist3=(a,b)=>Math.hypot((a.x||0)-(b.x||0),(a.y||0)-(b.y||0),(a.z||0)-(b.z||0));
const angleAt=(a,b,c)=>{
  const ab=[a.x-b.x,a.y-b.y,a.z-b.z], cb=[c.x-b.x,c.y-b.y,c.z-b.z];
  const dot=ab[0]*cb[0]+ab[1]*cb[1]+ab[2]*cb[2];
  const la=Math.hypot(...ab), lc=Math.hypot(...cb);
  if(!la||!lc)return 180;
  return Math.acos(Math.max(-1,Math.min(1,dot/(la*lc))))*180/Math.PI;
};
async function initPoseEditor(){
  if(S.pose.loading)return;
  if(S.pose.initialized){resizePoseRenderer();updatePosePrompt();return}
  S.pose.loading=true;
  if($('poseStatus'))$('poseStatus').textContent='正在加载 3D 控制器';
  try{
    const THREE=await import('three');
    const {OrbitControls}=await import('/assets/vendor/three/OrbitControls.js');
    const {TransformControls}=await import('/assets/vendor/three/TransformControls.js');
    S.pose.modules={THREE,OrbitControls,TransformControls};
    S.pose.state=newPoseState();
    setupPoseScene();
    await loadPosePresets();
    updatePosePrompt();
    S.pose.initialized=true;
    if($('poseStatus'))$('poseStatus').textContent='已就绪';
  }catch(e){
    if($('poseStatus'))$('poseStatus').textContent='3D 控制器加载失败：'+e.message;
  }finally{S.pose.loading=false}
}
function setupPoseScene(){
  const {THREE,OrbitControls,TransformControls}=S.pose.modules;
  const viewport=$('poseViewport');if(!viewport)return;
  viewport.querySelectorAll('canvas').forEach(x=>x.remove());
  const scene=new THREE.Scene();scene.background=new THREE.Color(0x101827);
  const camera=new THREE.PerspectiveCamera(45,1,0.1,100);
  const renderer=new THREE.WebGLRenderer({antialias:true});
  renderer.setPixelRatio(Math.min(window.devicePixelRatio||1,2));viewport.appendChild(renderer.domElement);
  scene.add(new THREE.HemisphereLight(0xffffff,0x445066,2.1));
  const grid=new THREE.GridHelper(3.2,16,0x39506d,0x24364f);grid.position.y=0;scene.add(grid);
  const axes=new THREE.AxesHelper(.8);axes.position.set(-1.25,.05,-1.25);scene.add(axes);
  const orbit=new OrbitControls(camera,renderer.domElement);orbit.target.set(0,.9,0);orbit.enableDamping=true;
  const transform=new TransformControls(camera,renderer.domElement);transform.setMode('translate');transform.setSize(.72);scene.add(transform);
  transform.addEventListener('dragging-changed',e=>{orbit.enabled=!e.value});
  transform.addEventListener('objectChange',()=>syncSelectedJointFromMesh());
  S.pose.scene=scene;S.pose.camera=camera;S.pose.renderer=renderer;S.pose.orbit=orbit;S.pose.transform=transform;
  buildPoseSkeleton();updatePoseCameraFromControls();resizePoseRenderer();
  renderer.domElement.addEventListener('pointerdown',pickPoseJoint);
  window.addEventListener('resize',resizePoseRenderer);
  const animate=()=>{if(!S.pose.renderer)return;requestAnimationFrame(animate);orbit.update();renderer.render(scene,camera)};animate();
}
function buildPoseSkeleton(){
  const {THREE}=S.pose.modules, scene=S.pose.scene;
  const matJoint=new THREE.MeshStandardMaterial({color:0x6ea8ff,roughness:.45});
  const matSelected=new THREE.MeshStandardMaterial({color:0xffd166,roughness:.35});
  S.pose.jointMaterial=matJoint;S.pose.selectedMaterial=matSelected;
  S.pose.state.joints.forEach((j,i)=>{
    const mesh=new THREE.Mesh(new THREE.SphereGeometry(i===1 ? .045 : .035,16,12),matJoint);
    mesh.position.set(j.x,j.y,j.z);mesh.userData.jointIndex=i;scene.add(mesh);S.pose.jointMeshes[i]=mesh;
  });
  const lineMat=new THREE.LineBasicMaterial({color:0xb8c7dd});
  S.pose.boneLines=POSE_BONES.map(pair=>{const geo=new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(),new THREE.Vector3()]);const line=new THREE.Line(geo,lineMat);line.userData.pair=pair;scene.add(line);return line});
  for(const [a,b] of POSE_BONES){S.pose.defaultLengths[`${a}-${b}`]=dist3(S.pose.state.joints[a],S.pose.state.joints[b])}
  updatePoseMeshes();
}
function resizePoseRenderer(){
  const viewport=$('poseViewport'), r=S.pose.renderer, camera=S.pose.camera;if(!viewport||!r||!camera)return;
  const rect=viewport.getBoundingClientRect();const w=Math.max(320,Math.floor(rect.width)),h=Math.max(420,Math.floor(rect.height||560));
  r.setSize(w,h,false);camera.aspect=w/h;camera.updateProjectionMatrix();
}
function updatePoseMeshes(){
  const {THREE}=S.pose.modules;if(!THREE)return;
  S.pose.state.joints.forEach((j,i)=>{const m=S.pose.jointMeshes[i];if(m)m.position.set(j.x,j.y,j.z)});
  S.pose.boneLines.forEach(line=>{const [a,b]=line.userData.pair,ja=poseJoint(a),jb=poseJoint(b);line.geometry.setFromPoints([new THREE.Vector3(ja.x,ja.y,ja.z),new THREE.Vector3(jb.x,jb.y,jb.z)]);line.geometry.attributes.position.needsUpdate=true});
}
function pickPoseJoint(event){
  const {THREE}=S.pose.modules;if(!THREE)return;
  const rect=S.pose.renderer.domElement.getBoundingClientRect();
  const pointer=new THREE.Vector2((event.clientX-rect.left)/rect.width*2-1,-((event.clientY-rect.top)/rect.height)*2+1);
  const raycaster=new THREE.Raycaster();raycaster.setFromCamera(pointer,S.pose.camera);
  const hits=raycaster.intersectObjects(Object.values(S.pose.jointMeshes));if(!hits.length)return;
  selectPoseJoint(hits[0].object.userData.jointIndex);
}
function selectPoseJoint(index){
  Object.values(S.pose.jointMeshes).forEach(m=>m.material=S.pose.jointMaterial);
  const mesh=S.pose.jointMeshes[index];if(!mesh)return;
  mesh.material=S.pose.selectedMaterial;S.pose.selected=index;S.pose.transform.attach(mesh);
  if($('poseStatus'))$('poseStatus').textContent='选中：'+POSE_JOINTS[index][1];
}
function syncSelectedJointFromMesh(){
  const i=S.pose.selected;if(i===null||i===undefined)return;
  const mesh=S.pose.jointMeshes[i], joint=S.pose.state.joints[i];if(!mesh||!joint)return;
  joint.x=Number(mesh.position.x.toFixed(4));joint.y=Number(mesh.position.y.toFixed(4));joint.z=Number(mesh.position.z.toFixed(4));
  if(S.pose.state.options.lock_bone_length)enforcePoseBoneLength(i);
  updatePoseMeshes();updatePosePrompt();
}
function enforcePoseBoneLength(index){
  const mesh=S.pose.jointMeshes[index], joint=S.pose.state.joints[index];if(!mesh||!joint)return;
  const links=POSE_BONES.filter(([a,b])=>a===index||b===index);if(!links.length)return;
  const [a,b]=links[0], anchorIndex=a===index?b:a, anchor=S.pose.state.joints[anchorIndex], key=S.pose.defaultLengths[`${a}-${b}`]||S.pose.defaultLengths[`${b}-${a}`];
  if(!anchor||!key)return;
  const dx=joint.x-anchor.x,dy=joint.y-anchor.y,dz=joint.z-anchor.z,len=Math.hypot(dx,dy,dz)||1;
  joint.x=anchor.x+dx/len*key;joint.y=anchor.y+dy/len*key;joint.z=anchor.z+dz/len*key;mesh.position.set(joint.x,joint.y,joint.z);
}
function resetPoseState(){
  if(!S.pose.modules)return;
  S.pose.state=newPoseState();syncPoseControlsFromState();updatePoseMeshes();updatePoseCameraFromControls();updatePosePrompt();if($('posePresetName'))$('posePresetName').value='';
}
function syncPoseOptions(){if(!S.pose.state)return;S.pose.state.options.lock_bone_length=!!$('poseLockBones')?.checked;updatePosePrompt()}
function syncPoseControlsFromState(){
  const c=S.pose.state.camera,o=S.pose.state.options;
  if($('poseYaw'))$('poseYaw').value=c.yaw;if($('posePitch'))$('posePitch').value=c.pitch;if($('poseRoll'))$('poseRoll').value=c.roll;if($('poseDistance'))$('poseDistance').value=c.distance;
  if($('poseShot'))$('poseShot').value=c.shot;if($('poseFraming'))$('poseFraming').value=c.framing;if($('poseFacing'))$('poseFacing').value=o.facing||'auto';if($('poseIntensity'))$('poseIntensity').value=o.intensity||'natural';if($('poseLockBones'))$('poseLockBones').checked=o.lock_bone_length!==false;
}
function updatePoseCameraFromControls(){
  if(!S.pose.state)return;
  const c=S.pose.state.camera;
  c.yaw=Number($('poseYaw')?.value||0);c.pitch=Number($('posePitch')?.value||10);c.roll=Number($('poseRoll')?.value||0);c.distance=Number($('poseDistance')?.value||4.2);c.shot=$('poseShot')?.value||c.shot;c.framing=$('poseFraming')?.value||c.framing;
  S.pose.state.options.facing=$('poseFacing')?.value||'auto';S.pose.state.options.intensity=$('poseIntensity')?.value||'natural';
  if($('poseYawValue'))$('poseYawValue').textContent=c.yaw;if($('posePitchValue'))$('posePitchValue').textContent=c.pitch;
  if(S.pose.camera){const yaw=c.yaw*Math.PI/180,pitch=c.pitch*Math.PI/180,d=Math.max(1.8,c.distance);S.pose.camera.position.set(Math.sin(yaw)*Math.cos(pitch)*d,.95+Math.sin(pitch)*d,Math.cos(yaw)*Math.cos(pitch)*d);S.pose.camera.rotation.z=c.roll*Math.PI/180;S.pose.camera.lookAt(0,.9,0)}
  updatePosePrompt();
}
function generatePosePrompt(state=S.pose.state){
  if(!state)return '';
  const j=state.joints,c=state.camera,o=state.options||{},tags=['single person','anime illustration pose','complete body visible','clear readable body posture'];
  const neck=j[1],nose=j[0],rh=j[8],lh=j[11],rw=j[4],lw=j[7],rs=j[2],ls=j[5],rk=j[9],lk=j[12],ra=j[10],la=j[13];
  const hip={x:(rh.x+lh.x)/2,y:(rh.y+lh.y)/2,z:(rh.z+lh.z)/2};
  const kneeAng=(angleAt(rh,rk,ra)+angleAt(lh,lk,la))/2;
  if(ra.y>.18&&la.y>.18)tags.push('jumping pose','feet lifted from ground');
  else if(kneeAng<115)tags.push('deep crouching pose','both knees strongly bent');
  else if(kneeAng<150)tags.push('knees bent','lowered center of gravity');
  else tags.push('standing pose','balanced weight distribution');
  const armRaised=(rw.y>rs.y+.22)+(lw.y>ls.y+.22);
  if(armRaised===2)tags.push('both arms raised');
  else if(armRaised===1)tags.push(rw.y>rs.y+.22?'right arm raised':'left arm raised');
  if(Math.abs(rw.x-lh.x)<.18&&Math.abs(lw.x-rh.x)<.18&&rw.y>.9&&lw.y>.9)tags.push('arms crossed in front of chest');
  else if(Math.abs(rw.x-rh.x)<.2&&Math.abs(lw.x-lh.x)<.2&&rw.y<1.05&&lw.y<1.05)tags.push('hands near hips');
  else if(Math.abs(rw.x-rs.x)>.45||Math.abs(lw.x-ls.x)>.45)tags.push('arms extended outward');
  if(Math.abs(nose.x-neck.x)>.08)tags.push(nose.x>neck.x?'head tilted left':'head tilted right');
  if(neck.y-hip.y<.48)tags.push('torso leaning forward');
  if(Math.abs(neck.x-hip.x)>.12)tags.push(neck.x>hip.x?'torso leaning left':'torso leaning right');
  const yaw=Math.abs(Number(c.yaw)||0),pitch=Number(c.pitch)||0;
  if(o.facing&&o.facing!=='auto')tags.push(o.facing);
  else if(yaw<25)tags.push('front view');else if(yaw<70)tags.push('three-quarter view');else if(yaw<115)tags.push('side profile view');else if(yaw<160)tags.push('back three-quarter view');else tags.push('back view');
  if(pitch>22)tags.push('high angle view');else if(pitch<-12)tags.push('low angle view');else tags.push('eye level view');
  const shot={full_body:'full body shot',upper_body:'upper body shot',close_up:'close-up shot'}[c.shot]||'full body shot';tags.push(shot);
  const framing={centered:'centered composition',dynamic:'dynamic diagonal composition',side:'side composition'}[c.framing]||'centered composition';tags.push(framing);
  if(o.intensity==='dynamic')tags.push('dynamic action pose','sense of motion');else if(o.intensity==='dramatic')tags.push('dramatic action pose','strong body twist','high energy movement');else tags.push('natural relaxed movement');
  return [...new Set(tags)].join(', ');
}
function updatePosePrompt(){
  if(!S.pose.state)return;
  S.pose.state.camera.shot=$('poseShot')?.value||S.pose.state.camera.shot;S.pose.state.camera.framing=$('poseFraming')?.value||S.pose.state.camera.framing;
  S.pose.state.options.facing=$('poseFacing')?.value||'auto';S.pose.state.options.intensity=$('poseIntensity')?.value||'natural';S.pose.state.options.lock_bone_length=!!$('poseLockBones')?.checked;
  const prompt=generatePosePrompt(S.pose.state);if($('posePrompt'))$('posePrompt').value=prompt;return prompt;
}
async function copyPosePrompt(){const text=updatePosePrompt()||'';try{await navigator.clipboard.writeText(text);$('posePresetMsg').textContent='已复制'}catch(e){$('posePresetMsg').textContent='复制失败，请手动选中'}}
function renderPosePresets(){
  const box=$('posePresetList');if(!box)return;
  box.innerHTML=(S.pose.presets||[]).map(p=>{const id=JSON.stringify(String(p.id||''));return `<div class="pose-preset"><div><div class="pose-preset-name">${esc(p.name||'-')}</div><div class="pose-preset-meta">${fmt(p.updated_at||p.created_at)}</div></div><div class="row"><button class="btn" onclick="applyPosePreset(${id})">加载</button><button class="btn danger" onclick="deletePosePreset(${id})">删除</button></div></div>`}).join('')||'<div class="empty-state">暂无姿态预设</div>';
}
async function loadPosePresets(){const r=await api('/api/pose/presets');S.pose.presets=r.presets||[];renderPosePresets()}
async function savePosePreset(){
  const name=($('posePresetName')?.value||'').trim();if(!name){$('posePresetMsg').textContent='请填写名称';return}
  S.pose.state.name=name;const prompt=updatePosePrompt();
  const r=await api('/api/pose/presets','POST',{name,state:S.pose.state,prompt});S.pose.presets=r.presets||[];renderPosePresets();$('posePresetMsg').textContent='已保存';
}
function applyPosePreset(id){
  const p=(S.pose.presets||[]).find(x=>String(x.id)===String(id));if(!p||!p.state)return;
  S.pose.state=JSON.parse(JSON.stringify(p.state));syncPoseControlsFromState();updatePoseMeshes();updatePoseCameraFromControls();updatePosePrompt();if($('posePresetName'))$('posePresetName').value=p.name||'';
}
async function deletePosePreset(id){const r=await api('/api/pose/presets/'+encodeURIComponent(id),'DELETE');S.pose.presets=r.presets||[];renderPosePresets();$('posePresetMsg').textContent='已删除'}

const RIG_JOINTS=[
  ['pelvis','Pelvis','body',1.45],['spine','Spine','body',1.05],['chest','Chest','body',1.35],['neck','Neck','body',1.0],['head','Head','face',1.25],['nose','Nose','face',.78],['l_eye','Left Eye','face',.58],['r_eye','Right Eye','face',.58],['l_ear','Left Ear','face',.58],['r_ear','Right Ear','face',.58],
  ['l_shoulder','Left Shoulder','arm',1.1],['l_elbow','Left Elbow','arm',1.0],['l_wrist','Left Wrist','arm',1.05],['l_palm','Left Palm','hand',.9],['l_thumb','Left Thumb','hand',.58],['l_index','Left Index','hand',.58],['l_middle','Left Middle','hand',.58],['l_ring','Left Ring','hand',.52],['l_pinky','Left Pinky','hand',.52],
  ['r_shoulder','Right Shoulder','arm',1.1],['r_elbow','Right Elbow','arm',1.0],['r_wrist','Right Wrist','arm',1.05],['r_palm','Right Palm','hand',.9],['r_thumb','Right Thumb','hand',.58],['r_index','Right Index','hand',.58],['r_middle','Right Middle','hand',.58],['r_ring','Right Ring','hand',.52],['r_pinky','Right Pinky','hand',.52],
  ['l_hip','Left Hip','leg',1.12],['l_knee','Left Knee','leg',1.05],['l_ankle','Left Ankle','leg',1.05],['l_toe','Left Toe','foot',.65],['l_heel','Left Heel','foot',.65],
  ['r_hip','Right Hip','leg',1.12],['r_knee','Right Knee','leg',1.05],['r_ankle','Right Ankle','leg',1.05],['r_toe','Right Toe','foot',.65],['r_heel','Right Heel','foot',.65]
];
const RIG_INDEX=Object.fromEntries(RIG_JOINTS.map((j,i)=>[j[0],i]));
const RIG_DEFAULT_POINTS={
  pelvis:[0,.9,0],spine:[0,1.14,0],chest:[0,1.36,0],neck:[0,1.52,0],head:[0,1.67,0],nose:[0,1.66,.1],l_eye:[.055,1.7,.09],r_eye:[-.055,1.7,.09],l_ear:[.15,1.66,.02],r_ear:[-.15,1.66,.02],
  l_shoulder:[.32,1.43,0],l_elbow:[.54,1.13,.02],l_wrist:[.6,.83,.04],l_palm:[.62,.76,.08],l_thumb:[.7,.78,.12],l_index:[.66,.68,.13],l_middle:[.61,.66,.14],l_ring:[.56,.68,.13],l_pinky:[.52,.72,.12],
  r_shoulder:[-.32,1.43,0],r_elbow:[-.54,1.13,.02],r_wrist:[-.6,.83,.04],r_palm:[-.62,.76,.08],r_thumb:[-.7,.78,.12],r_index:[-.66,.68,.13],r_middle:[-.61,.66,.14],r_ring:[-.56,.68,.13],r_pinky:[-.52,.72,.12],
  l_hip:[.18,.9,0],l_knee:[.21,.48,.02],l_ankle:[.22,.06,.04],l_toe:[.23,.02,.18],l_heel:[.21,.03,-.1],
  r_hip:[-.18,.9,0],r_knee:[-.21,.48,.02],r_ankle:[-.22,.06,.04],r_toe:[-.23,.02,.18],r_heel:[-.21,.03,-.1]
};
const RIG_BONES=[
  ['pelvis','spine','body',.046],['spine','chest','body',.05],['chest','neck','body',.04],['neck','head','face',.035],['head','nose','face',.018],['head','l_eye','face',.014],['head','r_eye','face',.014],['head','l_ear','face',.012],['head','r_ear','face',.012],
  ['chest','l_shoulder','arm',.035],['l_shoulder','l_elbow','arm',.036],['l_elbow','l_wrist','arm',.032],['l_wrist','l_palm','hand',.022],['l_palm','l_thumb','hand',.012],['l_palm','l_index','hand',.011],['l_palm','l_middle','hand',.011],['l_palm','l_ring','hand',.01],['l_palm','l_pinky','hand',.01],
  ['chest','r_shoulder','arm',.035],['r_shoulder','r_elbow','arm',.036],['r_elbow','r_wrist','arm',.032],['r_wrist','r_palm','hand',.022],['r_palm','r_thumb','hand',.012],['r_palm','r_index','hand',.011],['r_palm','r_middle','hand',.011],['r_palm','r_ring','hand',.01],['r_palm','r_pinky','hand',.01],
  ['pelvis','l_hip','leg',.04],['l_hip','l_knee','leg',.042],['l_knee','l_ankle','leg',.038],['l_ankle','l_toe','foot',.02],['l_ankle','l_heel','foot',.018],
  ['pelvis','r_hip','leg',.04],['r_hip','r_knee','leg',.042],['r_knee','r_ankle','leg',.038],['r_ankle','r_toe','foot',.02],['r_ankle','r_heel','foot',.018]
];
const RIG_CHILDREN={
  pelvis:['spine','l_hip','r_hip'],spine:['chest'],chest:['neck','l_shoulder','r_shoulder'],neck:['head'],head:['nose','l_eye','r_eye','l_ear','r_ear'],
  l_shoulder:['l_elbow'],l_elbow:['l_wrist'],l_wrist:['l_palm'],l_palm:['l_thumb','l_index','l_middle','l_ring','l_pinky'],
  r_shoulder:['r_elbow'],r_elbow:['r_wrist'],r_wrist:['r_palm'],r_palm:['r_thumb','r_index','r_middle','r_ring','r_pinky'],
  l_hip:['l_knee'],l_knee:['l_ankle'],l_ankle:['l_toe','l_heel'],r_hip:['r_knee'],r_knee:['r_ankle'],r_ankle:['r_toe','r_heel']
};
const RIG_SIDE={
  l:{shoulder:'l_shoulder',elbow:'l_elbow',wrist:'l_wrist',palm:'l_palm',hip:'l_hip',knee:'l_knee',ankle:'l_ankle',toe:'l_toe',heel:'l_heel',fingers:['l_thumb','l_index','l_middle','l_ring','l_pinky']},
  r:{shoulder:'r_shoulder',elbow:'r_elbow',wrist:'r_wrist',palm:'r_palm',hip:'r_hip',knee:'r_knee',ankle:'r_ankle',toe:'r_toe',heel:'r_heel',fingers:['r_thumb','r_index','r_middle','r_ring','r_pinky']}
};
const COCO18_TO_RIG=['nose','neck','r_shoulder','r_elbow','r_wrist','l_shoulder','l_elbow','l_wrist','r_hip','r_knee','r_ankle','l_hip','l_knee','l_ankle','r_eye','l_eye','r_ear','l_ear'];
const RIG_COLORS={body:0xffd166,arm:0x6ea8ff,leg:0x78d68b,hand:0xff9db5,foot:0xb9a7ff,face:0xffe5a8};
function rigDefault(id){const p=RIG_DEFAULT_POINTS[id]||[0,0,0];return {x:p[0],y:p[1],z:p[2]}}
function rigGroup(id){return (RIG_JOINTS.find(j=>j[0]===id)||[])[2]||'body'}
function rigLabel(id){return (RIG_JOINTS.find(j=>j[0]===id)||[])[1]||id}
function rigClone(p){return {x:Number(p?.x||0),y:Number(p?.y||0),z:Number(p?.z||0)}}
function rigAdd(a,b){return {x:a.x+b.x,y:a.y+b.y,z:a.z+b.z}}
function rigSub(a,b){return {x:a.x-b.x,y:a.y-b.y,z:a.z-b.z}}
function rigMul(a,n){return {x:a.x*n,y:a.y*n,z:a.z*n}}
function rigLen(a){return Math.hypot(a.x,a.y,a.z)}
function rigDot(a,b){return a.x*b.x+a.y*b.y+a.z*b.z}
function rigNorm(a){const l=rigLen(a)||1;return rigMul(a,1/l)}
function rigDist(a,b){return rigLen(rigSub(a,b))}
function rigCross(a,b){return {x:a.y*b.z-a.z*b.y,y:a.z*b.x-a.x*b.z,z:a.x*b.y-a.y*b.x}}
function rigAngle(a,b,c){const ab=rigSub(a,b),cb=rigSub(c,b),la=rigLen(ab),lc=rigLen(cb);if(!la||!lc)return 180;return Math.acos(Math.max(-1,Math.min(1,rigDot(ab,cb)/(la*lc))))*180/Math.PI}
function rigStateMap(state=S.pose.state){const map={};(state?.joints||[]).forEach(j=>{if(j&&j.id)map[j.id]=j});return map}
function rigJoint(id,state=S.pose.state){const map=rigStateMap(state);return map[id]||{id,name:rigLabel(id),...rigDefault(id),visible:true,group:rigGroup(id)}}
function rigSet(id,pos,state=S.pose.state){const j=rigJoint(id,state);j.x=Number(pos.x.toFixed?pos.x.toFixed(4):pos.x);j.y=Number(pos.y.toFixed?pos.y.toFixed(4):pos.y);j.z=Number(pos.z.toFixed?pos.z.toFixed(4):pos.z)}
function rigBoneKey(a,b){return `${a}-${b}`}
function rigDescendants(id,out=[]){(RIG_CHILDREN[id]||[]).forEach(child=>{out.push(child);rigDescendants(child,out)});return out}
function rigTranslate(ids,delta){[...new Set(ids)].forEach(id=>rigSet(id,rigAdd(rigJoint(id),delta)))}
function rigClampFrom(baseId,id,maxScale=1.8){const base=rigJoint(baseId),p=rigJoint(id),key=rigBoneKey(baseId,id),baseLen=S.pose.defaultLengths?.[key]||S.pose.defaultLengths?.[rigBoneKey(id,baseId)]||rigDist(base,p);const max=baseLen*maxScale,d=rigDist(base,p);if(d>max)rigSet(id,rigAdd(base,rigMul(rigNorm(rigSub(p,base)),max)))}
function newRigPoseState(){
  return {
    version:2,name:'',canvas:{width:1024,height:1440},camera:{yaw:0,pitch:10,roll:0,distance:4.2,shot:'full_body',framing:'centered'},
    joints:RIG_JOINTS.map(([id,name,group])=>({id,name,group,...rigDefault(id),visible:true})),
    options:{lock_bone_length:true,human_limits:true,show_hands:true,show_face:true,show_feet:true,show_guides:true,style:'anime illustration pose',prompt_language:'en',facing:'auto',intensity:'natural',focus:'all',handle_scale:1}
  };
}
function normalizeRigPoseState(input){
  const base=newRigPoseState(), state=input&&typeof input==='object'?input:{};
  base.name=String(state.name||'');base.version=Number(state.version||2)||2;
  base.canvas={...base.canvas,...(state.canvas||{})};base.camera={...base.camera,...(state.camera||{})};base.options={...base.options,...(state.options||{})};
  if(Array.isArray(state.joints)){
    state.joints.forEach((j,i)=>{
      if(!j||typeof j!=='object')return;
      const id=j.id&&RIG_INDEX[j.id]!==undefined?j.id:COCO18_TO_RIG[i];
      const target=base.joints.find(x=>x.id===id);if(!target)return;
      target.x=Number(j.x ?? target.x);target.y=Number(j.y ?? target.y);target.z=Number(j.z ?? target.z);target.visible=j.visible!==false;
    });
  }
  const lh=rigJoint('l_hip',base),rh=rigJoint('r_hip',base),ls=rigJoint('l_shoulder',base),rs=rigJoint('r_shoulder',base),neck=rigJoint('neck',base);
  if(state.version<2||!state.version){
    rigSet('pelvis',{x:(lh.x+rh.x)/2,y:(lh.y+rh.y)/2,z:(lh.z+rh.z)/2},base);
    rigSet('chest',{x:(ls.x+rs.x)/2,y:(ls.y+rs.y)/2,z:(ls.z+rs.z)/2},base);
    rigSet('spine',{x:(lh.x+rh.x+ls.x+rs.x)/4,y:(lh.y+rh.y+ls.y+rs.y)/4,z:(lh.z+rh.z+ls.z+rs.z)/4},base);
    rigSet('head',{x:neck.x,y:neck.y+.15,z:neck.z},base);
  }
  return base;
}
async function initPoseEditor(){
  if(S.pose.loading)return;
  if(S.pose.initialized){resizePoseRenderer();syncPoseControlsFromState();updatePoseVisibility();updatePosePrompt();return}
  S.pose.loading=true;if($('poseStatus'))$('poseStatus').textContent='正在加载 IK 人体控制器';
  try{
    const THREE=await import('three');
    const {OrbitControls}=await import('/assets/vendor/three/OrbitControls.js');
    const {TransformControls}=await import('/assets/vendor/three/TransformControls.js');
    S.pose.modules={THREE,OrbitControls,TransformControls};S.pose.state=normalizeRigPoseState(S.pose.state||newRigPoseState());
    setupPoseScene();syncPoseControlsFromState();await loadPosePresets();updatePosePrompt();syncPoseGenerateControls();previewPoseControlImage();S.pose.initialized=true;
    if($('poseStatus'))$('poseStatus').textContent='已就绪：拖动手腕/脚踝会自动带动肘膝';
  }catch(e){if($('poseStatus'))$('poseStatus').textContent='3D 控制器加载失败：'+e.message}
  finally{S.pose.loading=false}
}
function setupPoseScene(){
  const {THREE,OrbitControls,TransformControls}=S.pose.modules,viewport=$('poseViewport');if(!viewport)return;
  viewport.querySelectorAll('canvas').forEach(x=>x.remove());
  const scene=new THREE.Scene();scene.background=new THREE.Color(0x101827);
  const camera=new THREE.PerspectiveCamera(45,1,0.1,100);
  const renderer=new THREE.WebGLRenderer({antialias:true});renderer.setPixelRatio(Math.min(window.devicePixelRatio||1,2));viewport.appendChild(renderer.domElement);
  scene.add(new THREE.HemisphereLight(0xffffff,0x253247,2.2));const key=new THREE.DirectionalLight(0xffffff,1.25);key.position.set(1.8,3.4,2.4);scene.add(key);
  const grid=new THREE.GridHelper(3.6,18,0x39506d,0x24364f);scene.add(grid);const axes=new THREE.AxesHelper(.82);axes.position.set(-1.38,.04,-1.35);scene.add(axes);
  const orbit=new OrbitControls(camera,renderer.domElement);orbit.target.set(0,.9,0);orbit.enableDamping=true;orbit.dampingFactor=.08;
  const transform=new TransformControls(camera,renderer.domElement);transform.setMode('translate');transform.setSize(.72);scene.add(transform);
  transform.addEventListener('dragging-changed',e=>{orbit.enabled=!e.value});
  transform.addEventListener('objectChange',()=>syncSelectedJointFromMesh());
  S.pose.scene=scene;S.pose.camera=camera;S.pose.renderer=renderer;S.pose.orbit=orbit;S.pose.transform=transform;S.pose.defaultLengths={};S.pose.selected=null;
  buildPoseSkeleton();updatePoseCameraFromControls();resizePoseRenderer();renderer.domElement.addEventListener('pointerdown',pickPoseJoint);window.addEventListener('resize',resizePoseRenderer);
  const animate=()=>{if(!S.pose.renderer)return;requestAnimationFrame(animate);orbit.update();renderer.render(scene,camera)};animate();
}
function buildPoseSkeleton(){
  const {THREE}=S.pose.modules,scene=S.pose.scene;S.pose.jointMeshes={};S.pose.boneMeshes=[];
  S.pose.materials={};Object.entries(RIG_COLORS).forEach(([g,c])=>S.pose.materials[g]=new THREE.MeshStandardMaterial({color:c,roughness:.42,metalness:.03}));
  S.pose.selectedMaterial=new THREE.MeshStandardMaterial({color:0xffffff,emissive:0xffb703,emissiveIntensity:.38,roughness:.25});
  RIG_JOINTS.forEach(([id,name,group,size])=>{const mesh=new THREE.Mesh(new THREE.SphereGeometry(.032*size,18,14),S.pose.materials[group]||S.pose.materials.body);mesh.position.set(rigJoint(id).x,rigJoint(id).y,rigJoint(id).z);mesh.userData={jointId:id,baseScale:size,baseMaterial:mesh.material};scene.add(mesh);S.pose.jointMeshes[id]=mesh});
  RIG_BONES.forEach(([a,b,group,radius])=>{const mesh=new THREE.Mesh(new THREE.CylinderGeometry(radius,radius,1,12),S.pose.materials[group]||S.pose.materials.body);mesh.userData={a,b,group};scene.add(mesh);S.pose.boneMeshes.push(mesh);S.pose.defaultLengths[rigBoneKey(a,b)]=rigDist(rigJoint(a),rigJoint(b))});
  updatePoseMeshes();updatePoseVisibility();
}
function resizePoseRenderer(){
  const viewport=$('poseViewport'),r=S.pose.renderer,camera=S.pose.camera;if(!viewport||!r||!camera)return;
  const rect=viewport.getBoundingClientRect(),w=Math.max(360,Math.floor(rect.width)),h=Math.max(520,Math.floor(rect.height||640));r.setSize(w,h,false);camera.aspect=w/h;camera.updateProjectionMatrix();
}
function updatePoseMeshes(){
  const {THREE}=S.pose.modules;if(!THREE||!S.pose.state)return;
  RIG_JOINTS.forEach(([id])=>{const j=rigJoint(id),m=S.pose.jointMeshes[id];if(m)m.position.set(j.x,j.y,j.z)});
  S.pose.boneMeshes.forEach(mesh=>{const a=rigJoint(mesh.userData.a),b=rigJoint(mesh.userData.b),va=new THREE.Vector3(a.x,a.y,a.z),vb=new THREE.Vector3(b.x,b.y,b.z),mid=va.clone().add(vb).multiplyScalar(.5),dir=vb.clone().sub(va),len=Math.max(.001,dir.length());mesh.position.copy(mid);mesh.scale.set(1,len,1);mesh.quaternion.setFromUnitVectors(new THREE.Vector3(0,1,0),dir.normalize())});
  updatePoseHandleScale(false);updatePoseVisibility(false);
}
function rigVisible(id,group=rigGroup(id)){
  const o=S.pose.state?.options||{},focus=$('poseFocus')?.value||o.focus||'all';
  if(group==='hand'&&!o.show_hands)return false;if(group==='face'&&!o.show_face)return false;if(group==='foot'&&!o.show_feet)return false;
  if(focus==='all')return true;if(focus==='body')return ['body','arm','leg'].includes(group);if(focus==='hands')return ['body','arm','hand'].includes(group);if(focus==='feet')return ['body','leg','foot'].includes(group);if(focus==='face')return ['body','face'].includes(group);return true;
}
function updatePoseVisibility(update=true){
  if(!S.pose.state)return;const o=S.pose.state.options;o.focus=$('poseFocus')?.value||o.focus||'all';
  Object.entries(S.pose.jointMeshes||{}).forEach(([id,m])=>m.visible=rigVisible(id));
  (S.pose.boneMeshes||[]).forEach(m=>m.visible=(m.userData.group!=='hand'||o.show_hands)&&(m.userData.group!=='face'||o.show_face)&&(m.userData.group!=='foot'||o.show_feet)&&(o.show_guides!==false)&&rigVisible(m.userData.a,rigGroup(m.userData.a))&&rigVisible(m.userData.b,rigGroup(m.userData.b)));
  if(update)updatePosePrompt();
}
function pickPoseJoint(event){
  const {THREE}=S.pose.modules;if(!THREE||!S.pose.renderer)return;
  const rect=S.pose.renderer.domElement.getBoundingClientRect(),pointer=new THREE.Vector2((event.clientX-rect.left)/rect.width*2-1,-((event.clientY-rect.top)/rect.height)*2+1),raycaster=new THREE.Raycaster();raycaster.setFromCamera(pointer,S.pose.camera);
  const hits=raycaster.intersectObjects(Object.values(S.pose.jointMeshes).filter(m=>m.visible));if(hits.length)selectPoseJoint(hits[0].object.userData.jointId);
}
function selectPoseJoint(id){
  Object.values(S.pose.jointMeshes||{}).forEach(m=>m.material=m.userData.baseMaterial);
  const mesh=S.pose.jointMeshes?.[id];if(!mesh)return;mesh.material=S.pose.selectedMaterial;S.pose.selected=id;S.pose.transform.attach(mesh);
  const j=rigJoint(id);if($('poseStatus'))$('poseStatus').textContent='选中：'+rigLabel(id);if($('poseSelected'))$('poseSelected').innerHTML=`<b>${esc(rigLabel(id))}</b><span class="muted">${esc(rigGroup(id))} / x ${j.x.toFixed(2)} · y ${j.y.toFixed(2)} · z ${j.z.toFixed(2)}</span>`;
}
function syncSelectedJointFromMesh(){
  const id=S.pose.selected,mesh=S.pose.jointMeshes?.[id];if(!id||!mesh)return;
  const old=rigClone(rigJoint(id)),next={x:mesh.position.x,y:mesh.position.y,z:mesh.position.z},delta=rigSub(next,old);applyRigEdit(id,next,delta);rigApplyHumanLimits();updatePoseMeshes();updatePosePrompt();selectPoseJoint(id);
}
function applyRigEdit(id,next,delta){
  const group=rigGroup(id),side=id.startsWith('l_')?'l':id.startsWith('r_')?'r':'';
  if(id==='pelvis'){rigTranslate(RIG_JOINTS.map(j=>j[0]),delta);return}
  if(['spine','chest','neck'].includes(id)){rigTranslate([id,...rigDescendants(id)],delta);return}
  if(id==='head'){rigTranslate(['head','nose','l_eye','r_eye','l_ear','r_ear'],delta);return}
  if(group==='face'){rigSet(id,next);return}
  if(side&&['l_wrist','r_wrist'].includes(id)){rigSolveArm(side,next);return}
  if(side&&['l_palm','r_palm'].includes(id)){const s=RIG_SIDE[side],offset=rigSub(rigJoint(s.palm),rigJoint(s.wrist));rigSolveArm(side,rigSub(next,offset));return}
  if(side&&(RIG_SIDE[side].fingers||[]).includes(id)){rigSet(id,next);rigClampFrom(RIG_SIDE[side].palm,id,2.2);return}
  if(side&&['l_elbow','r_elbow'].includes(id)){const s=RIG_SIDE[side];rigSet(s.elbow,next);rigSolveTwoBoneIK(s.shoulder,s.elbow,s.wrist,rigJoint(s.wrist),next);return}
  if(side&&['l_shoulder','r_shoulder'].includes(id)){rigTranslate([id,...rigDescendants(id)],delta);return}
  if(side&&['l_ankle','r_ankle'].includes(id)){rigSolveLeg(side,next);return}
  if(side&&['l_toe','r_toe','l_heel','r_heel'].includes(id)){rigSet(id,next);rigClampFrom(id.includes('l_')?'l_ankle':'r_ankle',id,2);return}
  if(side&&['l_knee','r_knee'].includes(id)){const s=RIG_SIDE[side];rigSet(s.knee,next);rigSolveTwoBoneIK(s.hip,s.knee,s.ankle,rigJoint(s.ankle),next);return}
  if(side&&['l_hip','r_hip'].includes(id)){rigTranslate([id,...rigDescendants(id)],delta);return}
  rigSet(id,next);
}
function rigSolveTwoBoneIK(rootId,midId,endId,target,polePos=null){
  const root=rigClone(rigJoint(rootId)),mid=rigClone(rigJoint(midId)),l1=S.pose.defaultLengths[rigBoneKey(rootId,midId)]||rigDist(root,mid),l2=S.pose.defaultLengths[rigBoneKey(midId,endId)]||rigDist(mid,rigJoint(endId));
  let toTarget=rigSub(target,root),d=rigLen(toTarget)||.001,max=l1+l2-.004,min=Math.abs(l1-l2)+.004;if(d>max){toTarget=rigMul(rigNorm(toTarget),max);d=max}else if(d<min){toTarget=rigMul(rigNorm(toTarget),min);d=min}
  const end=rigAdd(root,toTarget),axis=rigNorm(toTarget);let pole=polePos?rigSub(polePos,root):rigSub(mid,root);let poleFlat=rigSub(pole,rigMul(axis,rigDot(pole,axis)));
  if(rigLen(poleFlat)<.001){const fallback=Math.abs(axis.y)<.9?{x:0,y:1,z:0}:{x:1,y:0,z:0};poleFlat=rigCross(axis,fallback)}
  const poleDir=rigNorm(poleFlat),x=(d*d+l1*l1-l2*l2)/(2*d),h=Math.sqrt(Math.max(0,l1*l1-x*x));rigSet(midId,rigAdd(rigAdd(root,rigMul(axis,x)),rigMul(poleDir,h)));rigSet(endId,end);
}
function rigMoveWristDetails(side,delta){const s=RIG_SIDE[side];rigTranslate([s.palm,...s.fingers],delta)}
function rigMoveFootDetails(side,delta){const s=RIG_SIDE[side];rigTranslate([s.toe,s.heel],delta)}
function rigSolveArm(side,target){const s=RIG_SIDE[side],before=rigClone(rigJoint(s.wrist));if(S.pose.state?.options?.lock_bone_length===false)rigSet(s.wrist,target);else rigSolveTwoBoneIK(s.shoulder,s.elbow,s.wrist,target);rigMoveWristDetails(side,rigSub(rigJoint(s.wrist),before))}
function rigSolveLeg(side,target){const s=RIG_SIDE[side],before=rigClone(rigJoint(s.ankle));if(S.pose.state?.options?.lock_bone_length===false)rigSet(s.ankle,target);else rigSolveTwoBoneIK(s.hip,s.knee,s.ankle,target);rigMoveFootDetails(side,rigSub(rigJoint(s.ankle),before))}
function rigApplyHumanLimits(){
  const o=S.pose.state?.options||{};if(!o.human_limits)return;
  ['l_ankle','r_ankle','l_toe','r_toe','l_heel','r_heel'].forEach(id=>{const j=rigJoint(id);if(j.y<0)rigSet(id,{...j,y:0})});
  ['l','r'].forEach(side=>{const s=RIG_SIDE[side];s.fingers.forEach(id=>rigClampFrom(s.palm,id,2.15));rigClampFrom(s.ankle,s.toe,2);rigClampFrom(s.ankle,s.heel,2)});
  const neck=rigJoint('neck'),head=rigJoint('head');if(head.y<neck.y+.09)rigSet('head',{...head,y:neck.y+.09});
}
function resetPoseState(){S.pose.state=newRigPoseState();if(S.pose.transform)S.pose.transform.detach();S.pose.selected=null;syncPoseControlsFromState();updatePoseMeshes();updatePoseCameraFromControls();updatePosePrompt();if($('posePresetName'))$('posePresetName').value='';if($('poseSelected'))$('poseSelected').innerHTML='<b>未选中控制点</b><span class="muted">点击身体、手脚或脸部关键点后，可用 XYZ 轴精调。</span>'}
function syncPoseOptions(){if(!S.pose.state)return;const o=S.pose.state.options;o.lock_bone_length=!!$('poseLockBones')?.checked;o.human_limits=!!$('poseHumanLimits')?.checked;o.show_hands=!!$('poseShowHands')?.checked;o.show_face=!!$('poseShowFace')?.checked;o.show_feet=!!$('poseShowFeet')?.checked;o.show_guides=!!$('poseShowGuides')?.checked;o.focus=$('poseFocus')?.value||'all';o.handle_scale=Number($('poseHandleSize')?.value||1);updatePoseVisibility();updatePosePrompt()}
function syncPoseControlsFromState(){
  const c=S.pose.state.camera,o=S.pose.state.options;
  if($('poseYaw'))$('poseYaw').value=c.yaw;if($('posePitch'))$('posePitch').value=c.pitch;if($('poseRoll'))$('poseRoll').value=c.roll;if($('poseDistance'))$('poseDistance').value=c.distance;if($('poseShot'))$('poseShot').value=c.shot;if($('poseFraming'))$('poseFraming').value=c.framing;if($('poseFacing'))$('poseFacing').value=o.facing||'auto';if($('poseIntensity'))$('poseIntensity').value=o.intensity||'natural';
  if($('poseLockBones'))$('poseLockBones').checked=o.lock_bone_length!==false;if($('poseHumanLimits'))$('poseHumanLimits').checked=o.human_limits!==false;if($('poseShowHands'))$('poseShowHands').checked=o.show_hands!==false;if($('poseShowFace'))$('poseShowFace').checked=o.show_face!==false;if($('poseShowFeet'))$('poseShowFeet').checked=o.show_feet!==false;if($('poseShowGuides'))$('poseShowGuides').checked=o.show_guides!==false;if($('poseFocus'))$('poseFocus').value=o.focus||'all';if($('poseHandleSize'))$('poseHandleSize').value=o.handle_scale||1;updatePoseHandleScale(false);
}
function updatePoseHandleScale(update=true){const scale=Number($('poseHandleSize')?.value||S.pose.state?.options?.handle_scale||1);if(S.pose.state)S.pose.state.options.handle_scale=scale;if($('poseHandleValue'))$('poseHandleValue').textContent=scale.toFixed(1);Object.values(S.pose.jointMeshes||{}).forEach(m=>m.scale.setScalar(scale));if(update)updatePosePrompt()}
function updatePoseCameraFromControls(){
  if(!S.pose.state)return;const c=S.pose.state.camera;c.yaw=Number($('poseYaw')?.value||0);c.pitch=Number($('posePitch')?.value||10);c.roll=Number($('poseRoll')?.value||0);c.distance=Number($('poseDistance')?.value||4.2);c.shot=$('poseShot')?.value||c.shot;c.framing=$('poseFraming')?.value||c.framing;S.pose.state.options.facing=$('poseFacing')?.value||'auto';S.pose.state.options.intensity=$('poseIntensity')?.value||'natural';
  if($('poseYawValue'))$('poseYawValue').textContent=c.yaw;if($('posePitchValue'))$('posePitchValue').textContent=c.pitch;
  if(S.pose.camera){const yaw=c.yaw*Math.PI/180,pitch=c.pitch*Math.PI/180,d=Math.max(1.8,c.distance);S.pose.camera.position.set(Math.sin(yaw)*Math.cos(pitch)*d,.95+Math.sin(pitch)*d,Math.cos(yaw)*Math.cos(pitch)*d);S.pose.camera.lookAt(0,.9,0);S.pose.camera.rotateZ(c.roll*Math.PI/180)}
  updatePosePrompt();
}
function applyPoseTemplate(name){
  S.pose.state=newRigPoseState();syncPoseControlsFromState();
  if(name==='arms_up'){rigSolveArm('l',{x:.38,y:1.92,z:.05});rigSolveArm('r',{x:-.38,y:1.92,z:.05});}
  if(name==='crossed_arms'){rigSolveArm('l',{x:-.27,y:1.28,z:.18});rigSolveArm('r',{x:.27,y:1.28,z:.18});rigSet('l_elbow',{x:.44,y:1.2,z:.12});rigSet('r_elbow',{x:-.44,y:1.2,z:.12});}
  if(name==='running'){rigTranslate(['chest','neck','head','nose','l_eye','r_eye','l_ear','r_ear','l_shoulder','r_shoulder','l_elbow','r_elbow','l_wrist','r_wrist','l_palm','r_palm','l_thumb','l_index','l_middle','l_ring','l_pinky','r_thumb','r_index','r_middle','r_ring','r_pinky'],{x:0,y:.02,z:.18});rigSolveArm('l',{x:.5,y:.98,z:-.28});rigSolveArm('r',{x:-.42,y:1.58,z:.28});rigSet('l_knee',{x:.3,y:.7,z:.32});rigSet('l_ankle',{x:.35,y:.05,z:.62});rigSet('l_toe',{x:.36,y:.02,z:.78});rigSet('l_heel',{x:.32,y:.03,z:.49});rigSet('r_knee',{x:-.26,y:.42,z:-.26});rigSet('r_ankle',{x:-.42,y:.06,z:-.42});rigSet('r_toe',{x:-.45,y:.02,z:-.25});rigSet('r_heel',{x:-.38,y:.03,z:-.55});S.pose.state.options.intensity='dynamic';}
  if(name==='sitting'){rigTranslate(RIG_JOINTS.map(j=>j[0]),{x:0,y:-.2,z:0});rigSet('pelvis',{x:0,y:.68,z:0});rigSet('spine',{x:0,y:.98,z:0});rigSet('chest',{x:0,y:1.22,z:.04});rigSet('neck',{x:0,y:1.39,z:.04});rigSet('head',{x:0,y:1.54,z:.05});['l','r'].forEach(side=>{const sign=side==='l'?1:-1;rigSet(`${side}_hip`,{x:.22*sign,y:.68,z:0});rigSet(`${side}_knee`,{x:.27*sign,y:.5,z:.42});rigSet(`${side}_ankle`,{x:.28*sign,y:.05,z:.76});rigSet(`${side}_toe`,{x:.29*sign,y:.02,z:.93});rigSet(`${side}_heel`,{x:.27*sign,y:.03,z:.64})});rigSolveArm('l',{x:.45,y:.86,z:.18});rigSolveArm('r',{x:-.45,y:.86,z:.18});}
  if(name==='kneeling'){rigSet('pelvis',{x:0,y:.76,z:0});rigSet('spine',{x:0,y:1.05,z:0});rigSet('chest',{x:0,y:1.31,z:0});rigSet('l_knee',{x:.25,y:.48,z:.35});rigSet('l_ankle',{x:.28,y:.05,z:.68});rigSet('l_toe',{x:.3,y:.02,z:.86});rigSet('l_heel',{x:.26,y:.03,z:.55});rigSet('r_knee',{x:-.2,y:.05,z:-.08});rigSet('r_ankle',{x:-.34,y:.05,z:-.5});rigSet('r_toe',{x:-.34,y:.02,z:-.32});rigSet('r_heel',{x:-.33,y:.03,z:-.62});rigSolveArm('l',{x:.42,y:1.05,z:.16});rigSolveArm('r',{x:-.42,y:1.05,z:.16});}
  rigApplyHumanLimits();updatePoseMeshes();updatePoseCameraFromControls();updatePosePrompt();if($('poseStatus'))$('poseStatus').textContent='已套用动作模板：'+name;
}
function mirrorPoseState(){
  if(!S.pose.state)return;const snap=Object.fromEntries(S.pose.state.joints.map(j=>[j.id,rigClone(j)])),pairs=[['l_eye','r_eye'],['l_ear','r_ear'],['l_shoulder','r_shoulder'],['l_elbow','r_elbow'],['l_wrist','r_wrist'],['l_palm','r_palm'],['l_thumb','r_thumb'],['l_index','r_index'],['l_middle','r_middle'],['l_ring','r_ring'],['l_pinky','r_pinky'],['l_hip','r_hip'],['l_knee','r_knee'],['l_ankle','r_ankle'],['l_toe','r_toe'],['l_heel','r_heel']];
  const mir=p=>({x:-p.x,y:p.y,z:p.z});pairs.forEach(([l,r])=>{rigSet(l,mir(snap[r]));rigSet(r,mir(snap[l]))});['pelvis','spine','chest','neck','head','nose'].forEach(id=>rigSet(id,mir(snap[id])));updatePoseMeshes();updatePosePrompt();
}
function rigNear(a,b,t){return rigDist(a,b)<t}
function generatePosePrompt(state=S.pose.state){
  state=normalizeRigPoseState(state);const p=id=>rigJoint(id,state),c=state.camera||{},o=state.options||{},tags=['single person','anatomically plausible pose','clear readable body posture','anime illustration pose'];
  const pelvis=p('pelvis'),chest=p('chest'),neck=p('neck'),head=p('head'),nose=p('nose'),la=p('l_ankle'),ra=p('r_ankle'),lk=p('l_knee'),rk=p('r_knee'),lh=p('l_hip'),rh=p('r_hip');
  const kneeAvg=(rigAngle(lh,lk,la)+rigAngle(rh,rk,ra))/2,feetAir=(la.y>.16&&ra.y>.16),pelvisLow=pelvis.y<.78,oneKneeDown=lk.y<.12||rk.y<.12;
  if(feetAir)tags.push('jumping pose','both feet off ground');else if(oneKneeDown)tags.push(lk.y<.12&&rk.y<.12?'kneeling pose':'one knee on ground');else if(pelvisLow&&kneeAvg<125)tags.push('sitting pose','bent knees');else if(kneeAvg<112)tags.push('deep crouching pose','lowered center of gravity');else if(kneeAvg<150)tags.push('knees bent');else tags.push('standing pose','balanced weight');
  const stance=Math.abs(p('l_ankle').x-p('r_ankle').x);if(stance>.72)tags.push('wide stance');if(Math.abs(p('l_ankle').z-p('r_ankle').z)>.55)tags.push('staggered stance');
  if(chest.z-pelvis.z>.15)tags.push('torso leaning forward');if(chest.z-pelvis.z<-.15)tags.push('torso leaning backward');if(Math.abs(chest.x-pelvis.x)>.13)tags.push(chest.x>pelvis.x?'torso leaning left':'torso leaning right');
  if(nose.y-head.y>.04)tags.push('head tilted up');if(nose.y<head.y-.05)tags.push('head tilted down');if(Math.abs(nose.x-head.x)>.06)tags.push(nose.x>head.x?'head turned left':'head turned right');
  ['l','r'].forEach(side=>{const s=RIG_SIDE[side],label=side==='l'?'left':'right',sh=p(s.shoulder),el=p(s.elbow),wr=p(s.wrist),pa=p(s.palm),hip=p(s.hip),otherSh=p(RIG_SIDE[side==='l'?'r':'l'].shoulder),armAng=rigAngle(sh,el,wr);if(wr.y>head.y+.08)tags.push(`${label} hand above head`);else if(wr.y>sh.y+.12)tags.push(`${label} arm raised`);if(rigNear(pa,nose,.24))tags.push(`${label} hand near face`);if(rigNear(pa,otherSh,.25))tags.push(`${label} arm across chest`);if(rigNear(pa,hip,.24))tags.push(`${label} hand on hip`);if(Math.abs(wr.x-sh.x)>.46)tags.push(`${label} arm extended sideways`);if(wr.z-sh.z>.22)tags.push(`${label} arm reaching forward`);if(wr.z-sh.z<-.22)tags.push(`${label} arm behind body`);tags.push(armAng<135?`${label} elbow bent`:`${label} arm straight`);const spread=s.fingers.map(id=>rigDist(p(id),pa)).reduce((a,b)=>a+b,0)/s.fingers.length;if(spread>.13)tags.push(`${label} hand open`,`visible ${label} fingers`)});
  ['l','r'].forEach(side=>{const s=RIG_SIDE[side],label=side==='l'?'left':'right',hip=p(s.hip),knee=p(s.knee),ankle=p(s.ankle),toe=p(s.toe),legAng=rigAngle(hip,knee,ankle);if(knee.y>hip.y-.12)tags.push(`${label} knee lifted`);if(legAng<135)tags.push(`${label} leg bent`);if(toe.y>.08&&ankle.y<.12)tags.push(`${label} foot pointed`)});
  const yaw=Math.abs(Number(c.yaw)||0),pitch=Number(c.pitch)||0;if(o.facing&&o.facing!=='auto')tags.push(o.facing);else if(yaw<25)tags.push('front view');else if(yaw<70)tags.push('three-quarter view');else if(yaw<115)tags.push('side profile view');else if(yaw<160)tags.push('back three-quarter view');else tags.push('back view');
  if(pitch>22)tags.push('high angle view');else if(pitch<-12)tags.push('low angle view');else tags.push('eye level view');
  tags.push({full_body:'full body shot',upper_body:'upper body shot',close_up:'close-up shot'}[c.shot]||'full body shot');tags.push({centered:'centered composition',dynamic:'dynamic diagonal composition',side:'side composition'}[c.framing]||'centered composition');
  if(o.intensity==='dynamic')tags.push('dynamic action pose','sense of motion');else if(o.intensity==='dramatic')tags.push('dramatic action pose','strong body twist','high energy movement');else tags.push('natural relaxed movement');
  return [...new Set(tags)].join(', ');
}
function updatePosePrompt(){
  if(!S.pose.state)return;S.pose.state.camera.shot=$('poseShot')?.value||S.pose.state.camera.shot;S.pose.state.camera.framing=$('poseFraming')?.value||S.pose.state.camera.framing;S.pose.state.options.facing=$('poseFacing')?.value||'auto';S.pose.state.options.intensity=$('poseIntensity')?.value||'natural';syncPoseAutoGenerateSettings(false);const prompt=generatePosePrompt(S.pose.state);if($('posePrompt'))$('posePrompt').value=prompt;return prompt;
}
function poseAutoGenerationPreset(state=S.pose.state){
  state=normalizeRigPoseState(state);const shot=state.camera?.shot||'full_body',distance=Number(state.camera?.distance||4.2);
  let preset={width:1024,height:1440,strength:.88,end:.78,label:'full body'};
  if(shot==='upper_body')preset={width:1024,height:1280,strength:.80,end:.70,label:'upper body'};
  if(shot==='close_up')preset={width:1024,height:1024,strength:.72,end:.58,label:'close-up'};
  if(shot==='full_body'&&distance>=4.6)preset.strength=.92;
  return preset;
}
function syncPoseAutoGenerateSettings(updatePreview=false){
  if(!S.pose.state)return poseAutoGenerationPreset();
  const p=poseAutoGenerationPreset(S.pose.state);
  S.pose.state.canvas={width:p.width,height:p.height};
  if(!$('poseManualControl')?.checked){
    if($('poseControlStrength'))$('poseControlStrength').value=p.strength;
    if($('poseControlEnd'))$('poseControlEnd').value=p.end;
  }
  if($('poseAutoSummary'))$('poseAutoSummary').value=`auto ${p.label} / ${p.width}x${p.height} / cn ${Number($('poseControlStrength')?.value||p.strength).toFixed(2)}:${Number($('poseControlEnd')?.value||p.end).toFixed(2)}`;
  syncPoseGenerateControls(false);
  if(updatePreview)previewPoseControlImage();
  return p;
}
function syncPoseGenerateControls(markManual=false){
  if(markManual&&$('poseManualControl'))$('poseManualControl').checked=true;
  if(!$('poseManualControl')?.checked&&S.pose.state){
    const p=poseAutoGenerationPreset(S.pose.state);
    if($('poseControlStrength'))$('poseControlStrength').value=p.strength;
    if($('poseControlEnd'))$('poseControlEnd').value=p.end;
  }
  if($('poseControlStrengthValue'))$('poseControlStrengthValue').textContent=Number($('poseControlStrength')?.value||.88).toFixed(2);
  if($('poseControlEndValue'))$('poseControlEndValue').textContent=Number($('poseControlEnd')?.value||.78).toFixed(2);
  if($('poseAutoSummary')&&S.pose.state){const p=poseAutoGenerationPreset(S.pose.state);$('poseAutoSummary').value=`auto ${p.label} / ${p.width}x${p.height} / cn ${Number($('poseControlStrength')?.value||p.strength).toFixed(2)}:${Number($('poseControlEnd')?.value||p.end).toFixed(2)}`;}
}
function poseProjectedPoints(state=S.pose.state,width=896,height=1152){
  state=normalizeRigPoseState(state);const c=state.camera||{},center={x:0,y:.9,z:0},yaw=-(Number(c.yaw)||0)*Math.PI/180,pitch=-(Number(c.pitch)||0)*Math.PI/180,roll=(Number(c.roll)||0)*Math.PI/180;
  const cy=Math.cos(yaw),sy=Math.sin(yaw),cp=Math.cos(pitch),sp=Math.sin(pitch),cr=Math.cos(roll),sr=Math.sin(roll),raw={};
  state.joints.forEach(j=>{let x=j.x-center.x,y=j.y-center.y,z=j.z-center.z;const x1=x*cy-z*sy,z1=x*sy+z*cy,y1=y*cp-z1*sp;const x2=x1*cr-y1*sr,y2=x1*sr+y1*cr;raw[j.id]={x:x2,y:y2,z:z1}});
  const vals=Object.values(raw),minX=Math.min(...vals.map(p=>p.x)),maxX=Math.max(...vals.map(p=>p.x)),minY=Math.min(...vals.map(p=>p.y)),maxY=Math.max(...vals.map(p=>p.y));
  const shot=state.camera?.shot||'full_body',marginRatio=(shot==='full_body'?0.16:(shot==='upper_body'?0.11:0.08)),margin=Math.max(64,Math.min(width,height)*marginRatio),scale=Math.min((width-margin*2)/Math.max(.1,maxX-minX),(height-margin*2)/Math.max(.1,maxY-minY));
  const offsetX=(width-(maxX-minX)*scale)/2-minX*scale,offsetY=(height-(maxY-minY)*scale)/2+maxY*scale;
  return Object.fromEntries(Object.entries(raw).map(([id,p])=>[id,{x:offsetX+p.x*scale,y:offsetY-p.y*scale,z:p.z}]));
}
function buildPoseControlImageDataUrl(updatePreview=true){
  syncPoseAutoGenerateSettings(false);
  const state=normalizeRigPoseState(S.pose.state),w=Number(state.canvas?.width||1024),h=Number(state.canvas?.height||1440),canvas=document.createElement('canvas');canvas.width=w;canvas.height=h;
  const ctx=canvas.getContext('2d');ctx.fillStyle='#000';ctx.fillRect(0,0,w,h);ctx.lineCap='round';ctx.lineJoin='round';
  const pts=poseProjectedPoints(state,w,h),lineWidth=Math.max(4,w/180),jointRadius=Math.max(4,w/210);
  const coco18BodyLines=[
    ['neck','r_shoulder','#ff4b4b'],['r_shoulder','r_elbow','#ff8a34'],['r_elbow','r_wrist','#ffd166'],
    ['neck','l_shoulder','#43d97f'],['l_shoulder','l_elbow','#49c6ff'],['l_elbow','l_wrist','#4f7dff'],
    ['neck','r_hip','#a66cff'],['r_hip','r_knee','#d66cff'],['r_knee','r_ankle','#ff6ec7'],
    ['neck','l_hip','#68e1fd'],['l_hip','l_knee','#56d364'],['l_knee','l_ankle','#b5e853'],
    ['neck','nose','#f7fbff']
  ];
  coco18BodyLines.forEach(([a,b,color])=>{const pa=pts[a],pb=pts[b];if(!pa||!pb)return;ctx.strokeStyle=color;ctx.lineWidth=lineWidth;ctx.beginPath();ctx.moveTo(pa.x,pa.y);ctx.lineTo(pb.x,pb.y);ctx.stroke()});
  ['nose','neck','l_shoulder','l_elbow','l_wrist','r_shoulder','r_elbow','r_wrist','l_hip','l_knee','l_ankle','r_hip','r_knee','r_ankle'].forEach(id=>{const p=pts[id];if(!p)return;ctx.fillStyle='#f7fbff';ctx.beginPath();ctx.arc(p.x,p.y,jointRadius,0,Math.PI*2);ctx.fill()});
  const data=canvas.toDataURL('image/png');if(updatePreview&&$('poseControlPreview'))$('poseControlPreview').src=data;S.pose.lastControlImage=data;return data;
}
function previewPoseControlImage(){buildPoseControlImageDataUrl(true);if($('poseGenerateMsg'))$('poseGenerateMsg').textContent='控制图已刷新'}
async function generatePoseImage(){
  const prompt=($('poseDrawPrompt')?.value||'').trim();if(!prompt){$('poseGenerateMsg').textContent='请先填写画面提示词';return}
  const btn=$('poseGenerateBtn');if(btn)btn.disabled=true;$('poseGenerateMsg').textContent='正在生成控制图并提交 ComfyUI...';syncPoseGenerateControls();
  try{
    syncPoseAutoGenerateSettings(false);const poseImage=buildPoseControlImageDataUrl(true),posePrompt=updatePosePrompt()||'',manual=!!$('poseManualControl')?.checked;
    const r=await api('/api/pose/generate','POST',{prompt,pose_prompt:posePrompt,pose_image:poseImage,controlnet_name:$('poseControlNetName')?.value||'',strength:Number($('poseControlStrength')?.value||.88),end_percent:Number($('poseControlEnd')?.value||.78),manual_control:manual,include_pose_prompt:!!$('poseIncludePrompt')?.checked,free_memory_before:!!$('poseFreeMemory')?.checked,state:S.pose.state});
    if($('poseResultImage')){$('poseResultImage').src=r.image;$('poseResultImage').classList.remove('hidden')}
    if($('poseGenerateMeta')){const role=r.meta?.profile_character||'-',lora=r.meta?.profile_lora||r.meta?.lora_name||'-',size=r.meta?.generation_width&&r.meta?.generation_height?`${r.meta.generation_width}x${r.meta.generation_height}`:(r.meta?.pose_generation_preset?`${r.meta.pose_generation_preset.width}x${r.meta.pose_generation_preset.height}`:'-'),camera=r.meta?.pose_camera_prompt||'-',warn=r.meta?.lora_warning?` / 提醒：${r.meta.lora_warning}`:'';$('poseGenerateMeta').textContent=`角色：${role} / LoRA：${lora} / 尺寸：${size} / 镜头：${camera} / 工作流：${r.meta?.workflow||'-'} / ControlNet：${r.meta?.pose_controlnet_name||'-'} / 强度：${r.meta?.pose_control_strength??'-'} / 输出：${r.image_path||'-'}${warn}`;}
    $('poseGenerateMsg').textContent='生成完成';
  }catch(e){$('poseGenerateMsg').textContent=e.message||'生成失败'}
  finally{if(btn)btn.disabled=false}
}
async function copyPosePrompt(){const text=updatePosePrompt()||'';try{await navigator.clipboard.writeText(text);if($('posePresetMsg'))$('posePresetMsg').textContent='已复制'}catch(e){if($('posePresetMsg'))$('posePresetMsg').textContent='复制失败，请手动选中'}}
function renderPosePresets(){const box=$('posePresetList');if(!box)return;box.innerHTML=(S.pose.presets||[]).map(p=>{const id=JSON.stringify(String(p.id||''));return `<div class="pose-preset"><div><div class="pose-preset-name">${esc(p.name||'-')}</div><div class="pose-preset-meta">${fmt(p.updated_at||p.created_at)}</div></div><div class="row"><button class="btn" onclick="applyPosePreset(${id})">加载</button><button class="btn danger" onclick="deletePosePreset(${id})">删除</button></div></div>`}).join('')||'<div class="empty-state">暂无姿态预设</div>'}
async function loadPosePresets(){const r=await api('/api/pose/presets');S.pose.presets=r.presets||[];renderPosePresets()}
async function savePosePreset(){const name=($('posePresetName')?.value||'').trim();if(!name){$('posePresetMsg').textContent='请填写名称';return}S.pose.state.name=name;const prompt=updatePosePrompt();const r=await api('/api/pose/presets','POST',{name,state:S.pose.state,prompt});S.pose.presets=r.presets||[];renderPosePresets();$('posePresetMsg').textContent='已保存'}
function applyPosePreset(id){const p=(S.pose.presets||[]).find(x=>String(x.id)===String(id));if(!p||!p.state)return;S.pose.state=normalizeRigPoseState(JSON.parse(JSON.stringify(p.state)));syncPoseControlsFromState();updatePoseMeshes();updatePoseCameraFromControls();updatePosePrompt();if($('posePresetName'))$('posePresetName').value=p.name||''}
async function deletePosePreset(id){const r=await api('/api/pose/presets/'+encodeURIComponent(id),'DELETE');S.pose.presets=r.presets||[];renderPosePresets();$('posePresetMsg').textContent='已删除'}
const rawHtml=v=>({__html:String(v)});
function kvRows(rows){
  return rows.filter(x=>{
    if(!x||x[1]===undefined||x[1]===null)return false;
    const v=x[1];
    return String(v&&typeof v==='object'&&v.__html!==undefined?v.__html:v).trim()!=='';
  }).map(([k,v])=>{
    const html=v&&typeof v==='object'&&v.__html!==undefined?v.__html:esc(v);
    return `<div class="kv-row"><div class="kv-key">${esc(k)}</div><div class="kv-value">${html}</div></div>`;
  }).join('');
}
function setComfyPage(page){
  S.comfyPage=page||'overview';
  document.querySelectorAll('.comfy-nav-btn').forEach(b=>b.classList.toggle('active',b.dataset.comfyPage===S.comfyPage));
  document.querySelectorAll('.comfy-page').forEach(p=>p.classList.add('hidden'));
  const target=$('comfy-page-'+S.comfyPage);
  if(target)target.classList.remove('hidden');
}
function renderHistory(){
  S.previewHistory=S.data.history||[];
  $('history').innerHTML=S.previewHistory.map((h,i)=>{
    const state=h.failed?'<span class="tag danger">失败</span>':h.from_cache?'<span class="tag warn">缓存</span>':'<span class="tag ok">成功</span>';
    const mode=h.mode==='edit'?'<span class="tag">图生图</span>':'<span class="tag">文生图</span>';
    const img=h.image_url?`<button class="history-preview" onclick="showImagePreview(${i})"><img class="thumb" src="${withToken(h.image_url)}" alt="历史图片"></button>`:'<div class="thumb"></div>';
    const params=gptParamLine(h);
    return `<div class="card history-card">${img}<div class="row">${state}${mode}<span class="tag">${fmt(h.created_at)}</span></div><div class="history-meta muted"><div>用户：${esc(h.user_name||h.user_id||'-')}</div><div>模型：${esc(h.model||'-')}</div><div>尺寸：${esc(h.request_size||'-')} ${h.width&&h.height?`/ ${esc(h.width)}x${esc(h.height)}`:''}</div>${params?`<div>参数：${esc(params)}</div>`:''}<div>耗时：${h.duration_ms?Math.round(h.duration_ms/1000)+'秒':'-'}</div></div><div class="history-prompt">${esc((h.prompt||'').slice(0,120))||'无提示词'}</div></div>`;
  }).join('')||'<div class="muted">暂无历史</div>';
}
function favoriteCharacter(f){return String(f?.character||'未分类').trim()||'未分类'}
function favoriteMatch(f,kw){
  if(!kw)return true;
  return [f.character,f.file_name,f.relative_path,f.sender_id,f.sender_name,f.group_id,f.prompt_preview,f.model,f.workflow,f.lora_name].some(v=>String(v||'').toLowerCase().includes(kw));
}
function favoriteGroups(all){
  const map=new Map();
  for(const f of all){
    const name=favoriteCharacter(f);
    if(!map.has(name))map.set(name,{key:name,name,count:0,items:[],preview:''});
    const g=map.get(name);g.count++;g.items.push(f);if(!g.preview&&f.image_url)g.preview=f.image_url;
  }
  const groups=Array.from(map.values()).sort((a,b)=>b.count-a.count||a.name.localeCompare(b.name,'zh-Hans-CN'));
  const preview=(all.find(f=>f.image_url)||{}).image_url||'';
  return [{key:'all',name:'全部角色',count:all.length,items:all,preview},...groups];
}
function renderFavoriteRoles(groups){
  const box=$('favoriteRoles');if(!box)return;
  box.innerHTML=groups.map(g=>{
    const active=S.favoriteRole===g.key?' active':'';
    const img=g.preview?`<img class="role-avatar" src="${withToken(g.preview)}" alt="">`:'<div class="role-avatar"></div>';
    const sub=g.key==='all'?'全部收藏':'角色分组';
    return `<button class="role-button${active}" data-role="${esc(g.key)}">${img}<div><div class="role-name">${esc(g.name)}</div><div class="role-sub">${sub}</div></div><div class="role-count">${g.count}</div></button>`;
  }).join('')||'<div class="empty-state">暂无角色</div>';
  box.querySelectorAll('.role-button').forEach(btn=>btn.onclick=()=>{S.favoriteRole=btn.dataset.role||'all';renderFavorites()});
}
function sortFavorites(items,mode){
  const arr=[...items];
  if(mode==='role')arr.sort((a,b)=>favoriteCharacter(a).localeCompare(favoriteCharacter(b),'zh-Hans-CN')||(Number(b.mtime||0)-Number(a.mtime||0)));
  else if(mode==='file')arr.sort((a,b)=>String(a.file_name||'').localeCompare(String(b.file_name||''),'zh-Hans-CN'));
  else arr.sort((a,b)=>(Number(b.mtime||0)-Number(a.mtime||0))||String(b.created_at||'').localeCompare(String(a.created_at||'')));
  return arr;
}
function renderFavorites(){
  const box=$('favorites');if(!box)return;
  const kw=($('favoriteSearch')?.value||'').trim().toLowerCase();
  const all=S.data.favorites||[];
  const groups=favoriteGroups(all);
  if(!groups.some(g=>g.key===S.favoriteRole))S.favoriteRole='all';
  renderFavoriteRoles(groups);
  const roleName=S.favoriteRole==='all'?'全部角色':S.favoriteRole;
  const selectedRaw=(S.favoriteRole==='all'?all:all.filter(f=>favoriteCharacter(f)===S.favoriteRole)).filter(f=>favoriteMatch(f,kw));
  const selected=sortFavorites(selectedRaw,$('favoriteSort')?.value||'latest');
  S.previewFavorites=selected;
  const roleCount=S.favoriteRole==='all'?all.length:all.filter(f=>favoriteCharacter(f)===S.favoriteRole).length;
  if($('favoriteRoleTitle'))$('favoriteRoleTitle').textContent=roleName;
  if($('favoriteRoleMeta'))$('favoriteRoleMeta').textContent=`当前角色 ${roleCount} 张收藏${kw?`，搜索后 ${selected.length} 张`:''}`;
  if($('favoriteStats'))$('favoriteStats').innerHTML=[`<span class="tag ok">收藏 ${all.length} 张</span>`,`<span class="tag">角色 ${Math.max(0,groups.length-1)} 个</span>`,kw?`<span class="tag warn">搜索 ${selected.length} 张</span>`:''].join('');
  box.innerHTML=selected.map((f,i)=>{
    const source=[f.model,f.workflow,f.lora_name].filter(Boolean).join(' / ');
    const prompt=f.prompt_preview?`<div class="favorite-meta clamp">${esc(f.prompt_preview.slice(0,160))}</div>`:'';
    return `<article class="favorite-card"><button class="favorite-preview" onclick="showFavoritePreview(${i})"><img class="favorite-img" src="${withToken(f.image_url)}" alt="收藏图片"></button><div class="favorite-title">${esc(favoriteCharacter(f))}</div><div class="favorite-meta"><div>文件：${esc(f.file_name||'-')}</div><div>收藏人：${esc(f.sender_name||f.sender_id||'-')}</div><div>来源：${esc(f.group_id?('群 '+f.group_id):'私聊/本地')}</div>${source?`<div>生成：${esc(source)}</div>`:''}</div>${prompt}</article>`;
  }).join('')||'<div class="empty-state">暂无匹配收藏。引用或附带图片发送“酒狐收藏”后会出现在这里。</div>';
}
function showImagePreview(index){
  const h=S.previewHistory[index];if(!h||!h.image_url)return;
  const params=gptParamLine(h);
  $('modalImg').src=withToken(h.image_url);
  $('modalTitle').textContent=`${h.mode==='edit'?'图生图':'文生图'} / ${h.failed?'失败':h.from_cache?'缓存':'成功'}`;
  $('modalMeta').innerHTML=`用户：${esc(h.user_name||h.user_id||'-')}　模型：${esc(h.model||'-')}　尺寸：${esc(h.request_size||'-')} ${h.width&&h.height?`/ ${esc(h.width)}x${esc(h.height)}`:''}${params?`<br>参数：${esc(params)}`:''}<br>提示词：${esc(h.prompt||'')}`;
  $('imageModal').classList.remove('hidden');
}
function showFavoritePreview(index){
  const f=S.previewFavorites[index];if(!f||!f.image_url)return;
  $('modalImg').src=withToken(f.image_url);
  $('modalTitle').textContent=`收藏 / ${f.character||'未分类'}`;
  $('modalMeta').innerHTML=`文件：${esc(f.file_name||'-')}<br>收藏人：${esc(f.sender_name||f.sender_id||'-')}　来源：${esc(f.group_id?('群 '+f.group_id):'私聊/本地')}<br>模型：${esc(f.model||'-')}　工作流：${esc(f.workflow||'-')}　LoRA：${esc(f.lora_name||'-')}<br>路径：${esc(f.relative_path||'-')}<br>提示词：${esc(f.prompt_preview||'')}`;
  $('imageModal').classList.remove('hidden');
}
function hideImagePreview(){$('imageModal').classList.add('hidden');$('modalImg').src=''}
function modalBackdrop(event){if(event.target.id==='imageModal')hideImagePreview()}
document.addEventListener('keydown',e=>{if(e.key==='Escape')hideImagePreview()});
async function loadModelManager(toast=false){try{const r=await api('/api/comfy/model/status','POST',{});S.modelManager=r.model_manager||null;if(toast){renderModelManager()}}catch(e){S.modelManager={error:e.message};if(toast)alert(e.message)}}
function renderModelManager(){
  const m=S.modelManager||{},ill=m.illustrious||{},ck=ill.checkpoint_status||{},dl=m.download||{};
  const family=(m.families||[]).find(f=>f.id===m.active_family)||{};
  const familyTags=(m.families||[]).map(f=>`<span class="tag">${esc(f.label)}</span>`).join(' ')||'-';
  const modelHtml=kvRows([
    ['当前模型族',rawHtml(`<b>${esc(m.active_label||m.active_family||'-')}</b>`)],
    ['LoRA BaseModel',(family.base_models||[]).join(', ')||'-'],
    ['可用模型族',rawHtml(familyTags)],
    m.error?['接口错误',rawHtml(`<span class="tag danger">${esc(m.error)}</span>`)]:null,
  ]);
  if($('modelState'))$('modelState').innerHTML=modelHtml;
  if($('modelStateCopy'))$('modelStateCopy').innerHTML=modelHtml;
  if($('illustriousState')){
    const pct=dl.total?Math.min(100,Math.round((dl.done||0)*100/dl.total)):0;
    const speed=dl.speed_bps?`${Math.round(dl.speed_bps/1024/1024*10)/10} MB/s`:'-';
    const suggestions=(ck.suggested_dirs||[]).map(x=>esc(x)).join('<br>');
    $('illustriousState').innerHTML=kvRows([
      ['Checkpoint',ill.checkpoint_name||'-'],
      ['文生图工作流',ill.txt2img_workflow||ill.workflow||'-'],
      ['图生图工作流',ill.img2img_workflow||'-'],
      ['检测状态',rawHtml(ck.available?'<span class="tag ok">已检测到</span>':'<span class="tag warn">未检测到</span>')],
      ['路径',ck.path||'-'],
      ['下载',dl.running?`进行中 ${pct}% / ${speed}`:(dl.message||'空闲')],
      ['目标',dl.target||'-'],
      dl.error?['失败',rawHtml(`<span class="tag danger">${esc(dl.error)}</span>`)]:null,
      ck.warn?['提示',rawHtml(`<span>${esc(ck.warn).replace(/\n/g,'<br>')}</span>`)]:null,
      suggestions?['建议目录',rawHtml(`<span>${suggestions}</span>`)]:null,
    ]);
  }
  if($('modelCheckpointDir')){
    $('modelCheckpointDir').value=m.checkpoint_dir||'';
    $('modelDownloadProxy').value=m.proxy||'';
    $('modelDownloadTimeout').value=m.timeout||3600;
  }
}
function fillComfySettings(){
  const ec=S.data?.comfy?.editable_config||{},wf=ec.workflow_settings||{},im=ec.img2img_settings||{},av=ec.avatar_redraw_settings||{};
  if(!$('comfyServerAddress'))return;
  $('comfyServerAddress').value=ec.server_address||'';
  $('comfyWorkflowFile').value=wf.json_file||'finish_optimized_api.json';
  $('comfyInputNode').value=wf.input_node_id||'11';
  $('comfyNegNode').value=wf.neg_node_id||'12';
  $('comfyOutputNode').value=wf.output_node_id||'66';
  $('img2imgEnable').checked=im.enable!==false;
  $('characterReplaceEnable').checked=im.character_replace_enable!==false;
  $('autoCharacterReplaceWorkflow').checked=im.auto_use_character_replace_workflow!==false;
  $('img2imgDenoise').value=im.denoise_strength??0.65;
  $('characterReplaceWorkflow').value=im.character_replace_workflow||'character_replace_anima_reference_api.json';
  $('characterReplaceInputNode').value=im.character_replace_input_node_id||'11';
  $('characterReplaceNegNode').value=im.character_replace_neg_node_id||'12';
  $('characterReplaceOutputNode').value=im.character_replace_output_node_id||'404';
  $('characterReplaceLoraNode').value=im.character_replace_lora_node_id||'99';
  $('characterReplaceDenoise').value=im.character_replace_denoise_strength??1.0;
  $('characterReplaceLoraModel').value=im.character_replace_lora_strength_model??1.05;
  $('characterReplaceLoraClip').value=im.character_replace_lora_strength_clip??1.0;
  $('characterReplaceControlStrength').value=im.character_replace_control_strength??1.0;
  $('characterReplaceControlEnd').value=im.character_replace_control_end_percent??1.0;
  $('characterReplacePreprocessor').value=im.character_replace_preprocessor||'DepthAnythingV2Preprocessor';
  $('characterReplacePreprocessorResolution').value=im.character_replace_preprocessor_resolution??1024;
  $('characterReplaceScaleToLength').value=im.character_replace_scale_to_length??1536;
  $('characterReplacePrompt').value=im.character_replace_prompt||'';
  $('characterReplaceNegative').value=im.negative_prompt||'';
  $('avatarRedrawEnable').checked=av.enable!==false;
  $('avatarRedrawPrompt').value=av.prompt||'猪猪';
  $('avatarRedrawUrlTemplate').value=av.avatar_url_template||'https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640';
  $('avatarRedrawTimeout').value=av.timeout??20;
}
async function switchModelFamily(family){await api('/api/comfy/model/switch','POST',{family});await loadModelManager(false);renderModelManager();await refresh(false)}
async function downloadIllustrious(){await api('/api/comfy/model/download_illustrious','POST',{});await loadModelManager(false);renderModelManager();alert('已开始下载 Illustrious，进度会在本页刷新显示。')}
async function saveModelDownloadSettings(){try{const r=await api('/api/comfy/model/save_download_settings','POST',{checkpoint_dir:$('modelCheckpointDir').value,proxy:$('modelDownloadProxy').value,timeout:Number($('modelDownloadTimeout').value||3600)});S.modelManager=r.model_manager||S.modelManager;$('modelSaveMsg').textContent='已保存';renderModelManager()}catch(e){$('modelSaveMsg').textContent=e.message}}
async function saveComfySettings(){
  try{
    const payload={
      server_address:$('comfyServerAddress').value,
      workflow_settings:{json_file:$('comfyWorkflowFile').value,input_node_id:$('comfyInputNode').value,neg_node_id:$('comfyNegNode').value,output_node_id:$('comfyOutputNode').value},
      img2img_settings:{
        enable:$('img2imgEnable').checked,
        auto_use_character_replace_workflow:$('autoCharacterReplaceWorkflow').checked,
        character_replace_enable:$('characterReplaceEnable').checked,
        denoise_strength:Number($('img2imgDenoise').value||0.65),
        character_replace_workflow:$('characterReplaceWorkflow').value,
        character_replace_input_node_id:$('characterReplaceInputNode').value,
        character_replace_neg_node_id:$('characterReplaceNegNode').value,
        character_replace_output_node_id:$('characterReplaceOutputNode').value,
        character_replace_lora_node_id:$('characterReplaceLoraNode').value,
        character_replace_denoise_strength:Number($('characterReplaceDenoise').value||1),
        character_replace_lora_strength_model:Number($('characterReplaceLoraModel').value||1.05),
        character_replace_lora_strength_clip:Number($('characterReplaceLoraClip').value||1),
        character_replace_control_strength:Number($('characterReplaceControlStrength').value||1),
        character_replace_control_end_percent:Number($('characterReplaceControlEnd').value||1),
        character_replace_preprocessor:$('characterReplacePreprocessor').value,
        character_replace_preprocessor_resolution:Number($('characterReplacePreprocessorResolution').value||1024),
        character_replace_scale_to_length:Number($('characterReplaceScaleToLength').value||1536),
        character_replace_prompt:$('characterReplacePrompt').value,
        negative_prompt:$('characterReplaceNegative').value
      },
      avatar_redraw_settings:{enable:$('avatarRedrawEnable').checked,prompt:$('avatarRedrawPrompt').value,avatar_url_template:$('avatarRedrawUrlTemplate').value,timeout:Number($('avatarRedrawTimeout').value||20)}
    };
    const r=await api('/api/comfy/model/save_comfy_settings','POST',payload);
    S.modelManager=r.model_manager||S.modelManager;
    if(r.comfy)S.data.comfy=r.comfy;
    $('comfySaveMsg').textContent='已保存';
    document.querySelectorAll('[data-comfy-save-mirror]').forEach(x=>x.textContent='已保存');
    fillComfySettings();renderComfy();renderModelManager();
  }catch(e){$('comfySaveMsg').textContent=e.message;document.querySelectorAll('[data-comfy-save-mirror]').forEach(x=>x.textContent=e.message)}
}
function renderComfy(){
  const c=S.data.comfy||{},box=$('comfyStatus');if(!box)return;
  const skip=new Set(['editable_config','Anima 图生图推荐']);
  const rows=Object.entries(c).filter(([k])=>!skip.has(k)).map(([k,v])=>[k,typeof v==='object'?JSON.stringify(v):v]);
  box.innerHTML=kvRows(rows)||'<div class="empty-state">暂无 ComfyUI 状态</div>';
}
if(S.token){$('login').classList.add('hidden');$('app').classList.remove('hidden');refresh().catch(()=>logout())}
</script>
</body>
</html>"""

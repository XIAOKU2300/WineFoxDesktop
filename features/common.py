import asyncio
import base64
import hashlib
import html as html_lib
import importlib
import json
import logging
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import traceback
import uuid
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse

import aiohttp

try:
    from astrbot.api import logger
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.message_components import *
except Exception:  # pragma: no cover - local static tooling may not have AstrBot.
    logger = logging.getLogger(__name__)
    AstrMessageEvent = object

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PLUGIN_ID = "astrbot_plugin_comfyui_pro_UltraPlusMax"
PLUGIN_REPO = "https://github.com/XIAOKU2300/astrbot_plugin_comfyui_pro_UltraPlusMax.git"


def _metadata_version(default: str = "8.0.0") -> str:
    try:
        text = (PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8")
        match = re.search(r"(?m)^version:\s*(.+?)\s*$", text)
        if match:
            return match.group(1).strip()
    except Exception:
        pass
    return default


PLUGIN_VERSION = _metadata_version()

MODEL_FAMILY_ANIMA = "anima"
MODEL_FAMILY_ILLUSTRIOUS = "illustrious"
SUPPORTED_MODEL_FAMILIES = {MODEL_FAMILY_ANIMA, MODEL_FAMILY_ILLUSTRIOUS}

WAN_I2V_WORKFLOW = "wan_i2v_api.json"
WAN_I2V_INPUT_NODE_ID = "5"
WAN_I2V_NEG_NODE_ID = "6"
WAN_I2V_OUTPUT_NODE_ID = "11"
# 8.5.1：图生视频 CLIPVision 语义引导模型（放 ComfyUI/models/clip_vision/）
WAN_I2V_CLIP_VISION_MODEL = "clip_vision_h.safetensors"
WAN_I2V_NEGATIVE_PROMPT = (
    "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，"
    "最差质量，低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，"
    "画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，静止不动的画面，"
    "杂乱的背景，三条腿，背景人很多，倒着走，肢体变形，足部畸形，多余脚趾，"
    "融化的脚趾，穿模，闪烁，时序不一致，过饱和，色彩断层，色彩漂移，荧光伪影"
)

IMAGE_AUDIT_SCORE_KEYS = ("drawings", "hentai", "neutral", "porn", "sexy")
IMAGE_AUDIT_LABELS = {
    "drawings": "??",
    "hentai": "?????",
    "neutral": "??",
    "porn": "??",
    "sexy": "??",
}
IMAGE_AUDIT_ALIASES = {
    "drawing": "drawings",
    "drawings": "drawings",
    "hentai": "hentai",
    "neutral": "neutral",
    "normal": "neutral",
    "porn": "porn",
    "pornography": "porn",
    "sexy": "sexy",
}
IMAGE_AUDIT_MODEL_URL = "https://github.com/GantMan/nsfw_model/releases/download/1.1.0/nsfw_mobilenet_v2_140_224.zip"
IMAGE_AUDIT_MODEL_ARCHIVE_NAME = "nsfw_mobilenet_v2_140_224.zip"

def _safe_float_value(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)

def _safe_probability_value(value, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    return min(max(number, 0.0), 1.0)

class PluginFeature:
    def __init__(self, plugin):
        object.__setattr__(self, "_plugin", plugin)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_plugin"), name)

    def __setattr__(self, name, value):
        if name == "_plugin":
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_plugin"), name, value)


__all__ = [name for name in globals() if not name.startswith("__")]

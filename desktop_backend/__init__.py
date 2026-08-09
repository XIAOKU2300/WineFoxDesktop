"""WineFox Desktop 本地后端（Agent 07）。

只监听 127.0.0.1，进程级一次性 bootstrap + HttpOnly/SameSite=Strict Cookie 会话。
业务核心一律通过 ``protocols`` 中的 Protocol 注入（测试使用 fake，Agent 02/03/04/05
按其实现真实引擎/存储）。
"""

from __future__ import annotations

import re
from pathlib import Path

__version__ = "0.1.0"

# 握手协议版本：ready JSON 中的 "protocol" 字段
PROTOCOL_VERSION = 1

# 默认静态目录（相对进程 CWD，可参数注入）
DEFAULT_STATIC_DIR = "desktop_ui"

# multipart 上传默认上限（字节）
DEFAULT_MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def _load_plugin_version() -> str:
    """从插件 metadata.yaml 读取版本，失败时回退到 __version__。"""
    try:
        meta = Path(__file__).resolve().parent.parent / "metadata.yaml"
        text = meta.read_text(encoding="utf-8")
        m = re.search(r"(?m)^version:\s*(.+?)\s*$", text)
        if m:
            return m.group(1).strip().strip("\"'")
    except Exception:
        pass
    return __version__


VERSION = _load_plugin_version()

__all__ = [
    "VERSION",
    "PROTOCOL_VERSION",
    "DEFAULT_STATIC_DIR",
    "DEFAULT_MAX_UPLOAD_BYTES",
    "create_app",
    "WineFoxBackendServer",
    "AppError",
]

# 延迟导入避免循环依赖
from .app import create_app  # noqa: E402
from .errors import AppError  # noqa: E402
from .server import WineFoxBackendServer  # noqa: E402

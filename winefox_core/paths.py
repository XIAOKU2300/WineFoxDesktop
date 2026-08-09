"""WineFox Desktop 路径管理。

职责：
- 默认根目录为 %LOCALAPPDATA%\\WineFox（无 LOCALAPPDATA 时退回 ~/.winefox）。
- 定义 config / data / workflows / outputs / uploads / cache / logs / backups / trash
  九个受管子目录。
- 安装目录只读：受管目录永远不会指向安装目录，写操作被拒绝。
- 测试可注入根目录；拒绝路径逃逸（绝对路径、.. 等一律 ValueError）。
- 只使用 pathlib，不做字符串拼接。

规则：本模块不写任何真实密钥、不修改全局环境。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("winefox_core.paths")

APP_DIR_NAME = "WineFox"

#: 受管子目录（相对根的固定名称，禁止通过相对路径注入其他目录）。
MANAGED_SUBDIRS = (
    "config",
    "data",
    "workflows",
    "outputs",
    "uploads",
    "cache",
    "logs",
    "backups",
    "trash",
)

_DEFAULT_SUBDIR_MODE = 0o755


class PathError(ValueError):
    """路径非法（逃逸、绝对路径、越界等）。"""


def default_root() -> Path:
    """默认根目录：%LOCALAPPDATA%\\WineFox。"""
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local).expanduser() / APP_DIR_NAME
    return Path.home() / f".{APP_DIR_NAME.lower()}"


class PathManager:
    """WineFox 路径管理器。

    >>> pm = PathManager(Path("C:/tmp/winefox-root"))
    >>> pm.root  # 已 resolve 的绝对根
    >>> pm.subdir("workflows")  # 受管子目录
    >>> pm.resolve_managed("uploads", "sub/2024/x.png")  # 防逃逸拼接
    """

    def __init__(self, root: Path | None = None, install_dir: Path | None = None):
        if root is None:
            root = default_root()
        self._root = Path(root).expanduser().resolve(strict=False)
        self._install_dir = (
            Path(install_dir).expanduser().resolve(strict=False) if install_dir is not None else None
        )
        self._managed_cache: dict[str, Path] | None = None

    # ------------------------------------------------------------------ 基础
    @property
    def root(self) -> Path:
        return self._root

    @property
    def install_dir(self) -> Path | None:
        """插件/应用安装目录（只读）。"""
        return self._install_dir

    def is_readonly(self, path: Path | str) -> bool:
        """安装目录及其子路径一律只读。"""
        if self._install_dir is None:
            return False
        return _resolve(path).is_relative_to(self._install_dir)

    def writable(self, path: Path | str) -> bool:
        """路径是否允许写入：必须在根内且不在安装目录内。"""
        resolved = _resolve(path)
        if not resolved.is_relative_to(self._root):
            return False
        return not self.is_readonly(resolved)

    # ------------------------------------------------------------------ 受管目录
    def managed_subdirs(self) -> dict[str, Path]:
        if self._managed_cache is None:
            self._managed_cache = {
                name: self._root / name for name in MANAGED_SUBDIRS
            }
        return dict(self._managed_cache)

    def subdir(self, name: str) -> Path:
        """返回受管子目录（不创建）。name 必须属于 MANAGED_SUBDIRS。"""
        if name not in MANAGED_SUBDIRS:
            raise PathError(
                f"未知受管目录名 {name!r}，允许: {', '.join(MANAGED_SUBDIRS)}"
            )
        return self._root / name

    def resolve_managed(self, subdir: str, relative: str | Path | None = None) -> Path:
        """在受管子目录内安全拼接相对路径，拒绝逃逸。

        任意相对路径部件含绝对路径、盘符、``..``、前缀 ``~`` 或 NUL 字节都抛
        PathError。返回的路径保证位于该受管子目录之下（resolve 后校验）。
        """
        base = self.subdir(subdir)
        if relative is None or str(relative) in ("", "."):
            return base
        rel_str = str(relative).replace("\\", "/")
        if rel_str.startswith(("/", "\\")) or _has_drive_prefix(rel_str):
            raise PathError(f"禁止绝对路径: {relative!r}")
        parts = [p for p in rel_str.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise PathError(f"禁止路径逃逸(..): {relative!r}")
        if any("~" in p for p in parts):
            raise PathError(f"禁止 ~ 展开: {relative!r}")
        if any("\x00" in p for p in parts):
            raise PathError("路径包含 NUL 字节")

        candidate = base.joinpath(*parts)
        # 最终防御：resolve 后必须仍在 base 之下（防止符号链接逃逸）。
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(base.resolve(strict=False)):
            raise PathError(f"路径逃逸被拒绝: {relative!r}")
        return candidate

    # ------------------------------------------------------------------ 创建
    def ensure_dirs(self) -> dict[str, Path]:
        """创建全部受管子目录，返回 name->Path。安装目录只读性同时被校验。"""
        created: dict[str, Path] = {}
        for name, path in self.managed_subdirs().items():
            if self.is_readonly(path):
                raise PathError(f"受管目录落在只读安装目录内: {path}")
            if not path.exists():
                path.mkdir(parents=True, exist_ok=True)
                try:
                    path.chmod(_DEFAULT_SUBDIR_MODE)
                except OSError:
                    pass  # Windows 上 chmod 语义有限，忽略
            if not path.is_dir():
                raise PathError(f"受管目录不是目录: {path}")
            created[name] = path
        logger.debug("winefox paths ensured at %s", self._root)
        return created

    # ------------------------------------------------------------------ 便捷别名
    @property
    def config_dir(self) -> Path:
        return self.subdir("config")

    @property
    def data_dir(self) -> Path:
        return self.subdir("data")

    @property
    def workflow_dir(self) -> Path:
        return self.subdir("workflows")

    @property
    def output_dir(self) -> Path:
        return self.subdir("outputs")

    @property
    def upload_dir(self) -> Path:
        return self.subdir("uploads")

    @property
    def cache_dir(self) -> Path:
        return self.subdir("cache")

    @property
    def log_dir(self) -> Path:
        return self.subdir("logs")

    @property
    def backup_dir(self) -> Path:
        return self.subdir("backups")

    @property
    def trash_dir(self) -> Path:
        return self.subdir("trash")


def _resolve(path: Path | str) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _has_drive_prefix(value: str) -> bool:
    if len(value) >= 2 and value[1] == ":":
        return True
    if value.startswith("//"):
        return True
    return False

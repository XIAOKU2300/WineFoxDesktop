"""ComfyUI root discovery and local-path safety for the environment center.

The old BAT's ``Test-ComfyRoot`` decided a root by looking for ``models`` plus
(``custom_nodes`` or ``main.py``).  We keep that rule verbatim but add the
desktop-only guards from the migration doc (section 3.1 / security 11):

- never accept the WineFox install dir (read-only) or any path under it,
- never accept a system directory or a drive root,
- never let a manifest-relative ``dest`` escape the selected ComfyUI root.

This module is filesystem-only and never writes.
"""

from __future__ import annotations

import os
import platform
import re
from pathlib import Path
from typing import Iterable

from .setup_errors import SetupError, SetupErrorCode

#: Windows system dirs that must never be treated as a ComfyUI root.
_SYSTEM_DIR_NAMES = {
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "$recycle.bin",
    "system volume information",
    "perflogs",
    "recovery",
    "system32",
    "syswow64",
}

_PORTABLE_NAMES = ("ComfyUI", "ComfyUI_windows_portable")
_LAUNCHER_NAME = "A绘世启动器"
_LAUNCHER_COMFYUI = f"{_LAUNCHER_NAME}/ComfyUI"

_AMBIGUOUS_RE = re.compile(r"[^\x00-\x7f]")


def _resolve(path: Path | str) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def is_root_drive(path: Path | str) -> bool:
    resolved = _resolve(path)
    return len(resolved.parts) == 1


def is_system_dir(path: Path | str) -> bool:
    """True if the path sits inside a Windows system directory or at a drive root."""
    resolved = _resolve(path)
    parts = [p.lower() for p in resolved.parts]
    return any(name in parts for name in _SYSTEM_DIR_NAMES)


def is_inside(path: Path | str, base: Path | str) -> bool:
    return _resolve(path).is_relative_to(_resolve(base))


def test_root(path: Path | str) -> bool:
    """Old BAT ``Test-ComfyRoot``: ``models`` dir AND (``custom_nodes`` or ``main.py``)."""
    root = _resolve(path)
    if not root.is_dir():
        return False
    if not (root / "models").is_dir():
        return False
    return (root / "custom_nodes").is_dir() or (root / "main.py").is_file()


def validate_comfyui_root(
    path: Path | str,
    *,
    install_dir: Path | str | None = None,
    check_writable: bool = False,
) -> Path:
    """Validate a candidate ComfyUI root, returning the resolved path.

    Raises :class:`SetupError` with a stable code when the path is unusable.
    """
    resolved = _resolve(path)
    if not resolved.is_dir():
        raise SetupError(
            SetupErrorCode.ROOT_INVALID,
            f"目录不存在: {resolved}",
            details={"path": str(resolved)},
        )
    if is_root_drive(resolved):
        raise SetupError(SetupErrorCode.ROOT_INVALID, "不能把盘符根目录当作 ComfyUI 根", details={"path": str(resolved)})
    if is_system_dir(resolved):
        raise SetupError(
            SetupErrorCode.ROOT_INVALID,
            "系统目录不能作为 ComfyUI 根",
            details={"path": str(resolved)},
        )
    if install_dir is not None and is_inside(resolved, install_dir):
        raise SetupError(
            SetupErrorCode.ROOT_READ_ONLY,
            "不能使用 WineFox 安装目录作为 ComfyUI 根（只读）",
            details={"path": str(resolved), "install_dir": str(_resolve(install_dir))},
        )
    if not test_root(resolved):
        raise SetupError(
            SetupErrorCode.ROOT_INVALID,
            "该目录不是有效的 ComfyUI 根（缺少 models 目录且无 custom_nodes/main.py）",
            details={"path": str(resolved)},
        )
    if check_writable:
        if _is_readonly_acl(resolved):
            raise SetupError(
                SetupErrorCode.ROOT_READ_ONLY,
                "ComfyUI 根不可写",
                details={"path": str(resolved)},
            )
    return resolved


def validate_bootstrap_root(
    path: Path | str,
    *,
    install_dir: Path | str | None = None,
) -> Path:
    """Validate a target directory for from-scratch ComfyUI installation.

    Accepts a non-existent or empty directory (or an already-valid root);
    rejects drive roots, system dirs and anything inside the install dir.
    """
    resolved = _resolve(path)
    if is_root_drive(resolved):
        raise SetupError(SetupErrorCode.ROOT_INVALID, "不能把盘符根目录作为安装目标", details={"path": str(resolved)})
    if is_system_dir(resolved):
        raise SetupError(SetupErrorCode.ROOT_INVALID, "系统目录不能作为安装目标", details={"path": str(resolved)})
    if install_dir is not None and is_inside(resolved, install_dir):
        raise SetupError(
            SetupErrorCode.ROOT_READ_ONLY,
            "不能使用 WineFox 安装目录作为安装目标（只读）",
            details={"path": str(resolved), "install_dir": str(_resolve(install_dir))},
        )
    if resolved.exists():
        if not resolved.is_dir():
            raise SetupError(SetupErrorCode.ROOT_INVALID, "安装目标不是目录", details={"path": str(resolved)})
        if test_root(resolved):
            return resolved
        if any(resolved.iterdir()):
            raise SetupError(
                SetupErrorCode.ROOT_INVALID,
                "目录非空且不是有效 ComfyUI 根；从零装机请选择空目录",
                details={"path": str(resolved)},
            )
    parent = resolved.parent
    if not parent.exists() or not parent.is_dir():
        raise SetupError(SetupErrorCode.ROOT_INVALID, "父目录不存在", details={"path": str(parent)})
    try:
        probe = parent / ".winefox_write_probe"
        probe.write_bytes(b"x")
        probe.unlink()
    except OSError as exc:
        raise SetupError(
            SetupErrorCode.ROOT_READ_ONLY,
            f"父目录不可写: {exc.strerror or exc}",
            details={"path": str(parent)},
        ) from exc
    return resolved


def _is_readonly_acl(path: Path) -> bool:
    """Best-effort ACL writability check (Windows/macOS)."""
    if platform.system() == "Windows":
        probe = path / ".winefox_write_probe"
        try:
            probe.write_bytes(b"x")
            probe.unlink()
            return False
        except OSError:
            return True
    return not os.access(path, os.W_OK)


def find_candidates(
    bases: Iterable[Path | str],
    *,
    install_dir: Path | str | None = None,
) -> list[Path]:
    """Discover ComfyUI roots near ``bases`` (read-only).

    Mirrors the old BAT candidate order: base itself, ``ComfyUI``,
    ``ComfyUI_windows_portable/ComfyUI``, ``A绘世启动器/ComfyUI``, then
    top-level subdirs of each base.  Rejected candidates (system dirs, the
    install dir, drive roots, non-ComfyUI dirs) are silently skipped.
    """
    seen: set[Path] = set()
    found: list[Path] = []
    for base_raw in bases:
        base = _resolve(base_raw)
        if not base.is_dir():
            continue
        ordered: list[Path] = [base]
        ordered.append(base / "ComfyUI")
        ordered.append(base / "ComfyUI_windows_portable" / "ComfyUI")
        ordered.append(base / _LAUNCHER_COMFYUI)
        for child in sorted(base.iterdir(), key=lambda p: p.name.casefold()):
            if child.is_dir():
                ordered.append(child)
        for candidate in ordered:
            resolved = _resolve(candidate)
            if resolved in seen or not test_root(resolved):
                continue
            if is_system_dir(resolved) or is_root_drive(resolved):
                continue
            if install_dir is not None and is_inside(resolved, install_dir):
                continue
            seen.add(resolved)
            found.append(resolved)
    return found


def safe_comfyui_path(root: Path | str, relative: str) -> Path:
    """Join ``relative`` under the ComfyUI ``root`` without escape.

    ``relative`` uses forward slashes (manifest form).  Rejects absolute
    paths, ``..``, drive prefixes and NUL bytes; resolves and re-checks so a
    symlinked intermediate cannot escape the root.
    """
    root_resolved = _resolve(root)
    rel = relative.replace("\\", "/")
    if rel.startswith(("/", "\\")) or "\x00" in rel:
        raise SetupError(SetupErrorCode.ROOT_INVALID, f"非法相对路径: {relative!r}")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise SetupError(SetupErrorCode.ROOT_INVALID, f"路径逃逸被拒绝: {relative!r}")
    if any(len(p) >= 2 and p[1] == ":" for p in parts):
        raise SetupError(SetupErrorCode.ROOT_INVALID, f"路径含盘符: {relative!r}")
    candidate = root_resolved.joinpath(*parts)
    if not candidate.resolve(strict=False).is_relative_to(root_resolved):
        raise SetupError(SetupErrorCode.ROOT_INVALID, f"路径逃逸被拒绝: {relative!r}")
    return candidate


def contains_non_ascii(path: Path | str) -> bool:
    """Whether any path component is non-ASCII (Chinese dirs are supported)."""
    return bool(_AMBIGUOUS_RE.search(str(path)))

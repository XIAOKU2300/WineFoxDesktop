"""Resolve, validate and copy the bundled LoraBridge source.

The LoraBridge node is shipped inside the sidecar as a resource (it is small
and part of the project, so bundling is allowed).  The environment center
copies it into the selected ComfyUI ``custom_nodes`` folder instead of
fetching an unpinned GitHub checkout.

All filesystem work here is synchronous and blocking; callers must run it via
``asyncio.to_thread`` so the aiohttp event loop / WebView never stall.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .setup_manifest import _resource_path, resource_root

_VERSION_RE = re.compile(r"BRIDGE_VERSION\s*=\s*[\"']([^\"']+)[\"']")
_EXCLUDED_DIRS = {".git", "__pycache__", ".venv", "venv"}
_EXCLUDED_SUFFIXES = {".pyc", ".pyo"}


class BridgeSourceError(ValueError):
    """Raised when the bundled Bridge source is missing or unsafe."""


@dataclass(frozen=True)
class BridgeCopyResult:
    dest: Path
    version: str
    files_copied: int
    total_bytes: int


def resolve_bridge_source(source: str = "comfyui_custom_nodes/ComfyUI-AstrBot-LoraBridge") -> Path:
    """Return the packaged Bridge source directory (verified to exist and to
    stay inside the resource root)."""
    path = _resource_path(source)
    if not path.is_dir():
        raise BridgeSourceError(f"bundled Bridge source not found: {path}")
    resolved = path.resolve()
    root = resource_root().resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise BridgeSourceError(f"Bridge source escapes resource root: {resolved}") from None
    return path


def parse_bridge_version(init_path: Path) -> str:
    try:
        text = init_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise BridgeSourceError(f"cannot read Bridge __init__.py: {exc}") from exc
    match = _VERSION_RE.search(text)
    if not match:
        raise BridgeSourceError("Bridge __init__.py does not declare BRIDGE_VERSION")
    return match.group(1)


def validate_bridge_dir(source: Path) -> str:
    """Validate a Bridge source dir and return its declared version."""
    if not source.is_dir():
        raise BridgeSourceError(f"Bridge source is not a directory: {source}")
    init = source / "__init__.py"
    if not init.is_file():
        raise BridgeSourceError(f"Bridge source missing __init__.py: {source}")
    version = parse_bridge_version(init)
    if not version:
        raise BridgeSourceError("Bridge version string is empty")
    return version


def _scan_entries(source: Path) -> list[tuple[Path, str, bool]]:
    """Return (src_path, rel, is_dir) entries, refusing symlinks that escape."""
    entries: list[tuple[Path, str, bool]] = []
    for dirpath, dirnames, filenames in os.walk(source):
        dirpath_p = Path(dirpath)
        rel_dir = dirpath_p.relative_to(source).as_posix()
        keep_dirs = []
        for name in dirnames:
            if name in _EXCLUDED_DIRS:
                continue
            child = dirpath_p / name
            if child.is_symlink():
                raise BridgeSourceError(f"Bridge source contains symlink: {child}")
            keep_dirs.append(name)
            entries.append((child, f"{rel_dir}/{name}".lstrip("/") + "/", True))
        dirnames[:] = keep_dirs
        for name in filenames:
            if Path(name).suffix in _EXCLUDED_SUFFIXES:
                continue
            child = dirpath_p / name
            if child.is_symlink():
                raise BridgeSourceError(f"Bridge source contains symlink: {child}")
            entries.append((child, f"{rel_dir}/{name}".lstrip("/"), False))
    return entries


def copy_bridge_source(source: Path, dest: Path) -> BridgeCopyResult:
    """Copy the validated Bridge tree into ``dest`` (e.g. custom_nodes/<name>).

    The destination is created if missing; an existing destination is left
    untouched (the caller decides whether to replace it first).
    """
    version = validate_bridge_dir(source)
    entries = _scan_entries(source)
    files_copied = 0
    total_bytes = 0
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for src, rel, is_dir in entries:
        target = dest / rel.replace("/", os.sep)
        if is_dir:
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        files_copied += 1
        total_bytes += src.stat().st_size
    return BridgeCopyResult(dest=dest, version=version, files_copied=files_copied, total_bytes=total_bytes)


def seed_bridge_source(
    custom_nodes_dir: Path,
    dest_folder: str = "ComfyUI-AstrBot-LoraBridge",
    source: str = "comfyui_custom_nodes/ComfyUI-AstrBot-LoraBridge",
    replace_existing: bool = False,
) -> BridgeCopyResult:
    """High-level entry: resolve the bundle, optionally clear an existing
    install, then copy.  Blocking — call via ``asyncio.to_thread``."""
    src = resolve_bridge_source(source)
    dest = Path(custom_nodes_dir) / dest_folder
    if replace_existing and dest.exists():
        shutil.rmtree(dest)
    elif dest.exists():
        # Never clobber an existing (possibly user-modified) install.
        raise BridgeSourceError(f"Bridge already installed at {dest}; refusing to overwrite")
    return copy_bridge_source(src, dest)

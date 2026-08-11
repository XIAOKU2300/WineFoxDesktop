"""Python interpreter resolution for the environment center.

Ports the old BAT ``Resolve-ComfyPython`` filesystem candidate order
(migration doc 3.6) as a pure read-only walk: environment variable, embedded
pythons under the root/parent, then venvs.  Running-process and system-python
fallbacks are injected by the executor (they are not purely filesystem).

Version/pip probing runs in the executor via ``asyncio.to_thread``; this
module only locates candidate executables.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .setup_manifest import PythonSpec
from .setup_errors import SetupErrorCode

_FS_CANDIDATES = (
    ("paths:root_parent/python_embeded/python.exe", "root_parent", "python_embeded"),
    ("paths:root_parent/python/python.exe", "root_parent", "python"),
    ("paths:root_parent/python310/python.exe", "root_parent", "python310"),
    ("paths:root_parent/python311/python.exe", "root_parent", "python311"),
    ("paths:root_parent/python312/python.exe", "root_parent", "python312"),
    ("paths:root/python_embeded/python.exe", "root", "python_embeded"),
    ("paths:root/python/python.exe", "root", "python"),
    ("paths:root/venv/Scripts/python.exe", "root", "venv"),
    ("paths:root/.venv/Scripts/python.exe", "root", ".venv"),
)


@dataclass(frozen=True)
class PythonProbe:
    found: bool
    executable: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "found": self.found,
            "executable": self.executable,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


def _candidate_paths(root: Path, spec: PythonSpec) -> Iterable[Path]:
    root = root.expanduser().resolve(strict=False)
    root_parent = root.parent
    for _, where, name in _FS_CANDIDATES:
        base = root_parent if where == "root_parent" else root
        yield base / name / "python.exe"
        # Windows may name it python3.exe in some embedded distributions.
        yield base / name / "python3.exe"


def resolve_python_executable(root: Path, spec: PythonSpec, env: dict | None = None) -> PythonProbe:
    """Locate the first existing python executable under the filesystem rules."""
    env = env or {}
    env_path = env.get("COMFYUI_PYTHON")
    if env_path:
        candidate = Path(env_path).expanduser()
        if candidate.is_file():
            return PythonProbe(found=True, executable=str(candidate.resolve()))
        return PythonProbe(
            found=False,
            error_code=SetupErrorCode.PYTHON_NOT_FOUND,
            error_message=f"COMFYUI_PYTHON 指向的文件不存在: {candidate}",
        )
    for candidate in _candidate_paths(root, spec):
        if candidate.is_file():
            return PythonProbe(found=True, executable=str(candidate.resolve()))
    return PythonProbe(
        found=False,
        error_code=SetupErrorCode.PYTHON_NOT_FOUND,
        error_message="未在 ComfyUI 根目录或其上级目录找到嵌入式/虚拟环境 Python",
    )

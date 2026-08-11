"""Local file verification for downloaded models and node dirs.

Ports the old BAT ``Get-SafetensorsFileProblem`` / ``Get-GgufFileProblem``
(migration doc 3.9) into pure Python.  These functions are synchronous and
cheap (header-only reads); callers run them via ``asyncio.to_thread``.
"""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

_GB = 1024 ** 3

#: safetensors header length must sit within [2, 128 MiB] (old BAT rule).
SAFETENSORS_MAX_HEADER_LEN = 134217728
SAFETENSORS_MIN_BYTES = 16
GGUF_MAGIC = b"GGUF"

_STRUCTURED_KINDS = {"safetensors", "gguf"}


class VerifyFailure(Exception):
    """Raised when a file fails structural verification."""


def _read_exact(path: Path, size: int) -> bytes:
    with open(path, "rb") as fh:
        data = fh.read(size)
        if len(data) < size:
            raise VerifyFailure(f"文件过短: {path.name} ({len(data)}/{size} 字节)")
        return data


def verify_safetensors(path: Path) -> None:
    """Validate the safetensors header/tensor layout (raises VerifyFailure)."""
    size = path.stat().st_size
    if size < SAFETENSORS_MIN_BYTES:
        raise VerifyFailure(f"{path.name} 小于 16 字节，不是有效的 safetensors")
    header_len = struct.unpack("<Q", _read_exact(path, 8))[0]
    if not (2 <= header_len <= SAFETENSORS_MAX_HEADER_LEN):
        raise VerifyFailure(f"{path.name} header 长度非法: {header_len}")
    with open(path, "rb") as fh:
        fh.seek(8)  # skip the 8-byte length prefix; header body starts after it
        header_raw = fh.read(header_len)
    if len(header_raw) != header_len:
        raise VerifyFailure(f"{path.name} header 不完整")
    try:
        header = json.loads(header_raw.decode("utf-8", errors="strict"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise VerifyFailure(f"{path.name} header 不是合法 JSON: {exc}") from exc
    if not isinstance(header, dict):
        raise VerifyFailure(f"{path.name} header 必须是对象")
    data_start = 8 + header_len
    data_len = size - data_start
    if data_len < 0:
        raise VerifyFailure(f"{path.name} 数据区越界")
    max_end = 0
    tensor_count = 0
    for key, info in header.items():
        if key == "__metadata__":
            continue
        if not isinstance(info, dict):
            raise VerifyFailure(f"{path.name} tensor {key!r} 元数据非法")
        offsets = info.get("data_offsets")
        if not isinstance(offsets, list) or len(offsets) != 2:
            raise VerifyFailure(f"{path.name} tensor {key!r} 缺少 data_offsets")
        start, end = offsets
        if not (isinstance(start, int) and isinstance(end, int)):
            raise VerifyFailure(f"{path.name} tensor {key!r} offsets 非整数")
        if start < 0 or end < start or end > data_len:
            raise VerifyFailure(f"{path.name} tensor {key!r} 偏移越界 ({start},{end})")
        max_end = max(max_end, end)
        tensor_count += 1
    if tensor_count < 1:
        raise VerifyFailure(f"{path.name} 不含任何 tensor")
    if max_end != data_len:
        raise VerifyFailure(f"{path.name} 数据区末尾不匹配 (maxEnd={max_end}, len={data_len})")


def verify_gguf(path: Path) -> None:
    """Validate the GGUF magic (raises VerifyFailure)."""
    if _read_exact(path, 4) != GGUF_MAGIC:
        raise VerifyFailure(f"{path.name} 不是 GGUF 文件（magic 不符）")


def verify_min_size(path: Path, min_gb: float) -> None:
    size = path.stat().st_size
    if size < int(min_gb * _GB):
        raise VerifyFailure(f"{path.name} 大小不足 {min_gb:.2f} GiB (实际 {size / _GB:.2f} GiB)")


def verify_model_file(path: Path, kind: str, min_gb: float | None = None) -> None:
    """Verify a downloaded model by kind; raises VerifyFailure on problems."""
    if min_gb is not None:
        verify_min_size(path, min_gb)
    if kind in _STRUCTURED_KINDS:
        checker = verify_safetensors if kind == "safetensors" else verify_gguf
        checker(path)


def sha256_of(path: Path) -> str:
    """Streaming SHA-256 (64 lowercase hex)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_node_dir(path: Path) -> bool:
    """A usable custom node directory must contain __init__.py."""
    return (path / "__init__.py").is_file()

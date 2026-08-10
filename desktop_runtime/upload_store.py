"""Private upload storage addressed only by random IDs."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from desktop_backend.models import ImageData


_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class FileUploadStore:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _paths(self, upload_id: str) -> tuple[Path, Path]:
        if not _ID_RE.fullmatch(str(upload_id or "")):
            raise ValueError("invalid upload id")
        return self.directory / f"{upload_id}.bin", self.directory / f"{upload_id}.json"

    async def save(self, upload_id: str, data: bytes, *, filename: str, content_type: str, kind: str) -> None:
        body_path, meta_path = self._paths(upload_id)
        tmp = body_path.with_suffix(".tmp")
        with tmp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, body_path)
        safe_name = Path(str(filename or "upload.bin")).name
        meta_path.write_text(
            json.dumps({"filename": safe_name, "content_type": content_type, "kind": kind}, ensure_ascii=False),
            encoding="utf-8",
        )

    async def get(self, upload_id: str) -> ImageData | None:
        try:
            body_path, meta_path = self._paths(upload_id)
        except ValueError:
            return None
        if not body_path.is_file() or not meta_path.is_file():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return ImageData(
                data=body_path.read_bytes(),
                content_type=str(meta.get("content_type") or "application/octet-stream"),
                filename=str(meta.get("filename") or "upload.bin"),
            )
        except (OSError, ValueError):
            return None

    async def delete(self, upload_id: str) -> bool:
        try:
            body_path, meta_path = self._paths(upload_id)
        except ValueError:
            return False
        found = body_path.exists() or meta_path.exists()
        body_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        return found

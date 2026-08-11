"""Application composition root for the WineFox desktop sidecar."""

from __future__ import annotations

import sys
import os
from pathlib import Path
from typing import TextIO

from desktop_backend.server import WineFoxBackendServer
from winefox_core.paths import PathManager
from winefox_core.secrets import SecretStore
from winefox_core.storage import Database

from .config_adapter import DesktopConfigAdapter
from .engine import DesktopEngine
from .upload_store import FileUploadStore


def resource_root() -> Path:
    frozen = getattr(sys, "_MEIPASS", None)
    return Path(frozen).resolve() if frozen else Path(__file__).resolve().parent.parent


def resource_path(name: str) -> Path:
    root = resource_root()
    # The frozen sidecar keeps the full desktop assets under plugin_data/;
    # the legacy fake sidecar's desktop_ui must not shadow them.
    candidates = (root / "plugin_data" / name, root / name)
    return next((path for path in candidates if path.exists()), candidates[0])


class DesktopRuntime:
    def __init__(self, *, data_root: Path | None = None) -> None:
        if data_root is None and os.environ.get("WINEFOX_DATA_ROOT"):
            data_root = Path(os.environ["WINEFOX_DATA_ROOT"])
        self.paths = PathManager(root=data_root, install_dir=resource_root())
        self.paths.ensure_dirs()
        self.config = DesktopConfigAdapter(self.paths.config_dir, SecretStore())
        self.uploads = FileUploadStore(self.paths.upload_dir)
        self.database = Database(self.paths.data_dir / "winefox.db")
        self.database.initialize()
        self.engine = DesktopEngine(
            database=self.database,
            config=self.config,
            uploads=self.uploads,
            workflow_dir=resource_path("workflow"),
            output_dir=self.paths.output_dir,
            data_root=self.paths.root,
        )
        self.server = WineFoxBackendServer(
            engine=self.engine,
            config=self.config,
            uploads=self.uploads,
            static_dir=str(resource_path("desktop_ui")),
        )
        # 房间网关管理器：由 sidecar 注入 emit 回调并启动。
        self.room_manager = None  # type: ignore[assignment]

    async def start(self, *, ready_stream: TextIO | None = None) -> dict:
        await self.engine.start()
        try:
            return await self.server.start(ready_stream=ready_stream)
        except Exception:
            await self.engine.stop()
            raise

    async def stop(self) -> None:
        await self.server.stop()
        await self.engine.stop()
        self.database.close()

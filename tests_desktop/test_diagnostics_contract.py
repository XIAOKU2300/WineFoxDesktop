from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from desktop_backend.handlers import _normalize_diagnostics_contract
from desktop_runtime.engine import DesktopEngine


class _Database:
    def __init__(self, errors: list[str] | None = None) -> None:
        self.errors = errors or []

    def verify_integrity(self) -> list[str]:
        return list(self.errors)


class _Config:
    def snapshot(self) -> dict:
        return {
            "comfyui": {"server_address": "http://127.0.0.1:8188", "ssl_verify": True},
            "auth": {"mode": "none"},
            "ai_provider": {
                "enabled": True,
                "base_url": "https://api.example.invalid/v1",
                "model": "text-model",
            },
        }

    def get_secret(self, name: str) -> str | None:
        return "configured" if name == "ai_api_key" else None


class _Workflows:
    def all_entries(self) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(mode="txt2img", path="workflow/a.json", valid=True),
            SimpleNamespace(mode="img2img", path="workflow/b.json", valid=True),
            SimpleNamespace(mode="video", path="workflow/video.json", valid=True),
        ]


class _Queue:
    def qsize(self) -> int:
        return 0


class DiagnosticsContractTests(unittest.IsolatedAsyncioTestCase):
    def _engine(self, root: Path) -> DesktopEngine:
        engine = object.__new__(DesktopEngine)
        engine.db = _Database()
        engine.config = _Config()
        engine.workflows = _Workflows()
        engine.loaded_lora_profiles = SimpleNamespace(count=7)
        engine.data_root = root
        engine._current_job_id = None
        engine._queue = _Queue()
        engine.probe_comfyui = AsyncMock(return_value={
            "online": True,
            "server": "http://127.0.0.1:8188",
            "version": "0.3.50",
            "latency_ms": 42.5,
            "node_count": 321,
            "queue": {"running": 0, "pending": 0},
            "checkpoints": ["model.safetensors"],
            "loras": ["character.safetensors", "style.safetensors"],
            "missing_nodes": ["missingcustomnode"],
            "workflow_compatible": 1,
            "warnings": [],
            "summary": "ComfyUI 连接正常",
        })
        return engine

    async def test_runtime_contract_contains_real_counts_and_redacted_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_dir = root / "logs"
            log_dir.mkdir()
            secret = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
            log_dir.joinpath("winefox.log").write_text(
                f"INFO file=C:\\Users\\Example\\private.txt token={secret}\n",
                encoding="utf-8",
            )
            engine = self._engine(root)

            result = await engine.diagnostics()

            engine.probe_comfyui.assert_awaited_once_with()
            self.assertTrue(result["comfyui"]["connected"])
            self.assertEqual(result["nodes"]["total"], 321)
            self.assertEqual(result["nodes"]["missing"], ["missingcustomnode"])
            self.assertEqual(result["catalog"]["workflows"], 2)
            self.assertEqual(result["catalog"]["compatible_workflows"], 1)
            self.assertEqual(result["catalog"]["checkpoints"], 1)
            self.assertEqual(result["catalog"]["loras"], 2)
            exported = "\n".join(result["log_tail"]["lines"])
            self.assertNotIn(secret, exported)
            self.assertNotIn("C:\\Users\\Example", exported)
            self.assertIn("[REDACTED]", exported)
            self.assertIn("[LOCAL_PATH]", exported)

    async def test_failed_probe_is_structured_without_hiding_other_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self._engine(Path(directory))
            engine.probe_comfyui = AsyncMock(return_value={
                "online": False,
                "server": "http://127.0.0.1:8188",
                "node_count": None,
                "checkpoints": [],
                "loras": [],
                "missing_nodes": [],
                "workflow_compatible": None,
                "warnings": ["object_info: timeout"],
                "error_code": "COMFYUI_TIMEOUT",
                "summary": "连接超时",
            })

            result = await engine.diagnostics()

            self.assertFalse(result["comfyui"]["connected"])
            self.assertEqual(result["comfyui"]["error"]["code"], "COMFYUI_TIMEOUT")
            self.assertTrue(any(item["code"] == "COMFYUI_TIMEOUT" for item in result["failures"]))
            self.assertTrue(any(item["id"] == "database" for item in result["config_health"]))
            self.assertIn("detail", result["log_tail"])

    def test_handler_normalizes_legacy_engine_payload(self) -> None:
        result = _normalize_diagnostics_contract({
            "version": "8.5.9",
            "comfyui": {"status": "ok", "server": "http://localhost:8188"},
            "checks": [{"name": "SQLite", "status": "ok"}],
        })

        self.assertTrue(result["comfyui"]["connected"])
        self.assertEqual(result["comfyui"]["url"], "http://localhost:8188")
        for field in ("app", "plugin", "nodes", "catalog", "config_health", "log_tail", "failures"):
            self.assertIn(field, result)

    def test_frontend_uses_single_diagnostics_request(self) -> None:
        source = (Path(__file__).parents[1] / "desktop_ui" / "js" / "views" / "diagnostics.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("await api.diagnostics({ signal })", source)
        self.assertNotIn("api.comfyui()", source)


if __name__ == "__main__":
    unittest.main()

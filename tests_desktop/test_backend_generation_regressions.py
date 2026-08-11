from __future__ import annotations

import asyncio
import unittest
from io import BytesIO
from typing import Any

from PIL import Image

from desktop_runtime.engine import _detect_image_output
from desktop_runtime.lora_profiles import (
    merge_prompt_tags,
    resolve_prompt_loras,
    resolve_selected_lora_triggers,
)
from winefox_core.comfyui_executor import ComfyUIExecutor
from winefox_core.comfyui_types import (
    ComfyUIError,
    ComfyUIServerConfig,
    ErrorKind,
    ExecutionSnapshot,
)
from winefox_core.lora_service import LoraService


class _HistoryTransport:
    base_url = "http://127.0.0.1:8188"

    def __init__(self) -> None:
        self.view_requests: list[dict[str, Any]] = []

    async def start(self):
        return self

    async def close(self) -> None:
        return None

    async def get_json(self, path: str, **_kwargs):
        if path == "/queue":
            return {
                "queue_running": [[7, "prompt-array", {}, {}, []]],
                "queue_pending": [[8, "prompt-pending", {}, {}, []]],
            }
        if path == "/history/prompt-direct":
            return {
                "status": {"status_str": "success"},
                "outputs": {
                    "9": {
                        "images": [
                            {
                                "filename": "../../outside.png",
                                "subfolder": "",
                                "type": "output",
                            }
                        ]
                    },
                    "10": {
                        "files": [
                            {
                                "filename": "not-returned.png",
                                "subfolder": "C:/Windows",
                                "type": "output",
                            }
                        ]
                    },
                    "12": {
                        "ui": {
                            "gifs": [
                                {
                                    "filename": "final.png",
                                    "subfolder": "nested/results",
                                    "type": "temp",
                                }
                            ]
                        }
                    },
                },
            }
        return {}

    async def get_bytes(self, _path: str, *, params=None, **_kwargs) -> bytes:
        params = dict(params or {})
        self.view_requests.append(params)
        if params.get("filename") == "final.png":
            return b"valid-image-placeholder"
        raise ComfyUIError("missing output", kind=ErrorKind.DOWNLOAD, status=404)


class _MetadataTransport:
    base_url = "http://127.0.0.1:8188"

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def start(self):
        return self

    async def close(self) -> None:
        return None

    async def get_json(self, path: str, *, params=None, **_kwargs):
        params = dict(params or {})
        self.requests.append((path, params))
        if str(params.get("filename") or "").startswith("slow"):
            await asyncio.sleep(1)
        return {"modelspec.title": str(params.get("filename") or "")}


class BackendGenerationRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_lora_metadata_batch_is_bounded_and_uses_standard_endpoint(self) -> None:
        transport = _MetadataTransport()
        executor = ComfyUIExecutor(ComfyUIServerConfig(), transport=transport)

        result = await executor.fetch_lora_metadata_batch(
            [
                "characters/A.safetensors",
                "characters/B.safetensors",
                "characters/C.safetensors",
                "../escape.safetensors",
                "model.ckpt",
            ],
            max_items=2,
            concurrency=2,
            minimum_interval_seconds=0,
        )

        self.assertEqual(set(result), {"characters/A.safetensors", "characters/B.safetensors"})
        self.assertEqual(len(transport.requests), 2)
        self.assertTrue(all(path == "/view_metadata/loras" for path, _ in transport.requests))
        self.assertEqual(
            {request[1]["filename"] for request in transport.requests},
            {"characters/A.safetensors", "characters/B.safetensors"},
        )

    async def test_lora_metadata_timeout_is_non_fatal(self) -> None:
        transport = _MetadataTransport()
        executor = ComfyUIExecutor(ComfyUIServerConfig(), transport=transport)

        result = await executor.fetch_lora_metadata_batch(
            ["slow_model.safetensors"],
            timeout_seconds=0.01,
            total_timeout_seconds=0.05,
        )

        self.assertEqual(result, {})

    async def test_native_queue_arrays_preserve_prompt_ids(self) -> None:
        transport = _HistoryTransport()
        executor = ComfyUIExecutor(ComfyUIServerConfig(), transport=transport)

        queue = await executor.fetch_queue()

        self.assertEqual(queue.running_ids, ("prompt-array",))
        self.assertEqual(queue.pending_ids, ("prompt-pending",))

    async def test_running_array_entry_is_not_misreported_as_prompt_lost(self) -> None:
        transport = _HistoryTransport()
        executor = ComfyUIExecutor(ComfyUIServerConfig(), transport=transport)
        snapshot = ExecutionSnapshot(
            workflow={},
            options={
                "poll_attempts": 1,
                "queue_missing_grace_seconds": 0,
                "queue_missing_confirmations": 1,
            },
        )

        result = await executor.wait_for_completion("prompt-array", snapshot)

        self.assertFalse(result.ok)
        self.assertEqual(result.error.kind, ErrorKind.GENERATION_TIMEOUT)
        self.assertNotEqual(result.error.kind, ErrorKind.PROMPT_LOST)

    async def test_direct_history_multi_output_and_safe_fallback_download(self) -> None:
        transport = _HistoryTransport()
        executor = ComfyUIExecutor(ComfyUIServerConfig(), transport=transport)
        snapshot = ExecutionSnapshot(
            workflow={},
            output_node_id="9",
            options={"poll_attempts": 1, "decode_check": False},
        )

        result = await executor.wait_for_completion("prompt-direct", snapshot)

        self.assertTrue(result.ok)
        self.assertEqual(result.images, [b"valid-image-placeholder"])
        self.assertEqual(result.meta["image_filenames"], ["final.png"])
        self.assertEqual(result.meta["image_download_failures"], 2)
        self.assertEqual(
            transport.view_requests,
            [{"filename": "final.png", "subfolder": "nested/results", "type": "temp"}],
        )


class ImagePersistenceRegressionTests(unittest.TestCase):
    def encoded(self, image_format: str) -> bytes:
        output = BytesIO()
        Image.new("RGB", (2, 2), (120, 30, 80)).save(output, format=image_format)
        return output.getvalue()

    def test_real_image_format_controls_extension_and_mime(self) -> None:
        self.assertEqual(_detect_image_output(self.encoded("PNG")), (".png", "image/png"))
        self.assertEqual(_detect_image_output(self.encoded("JPEG")), (".jpg", "image/jpeg"))
        self.assertEqual(_detect_image_output(self.encoded("WEBP")), (".webp", "image/webp"))
        self.assertEqual(_detect_image_output(self.encoded("GIF")), (".gif", "image/gif"))

    def test_invalid_image_bytes_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "undecodable"):
            _detect_image_output(b"not-an-image")

    def test_unknown_decodable_image_format_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported image format"):
            _detect_image_output(self.encoded("PPM"))


class LoraMigrationRegressionTests(unittest.TestCase):
    def _service(self) -> LoraService:
        return LoraService(
            {
                "anima": {
                    "bailu": {
                        "lora": "characters/Bailu.safetensors",
                        "character": "\u767d\u9732",
                        "official_aliases": ["\u5c0f\u767d\u9732", "bailu"],
                        "activation_tags": ["bailu_character", "white hair"],
                    },
                    "jingliu": {
                        "lora": "characters/Jingliu.safetensors",
                        "character": "\u955c\u6d41",
                        "search_terms": ["\u955c\u6d41\u5e08\u7956", "jingliu"],
                        "trigger_prompt": "jingliu_character, blindfold",
                    },
                }
            }
        )

    def test_chinese_alias_matches_and_trigger_is_injected_once(self) -> None:
        resolved = resolve_prompt_loras(
            self._service(),
            "\u753b\u4e00\u5f20\u5c0f\u767d\u9732\u5728\u96ea\u5730",
            "anima",
            ["models/loras/Bailu.safetensors"],
        )

        self.assertEqual(len(resolved.loras), 1)
        self.assertIn("bailu_character", resolved.triggers)
        prompt = merge_prompt_tags("portrait, bailu_character", resolved.triggers)
        self.assertEqual(prompt.casefold().count("bailu_character"), 1)

    def test_multiple_alias_matches_select_and_inject_multiple_loras(self) -> None:
        service = self._service()
        resolved = resolve_prompt_loras(
            service,
            "bailu and jingliu",
            "anima",
            ["Bailu.safetensors", "Jingliu.safetensors"],
        )

        self.assertEqual(len(resolved.loras), 2)
        self.assertIn("bailu_character", resolved.triggers)
        self.assertIn("jingliu_character", resolved.triggers)

    def test_explicitly_selected_loras_also_receive_profile_triggers(self) -> None:
        triggers = resolve_selected_lora_triggers(
            self._service(),
            "anima",
            [
                {"name": "models/loras/Bailu.safetensors"},
                {"id": "characters/Jingliu.safetensors"},
                {"name": "Bailu.safetensors"},
            ],
        )

        self.assertEqual(triggers.count("bailu_character"), 1)
        self.assertIn("jingliu_character", triggers)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from desktop_runtime.config_adapter import DesktopConfigAdapter
from desktop_runtime.engine import DesktopEngine
from desktop_runtime.llm_client import resolve_provider
from winefox_core.secrets import MemorySecretBackend, SecretStore


class VisionProviderConfigTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="winefox_vision_provider_")
        self.config_dir = Path(self.temp.name)
        self.secrets = SecretStore(backend=MemorySecretBackend())
        self.adapter = DesktopConfigAdapter(self.config_dir, self.secrets)

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_two_providers_persist_separately_and_remain_redacted(self) -> None:
        settings = await self.adapter.patch_settings(
            {
                "ai_provider": {
                    "enabled": True,
                    "base_url": "https://text.example/v1",
                    "model": "text-model",
                    "utility_model": "utility-model",
                    "timeout_seconds": 35,
                    "api_key": "must-not-persist",
                },
                "vision_provider": {
                    "enabled": True,
                    "follow_text_provider": False,
                    "base_url": "https://vision.example/v1",
                    "model": "vision-model",
                    "timeout_seconds": 95,
                    "api_key": "must-not-persist-either",
                },
            }
        )

        self.assertEqual(settings["ai_provider"]["base_url"], "https://text.example/v1")
        self.assertEqual(settings["ai_provider"]["model"], "text-model")
        self.assertEqual(settings["vision_provider"]["base_url"], "https://vision.example/v1")
        self.assertEqual(settings["vision_provider"]["model"], "vision-model")
        self.assertFalse(settings["vision_provider"]["follow_text_provider"])
        self.assertNotIn("api_key", settings["ai_provider"])
        self.assertNotIn("api_key", settings["vision_provider"])

        raw = (self.config_dir / "desktop.json").read_text(encoding="utf-8")
        self.assertNotIn("must-not-persist", raw)
        persisted = json.loads(raw)
        self.assertNotIn("api_key", persisted["ai_provider"])
        self.assertNotIn("api_key", persisted["vision_provider"])

        reloaded = DesktopConfigAdapter(self.config_dir, self.secrets).snapshot()
        self.assertEqual(reloaded["ai_provider"]["model"], "text-model")
        self.assertEqual(reloaded["vision_provider"]["model"], "vision-model")

    async def test_secret_status_and_provider_resolution_do_not_cross(self) -> None:
        await self.adapter.put_secret("ai_api_key", "text-secret")
        await self.adapter.put_secret("vision_api_key", "vision-secret")
        statuses = {item["key"]: item["configured"] for item in (await self.adapter.secret_statuses())["items"]}
        self.assertTrue(statuses["ai_api_key"])
        self.assertTrue(statuses["vision_api_key"])

        settings = {
            "ai_provider": {
                "enabled": True,
                "base_url": "https://text.example/v1",
                "model": "text-model",
                "timeout_seconds": 30,
            },
            "vision_provider": {
                "enabled": True,
                "follow_text_provider": False,
                "base_url": "https://vision.example/v1",
                "model": "vision-model",
                "timeout_seconds": 90,
            },
        }
        text = resolve_provider(settings, "text")
        vision = resolve_provider(settings, "vision")
        self.assertEqual((text.source, text.secret_name), ("ai_provider", "ai_api_key"))
        self.assertEqual((vision.source, vision.secret_name), ("vision_provider", "vision_api_key"))
        self.assertEqual(vision.base_url, "https://vision.example/v1")
        self.assertEqual(vision.model, "vision-model")

    async def test_follow_text_provider_is_explicit(self) -> None:
        settings = {
            "ai_provider": {"base_url": "https://text.example/v1", "model": "text-model"},
            "vision_provider": {
                "enabled": True,
                "follow_text_provider": True,
                "base_url": "https://unused.example/v1",
                "model": "unused-vision-model",
                "timeout_seconds": 120,
            },
        }
        vision = resolve_provider(settings, "vision")
        self.assertTrue(vision.enabled)
        self.assertEqual(vision.source, "ai_provider")
        self.assertEqual(vision.secret_name, "ai_api_key")
        self.assertEqual(vision.base_url, "https://text.example/v1")
        self.assertEqual(vision.model, "text-model")
        self.assertEqual(vision.timeout_seconds, 120)

    async def test_legacy_vision_model_migrates_without_credentials(self) -> None:
        (self.config_dir / "desktop.json").write_text(
            json.dumps(
                {
                    "ai_provider": {
                        "vision_model": "legacy-vision-model",
                        "api_key": "legacy-plaintext-secret",
                    }
                }
            ),
            encoding="utf-8",
        )
        adapter = DesktopConfigAdapter(self.config_dir, self.secrets)
        snapshot = adapter.snapshot()
        self.assertEqual(snapshot["vision_provider"]["model"], "legacy-vision-model")
        self.assertNotIn("api_key", snapshot["ai_provider"])
        self.assertNotIn("api_key", snapshot["vision_provider"])

    async def test_vision_response_ignores_reasoning_and_meta_text(self) -> None:
        data = {
            "choices": [
                {
                    "message": {
                        "reasoning_content": "analysis: this must never be shown",
                        "content": "front view, soft smile, long hair, analysis of the request, blue sky",
                    }
                }
            ]
        }
        content = DesktopEngine._extract_llm_text(data)
        tags = DesktopEngine._sanitize_vision_tags(content)
        self.assertIn("front view", tags)
        self.assertIn("blue sky", tags)
        self.assertNotIn("analysis of the request", tags)
        self.assertNotIn("analysis: this must never be shown", tags)

    async def test_img2img_vision_execution_uses_independent_secret(self) -> None:
        await self.adapter.patch_settings(
            {
                "vision_provider": {
                    "enabled": True,
                    "follow_text_provider": False,
                    "base_url": "https://vision.example/v1",
                    "model": "vision-model",
                }
            }
        )
        await self.adapter.put_secret("vision_api_key", "vision-only-secret")

        observed: dict[str, object] = {}

        class FakeClient:
            def __init__(self, base_url: str, api_key: str, *, timeout_seconds: float) -> None:
                observed["base_url"] = base_url
                observed["api_key"] = api_key
                observed["timeout"] = timeout_seconds

            async def complete(self, messages, *, model: str, max_tokens: int, temperature: float):
                observed["messages"] = messages
                observed["model"] = model
                return {
                    "choices": [
                        {
                            "message": {
                                "reasoning_content": "do not leak this",
                                "content": "three quarter view, raised hand, soft smile, blue sky",
                            }
                        }
                    ]
                }

        engine = object.__new__(DesktopEngine)
        engine.config = self.adapter
        with patch("desktop_runtime.engine.OpenAICompatibleLLMClient", FakeClient):
            tags, meta = await engine._reverse_prompt_with_vision(
                b"\x89PNG\r\n\x1a\n",
                raw_prompt="a character",
                current_prompt="anime character",
                family="illustrious",
            )

        self.assertEqual(observed["base_url"], "https://vision.example/v1")
        self.assertEqual(observed["api_key"], "vision-only-secret")
        self.assertEqual(observed["model"], "vision-model")
        self.assertEqual(meta["status"], "completed")
        self.assertIn("raised hand", tags)
        content = observed["messages"][1]["content"]  # type: ignore[index]
        self.assertEqual(content[1]["image_url"]["url"].split(",", 1)[0], "data:image/png;base64")


if __name__ == "__main__":
    unittest.main()

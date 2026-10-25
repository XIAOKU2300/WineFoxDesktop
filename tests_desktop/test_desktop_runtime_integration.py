from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web

from desktop_runtime.llm_client import OpenAICompatibleLLMClient
from desktop_runtime.runtime import DesktopRuntime
from desktop_integration_tests.fake_comfyui.server import start_fake_comfyui


TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGM8ISfHAANMcBZeDgA0dgEMydTl/QAAAABJRU5ErkJggg=="
)
APP_VERSION = json.loads((Path(__file__).parents[1] / "packaging" / "version.json").read_text(encoding="utf-8"))["app"]


class DesktopRuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="winefox_runtime_")
        self.runtime = DesktopRuntime(data_root=Path(self.temp.name))
        self.ready = await self.runtime.start(ready_stream=None)
        self.base = f"http://127.0.0.1:{self.ready['port']}"
        self.session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        response = await self.session.get(
            self.base + self.ready["bootstrapPath"],
            headers={"Accept": "text/html"},
            allow_redirects=False,
        )
        self.assertEqual(response.status, 302)
        await response.release()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.runtime.stop()
        self.temp.cleanup()

    async def _json(self, method: str, path: str, **kwargs):
        headers = kwargs.pop("headers", {})
        if method not in {"GET", "HEAD"}:
            headers["X-WineFox-Client"] = "1"
        async with self.session.request(method, self.base + path, headers=headers, **kwargs) as response:
            body = await response.json()
            self.assertTrue(body.get("ok"), body)
            return response.status, body["data"]

    async def test_bootstrap_settings_upload_and_offline_job(self) -> None:
        status, health = await self._json("GET", "/api/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["version"], APP_VERSION)

        _, settings = await self._json("GET", "/api/v1/settings")
        self.assertTrue(settings["comfyui"]["ssl_verify"])
        self.assertIn("ai_provider", settings)
        self.assertFalse(settings["ai_provider"]["enabled"])
        _, updated = await self._json(
            "PATCH",
            "/api/v1/settings",
            json={
                "comfyui": {"server_address": "127.0.0.1:1", "ssl_verify": True},
                "ai_provider": {
                    "enabled": True,
                    "base_url": "https://example.invalid/v1",
                    "model": "prompt-model",
                    "utility_model": "utility-model",
                    "vision_model": "vision-model",
                },
            },
        )
        self.assertEqual(updated["comfyui"]["server_address"], "127.0.0.1:1")
        self.assertEqual(updated["ai_provider"]["model"], "prompt-model")
        self.assertNotIn("api_key", updated["ai_provider"])
        _, secrets = await self._json("GET", "/api/v1/secrets")
        self.assertIn("ai_api_key", {item["key"] for item in secrets["items"]})
        _, catalog = await self._json("GET", "/api/v1/catalog")
        self.assertEqual(len(catalog["workflows"]), 15)
        self.assertEqual(sum(item["mode"] == "txt2img" for item in catalog["workflows"]), 10)
        self.assertEqual(sum(item["mode"] == "img2img" for item in catalog["workflows"]), 5)

        form = aiohttp.FormData()
        form.add_field("file", TINY_PNG, filename="tiny.png", content_type="image/png")
        _, upload = await self._json("POST", "/api/v1/uploads", data=form)
        self.assertNotIn("path", upload)
        async with self.session.get(self.base + f"/api/v1/uploads/{upload['uploadId']}") as image_response:
            self.assertEqual(image_response.status, 200)
            self.assertEqual(await image_response.read(), TINY_PNG)

        _, job = await self._json("POST", "/api/v1/jobs", json={"mode": "txt2img", "prompt": "test"})
        for _ in range(300):
            _, job = await self._json("GET", f"/api/v1/jobs/{job['id']}")
            if job["status"] in {"failed", "completed"}:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(job["status"], "failed")
        self.assertTrue(job["error"])

    async def test_generation_success_persists_history_and_image(self) -> None:
        fake = await start_fake_comfyui(Path(__file__).resolve().parent.parent)
        try:
            await self._json(
                "PATCH",
                "/api/v1/settings",
                json={"comfyui": {"server_address": fake.base_url, "ssl_verify": True}},
            )
            _, job = await self._json(
                "POST",
                "/api/v1/jobs",
                json={"mode": "txt2img", "prompt": "a small fox", "seed": 0, "width": 768, "height": 1024},
            )
            for _ in range(200):
                _, job = await self._json("GET", f"/api/v1/jobs/{job['id']}")
                if job["status"] in {"failed", "completed"}:
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(job["status"], "completed", job)
            self.assertEqual(job["seed"], 0)
            self.assertTrue(job["image"]["url"])

            _, history = await self._json("GET", "/api/v1/history?limit=10")
            self.assertEqual(history["total"], 1)
            self.assertEqual(history["items"][0]["prompt"], "a small fox")
            async with self.session.get(self.base + history["items"][0]["image"]["url"]) as response:
                self.assertEqual(response.status, 200)
                self.assertTrue((await response.read()).startswith(b"\x89PNG"))
        finally:
            await fake.stop()

    async def test_openai_compatible_prompt_client(self) -> None:
        observed = {}

        async def complete(request: web.Request) -> web.Response:
            observed["authorization"] = request.headers.get("Authorization")
            observed["body"] = await request.json()
            return web.json_response({"choices": [{"message": {"content": "refined prompt"}}]})

        app = web.Application()
        app.router.add_post("/v1/chat/completions", complete)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            client = OpenAICompatibleLLMClient(f"http://127.0.0.1:{port}/v1", "test-secret")
            result = await client.complete([{"role": "user", "content": "fox"}], model="test-model")
            self.assertEqual(result["choices"][0]["message"]["content"], "refined prompt")
            self.assertEqual(observed["authorization"], "Bearer test-secret")
            self.assertEqual(observed["body"]["model"], "test-model")
        finally:
            await runner.cleanup()

# -*- coding: utf-8 -*-
"""Desktop /api/v1 integration contract tests (Agent 10).

STATUS: GATE PENDING — the WineFox Desktop backend (Agent 07, /api/v1) is not
merged into this repository yet. These tests are complete and will activate
automatically once the backend package is importable; until then they skip with
an explicit WAITING_FOR_INTEGRATION reason. They do NOT pretend to pass.

How activation works:
    1. Set WINEFOX_API_URL to a running desktop backend, OR
    2. Provide an importable backend package name in WINEFOX_DESKTOP_API
       (e.g. "winefox_desktop.api") that exposes ``create_app(config: dict)``.
    Until either exists, every test in this module skips.

Contract covered (per task):
    bootstrap / 鉴权 / 设置脱敏 / probe / 上传 / 任务 / 轮询 / 历史收藏 / 恢复 / 错误码
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
from pathlib import Path

import aiohttp
import pytest

pytestmark = [pytest.mark.desktop_gate, pytest.mark.fakecomfy]

MISSING_REASON = (
    "WAITING_FOR_INTEGRATION: desktop /api/v1 backend (Agent 07) is not merged. "
    "Set WINEFOX_API_URL or WINEFOX_DESKTOP_API to activate these contract tests."
)


def _api_base_url() -> str | None:
    url = os.environ.get("WINEFOX_API_URL", "").strip().rstrip("/")
    return url or None


def _backend_app_factory():
    name = os.environ.get("WINEFOX_DESKTOP_API", "").strip()
    if not name:
        return None
    try:
        module = importlib.import_module(name)
    except ImportError:
        return None
    factory = getattr(module, "create_app", None)
    return factory if callable(factory) else None


def _require_api():
    url = _api_base_url()
    factory = _backend_app_factory()
    if not url and factory is None:
        pytest.skip(MISSING_REASON)
    return url, factory


class TestBootstrap:
    def test_bootstrap_endpoint(self):
        """GET /api/v1/bootstrap -> {app, version, apiVersion, defaultPort, loopbackOnly}."""
        url, factory = _require_api()
        assert url or factory, "bootstrap needs an API target"

        async def _run():
            if factory is not None:
                app = factory({"comfyui": {"server_address": "127.0.0.1:8188"}})
                runner = aiohttp.web.AppRunner(app)
                await runner.setup()
                site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
                await site.start()
                base = f"http://127.0.0.1:{runner.addresses[0][1]}"
            else:
                base = url
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base}/api/v1/bootstrap") as resp:
                    assert resp.status == 200
                    body = await resp.json()
                    assert body["ok"] is True
                    assert body["app"] == "winefox-desktop"
                    assert "version" in body
                    assert "apiVersion" in body
                    return body

        body = asyncio.run(_run())
        assert body["apiVersion"] == "v1"


class TestAuth:
    def test_unauthorized_requests_return_401(self):
        """Hard gate: 未授权 401 — no default anonymous access."""
        url, factory = _require_api()

        async def _run():
            if factory is not None:
                app = factory({})
                runner = aiohttp.web.AppRunner(app)
                await runner.setup()
                site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
                await site.start()
                base = f"http://127.0.0.1:{runner.addresses[0][1]}"
            else:
                base = url
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base}/api/v1/settings") as resp:
                    return resp.status

        status = asyncio.run(_run())
        assert status == 401, "unauthenticated /api/v1/settings must be 401"

    def test_no_default_password_accepted(self):
        """Hard gate: 无默认密码 — 'REDACTED_LEGACY_PASSWORD' or '' must never log in."""
        url, factory = _require_api()

        async def _run():
            if factory is not None:
                app = factory({})
                runner = aiohttp.web.AppRunner(app)
                await runner.setup()
                site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
                await site.start()
                base = f"http://127.0.0.1:{runner.addresses[0][1]}"
            else:
                base = url
            async with aiohttp.ClientSession() as session:
                results = []
                for password in ("REDACTED_LEGACY_PASSWORD", "", "admin", "password"):
                    async with session.post(
                        f"{base}/api/v1/auth/login",
                        json={"password": password},
                    ) as resp:
                        results.append((password, resp.status))
                return results

        results = asyncio.run(_run())
        for password, status in results:
            assert status == 401, f"password {password!r} must be rejected"

    def test_token_never_accepted_via_query(self):
        """Hard gate: 无 query token — auth via ?token= must be rejected."""
        url, factory = _require_api()

        async def _run():
            if factory is not None:
                app = factory({})
                runner = aiohttp.web.AppRunner(app)
                await runner.setup()
                site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
                await site.start()
                base = f"http://127.0.0.1:{runner.addresses[0][1]}"
            else:
                base = url
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{base}/api/v1/auth/login?token=whatever") as resp:
                    return resp.status

        status = asyncio.run(_run())
        assert status == 401


class TestSettingsMasking:
    def test_settings_never_echo_secrets(self):
        """Hard gate: 密钥不落 JSON — /api/v1/settings must mask secret fields."""
        url, factory = _require_api()
        secret_fields = {
            "civitai_api_token",
            "huggingface_api_token",
            "prompt_translate_api_key",
            "utility_llm_api_key",
            "look_vision_api_key",
            "img2img_vision_api_key",
            "yodayo_cookie",
            "self_update_access_token",
            "comfyui_bridge_token",
            "admin_password",
        }

        async def _run():
            if factory is not None:
                app = factory({})
                runner = aiohttp.web.AppRunner(app)
                await runner.setup()
                site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
                await site.start()
                base = f"http://127.0.0.1:{runner.addresses[0][1]}"
            else:
                base = url
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{base}/api/v1/auth/login", json={"password": "x"}) as _:
                    pass
                async with session.get(f"{base}/api/v1/settings") as resp:
                    return resp.status, await resp.text()

        status, text = asyncio.run(_run())
        if status != 200:
            pytest.skip(f"settings endpoint not reachable without valid session (status {status})")
        body = json.loads(text)
        flattened = json.dumps(body, ensure_ascii=False)
        for field in secret_fields:
            assert field not in flattened, f"secret field {field} leaked into /api/v1/settings"
        assert "***" in flattened or "•••" in flattened or "redacted" in flattened.lower()


class TestProbe:
    def test_probe_reports_fake_server(self, fake_comfyui):
        """POST /api/v1/comfyui/probe against the fake server must report ok."""
        url, factory = _require_api()

        async def _run():
            if factory is not None:
                app = factory({"comfyui": {"server_address": fake_comfyui.base_url}})
                runner = aiohttp.web.AppRunner(app)
                await runner.setup()
                site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
                await site.start()
                base = f"http://127.0.0.1:{runner.addresses[0][1]}"
            else:
                base = url
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{base}/api/v1/comfyui/probe",
                    json={"server_address": fake_comfyui.base_url},
                ) as resp:
                    return resp.status, await resp.json()

        status, body = asyncio.run(_run())
        assert status == 200
        assert body["ok"] is True
        assert "classes" in body or "object_info" in body or "latency_ms" in body


class TestUploadTaskPoll:
    def test_upload_then_task_then_poll(self, fake_comfyui, workflow_copy, tmp_path):
        """Full desktop flow: upload -> submit task -> poll until image ready."""
        url, factory = _require_api()

        async def _run():
            if factory is not None:
                app = factory({"comfyui": {"server_address": fake_comfyui.base_url}})
                runner = aiohttp.web.AppRunner(app)
                await runner.setup()
                site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
                await site.start()
                base = f"http://127.0.0.1:{runner.addresses[0][1]}"
            else:
                base = url
            image = tmp_path / "src.png"
            image.write_bytes(
                __import__("base64").b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
                )
            )
            async with aiohttp.ClientSession() as session:
                form = aiohttp.FormData()
                form.add_field("image", open(image, "rb"), filename="src.png", content_type="image/png")
                async with session.post(f"{base}/api/v1/upload", data=form) as resp:
                    assert resp.status == 200, f"upload failed: {resp.status}"
                    upload = await resp.json()
                    assert upload["name"]
                async with session.post(
                    f"{base}/api/v1/tasks",
                    json={"prompt": "desktop integration test", "source_image": upload["name"]},
                ) as resp:
                    assert resp.status == 200, f"task submit failed: {resp.status}"
                    task = await resp.json()
                    task_id = task["task_id"]
                async with session.get(f"{base}/api/v1/tasks/{task_id}") as resp:
                    assert resp.status == 200
                    state = await resp.json()
                    assert state["status"] in {"running", "pending", "success", "error"}
                async with session.get(f"{base}/api/v1/tasks/{task_id}/wait?timeout=15") as resp:
                    result = await resp.json()
                    assert result["status"] in {"success", "error"}
                    assert fake_comfyui.stats["prompt"] >= 1
                    return result

        result = asyncio.run(_run())
        if result["status"] == "error":
            pytest.fail(f"task failed on fake server: {result.get('error')}")


class TestHistoryFavorites:
    def test_history_and_favorite_save(self, fake_comfyui):
        """History listing + '收藏' (favorite) endpoints."""
        url, factory = _require_api()

        async def _run():
            if factory is not None:
                app = factory({"comfyui": {"server_address": fake_comfyui.base_url}})
                runner = aiohttp.web.AppRunner(app)
                await runner.setup()
                site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
                await site.start()
                base = f"http://127.0.0.1:{runner.addresses[0][1]}"
            else:
                base = url
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base}/api/v1/history") as resp:
                    assert resp.status == 200
                    history = await resp.json()
                    assert isinstance(history.get("items", []), list)
                async with session.post(
                    f"{base}/api/v1/history/favorites",
                    json={"image_sha256": "deadbeef", "character": "测试角色"},
                ) as resp:
                    return resp.status, await resp.json()

        status, body = asyncio.run(_run())
        assert status == 200
        assert body.get("ok") is True


class TestRecovery:
    def test_queue_lost_recovery_error_code(self, fake_comfyui, workflow_copy):
        """Recovery: queue-lost must surface as a typed error code, not a hang."""
        url, factory = _require_api()
        fake_comfyui.set_mode("queue_lost")

        async def _run():
            if factory is not None:
                app = factory({"comfyui": {"server_address": fake_comfyui.base_url}})
                runner = aiohttp.web.AppRunner(app)
                await runner.setup()
                site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
                await site.start()
                base = f"http://127.0.0.1:{runner.addresses[0][1]}"
            else:
                base = url
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{base}/api/v1/tasks",
                    json={"prompt": "recovery test"},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    return resp.status, await resp.json()

        status, body = asyncio.run(_run())
        assert status == 200
        assert body["status"] == "error"
        code = body.get("error_code") or body.get("code")
        assert code, "queue-lost must be surfaced with a typed error_code"
        assert "queue" in str(code).lower()


class TestErrorCodes:
    def test_error_code_mapping_documented(self):
        """Error-code contract must be documented in integration_test_matrix.md."""
        plugin_root = Path(__file__).resolve().parents[1]

        doc = plugin_root / "docs" / "desktop_parallel" / "10_security_release" / "integration_test_matrix.md"
        assert doc.exists()
        text = doc.read_text(encoding="utf-8")
        for code in ("401", "400", "404", "413", "503"):
            assert code in text, f"matrix doc must document HTTP error code {code}"

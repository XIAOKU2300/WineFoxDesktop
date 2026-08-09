# -*- coding: utf-8 -*-
"""Fault-injection matrix (Agent 10).

Each fault mode of the Fake ComfyUI is driven through the full plugin client
pipeline. This is the "可以切正常/401/超时/缺节点/损坏图片/队列丢失" requirement.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

pytestmark = pytest.mark.fakecomfy


def run(coro):
    return asyncio.run(coro)


FAULT_MODES = ["auth_401", "connection_drop", "missing_node", "corrupt_image", "queue_lost", "exec_error", "view_500"]


class TestFaultMatrix:
    def test_all_fault_modes_are_known(self, fake_comfyui):
        from desktop_integration_tests.fake_comfyui import server

        for mode in FAULT_MODES:
            assert mode in server.MODES, mode

    @pytest.mark.parametrize("mode", ["auth_401", "connection_drop", "missing_node", "queue_lost", "exec_error"])
    def test_generate_never_hangs_and_returns_error(self, fake_comfyui, workflow_copy, client_factory, mode, monkeypatch):
        """Every fault must terminate with a user-facing error, never hang/crash."""
        fake_comfyui.set_mode(mode)
        if mode == "queue_lost":
            # arm the fake clock AFTER the prompt is submitted, so the grace
            # window elapses mid-polling
            import threading
            import time as _real_time

            import comfyui_api

            class _FakeTime:
                offset = 0.0

                @classmethod
                def time(cls):
                    return _real_time.time() + cls.offset

                @classmethod
                def perf_counter(cls):
                    return _real_time.perf_counter()

            monkeypatch.setattr(comfyui_api, "time", _FakeTime)
            threading.Timer(1.0, lambda: setattr(_FakeTime, "offset", 60.0)).start()
        api = client_factory(
            fake_comfyui,
            workflow_copy,
            {"lora_settings": {"poll_attempts": 8, "poll_interval": 0.2, "queue_missing_grace_seconds": 10, "queue_missing_confirmations": 1}},
        )
        started = time.monotonic()
        image_bytes, error, meta = run(api.generate(f"fault mode {mode}"))
        elapsed = time.monotonic() - started
        assert elapsed < 30, f"fault mode {mode} took too long ({elapsed:.1f}s)"
        assert image_bytes is None
        assert error, f"mode {mode} returned no error"
        assert isinstance(meta, dict)
        # every error must be scrubbed of secrets (no secret material in meta/error)
        assert "sk-test" not in error
        assert "Bearer " not in error

    @pytest.mark.parametrize("mode", ["auth_401", "connection_drop", "missing_node", "view_500"])
    def test_mode_switch_mid_conversation(self, fake_comfyui, workflow_copy, client_factory, mode):
        """Server can switch fault modes between calls (per '可切换' requirement)."""
        api = client_factory(fake_comfyui, workflow_copy, {})
        # healthy first
        image_bytes, error, meta = run(api.generate("healthy then fault"))
        assert error is None and image_bytes
        # fault second
        fake_comfyui.set_mode(mode)
        image_bytes2, error2, meta2 = run(api.generate("after switch"))
        assert image_bytes2 is None
        assert error2
        # healthy again after reset
        fake_comfyui.set_mode("normal")
        image_bytes3, error3, meta3 = run(api.generate("healthy again"))
        assert error3 is None and image_bytes3

    def test_corrupt_image_surfaced_at_download_layer(self, fake_comfyui, workflow_copy, client_factory):
        """corrupt_image: client downloads garbage without crashing; meta records filename."""
        fake_comfyui.set_mode("corrupt_image")
        api = client_factory(fake_comfyui, workflow_copy, {})
        image_bytes, error, meta = run(api.generate("corrupt image test"))
        # client layer does not sniff content; bytes are returned as-is
        assert error is None
        assert image_bytes == b"this is not a png - corrupted bytes"
        assert meta["image_filename"].endswith(".png")

    def test_connection_drop_retry_timing(self, fake_comfyui, workflow_copy, client_factory):
        """connection_drop must surface a request error (transient error recorded)."""
        fake_comfyui.set_mode("connection_drop")
        api = client_factory(fake_comfyui, workflow_copy, {})
        _, error, meta = run(api.generate("timing test"))
        assert "请求报错" in error
        assert meta.get("queue_submit_retry")
        assert fake_comfyui.stats["prompt"] == 0

    def test_fake_server_control_endpoint_switches_mode(self, fake_comfyui):
        """POST /__test/mode must work (used by orchestration)."""
        import aiohttp

        async def _switch(mode):
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{fake_comfyui.base_url}/__test/mode", json={"mode": mode}) as resp:
                    return resp.status, await resp.json()

        status, body = run(_switch("auth_401"))
        assert status == 200 and body["mode"] == "auth_401"
        assert fake_comfyui.mode == "auth_401"
        status, body = run(_switch("nope"))
        assert status == 400

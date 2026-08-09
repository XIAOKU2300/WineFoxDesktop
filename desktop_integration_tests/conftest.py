# -*- coding: utf-8 -*-
"""Shared fixtures for WineFox Desktop integration tests (Agent 10).

Layout: desktop_integration_tests/ sits at the plugin repo root; the plugin
source is the repo root itself. astrbot is not installed in this environment,
so a minimal ``astrbot.api.logger`` stub is installed before importing
production modules (same pattern as tests/test_black_screen_detect.py).
"""
from __future__ import annotations

import copy
import logging
import shutil
import sys
import time
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

# Install a real-package astrbot stub (main.py imports astrbot submodules).
from desktop_integration_tests.astrbot_stub import install_astrbot_stub  # noqa: E402

install_astrbot_stub()


def _ensure_astrbot_logger() -> None:
    try:
        import astrbot.api as astrbot_api
    except Exception:
        return
    if getattr(astrbot_api, "logger", None) is None:
        astrbot_api.logger = logging.getLogger("astrbot.stub")


_ensure_astrbot_logger()
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

pytest_plugins = []
pytestmark = pytest.mark.fakecomfy


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "fakecomfy: runs against the Fake ComfyUI server (no real ComfyUI needed)",
    )
    config.addinivalue_line(
        "markers",
        "desktop_gate: contract test for the not-yet-merged desktop /api/v1 backend (skips until integration)",
    )
    config.addinivalue_line(
        "markers",
        "hardgate: security hard-gate check",
    )


@pytest.fixture(scope="session")
def plugin_root() -> Path:
    return PLUGIN_ROOT


@pytest.fixture()
def workflow_copy(tmp_path: Path, plugin_root: Path) -> Path:
    """Copy plugin workflow/*.json into a throwaway data_dir (source stays read-only)."""
    data_dir = tmp_path / "data"
    wf_dir = data_dir / "workflow"
    wf_dir.mkdir(parents=True)
    src = plugin_root / "workflow"
    for path in src.glob("*.json"):
        shutil.copy2(path, wf_dir / path.name)
    return data_dir


@pytest.fixture()
def fake_comfyui(workflow_copy: Path):
    """Start one Fake ComfyUI per test on 127.0.0.1:<ephemeral>.

    The server runs on a dedicated background-thread event loop so that its
    internal background tasks (task completion, delays) keep running while the
    test drives the plugin client from the main thread.
    """
    import asyncio
    import threading
    from desktop_integration_tests.fake_comfyui import start_fake_comfyui

    loop = asyncio.new_event_loop()
    holder = {"server": None, "error": None}

    def _run():
        asyncio.set_event_loop(loop)
        try:
            holder["server"] = loop.run_until_complete(
                start_fake_comfyui(workflow_root=workflow_copy, mode="normal")
            )
            loop.run_forever()
        except Exception as exc:  # pragma: no cover - thread bootstrap failure
            holder["error"] = exc
        finally:
            try:
                if holder["server"] is not None:
                    loop.run_until_complete(holder["server"].stop())
            except Exception:
                pass
            loop.close()

    thread = threading.Thread(target=_run, name="fake-comfyui", daemon=True)
    thread.start()
    deadline = time.time() + 15
    while (holder["server"] is None and holder["error"] is None) and time.time() < deadline:
        time.sleep(0.05)
    if holder["error"] is not None:
        raise holder["error"]
    assert holder["server"] is not None, "Fake ComfyUI failed to start"
    server = holder["server"]
    yield server
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=10)


@pytest.fixture()
def client_factory(plugin_root: Path):
    """Build a comfyui_api.ComfyUI instance wired to a fake server URL.

    Returns a factory: client_factory(fake_server, tmp_data_dir, config_overrides)
    """
    import comfyui_api

    def _make(fake, data_dir: Path, config_overrides: dict | None = None):
        config = {
            "server_address": fake.base_url,
            "server_ssl_verify": False,  # loopback http in tests only
            "sub_config": {"steps": 4, "width": 256, "height": 256, "negative_prompt": ""},
            "workflow_settings": {"json_file": "workflow_api.json", "input_node_id": "6"},
            "lora_settings": {
                "enable_lora": False,
                "auto_refresh_lora_list": False,
                "poll_interval": 0.2,
                "poll_attempts": 40,
                "poll_max_interval": 0.2,
                "queue_missing_grace_seconds": 10,
                "queue_missing_confirmations": 1,
            },
        }
        if config_overrides:
            config = _deep_merge(config, config_overrides)
        return comfyui_api.ComfyUI(config, data_dir=data_dir)

    return _make


def _deep_merge(base: dict, overrides: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_json_file_safe(path, default=None):
    """Dummy plugin helper mirroring PluginFeature._load_json_file_safe."""
    try:
        import json as _json

        data = _json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else default
    except Exception:
        return default


@pytest.fixture()
def dummy_plugin(tmp_path: Path):
    """Minimal stand-in plugin object for feature-level tests.

    The features only read attributes through getattr, so a plain object with
    the handful of attrs they touch is enough.
    """
    from types import SimpleNamespace

    plugin = SimpleNamespace()
    plugin.data_dir = tmp_path
    plugin.favorite_dir = tmp_path / "favorites"
    plugin._load_json_file_safe = _load_json_file_safe
    plugin.prompt_translate_api_key = "sk-test-prompt-translate-0001"
    plugin.utility_llm_api_key = "sk-test-utility-llm-0002"
    plugin.img2img_vision_api_key = "sk-test-vision-0003"
    plugin.look_vision_api_key = "sk-test-look-0004"
    plugin.civitai_api_token = "civitai-test-token-1234567890abcdef"
    plugin.huggingface_api_token = "hf_test_token_abcdef123456"
    plugin.yodayo_cookie = "yodayo_test_cookie_value_1234"
    plugin.self_update_access_token = "github_pat_11_TEST_PAT_abcdef1234567890"
    return plugin

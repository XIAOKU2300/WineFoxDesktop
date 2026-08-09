# -*- coding: utf-8 -*-
"""Plugin-core integration tests against the Fake ComfyUI server (Agent 10).

Covers the /api/v1 integration matrix mapped onto the *currently existing*
production surface (the plugin core, comfyui_api.ComfyUI) so the suite runs
green today. The desktop /api/v1 HTTP contract tests live in
``test_api_v1_integration.py`` and activate once Agent 07's backend is merged.

Matrix covered here:
    bootstrap / settings / probe / upload / task submit / polling /
    history + favorites / recovery / error codes.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.fakecomfy


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------
class TestBootstrap:
    def test_server_address_normalization(self):
        import comfyui_api

        norm = comfyui_api._normalize_server_address
        assert norm("127.0.0.1:8188") == "http://127.0.0.1:8188"
        assert norm(" http://localhost:8188/ ") == "http://localhost:8188"
        assert norm("https://comfy.example.com:8443") == "https://comfy.example.com:8443"
        assert norm("") == ""

    def test_client_init_creates_workflow_dir_and_lora_cache(self, workflow_copy, client_factory):
        from types import SimpleNamespace

        dummy_fake = SimpleNamespace(base_url="http://127.0.0.1:1")  # init only, no requests
        api = client_factory(dummy_fake, workflow_copy, {})
        assert api.workflow_dir.exists()
        assert (workflow_copy / "workflow" / "workflow_api.json").exists()
        assert api.lora_cache_path is not None
        # default workflow must load and be API-format
        wf = api._load_workflow()
        assert isinstance(wf, dict) and wf
        assert any(isinstance(n, dict) and n.get("class_type") for n in wf.values())

    def test_all_workflows_parse(self, plugin_root):
        import comfyui_api

        wf_dir = plugin_root / "workflow"
        files = sorted(wf_dir.glob("*.json"))
        assert files, "no workflow files found"
        for path in files:
            data = json.loads(path.read_text(encoding="utf-8"))
            assert isinstance(data, dict) and data, path.name
            for node in data.values():
                assert isinstance(node, dict), path.name
                assert node.get("class_type"), path.name

    def test_fake_server_serves_object_info(self, fake_comfyui):
        import aiohttp

        async def _get():
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{fake_comfyui.base_url}/object_info") as resp:
                    assert resp.status == 200
                    return await resp.json()

        info = run(_get())
        assert "CLIPTextEncode" in info
        assert "SaveImage" in info

    def test_fake_server_rejects_unknown_class(self, fake_comfyui):
        import aiohttp

        async def _post():
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{fake_comfyui.base_url}/prompt",
                    json={"prompt": {"1": {"class_type": "TotallyUnknownNode", "inputs": {}}}},
                ) as resp:
                    body = await resp.json()
                    return resp.status, body

        status, body = run(_post())
        assert status == 400
        assert "prompt_outputs_failed_validation" in json.dumps(body)


# ---------------------------------------------------------------------------
# settings + masking
# ---------------------------------------------------------------------------
class TestSettingsMasking:
    def test_redact_sensitive_text_removes_all_secret_forms(self, dummy_plugin):
        """Hard gate: 密钥不落日志 —— _redact_sensitive_text must scrub values."""
        from main import ComfyUIPlugin

        plugin = ComfyUIPlugin.__new__(ComfyUIPlugin)
        for key, value in vars(dummy_plugin).items():
            if key.startswith("_"):
                continue
            setattr(plugin, key, value)
        plugin._self_update_token = lambda *a, **k: ""  # bypass feature delegation
        plugin._self_update_feature = None

        sample = (
            f"api key={dummy_plugin.prompt_translate_api_key} "
            f"token={dummy_plugin.civitai_api_token} "
            f"hf={dummy_plugin.huggingface_api_token} "
            f"cookie={dummy_plugin.yodayo_cookie} "
            f"pat={dummy_plugin.self_update_access_token} "
            "Authorization: Bearer sk-live-bearer-token-123456 "
            "https://user:pass@example.com/x"
        )
        redacted = plugin._redact_sensitive_text(sample)
        for secret in [
            dummy_plugin.prompt_translate_api_key,
            dummy_plugin.civitai_api_token,
            dummy_plugin.huggingface_api_token,
            dummy_plugin.yodayo_cookie,
            dummy_plugin.self_update_access_token,
            "sk-live-bearer-token-123456",
            "pass@example.com",
        ]:
            assert secret not in redacted, f"secret leaked into redacted text: {secret}"
        assert "***" in redacted

    def test_redact_test_mode_text(self, dummy_plugin):
        from features.test_mode import TestModeFeature

        plugin = dummy_plugin
        feature = TestModeFeature.__new__(TestModeFeature)
        feature._plugin = plugin
        text = f"probe failed token={plugin.civitai_api_token} cookie={plugin.yodayo_cookie}"
        redacted = feature._redact_test_mode_text(text)
        assert plugin.civitai_api_token not in redacted
        assert plugin.yodayo_cookie not in redacted

    def test_config_schema_has_no_secret_defaults(self, plugin_root):
        """Hard gate: 无默认密码 —— API keys/tokens must not ship real defaults."""
        schema = json.loads((plugin_root / "_conf_schema.json").read_text(encoding="utf-8"))
        secret_paths = [
            ("civitai_settings", "api_token"),
            ("civitai_settings", "cookie"),
            ("civitai_settings", "huggingface_api_token"),
            ("civitai_settings", "yodayo_cookie"),
            ("civitai_settings", "comfyui_bridge_token"),
            ("llm_settings", "prompt_translate_api_key"),
            ("llm_settings", "utility_llm_api_key"),
            ("llm_settings", "img2img_vision_api_key"),
            ("llm_settings", "look_vision_api_key"),
        ]
        for section, key in secret_paths:
            entry = (schema.get(section) or {}).get("items", {}).get(key) or (schema.get(section) or {}).get(key)
            assert entry is not None, f"schema should declare {section}.items.{key}"
            default = str(entry.get("default") or "")
            assert default == "", f"{section}.items.{key} ships non-empty default {default!r}"
        # gpt_magic webui token is nested two levels deep
        webui = ((schema.get("gpt_magic_settings") or {}).get("items") or {}).get("webui") or {}
        token_entry = (webui.get("items") or {}).get("token") or {}
        assert str(token_entry.get("default") or "") == "", "gpt_magic webui token ships a default"

    @pytest.mark.xfail(
        reason=(
            "HARD-GATE FINDING: _conf_schema.json ships fixed default passwords "
            "(gpt_magic_settings.gallery.admin_password='REDACTED_LEGACY_PASSWORD', "
            "video_settings.archive_password='114514', comfy_nsfw_self_review.password='114514'). "
            "Desktop layer must force rotation on first run; plugin baseline unchanged."
        ),
        strict=False,
    )
    def test_config_schema_ships_no_fixed_passwords(self, plugin_root):
        """Recorded finding: fixed passwords exist in the baseline config schema."""
        schema = json.loads((plugin_root / "_conf_schema.json").read_text(encoding="utf-8"))
        defaults = {}

        def walk(obj, path=""):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if isinstance(v, dict) and "default" in v and isinstance(v["default"], str):
                        if any(word in k.lower() for word in ("password",)):
                            defaults[f"{path}.{k}"] = v["default"]
                    walk(v, f"{path}.{k}")

        walk(schema)
        assert defaults == {}, f"fixed password defaults shipped: {defaults}"


# ---------------------------------------------------------------------------
# probe / lora discovery
# ---------------------------------------------------------------------------
class TestProbeAndLoras:
    def test_fetch_lora_list_from_object_info_and_models(self, fake_comfyui, workflow_copy, client_factory):
        api = client_factory(fake_comfyui, workflow_copy, {})
        loras, warn = run(api.fetch_lora_list(force=True, allow_cache_fallback=False))
        assert loras, "expected lora names from fake server"
        assert any(name.endswith(".safetensors") for name in loras)
        assert "winefox_test_lora_a.safetensors" in loras
        assert warn is None or warn == ""
        # disk cache written
        cache = json.loads(api.lora_cache_path.read_text(encoding="utf-8"))
        assert cache["server"] == fake_comfyui.base_url
        assert "winefox_test_lora_a.safetensors" in cache["loras"]
        assert fake_comfyui.stats["object_info"] >= 1
        assert fake_comfyui.stats["models_loras"] >= 1

    def test_lora_cache_fallback_on_server_401(self, fake_comfyui, workflow_copy, client_factory):
        api = client_factory(fake_comfyui, workflow_copy, {})
        run(api.fetch_lora_list(force=True))
        fake_comfyui.set_mode("auth_401")
        loras, warn = run(api.fetch_lora_list(force=True, allow_cache_fallback=True))
        assert loras  # from cache
        assert "401" in (warn or "")
        # and with no fallback the empty list is explicit
        loras2, warn2 = run(api.fetch_lora_list(force=True, allow_cache_fallback=False))
        assert loras2 == []
        assert "未使用缓存" in warn2

    def test_probe_health_pattern(self, fake_comfyui, workflow_copy, client_factory):
        """Probe semantics used by the desktop /api/v1/comfyui/probe endpoint."""
        import aiohttp

        async def _probe():
            ok, detail = False, ""
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.get(f"{fake_comfyui.base_url}/object_info", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status == 200:
                            info = await resp.json()
                            ok = bool(info) and "KSampler" in info
                            detail = f"object_info classes={len(info)}"
                        else:
                            detail = f"status {resp.status}"
                except Exception as exc:
                    detail = str(exc)
            return {"ok": ok, "detail": detail}

        result = run(_probe())
        assert result["ok"], result
        fake_comfyui.set_mode("auth_401")
        result = run(_probe())
        assert not result["ok"]
        assert "401" in result["detail"]


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------
class TestUpload:
    def _make_image(self, path: Path) -> Path:
        # tiny valid PNG (1x1) - upload content is not validated by fake server
        import base64

        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
        path.write_bytes(png)
        return path

    def test_upload_input_image(self, fake_comfyui, workflow_copy, client_factory, tmp_path):
        api = client_factory(fake_comfyui, workflow_copy, {})
        img = self._make_image(tmp_path / "src.png")
        import aiohttp

        async def _upload():
            async with api._client_session(aiohttp.ClientTimeout(total=10)) as session:
                return await api._upload_input_image(session, str(img))

        info = run(_upload())
        assert info["name"].startswith("astrbot_img2img_")
        assert info["type"] == "input"
        assert info["subfolder"] == "astrbot_img2img"
        assert fake_comfyui.stats["upload_image"] == 1
        assert fake_comfyui.uploads[0]["size"] > 0

    def test_upload_missing_file_raises(self, fake_comfyui, workflow_copy, client_factory, tmp_path):
        api = client_factory(fake_comfyui, workflow_copy, {})
        import aiohttp

        async def _upload():
            async with api._client_session(aiohttp.ClientTimeout(total=10)) as session:
                return await api._upload_input_image(session, str(tmp_path / "nope.png"))

        with pytest.raises(FileNotFoundError):
            run(_upload())

    def test_oversized_upload_fails_with_status(self, fake_comfyui, workflow_copy, client_factory, tmp_path):
        """Hard gate: 超大上传失败 —— 413 must surface as an error, not hang."""
        fake_comfyui.max_upload_bytes = 512
        api = client_factory(fake_comfyui, workflow_copy, {})
        img = tmp_path / "big.png"
        img.write_bytes(b"\x89PNG\r\n" + b"x" * 4096)
        import aiohttp

        async def _upload():
            async with api._client_session(aiohttp.ClientTimeout(total=10)) as session:
                return await api._upload_input_image(session, str(img))

        with pytest.raises(RuntimeError) as excinfo:
            run(_upload())
        assert "413" in str(excinfo.value)

    def test_upload_path_traversal_rejected(self, fake_comfyui):
        """Hard gate: 路径穿越失败 —— fake server refuses traversal filenames."""
        import aiohttp

        async def _upload(name):
            form = aiohttp.FormData()
            form.add_field("image", b"fake", filename=name, content_type="image/png")
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{fake_comfyui.base_url}/upload/image", data=form) as resp:
                    return resp.status

        assert run(_upload("../../etc/passwd.png")) == 400
        assert run(_upload("..\\..\\evil.png")) == 400
        assert run(_upload("/absolute.png")) == 400
        assert run(_upload("normal_upload.png")) == 200


# ---------------------------------------------------------------------------
# full task lifecycle (submit -> poll -> download)
# ---------------------------------------------------------------------------
class TestTaskLifecycle:
    def test_generate_happy_path(self, fake_comfyui, workflow_copy, client_factory):
        api = client_factory(fake_comfyui, workflow_copy, {})
        image_bytes, error, meta = run(api.generate("a winefox test prompt, masterpiece"))
        assert error is None, error
        assert image_bytes and image_bytes[:8] == b"\x89PNG\r\n\x1a\n"
        assert meta["prompt_id"]
        assert meta["elapsed_seconds"] >= 0
        assert fake_comfyui.stats["prompt"] == 1
        assert fake_comfyui.stats["history"] >= 1
        assert fake_comfyui.stats["view"] == 1
        assert "prompt" in meta["prompt"]

    def test_generate_with_lora_refresh(self, fake_comfyui, workflow_copy, client_factory):
        """auto_refresh_lora_list=True exercises /models/loras inside generate()."""
        api = client_factory(
            fake_comfyui,
            workflow_copy,
            {
                "workflow_settings": {"json_file": "finish_illustrious_multi_lora_api.json", "input_node_id": "11", "output_node_id": "66"},
                "lora_settings": {"enable_lora": True, "auto_refresh_lora_list": True},
            },
        )
        image_bytes, error, meta = run(api.generate("lora refresh test"))
        assert error is None, error
        assert image_bytes
        assert fake_comfyui.stats["models_loras"] >= 1
        assert meta.get("lora_cache_count", 0) >= 3

    def test_generate_timeout_after_max_polls(self, fake_comfyui, workflow_copy, client_factory):
        """History never completes -> client must give up with '生成超时'."""
        fake_comfyui.exec_delay = 30.0  # task will never finish within poll budget
        api = client_factory(
            fake_comfyui,
            workflow_copy,
            {"lora_settings": {"poll_attempts": 2, "poll_interval": 0.2}},
        )
        image_bytes, error, meta = run(api.generate("timeout test"))
        assert image_bytes is None
        assert error == "生成超时"

    def test_generate_img2img_upload_path(self, fake_comfyui, workflow_copy, client_factory, tmp_path):
        import base64

        api = client_factory(
            fake_comfyui,
            workflow_copy,
            {"workflow_settings": {"json_file": "img2img_illustrious_api.json", "input_node_id": "11", "output_node_id": "66"}},
        )
        img = tmp_path / "src.png"
        img.write_bytes(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
            )
        )
        image_bytes, error, meta = run(api.generate("img2img test", source_image_path=str(img)))
        assert error is None, error
        assert image_bytes
        assert fake_comfyui.stats["upload_image"] == 1


# ---------------------------------------------------------------------------
# history + favorites (desktop 历史收藏 contract)
# ---------------------------------------------------------------------------
class TestHistoryFavorites:
    def test_history_item_shape(self, fake_comfyui, workflow_copy, client_factory):
        """The /history payload the desktop will surface must contain status+outputs."""
        import aiohttp

        async def _flow():
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{fake_comfyui.base_url}/prompt",
                    json={"prompt": {"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "hi", "clip": ["9", 0]}}}},
                ) as resp:
                    prompt_id = (await resp.json())["prompt_id"]
                await asyncio.sleep(0.15)
                async with session.get(f"{fake_comfyui.base_url}/history/{prompt_id}") as resp:
                    history = await resp.json()
                return history, prompt_id

        history, prompt_id = run(_flow())
        assert prompt_id in history
        item = history[prompt_id]
        assert item["status"]["status_str"] == "success"
        assert item["status"]["completed"] is True
        assert item["outputs"], "history outputs must be non-empty for the desktop gallery"
        images = list(item["outputs"].values())[0]["images"]
        assert images[0]["filename"].endswith(".png")

    def test_favorite_safe_path_part_rejects_traversal(self, dummy_plugin, tmp_path):
        from features.favorites import FavoritesFeature

        feature = FavoritesFeature.__new__(FavoritesFeature)
        feature._plugin = dummy_plugin
        assert feature._favorite_safe_path_part("../../etc/passwd", "未分类") == "etc_passwd"
        assert feature._favorite_safe_path_part("a<b>:c", "未分类") == "a_b__c"
        assert feature._favorite_safe_path_part("", "未分类") == "未分类"
        assert len(feature._favorite_safe_path_part("x" * 200, "未分类")) <= 80

    def test_copy_favorite_image_and_sha256_dedupe(self, dummy_plugin, tmp_path):
        from features.favorites import FavoritesFeature

        feature = FavoritesFeature.__new__(FavoritesFeature)
        feature._plugin = dummy_plugin
        src = tmp_path / "art.png"
        src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"payload")
        dest = feature._copy_favorite_image(str(src), "测试角色")
        assert dest.exists()
        assert dest.suffix == ".png"
        assert "测试角色" in str(dest)
        h1 = feature._favorite_file_sha256(str(src))
        h2 = feature._favorite_file_sha256(str(dest))
        assert h1 == h2 and len(h1) == 64
        found, meta = feature._find_favorite_image_by_sha256(h1)
        assert found is not None
        # traversal-safe character name
        dest2 = feature._copy_favorite_image(str(src), "../evil")
        assert ".." not in str(dest2.parent)


# ---------------------------------------------------------------------------
# recovery (queue lost / connection drop / oom)
# ---------------------------------------------------------------------------
class TestRecovery:
    def test_queue_lost_detected_after_confirmations(self, fake_comfyui, workflow_copy, client_factory, monkeypatch):
        """Hard gate: 队列丢失恢复 —— client must detect prompt vanished from queue."""
        import threading
        import time as _real_time

        import comfyui_api

        fake_comfyui.set_mode("queue_lost")

        class _FakeTime:
            offset = 0.0  # armed later, after the prompt is submitted

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
            {"lora_settings": {"poll_attempts": 12, "poll_interval": 0.2, "queue_missing_grace_seconds": 10, "queue_missing_confirmations": 1}},
        )
        image_bytes, error, meta = run(api.generate("queue lost test"))
        assert image_bytes is None
        assert "远端队列中找不到本次 prompt_id" in error
        assert meta.get("queue_prompt_missing") is True

    def test_connection_drop_surfaces_request_error(self, fake_comfyui, workflow_copy, client_factory):
        """connection drop on /prompt must be retried once then reported."""
        fake_comfyui.set_mode("connection_drop")
        api = client_factory(fake_comfyui, workflow_copy, {})
        image_bytes, error, meta = run(api.generate("drop test"))
        assert image_bytes is None
        assert error and "请求报错" in error
        assert meta.get("queue_submit_retry"), "client must record the transient error"

    def test_execution_error_surfaced_from_history(self, fake_comfyui, workflow_copy, client_factory):
        fake_comfyui.set_mode("exec_error")
        api = client_factory(fake_comfyui, workflow_copy, {})
        image_bytes, error, meta = run(api.generate("exec error test"))
        assert image_bytes is None
        assert "ComfyUI 工作流执行失败" in error
        assert "CUDA out of memory" in error
        assert meta.get("execution_error")

    def test_history_poll_transient_failure_recorded(self, fake_comfyui, workflow_copy, client_factory):
        """401 during polling must be recorded and retried, not crash."""
        api = client_factory(fake_comfyui, workflow_copy, {"lora_settings": {"poll_attempts": 3, "poll_interval": 0.2}})
        import aiohttp

        # queue a task, then flip the server to 401 before polling starts
        async def _queue_then_flip():
            async with api._client_session(aiohttp.ClientTimeout(total=10)) as session:
                prompt_id, err = await api._queue_workflow(session, api._load_workflow(), "123456", {})
                assert not err, err
                fake_comfyui.set_mode("auth_401")
                meta = {}
                history = await api._fetch_prompt_history(session, prompt_id, meta)
                assert history is None
                assert meta["history_last_status"] == 401
                # transient status failures do NOT increment the exception counter
                assert "history_poll_failures" not in meta

        run(_queue_then_flip())


# ---------------------------------------------------------------------------
# error codes (user-facing)
# ---------------------------------------------------------------------------
class TestErrorCodes:
    def test_unauthorized_401_surfaces(self, fake_comfyui, workflow_copy, client_factory):
        fake_comfyui.set_mode("auth_401")
        api = client_factory(fake_comfyui, workflow_copy, {})
        image_bytes, error, meta = run(api.generate("401 test"))
        assert image_bytes is None
        assert "401" in error

    def test_missing_node_validation_400(self, fake_comfyui, workflow_copy, client_factory):
        fake_comfyui.set_mode("missing_node")
        api = client_factory(fake_comfyui, workflow_copy, {"lora_settings": {"enable_lora": True}})
        image_bytes, error, meta = run(api.generate("missing node test"))
        assert image_bytes is None
        assert "400" in error
        assert meta.get("lora_retry_neutralized") is True, "client should neutralize lora and retry once"

    def test_view_download_retries_then_fails(self, fake_comfyui, workflow_copy, client_factory):
        """Hard gate: view 500s -> client retries -> final error mentions download."""
        fake_comfyui.set_mode("view_500")
        api = client_factory(
            fake_comfyui,
            workflow_copy,
            {"lora_settings": {"view_download_retries": 2}},
        )
        import aiohttp

        async def _download():
            async with api._client_session(aiohttp.ClientTimeout(total=10)) as session:
                meta = {}
                img_info = {"filename": "x.png", "subfolder": "", "type": "output"}
                return await api._download_image_bytes(session, img_info, meta)

        data, error = run(_download())
        assert data is None
        assert "下载图片失败" in error
        assert "500" in error

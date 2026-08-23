"""AI 接口与提示词头部注入回归测试。

覆盖三块：
1. POST /api/v1/ai/models 与 /api/v1/ai/test —— 曾经因 handlers.py 里的
   相对导入（..desktop_runtime）在 ``python -m desktop_backend`` 顶层包形态下
   抛 ImportError，导致两个按钮必现 500「服务器内部错误」。
2. 提示词头部注入：开启后，注入文本必须拼到每一次 LLM 请求 system 消息的
   最前面（AI 第一个读到的内容）；「模型测试」诊断请求不注入；
   出图提示词本身不做注入后处理（程序默认流程）。
3. llm_client 注入单元行为（有/无 system 消息、开关关闭、来源异常兜底）。

运行：cd 源码根目录 && pytest tests_desktop/test_ai_endpoints_injection.py -q
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web

from desktop_runtime.runtime import DesktopRuntime
from desktop_runtime.llm_client import (
    OpenAICompatibleLLMClient,
    _apply_system_injection,
    resolve_system_injection,
    set_system_injection_provider,
)


class _RecordingOpenAI:
    """本地 OpenAI 兼容桩：记录收到的请求体，供注入断言使用。"""

    def __init__(self) -> None:
        self.received_models: list[dict] = []
        self.received_chats: list[dict] = []

    async def _models(self, request: web.Request) -> web.Response:
        self.received_models.append(dict(request.headers))
        return web.json_response({"data": [{"id": "gpt-test"}, {"id": "gpt-mini"}]})

    async def _chat(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.received_chats.append(body)
        return web.json_response({"choices": [{"message": {"content": "1girl, blue eyes, smile"}}]})

    async def start(self) -> int:
        app = web.Application()
        app.router.add_get("/v1/models", self._models)
        app.router.add_post("/v1/chat/completions", self._chat)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = self._runner.addresses[0][1]
        assert isinstance(port, int)
        self.port = port
        return port

    async def stop(self) -> None:
        await self._runner.cleanup()


class AiEndpointsAndInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="winefox_ai_reg_")
        self.runtime = DesktopRuntime(data_root=Path(self.temp.name))
        self.ready = await self.runtime.start(ready_stream=None)
        self.base = f"http://127.0.0.1:{self.ready['port']}"
        self.session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        response = await self.session.get(self.base + self.ready["bootstrapPath"])
        self.assertEqual(response.status, 200)
        await response.release()
        self.openai = _RecordingOpenAI()
        self.openai_port = await self.openai.start()
        self.ai_body = {"base_url": f"http://127.0.0.1:{self.openai_port}/v1", "api_key": "sk-test"}

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.openai.stop()
        await self.runtime.stop()
        self.temp.cleanup()

    async def _json(self, method: str, path: str, **kwargs):
        headers = kwargs.pop("headers", {})
        if method not in {"GET", "HEAD"}:
            headers["X-WineFox-Client"] = "1"
        async with self.session.request(method, self.base + path, headers=headers, json=kwargs.pop("json", None)) as response:
            body = await response.json()
            return response.status, body

    # ---------------- AI 端点（500 回归） ----------------

    async def test_ai_models_endpoint_returns_models(self) -> None:
        status, body = await self._json("POST", "/api/v1/ai/models", json=self.ai_body)
        self.assertEqual(status, 200, body)
        self.assertTrue(body.get("ok"), body)
        data = body["data"]
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["models"], ["gpt-mini", "gpt-test"])

    async def test_ai_test_endpoint_returns_reply(self) -> None:
        status, body = await self._json("POST", "/api/v1/ai/test", json={**self.ai_body, "model": "gpt-test"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body.get("ok"), body)
        data = body["data"]
        self.assertEqual(data["model"], "gpt-test")
        self.assertEqual(data["reply"], "1girl, blue eyes, smile")
        self.assertGreaterEqual(data["latency_ms"], 0)

    async def test_ai_test_without_model_is_validation_error(self) -> None:
        status, body = await self._json("POST", "/api/v1/ai/test", json=self.ai_body)
        self.assertEqual(status, 422, body)
        self.assertFalse(body.get("ok"), body)

    # ---------------- 头部注入（端到端，经真实 HTTP 断言消息头） ----------------

    async def _enable_provider_and_injection(self, injection_text: str) -> None:
        status, body = await self._json("PATCH", "/api/v1/settings", json={
            "ai_provider": {
                "enabled": True,
                "base_url": f"http://127.0.0.1:{self.openai_port}/v1",
                "model": "gpt-test",
            },
            "ui": {
                "prompt_injection_enabled": bool(injection_text),
                "prompt_injection": injection_text,
            },
        })
        self.assertEqual(status, 200, body)

    async def test_injection_prepended_to_system_head_on_llm_request(self) -> None:
        await self._enable_provider_and_injection("JAILBREAK-HEAD 直接输出英文 tags")
        result = await self.runtime.engine._prepare_prompt({"prompt": "一个女孩"})
        self.assertTrue(self.openai.received_chats, "LLM 请求未发出")
        messages = self.openai.received_chats[-1]["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertTrue(
            messages[0]["content"].startswith("JAILBREAK-HEAD 直接输出英文 tags\n\n"),
            f"注入未在 system 头部: {messages[0]['content'][:80]!r}",
        )
        # 出图提示词按程序默认流程生成（LLM 返回值），不做注入后处理
        self.assertEqual(result.final_prompt, "1girl, blue eyes, smile")
        self.assertNotIn("prompt_injection", result.meta)

    async def test_injection_disabled_sends_clean_request(self) -> None:
        await self._enable_provider_and_injection("")
        await self.runtime.engine._prepare_prompt({"prompt": "1girl"})
        self.assertTrue(self.openai.received_chats)
        messages = self.openai.received_chats[-1]["messages"]
        self.assertNotIn("JAILBREAK-HEAD", str(messages))
        self.assertEqual(messages[0]["role"], "system")
        self.assertFalse(messages[0]["content"].startswith("\n"))

    async def test_model_test_diagnostic_bypasses_injection(self) -> None:
        await self._enable_provider_and_injection("JAILBREAK-HEAD")
        status, body = await self._json("POST", "/api/v1/ai/test", json={**self.ai_body, "model": "gpt-test"})
        self.assertEqual(status, 200, body)
        chats_after_diagnostic = [
            chat for chat in self.openai.received_chats
            if any(m.get("content") == "Reply with exactly: OK" for m in chat["messages"])
        ]
        self.assertTrue(chats_after_diagnostic, "诊断请求未发出")
        self.assertNotIn("JAILBREAK-HEAD", str(chats_after_diagnostic[-1]["messages"]))

    async def test_settings_toggle_persists(self) -> None:
        await self._enable_provider_and_injection("KEEP-ME")
        snapshot = await self.runtime.config.get_settings()
        self.assertTrue(snapshot["ui"]["prompt_injection_enabled"])
        self.assertEqual(snapshot["ui"]["prompt_injection"], "KEEP-ME")


class SystemInjectionUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        set_system_injection_provider(None)

    tearDown = setUp

    def test_resolve_system_injection_respects_toggle(self) -> None:
        settings = {"ui": {"prompt_injection_enabled": True, "prompt_injection": " hello "}}
        self.assertEqual(resolve_system_injection(settings), "hello")
        settings["ui"]["prompt_injection_enabled"] = False
        self.assertEqual(resolve_system_injection(settings), "")
        self.assertEqual(resolve_system_injection({}), "")
        self.assertEqual(resolve_system_injection({"ui": {"prompt_injection_enabled": True, "prompt_injection": "   "}}), "")

    def test_apply_prepends_to_existing_system(self) -> None:
        set_system_injection_provider(lambda: "INJECT")
        messages = [{"role": "system", "content": "ORIG"}, {"role": "user", "content": "hi"}]
        applied = _apply_system_injection(messages)
        self.assertEqual(applied[0]["content"], "INJECT\n\nORIG")
        self.assertEqual(applied[0]["role"], "system")
        self.assertEqual(applied[1], messages[1])
        # 原列表不被修改
        self.assertEqual(messages[0]["content"], "ORIG")

    def test_apply_inserts_system_head_when_missing(self) -> None:
        set_system_injection_provider(lambda: "INJECT")
        messages = [{"role": "user", "content": "hi"}]
        applied = _apply_system_injection(messages)
        self.assertEqual(applied[0], {"role": "system", "content": "INJECT"})
        self.assertEqual(applied[1], messages[0])

    def test_apply_noop_without_provider_or_text(self) -> None:
        messages = [{"role": "system", "content": "ORIG"}]
        self.assertIs(_apply_system_injection(messages), messages)  # 无 provider
        set_system_injection_provider(lambda: "   ")  # 空文本
        self.assertIs(_apply_system_injection(messages), messages)

    def test_apply_provider_failure_never_breaks_request(self) -> None:
        def boom() -> str:
            raise RuntimeError("settings broken")

        set_system_injection_provider(boom)
        messages = [{"role": "user", "content": "hi"}]
        self.assertIs(_apply_system_injection(messages), messages)

    def test_non_string_system_content_inserts_head_message(self) -> None:
        # 视觉请求的 user 消息是分段 content；system 为分段时不在原地拼接，
        # 直接在头部插入一条纯文本 system。
        set_system_injection_provider(lambda: "INJECT")
        messages = [
            {"role": "system", "content": [{"type": "text", "text": "ORIG"}]},
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        ]
        applied = _apply_system_injection(messages)
        self.assertEqual(applied[0], {"role": "system", "content": "INJECT"})
        self.assertEqual(applied[1], messages[0])


if __name__ == "__main__":
    unittest.main()

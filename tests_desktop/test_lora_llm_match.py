# -*- coding: utf-8 -*-
"""LoraLlmMatchService 测试：LLM 匹配 / 降级 / 磁盘缓存 / 逐出 / 字段安全。

核心断言：无 LLM 配置时优雅降级为 None；LLM 返回垃圾/未知 key/低置信时
返回 None 而不抛错；缓存命中不重复调用 LLM；缓存超限逐出最旧；缓存与
LLM payload 只含公开安全字段。

运行：``python -m unittest tests_desktop.test_lora_llm_match -v``
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop_runtime.lora_llm_match import LoraLlmMatchService  # noqa: E402


class FakeConfig:
    def __init__(self, snapshot=None, secrets=None):
        self._snapshot = dict(snapshot or {})
        self._secrets = dict(secrets or {})

    def snapshot(self):
        return json.loads(json.dumps(self._snapshot))

    def get_secret(self, name: str) -> str:
        return self._secrets.get(name, "")


class FakeLlmClient:
    def __init__(self, responses=None, *args, **kwargs):
        self._responses = list(responses or [])
        self.calls: list[tuple] = []

    async def complete(self, messages, *, model, max_tokens=450, temperature=0.1):
        self.calls.append((model, messages, max_tokens, temperature))
        content = self._responses.pop(0) if self._responses else "{}"
        return {"choices": [{"message": {"content": content}}]}


def _enabled_config() -> FakeConfig:
    return FakeConfig(
        snapshot={
            "ai_provider": {
                "enabled": True,
                "base_url": "http://localhost:1234/v1",
                "model": "gpt-test",
                "timeout_seconds": 30,
            }
        },
        secrets={"ai_api_key": "test-secret"},
    )


def _candidate(key: str, character: str, **kwargs) -> dict:
    cand = {
        "key": key,
        "character": character,
        "name": character,
        "aliases": [character.lower().replace(" ", "_")],
        "trigger_words": ["1girl", character.lower().replace(" ", "_")],
        "tags": ["character"],
        "lora": f"{character.lower().replace(' ', '_')}.safetensors",
    }
    cand.update(kwargs)
    return cand


CANDIDATES = [
    _candidate("c1", "Yae Miko"),
    _candidate("c2", "Aemeath", aliases=["爱弥斯", "aemeath"]),
]

VALID_RESPONSE = json.dumps({
    "key": "c1",
    "confidence": 0.98,
    "matched_text": "yae miko",
    "reason": "explicit name",
})


class LoraLlmMatchBase(IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="lora_llm_match_")
        self.tmp = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)

    def _make_service(self, *, config=None, llm_factory=None, **kwargs) -> LoraLlmMatchService:
        return LoraLlmMatchService(
            config=config or FakeConfig(),
            data_root=self.tmp,
            llm_client_factory=llm_factory or (lambda *a, **k: FakeLlmClient()),
            **kwargs,
        )


class LlmEnabled(LoraLlmMatchBase):
    def test_disabled_without_config(self):
        service = self._make_service()
        self.assertFalse(service.llm_enabled())

    def test_enabled_with_config(self):
        service = self._make_service(config=_enabled_config())
        self.assertTrue(service.llm_enabled())


class MatchBehaviour(LoraLlmMatchBase):
    async def test_match_returns_candidate(self):
        fake = FakeLlmClient(responses=[VALID_RESPONSE])
        service = self._make_service(config=_enabled_config(), llm_factory=lambda *a, **k: fake)
        result = await service.match(prompt="yae miko sitting in a cafe", candidates=CANDIDATES)
        self.assertIs(result["matched"], CANDIDATES[0])
        self.assertFalse(result["used_cache"])
        self.assertAlmostEqual(result["confidence"], 0.98)
        self.assertTrue(result["reason"])

    async def test_match_no_config_degrades(self):
        service = self._make_service()
        result = await service.match(prompt="yae miko", candidates=CANDIDATES)
        self.assertIsNone(result["matched"])
        self.assertFalse(result["used_cache"])
        self.assertIn("LLM", result["reason"])

    async def test_match_garbage_llm_degrades_none(self):
        fake = FakeLlmClient(responses=["this is not json", "still not json"])
        service = self._make_service(config=_enabled_config(), llm_factory=lambda *a, **k: fake)
        result = await service.match(prompt="yae miko", candidates=CANDIDATES)
        self.assertIsNone(result["matched"])
        self.assertFalse(result["used_cache"])

    async def test_match_unknown_key_degrades_none(self):
        fake = FakeLlmClient(responses=[json.dumps({
            "key": "ghost", "confidence": 0.99, "matched_text": "yae miko", "reason": "x",
        })])
        service = self._make_service(config=_enabled_config(), llm_factory=lambda *a, **k: fake)
        result = await service.match(prompt="yae miko", candidates=CANDIDATES)
        self.assertIsNone(result["matched"])

    async def test_match_low_confidence_degrades_none(self):
        fake = FakeLlmClient(responses=[json.dumps({
            "key": "c1", "confidence": 0.5, "matched_text": "yae miko", "reason": "weak",
        })])
        service = self._make_service(config=_enabled_config(), llm_factory=lambda *a, **k: fake)
        result = await service.match(prompt="yae miko", candidates=CANDIDATES)
        self.assertIsNone(result["matched"])

    async def test_match_text_not_in_prompt_rejected(self):
        fake = FakeLlmClient(responses=[json.dumps({
            "key": "c2", "confidence": 0.99, "matched_text": "aemeath", "reason": "x",
        })])
        service = self._make_service(config=_enabled_config(), llm_factory=lambda *a, **k: fake)
        result = await service.match(prompt="yae miko in a cafe", candidates=CANDIDATES)
        self.assertIsNone(result["matched"])

    async def test_match_empty_prompt_degrades_none(self):
        fake = FakeLlmClient(responses=[json.dumps({
            "key": "c1", "confidence": 0.99, "matched_text": "yae miko", "reason": "x",
        })])
        service = self._make_service(config=_enabled_config(), llm_factory=lambda *a, **k: fake)
        result = await service.match(prompt="", candidates=CANDIDATES)
        self.assertIsNone(result["matched"])


class MatchCache(LoraLlmMatchBase):
    async def test_cache_hit_skips_llm(self):
        fake = FakeLlmClient(responses=[VALID_RESPONSE])
        service = self._make_service(config=_enabled_config(), llm_factory=lambda *a, **k: fake)
        first = await service.match(prompt="yae miko in a cafe", candidates=CANDIDATES)
        self.assertIs(first["matched"], CANDIDATES[0])
        calls_after_first = len(fake.calls)
        second = await service.match(prompt="yae miko in a cafe", candidates=CANDIDATES)
        self.assertTrue(second["used_cache"])
        self.assertIs(second["matched"], CANDIDATES[0])
        self.assertAlmostEqual(second["confidence"], 0.98)
        self.assertEqual(len(fake.calls), calls_after_first)

    async def test_revision_change_invalidates_cache(self):
        fake = FakeLlmClient(responses=[VALID_RESPONSE, VALID_RESPONSE])
        service = self._make_service(config=_enabled_config(), llm_factory=lambda *a, **k: fake)
        await service.match(prompt="yae miko in a cafe", candidates=CANDIDATES, revision=1)
        second = await service.match(
            prompt="yae miko in a cafe", candidates=CANDIDATES, revision=2
        )
        self.assertFalse(second["used_cache"])
        self.assertEqual(len(fake.calls), 2)

    async def test_cache_eviction(self):
        fake = FakeLlmClient(responses=[VALID_RESPONSE])
        service = self._make_service(
            config=_enabled_config(), llm_factory=lambda *a, **k: fake, max_cache_entries=2
        )
        for i in range(5):
            await service.match(prompt=f"yae miko request {i}", candidates=CANDIDATES)
        files = list((self.tmp / "lora_llm_cache").glob("*.json"))
        self.assertLessEqual(len(files), 2)

    async def test_cache_survives_restart(self):
        fake = FakeLlmClient(responses=[VALID_RESPONSE])
        service = self._make_service(config=_enabled_config(), llm_factory=lambda *a, **k: fake)
        await service.match(prompt="yae miko in a cafe", candidates=CANDIDATES)
        service2 = self._make_service(
            config=_enabled_config(), llm_factory=lambda *a, **k: FakeLlmClient()
        )
        result = await service2.match(prompt="yae miko in a cafe", candidates=CANDIDATES)
        self.assertTrue(result["used_cache"])
        self.assertIsNotNone(result["matched"])


class FieldSafety(LoraLlmMatchBase):
    async def test_cache_and_payload_keep_public_safe_fields(self):
        fake = FakeLlmClient(responses=[VALID_RESPONSE])
        service = self._make_service(config=_enabled_config(), llm_factory=lambda *a, **k: fake)
        dirty = _candidate(
            "c1", "Yae Miko", aliases=["https://evil.example.com/x", "token=abc123"]
        )
        await service.match(prompt="yae miko in a cafe", candidates=[dirty])
        for path in (self.tmp / "lora_llm_cache").glob("*.json"):
            blob = path.read_text(encoding="utf-8")
            self.assertNotIn("http", blob)
            self.assertNotIn("token=", blob)
            self.assertNotIn("evil", blob)
        payload_blob = json.dumps(fake.calls[0][1], ensure_ascii=False)
        self.assertNotIn("http", payload_blob)
        self.assertNotIn("token=", payload_blob)

    async def test_success_cache_does_not_store_sensitive_prompt(self):
        fake = FakeLlmClient(responses=[VALID_RESPONSE])
        service = self._make_service(config=_enabled_config(), llm_factory=lambda *a, **k: fake)
        await service.match(
            prompt="yae miko token=super-secret C:\\private\\prompt.txt",
            candidates=CANDIDATES,
        )
        blobs = [
            path.read_text(encoding="utf-8")
            for path in (self.tmp / "lora_llm_cache").glob("*.json")
        ]
        self.assertTrue(blobs)
        self.assertNotIn("super-secret", "".join(blobs))
        self.assertNotIn("C:\\\\private", "".join(blobs))


if __name__ == "__main__":
    unittest.main()

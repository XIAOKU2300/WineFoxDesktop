# -*- coding: utf-8 -*-
"""LoraSearchService 编排测试：公开安全字段 / 预览注入 / 过滤 / 评分 / LLM 回退。

核心断言：搜索结果绝不携带真实 URL / 磁盘路径 / 凭据；预览图只暴露
一次性 token；无 LLM 配置时自动跳过 LLM 扩展并正常返回。

运行：``python -m unittest tests_desktop.test_lora_search -v``
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

from desktop_runtime.lora_search import LoraSearchService  # noqa: E402


class FakeConfig:
    def __init__(self, snapshot=None, secrets=None):
        self._snapshot = dict(snapshot or {})
        self._secrets = dict(secrets or {})

    def snapshot(self):
        return json.loads(json.dumps(self._snapshot))

    def get_secret(self, name: str) -> str:
        return self._secrets.get(name, "")


class FakeCivitaiClient:
    def __init__(self, results=None, **kwargs):
        self.calls: list[tuple] = []
        self.results = list(results or [])

    async def search_loras(self, query: str, limit: int = 10, base_models=None):
        self.calls.append((query, limit, tuple(base_models) if base_models else None))
        return list(self.results)


class FakePreview:
    def __init__(self):
        self.registered: list[tuple[str, str, str]] = []

    def register(self, token: str, url: str, source: str = "") -> str:
        token = token or ("b" * 24)
        self.registered.append((token, url, source))
        return token


def _item(name: str, **kwargs) -> dict:
    item = {
        "name": name,
        "version_name": "v1.0",
        "type": "lora",
        "base_model": "Illustrious XL",
        "model_id": "1001",
        "version_id": "2001",
        "page_url": "https://civitai.com/models/1001?modelVersionId=2001",
        "trained_words": ["character_yae", "sakura"],
        "tags": ["character", "genshin_impact"],
        "download_count": 5000,
        "heart_count": 800,
        "nsfw": False,
        "preview_url": "https://image.civitai.com/example_thumb.png",
        "source": "civitai",
    }
    item.update(kwargs)
    return item


class LoraSearchBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="lora_search_")
        self.tmp = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)

    def _make_service(self, *, config=None, client=None, preview=None, **kwargs) -> LoraSearchService:
        return LoraSearchService(
            config=config or FakeConfig(),
            data_root=self.tmp,
            client_factory=lambda **kw: client or FakeCivitaiClient(),
            preview_service=preview,
            **kwargs,
        )


class SearchPublicSafety(IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="lora_search_")
        self.tmp = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)

    def _make_service(self, *, config=None, client=None, preview=None) -> LoraSearchService:
        return LoraSearchService(
            config=config or FakeConfig(),
            data_root=self.tmp,
            client_factory=lambda **kw: client or FakeCivitaiClient(),
            preview_service=preview,
        )

    async def test_empty_query_returns_empty(self):
        service = self._make_service()
        result = await service.search("")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["query"], "")

    async def test_search_returns_public_safe_items(self):
        service = self._make_service(
            client=FakeCivitaiClient(results=[_item("Yae Miko Genshin Impact")]),
        )
        result = await service.search("yae miko genshin impact", sources=["civitai"])
        self.assertEqual(result["family"], "illustrious")
        self.assertGreaterEqual(len(result["items"]), 1)
        item = result["items"][0]
        # 公开安全字段白名单：绝不允许出现真实 URL / 路径 / 凭据
        for key in ("page_url", "preview_url", "download_url", "files", "hashes"):
            self.assertNotIn(key, item)
        blob = json.dumps(item, ensure_ascii=False)
        self.assertNotIn("civitai.com", blob)
        self.assertNotIn("http://", blob)
        self.assertNotIn("https://", blob)
        self.assertNotIn("token=", blob)
        for field in ("id", "source", "model_id", "version_id", "name", "score", "relevance"):
            self.assertIn(field, item)
        self.assertGreater(item["score"], 0)
        self.assertGreater(item["relevance"], 0)

    async def test_search_with_preview_service_exposes_token_not_url(self):
        preview = FakePreview()
        service = self._make_service(
            client=FakeCivitaiClient(results=[_item("Yae Miko Genshin Impact")]),
            preview=preview,
        )
        result = await service.search("yae miko genshin impact", sources=["civitai"])
        items = [x for x in result["items"] if x["preview_token"]]
        self.assertTrue(items, "有预览图的候选应生成预览 token")
        blob = json.dumps(result["items"], ensure_ascii=False)
        self.assertNotIn("image.civitai.com", blob)
        self.assertTrue(all(preview.registered), "预览 URL 应被登记进预览服务")

    async def test_search_without_preview_service_has_empty_token(self):
        service = self._make_service(
            client=FakeCivitaiClient(results=[_item("Yae Miko Genshin Impact")]),
        )
        result = await service.search("yae miko genshin impact", sources=["civitai"])
        for item in result["items"]:
            self.assertEqual(item["preview_token"], "")


class SearchFiltering(IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="lora_search_")
        self.tmp = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)

    def _make_service(self, *, client=None) -> LoraSearchService:
        return LoraSearchService(
            config=FakeConfig(),
            data_root=self.tmp,
            client_factory=lambda **kw: client or FakeCivitaiClient(),
        )

    async def test_base_model_incompatible_filtered(self):
        service = self._make_service(
            client=FakeCivitaiClient(results=[_item("Anima Only Character", base_model="Anima v2")]),
        )
        result = await service.search("anima character", sources=["civitai"])
        self.assertEqual(result["items"], [])

    async def test_controlnet_blocked(self):
        service = self._make_service(
            client=FakeCivitaiClient(results=[_item("ControlNet Depth LoRA")]),
        )
        result = await service.search("controlnet", sources=["civitai"])
        self.assertEqual(result["items"], [])

    async def test_non_lora_type_filtered(self):
        service = self._make_service(
            client=FakeCivitaiClient(results=[_item("Some Checkpoint", type="checkpoint")]),
        )
        result = await service.search("some checkpoint", sources=["civitai"])
        self.assertEqual(result["items"], [])


class SearchParsing(LoraSearchBase):
    def test_red_source_does_not_receive_civitai_credentials(self):
        captured = {}

        def red_factory(**kwargs):
            captured.update(kwargs)
            return FakeCivitaiClient()

        service = self._make_service(
            config=FakeConfig(
                secrets={
                    "civitai_api_token": "civitai-secret",
                    "civitai_cookie": "cookie-secret",
                }
            ),
            red_client_factory=red_factory,
        )
        service._source_client("civitai_red")
        self.assertEqual(captured.get("api_token"), "")
        self.assertEqual(captured.get("cookie"), "")

    def test_normalize_term(self):
        service = self._make_service()
        self.assertEqual(service._normalize_civitai_search_term("  【Yae Miko】  "), "Yae Miko")
        self.assertEqual(service._normalize_civitai_search_term("角色:Yae Miko"), "Yae Miko")
        self.assertEqual(service._normalize_civitai_search_term("https://civitai.com/models/1"), "")
        self.assertEqual(service._normalize_civitai_search_term(""), "")

    def test_parse_sources(self):
        service = self._make_service()
        self.assertEqual(service._parse_lora_search_sources(["civitai", "red"]), ["civitai", "civitai_red"])
        self.assertEqual(service._parse_lora_search_sources("civitai,hf"), ["civitai", "huggingface"])
        self.assertEqual(service._parse_lora_search_sources(["nope", ""]), ["civitai"])

    def test_spelling_variants(self):
        service = self._make_service()
        # 忠实移植：紧凑/大写变体 normalize 后与原词同 key，被过滤为空
        self.assertEqual(service._lora_search_spelling_variants("yae miko"), [])
        self.assertEqual(service._lora_search_spelling_variants("中文角色"), [])

    def test_popularity_score_formula(self):
        service = self._make_service()
        base = service._civitai_popularity_score(800, 5000, relevance=1, source_index=0)
        self.assertGreater(service._civitai_popularity_score(800, 5000, relevance=6, source_index=0), base)
        self.assertLess(service._civitai_popularity_score(800, 5000, relevance=1, source_index=5), base)


class SearchNoLLM(IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="lora_search_")
        self.tmp = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)

    def _make_service(self, *, client=None) -> LoraSearchService:
        return LoraSearchService(
            config=FakeConfig(),
            data_root=self.tmp,
            client_factory=lambda **kw: client or FakeCivitaiClient(),
        )

    async def test_llm_unavailable_returns_results(self):
        service = self._make_service(
            client=FakeCivitaiClient(results=[_item("Yae Miko Genshin Impact")]),
        )
        self.assertFalse(service._llm_enabled())
        result = await service.search("yae miko genshin impact", sources=["civitai"])
        self.assertGreaterEqual(len(result["items"]), 1)
        self.assertIn("searched_terms", result)
        self.assertIn("errors", result)


if __name__ == "__main__":
    unittest.main()

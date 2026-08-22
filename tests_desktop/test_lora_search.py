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
import time
import unittest
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop_runtime.lora_search import LoraSearchService  # noqa: E402
import civitai_api  # noqa: E402
from civitai_api import CivitaiClient  # noqa: E402


class ProxyCircuitPolicy(unittest.TestCase):
    def setUp(self):
        civitai_api._PROXY_CIRCUITS.clear()

    def tearDown(self):
        civitai_api._PROXY_CIRCUITS.clear()

    def test_proxy_setting_accepts_host_port(self):
        client = CivitaiClient(proxy="127.0.0.1:7890")
        self.assertEqual(client.proxy, "http://127.0.0.1:7890")

    def test_circuit_is_source_scoped_and_requires_repeated_failures(self):
        civitai = CivitaiClient(proxy="http://127.0.0.1:7890", source_key="civitai")
        huggingface = CivitaiClient(proxy="http://127.0.0.1:7890", source_key="huggingface")
        error = OSError("temporary connect failure")

        civitai._mark_proxy_failure(error)
        civitai._mark_proxy_failure(error)
        self.assertEqual(civitai._proxy_circuit_block_message(), "")
        self.assertEqual(huggingface._proxy_circuit_block_message(), "")

        civitai._mark_proxy_failure(error)
        self.assertIn("已暂时熔断", civitai._proxy_circuit_block_message())
        self.assertEqual(huggingface._proxy_circuit_block_message(), "")


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


class TermAwareCivitaiClient(FakeCivitaiClient):
    def __init__(self, results_by_term=None, slow_terms=None, **kwargs):
        super().__init__(**kwargs)
        self.results_by_term = dict(results_by_term or {})
        self.slow_terms = set(slow_terms or [])

    async def search_loras(self, query: str, limit: int = 10, base_models=None):
        self.calls.append((query, limit, tuple(base_models) if base_models else None))
        if query in self.slow_terms:
            import asyncio
            await asyncio.sleep(10)
        return list(self.results_by_term.get(query) or [])


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

    async def test_civitai_public_search_config_skips_slow_script_discovery(self):
        client = CivitaiClient()
        page_requests = []

        async def unexpected_page_request(url):
            page_requests.append(url)
            raise AssertionError("公开兜底配置可用时不应先抓取搜索页脚本")

        client._get_text = unexpected_page_request
        config = await client._get_web_search_config()
        self.assertEqual(config["source"], "fallback")
        self.assertTrue(config["host"].startswith("https://"))
        self.assertEqual(page_requests, [])

    async def test_civitai_cjk_search_prefers_web_channel_without_rest_fanout(self):
        client = CivitaiClient()

        async def web(*args, **kwargs):
            return [
                {"version_id": "1", "name": "web-first"},
                {"version_id": "2", "name": "web-only"},
            ]

        rest_calls = []

        async def rest(*args, **kwargs):
            rest_calls.append((args, kwargs))
            return [
                {"version_id": "1", "name": "rest-duplicate"},
                {"version_id": "3", "name": "rest-only"},
            ]

        client.search_loras_via_web = web
        client._search_loras_rest_api = rest
        items = await client.search_loras("维里奈", limit=5, base_models=["Illustrious"])
        self.assertEqual([item["name"] for item in items], ["web-first", "web-only"])
        self.assertEqual(rest_calls, [])

    async def test_civitai_cjk_search_keeps_rest_result_when_web_times_out(self):
        client = CivitaiClient()

        async def slow_web(*args, **kwargs):
            import asyncio
            await asyncio.sleep(10)
            return []

        async def fast_rest(*args, **kwargs):
            return [{"version_id": "3", "name": "rest-result"}]

        client.search_loras_via_web = slow_web
        client._search_loras_rest_api = fast_rest
        client._combined_search_timeout = lambda: 0.03
        items = await client.search_loras("维里奈", limit=5, base_models=["Illustrious"])
        self.assertEqual([item["name"] for item in items], ["rest-result"])

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

    async def test_search_pages_keep_twenty_items_and_web_total(self):
        raw = []
        for index in range(45):
            value = _item(
                f"Endfield LoRA {index}",
                model_id=str(1000 + index),
                version_id=str(2000 + index),
                page_url=f"https://civitai.com/models/{1000 + index}?modelVersionId={2000 + index}",
                _web_search=True,
                _web_search_rank=index,
                _search_total=45,
            )
            raw.append(value)
        service = self._make_service(client=FakeCivitaiClient(results=raw))
        first = await service.search("endfield", sources=["civitai"], page=1, limit=20)
        second = await service.search("endfield", sources=["civitai"], page=2, limit=20)
        third = await service.search("endfield", sources=["civitai"], page=3, limit=20)
        self.assertEqual(len(first["items"]), 20)
        self.assertEqual(len(second["items"]), 20)
        self.assertEqual(len(third["items"]), 5)
        self.assertEqual(first["total"], 45)
        self.assertTrue(first["has_more"])
        self.assertTrue(second["has_more"])
        self.assertFalse(third["has_more"])
        self.assertNotEqual(first["items"][0]["version_id"], second["items"][0]["version_id"])

    async def test_filtered_short_page_keeps_next_page_when_remote_total_is_larger(self):
        raw = []
        for index in range(17):
            raw.append(_item(
                f"Endfield filtered {index}",
                model_id=str(3000 + index),
                version_id=str(4000 + index),
                page_url=f"https://civitai.com/models/{3000 + index}?modelVersionId={4000 + index}",
                _web_search=True,
                _web_search_rank=index,
                _search_total=850,
            ))
        service = self._make_service(client=FakeCivitaiClient(results=raw))
        result = await service.search("endfield", sources=["civitai"], page=1, limit=20)
        self.assertEqual(len(result["items"]), 17)
        self.assertEqual(result["total"], 850)
        self.assertTrue(result["has_more"])


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

    async def test_profile_seed_searches_cjk_and_official_name_in_parallel(self):
        client = TermAwareCivitaiClient({
            "Verina": [_item("Verina | Wuthering Waves")],
        })
        service = LoraSearchService(
            config=FakeConfig(),
            data_root=self.tmp,
            client_factory=lambda **kw: client,
            profile_seed_fn=lambda query, family: ["Verina"] if query == "维里奈" else [],
        )
        result = await service.search("维里奈", sources=["civitai"])
        called_terms = [call[0] for call in client.calls]
        self.assertIn("维里奈", called_terms)
        self.assertIn("Verina", called_terms)
        self.assertIn("Verina", result["searched_terms"])
        self.assertTrue(any("Verina" in item["name"] for item in result["items"]))

    async def test_red_failure_does_not_hide_normal_civitai_results(self):
        class FailingRedClient(FakeCivitaiClient):
            async def search_loras(self, query: str, limit: int = 10, base_models=None):
                raise RuntimeError("red mirror unavailable")

        normal = FakeCivitaiClient(results=[_item("Endfield public result")])
        service = LoraSearchService(
            config=FakeConfig(),
            data_root=self.tmp,
            client_factory=lambda **kw: normal,
            red_client_factory=lambda **kw: FailingRedClient(),
        )
        result = await service.search("endfield", sources=["civitai", "civitai_red"])
        self.assertTrue(result["items"])
        self.assertTrue(any("Civitai Red" in error for error in result["errors"]))

    async def test_slow_cjk_term_times_out_without_losing_fast_alias_result(self):
        client = TermAwareCivitaiClient(
            {"Verina": [_item("Verina | Wuthering Waves")]},
            slow_terms={"维里奈"},
        )
        service = LoraSearchService(
            config=FakeConfig(),
            data_root=self.tmp,
            client_factory=lambda **kw: client,
            profile_seed_fn=lambda query, family: ["Verina"],
        )
        service._per_source_search_timeout = lambda: 0.03
        started = time.monotonic()
        result = await service.search("维里奈", sources=["civitai"])
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertTrue(result["items"])
        self.assertTrue(any("搜索超时（0.03 秒）" in error for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""LoraDownloadService（Phase 3 重写）测试：搜索委托 / 入队幂等 /
完整本地安装状态机 / 哈希与文件名安全。

运行：``python -m unittest tests_desktop.test_lora_download_service -v``
"""

from __future__ import annotations

import asyncio
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop_backend.errors import AppError  # noqa: E402
from desktop_runtime.lora_download import LoraDownloadService  # noqa: E402
from winefox_core.storage import (  # noqa: E402
    Database,
    LoraAssetStore,
    LoraDownloadJobStore,
    LoraProfileStore,
)
from winefox_core.storage_models import LoraDownloadState  # noqa: E402


class FakeConfig:
    def __init__(self, snapshot=None, secrets=None):
        self._snapshot = dict(snapshot or {})
        self._secrets = dict(secrets or {})

    def snapshot(self):
        return json.loads(json.dumps(self._snapshot))

    def get_secret(self, name: str) -> str:
        return self._secrets.get(name, "")


DEFAULT_SUMMARY = {
    "model_name": "Test Character LoRA",
    "version_name": "v1.0",
    "file_name": "test_character_lora.safetensors",
    "download_url": "https://civitai.com/api/download/models/123",
    "trained_words": ["test_character", "test_character2"],
    "model_id": 456,
    "version_id": 123,
    "base_model": "Illustrious XL",
    "page_url": "https://civitai.com/models/456?modelVersionId=123",
    "source": "civitai",
    "search_terms": ["test character"],
    "search_aliases": ["Test Character"],
}


def _safetensors_bytes(size: int = 2048) -> bytes:
    header = json.dumps({
        "weight": {
            "dtype": "U8",
            "shape": [size],
            "data_offsets": [0, size],
        }
    }, separators=(",", ":")).encode("utf-8")
    return struct.pack("<Q", len(header)) + header + (b"\x00" * size)


class FakeClient:
    def __init__(self, summary=None, version_files=None, search_results=None):
        self.summary = dict(summary or DEFAULT_SUMMARY)
        self.version_files = list(version_files or [])
        self.search_results = list(search_results or [])
        self.downloaded: list[Path] = []

    def _headers(self):
        return {}

    async def search_loras(self, query: str, limit: int = 10, base_models=None):
        return list(self.search_results)

    async def resolve_model(self, ref: str, base_models=None):
        return dict(self.summary)

    async def download_file(self, url: str, dest_path: Path, progress_cb=None):
        dest_path.write_bytes(_safetensors_bytes())
        self.downloaded.append(dest_path)
        return dest_path

    async def get_model_version(self, version_id: int):
        return {"files": list(self.version_files)}


class FakePreviewService:
    def __init__(self):
        self.registered: list[tuple[str, str, str]] = []

    def register(self, token: str, url: str, source: str = "") -> str | None:
        token = token or ("a" * 24)
        self.registered.append((token, url, str(source or "")))
        return token

    async def fetch(self, token: str):
        return None


class FakeSearchService:
    def __init__(self, items, **extra):
        self.items = list(items)
        self.extra = dict(extra)

    async def search(self, query: str, *, family: str = "illustrious", limit: int = 12, sources: list[str] | None = None):
        return {"query": query, "family": family, "items": list(self.items), **self.extra}


class LoraDownloadBase(IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="lora_dl_")
        self.tmp = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)
        self.db = Database(self.tmp / "winefox.db")
        self.db.initialize()
        self.addCleanup(self.db.close)
        self.jobs = LoraDownloadJobStore(self.db)
        self.assets = LoraAssetStore(self.db)

    def _make_service(self, *, client=None, search_factory=None, settings=None, preview=None) -> LoraDownloadService:
        settings = settings or {"lora_download": {"mode": "local", "local_dir": str(self.tmp / "models" / "loras")}}
        return LoraDownloadService(
            db=self.db,
            config=FakeConfig(snapshot=settings),
            data_root=self.tmp,
            connection_factory=lambda: None,
            on_installed=None,
            client_factory=lambda **kw: client or FakeClient(),
            search_service_factory=search_factory,
            preview_service=preview,
        )


class DownloadSearch(LoraDownloadBase):
    async def test_search_delegates_and_caches_result_ids(self):
        item = {
            "id": "a" * 32,
            "source": "civitai",
            "family": "illustrious",
            "model_id": "456",
            "version_id": "123",
            "name": "Test Character LoRA",
        }
        svc = self._make_service(search_factory=lambda **kw: FakeSearchService([item]))
        result = await svc.search("test character", family="illustrious", limit=5)
        self.assertEqual(len(result["items"]), 1)
        self.assertIn(item["id"], svc._search_cache)

    async def test_search_preserves_terms_alias_and_partial_source_errors(self):
        service = FakeSearchService(
            [],
            searched_terms=["维里奈", "Verina"],
            alias="Verina",
            errors=["Civitai/维里奈: 搜索超时（15 秒）"],
        )
        svc = self._make_service(search_factory=lambda **kw: service)
        result = await svc.search("维里奈", family="illustrious")
        self.assertEqual(result["searched_terms"], ["维里奈", "Verina"])
        self.assertEqual(result["alias"], "Verina")
        self.assertIn("搜索超时", result["errors"][0])

    async def test_search_fallback_when_service_unavailable(self):
        def broken_factory(**kw):
            raise RuntimeError("search service broken")

        client = FakeClient(search_results=[
            {"model_id": "456", "version_id": "123", "name": "Test Character LoRA",
             "base_model": "Illustrious XL", "download_count": 1, "heart_count": 1,
             "page_url": "https://civitai.com/models/456?modelVersionId=123",
             "trained_words": [], "tags": [], "nsfw": False},
        ])
        svc = self._make_service(client=client, search_factory=broken_factory)
        result = await svc.search("test character")
        self.assertEqual(len(result["items"]), 1)
        self.assertIn("civitai", str(result["items"][0]["source"]))

    async def test_search_invalid_query(self):
        svc = self._make_service()
        with self.assertRaises(AppError):
            await svc.search("")

    async def test_search_unknown_family(self):
        svc = self._make_service()
        with self.assertRaises(AppError):
            await svc.search("test", family="unknown_family")


class DownloadStart(LoraDownloadBase):
    async def test_start_download_requires_cached_result(self):
        svc = self._make_service(client=FakeClient())
        with self.assertRaises(AppError) as ctx:
            svc.start_download("ffffffffffffffffffffffffffffffff")
        self.assertEqual(ctx.exception.status, 404)

    async def test_start_download_nsfw_requires_consent(self):
        client = FakeClient()
        svc = self._make_service(client=client)
        result_id = "a" * 32
        svc._cache_result(result_id, family="illustrious", source="civitai",
                          model_id="456", version_id="123", query="test", nsfw=True)
        with self.assertRaises(AppError) as ctx:
            svc.start_download(result_id)
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, "LORA_R18_REQUIRES_CONSENT")
        # 提供 adult_consent 后放行
        svc.start_download(result_id, adult_consent=True)
        await svc.stop()

    async def test_start_download_sfw_no_consent_needed(self):
        client = FakeClient()
        svc = self._make_service(client=client)
        result_id = "a" * 32
        svc._cache_result(result_id, family="illustrious", source="civitai",
                          model_id="456", version_id="123", query="test", nsfw=False)
        snapshot = svc.start_download(result_id)
        self.assertEqual(snapshot["status"], "queued")
        await svc.stop()

    async def test_start_download_enqueues_and_requeues_terminal(self):
        client = FakeClient()
        svc = self._make_service(client=client)
        result_id = "a" * 32
        svc._cache_result(result_id, family="illustrious", source="civitai",
                          model_id="456", version_id="123", query="test")
        snapshot = svc.start_download(result_id, alias="测试角色")
        self.assertEqual(snapshot["status"], "queued")
        # 幂等：任务进行中再次调用不重复入队
        again = svc.start_download(result_id)
        self.assertEqual(again["status"], "queued")
        await svc.stop()

    async def test_requeue_after_failed(self):
        client = FakeClient()
        svc = self._make_service(client=client)
        result_id = "b" * 32
        self.jobs.enqueue(result_id, family="illustrious", alias="", source="civitai",
                          source_ref={"model_id": "456", "version_id": "123"}, request_timeout=60)
        # 失败态（retryable）可 requeue 回 queued；completed 为终态不可重试
        self.jobs.update_state(result_id, LoraDownloadState.RESOLVING)
        self.jobs.update_state(result_id, LoraDownloadState.FAILED)
        self.jobs.requeue(result_id)
        self.assertEqual(self.jobs.get(result_id).state, LoraDownloadState.QUEUED.value)


class DownloadFullFlow(LoraDownloadBase):
    async def test_local_install_flow_completes(self):
        client = FakeClient()
        svc = self._make_service(client=client)
        result_id = "c" * 32
        self.jobs.enqueue(result_id, family="illustrious", alias="测试角色", source="civitai",
                          source_ref={"source": "civitai", "family": "illustrious",
                                      "model_id": "456", "version_id": "123"},
                          request_timeout=60)
        await svc._run_download(result_id, "测试角色")
        record = self.jobs.get(result_id)
        self.assertEqual(record.state, LoraDownloadState.COMPLETED.value)
        result = dict(record.result or {})
        self.assertEqual(result.get("mode"), "local")
        self.assertTrue(result.get("filename"))
        self.assertTrue(result.get("asset_id"))
        # 档案写入 SQLite（唯一真相源），不再写 JSON
        profile_records = LoraProfileStore(self.db).get_family("illustrious")
        self.assertTrue(profile_records)
        # 资产登记
        assets = self.assets.list()
        self.assertEqual(len(assets), 1)

    async def test_local_install_registers_preview_key(self):
        summary = dict(DEFAULT_SUMMARY)
        summary["preview_url"] = "https://image.civitai.com/test_thumb.png"
        preview = FakePreviewService()
        svc = self._make_service(client=FakeClient(summary=summary), preview=preview)
        result_id = "f" * 32
        self.jobs.enqueue(result_id, family="illustrious", alias="", source="civitai",
                          source_ref={"source": "civitai", "model_id": "456", "version_id": "123"},
                          request_timeout=60)
        await svc._run_download(result_id, "")
        assets = self.assets.list()
        self.assertEqual(len(assets), 1)
        record = self.assets.get_by_id(assets[0].id)
        # 资产带 preview_key（同源代理 token），预览服务收到登记且来源一致
        self.assertTrue(record.preview_key)
        self.assertEqual(len(preview.registered), 1)
        token, url, source = preview.registered[0]
        self.assertEqual(url, "https://image.civitai.com/test_thumb.png")
        self.assertEqual(token, record.preview_key)
        # 公开字段只暴露 token 不暴露 URL
        item = dict(record.info or {})
        self.assertNotIn("https:", json.dumps(item))

    async def test_local_install_skips_unsafe_preview(self):
        from desktop_runtime.lora_preview import LoraPreviewService

        summary = dict(DEFAULT_SUMMARY)
        summary["preview_url"] = "http://127.0.0.1/evil.png"
        svc = self._make_service(client=FakeClient(summary=summary),
                                 preview=LoraPreviewService(data_root=self.tmp))
        result_id = "g" * 32
        self.jobs.enqueue(result_id, family="illustrious", alias="", source="civitai",
                          source_ref={"source": "civitai", "model_id": "456", "version_id": "123"},
                          request_timeout=60)
        await svc._run_download(result_id, "")
        assets = self.assets.list()
        self.assertEqual(len(assets), 1)
        record = self.assets.get_by_id(assets[0].id)
        # 不安全预览 URL 不登记 token，资产预览保持空
        self.assertFalse(record.preview_key)

    async def test_hash_mismatch_fails(self):
        version_files = [{"name": "test_character_lora.safetensors", "hashes": {"SHA256": "ab" * 32}}]
        client = FakeClient(version_files=version_files)
        svc = self._make_service(client=client)
        result_id = "d" * 32
        self.jobs.enqueue(result_id, family="illustrious", alias="", source="civitai",
                          source_ref={"model_id": "456", "version_id": "123"}, request_timeout=60)
        await svc._run_download(result_id, "")
        record = self.jobs.get(result_id)
        self.assertEqual(record.state, LoraDownloadState.FAILED.value)
        self.assertIn("哈希", record.error)

    async def test_download_error_marks_failed(self):
        class BrokenClient(FakeClient):
            async def resolve_model(self, ref, base_models=None):
                raise RuntimeError("civitai unreachable")

        svc = self._make_service(client=BrokenClient())
        result_id = "e" * 32
        self.jobs.enqueue(result_id, family="illustrious", alias="", source="civitai",
                          source_ref={"model_id": "456", "version_id": "123"}, request_timeout=60)
        await svc._run_download(result_id, "")
        record = self.jobs.get(result_id)
        self.assertEqual(record.state, LoraDownloadState.FAILED.value)


class DownloadHelpers(LoraDownloadBase):
    def test_build_reference(self):
        ref = LoraDownloadService._build_reference({"model_id": "456", "version_id": "123"})
        self.assertEqual(ref, "https://civitai.com/models/456?modelVersionId=123")
        self.assertEqual(LoraDownloadService._build_reference({"model_id": "456"}), "456")
        self.assertEqual(LoraDownloadService._build_reference({}), "")

    def test_build_huggingface_reference_preserves_nested_file(self):
        ref = LoraDownloadService._build_reference({
            "source": "huggingface",
            "model_id": "owner/repo",
            "file_name": "loras/character/model.safetensors",
        })
        self.assertEqual(
            ref,
            "https://huggingface.co/owner/repo?file=loras%2Fcharacter%2Fmodel.safetensors",
        )
        self.assertEqual(
            LoraDownloadService._build_reference({
                "source": "huggingface",
                "model_id": "owner/repo",
                "file_name": "../secret.safetensors",
            }),
            "https://huggingface.co/owner/repo",
        )

    def test_civitai_red_client_never_receives_civitai_credentials(self):
        captured = {}

        def red_factory(**kwargs):
            captured.update(kwargs)
            return FakeClient()

        svc = LoraDownloadService(
            db=self.db,
            config=FakeConfig(
                snapshot={"lora_download": {}},
                secrets={
                    "civitai_api_token": "civitai-secret",
                    "civitai_cookie": "cookie-secret",
                },
            ),
            data_root=self.tmp,
            connection_factory=lambda: None,
            red_client_factory=red_factory,
        )
        svc._client("civitai_red")
        self.assertEqual(captured.get("api_token"), "")
        self.assertNotIn("cookie", captured)

    def test_save_sqlite_profile_does_not_overwrite_user_profile(self):
        store = LoraProfileStore(self.db)
        original = {
            "lora": "hero.safetensors",
            "character": "Hero",
            "aliases": ["用户别名"],
            "trigger_words": ["custom_trigger"],
        }
        store.save("illustrious", "Hero", original)
        svc = self._make_service()
        resolved = svc._save_sqlite_profile("illustrious", {
            "lora": "hero.safetensors",
            "character": "Hero",
            "aliases": ["remote alias"],
            "trigger_words": ["remote_trigger"],
        })
        self.assertEqual(resolved["aliases"], ["用户别名"])
        self.assertEqual(
            store.get("illustrious", "Hero").profile["trigger_words"],
            ["custom_trigger"],
        )

    def test_extract_sha256(self):
        self.assertEqual(
            LoraDownloadService._extract_sha256({"hashes": {"SHA256": "ABC123"}}),
            "abc123",
        )
        self.assertEqual(LoraDownloadService._extract_sha256({"hashes": {}}), "")

    def test_unique_local_target_conflict_renames(self):
        # _unique_local_target 是实例方法，但只用静态的 _local_target/_is_installed_file
        svc = LoraDownloadService.__new__(LoraDownloadService)
        local_dir = self.tmp / "conflict"
        local_dir.mkdir(parents=True)
        target, renamed = svc._unique_local_target(str(local_dir), "same.safetensors")
        self.assertFalse(renamed)
        self.assertEqual(target.name, "same.safetensors")
        target.write_bytes(b"\x00" * 2048)
        target2, renamed2 = svc._unique_local_target(str(local_dir), "same.safetensors")
        self.assertTrue(renamed2)
        self.assertEqual(target2.name, "same (1).safetensors")

    def test_unique_local_target_never_overwrites_small_existing_file(self):
        svc = LoraDownloadService.__new__(LoraDownloadService)
        local_dir = self.tmp / "small-conflict"
        local_dir.mkdir(parents=True)
        existing = local_dir / "same.safetensors"
        existing.write_bytes(b"x")
        target, renamed = svc._unique_local_target(str(local_dir), existing.name)
        self.assertTrue(renamed)
        self.assertEqual(target.name, "same (1).safetensors")
        self.assertEqual(existing.read_bytes(), b"x")

    def test_save_sqlite_profile_preserves_user_edited_profile(self):
        svc = self._make_service()
        store = LoraProfileStore(self.db)
        store.save("illustrious", "Mizuki", {
            "lora": "mizuki.safetensors",
            "character": "Mizuki",
            "aliases": ["用户别名"],
            "trigger_words": ["user_trigger"],
        })
        saved = svc._save_sqlite_profile("illustrious", {
            "lora": "mizuki.safetensors",
            "character": "Mizuki",
            "aliases": ["remote_alias"],
            "trigger_words": ["remote_trigger"],
        })
        self.assertEqual(saved["aliases"], ["用户别名"])
        record = store.get("illustrious", "Mizuki")
        self.assertEqual(record.profile["trigger_words"], ["user_trigger"])
        self.assertEqual(record.revision, 1)

    def test_local_target_sanitizes_filename(self):
        target = LoraDownloadService._local_target(str(self.tmp), "../evil?name.safetensors")
        self.assertTrue(str(target).startswith(str(self.tmp.resolve())))
        self.assertNotIn(".." + str(Path("/")), str(target))


if __name__ == "__main__":
    unittest.main()

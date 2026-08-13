# -*- coding: utf-8 -*-
"""LoraLibraryService 测试：列表/详情/编辑/删除/隔离/恢复/匹配预览。

运行：``rtk proxy python -m pytest -q tests_desktop/test_lora_library_service.py``
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop_backend.errors import AppError  # noqa: E402
from desktop_runtime.lora_library import LoraLibraryService  # noqa: E402
from winefox_core.lora_service import LoraService  # noqa: E402
from winefox_core.storage import Database, LoraAssetStore, LoraProfileStore  # noqa: E402


class LoraLibraryServiceBase(IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="lora_lib_svc_")
        self.tmp = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)
        self.db = Database(self.tmp / "winefox.db")
        self.db.initialize()
        self.addCleanup(self.db.close)
        self.asset_store = LoraAssetStore(self.db)
        self.profile_store = LoraProfileStore(self.db)
        self.loras_dir = self.tmp / "models" / "loras"
        self.loras_dir.mkdir(parents=True, exist_ok=True)
        self.svc = LoraLibraryService(
            db=self.db,
            data_root=self.tmp,
            asset_store=self.asset_store,
            profile_store=self.profile_store,
        )

    def _weight_file(self, filename: str, content: bytes = b"lora-weight-bytes") -> Path:
        path = self.loras_dir / filename
        path.write_bytes(content)
        return path

    def _register(self, filename, family="illustrious", display_name="", model_id=None, version_id=None):
        return self.asset_store.register(
            family, filename,
            comfy_key=filename,
            display_name=display_name or Path(filename).stem,
            source="local",
            model_id=model_id,
            version_id=version_id,
        )

    def _seed_profile(self, family, key, character, aliases=None, triggers=None, role=None, lora_name=""):
        profile = {
            "lora": lora_name or key,
            "character": character,
            "aliases": list(aliases or []),
            "trigger_words": list(triggers or []),
            "role_terms": list(role or []),
            "source": "local",
            "name": key,
        }
        self.profile_store.save(family, key, profile)
        return profile


class TestListGet(LoraLibraryServiceBase):
    def test_list_returns_public_safe_fields(self):
        self._weight_file("anmi_style.safetensors")
        self._register("anmi_style.safetensors", display_name="Anmi Style", model_id=101, version_id=7)
        self._seed_profile(
            "illustrious", "anmi style", "Anmi",
            aliases=["anmi", "anmi_style"], triggers=["anmi"], lora_name="anmi_style.safetensors",
        )
        items = self.svc.list()
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["id"], 1)
        self.assertEqual(item["family"], "illustrious")
        self.assertEqual(item["filename"], "anmi_style.safetensors")
        self.assertEqual(item["model_id"], 101)
        self.assertEqual(item["version_id"], 7)
        self.assertEqual(item["name"], "Anmi Style")
        self.assertEqual(item["character"], "Anmi")
        self.assertIn("anmi", item["aliases"])
        self.assertEqual(item["triggers"], ["anmi"])
        self.assertEqual(item["state"], "installed")
        self.assertFalse(item["disabled"])
        self.assertIsNone(item["quarantine_relpath"])
        # 公开安全：无 URL / token / 绝对路径
        blob = json.dumps(item, ensure_ascii=False)
        for forbidden in ("http", "token", "authorization", "cookie", "C:", ":\\\\", "\\\\"):
            self.assertNotIn(forbidden, blob)

    def test_list_filters_by_family(self):
        self._register("a.safetensors", family="illustrious")
        self._register("b.safetensors", family="anima")
        self.assertEqual(len(self.svc.list()), 2)
        self.assertEqual(len(self.svc.list(family="anima")), 1)
        self.assertEqual(self.svc.list(family="anima")[0]["filename"], "b.safetensors")

    def test_list_excludes_quarantined_by_default(self):
        self._weight_file("q.safetensors")
        rec = self._register("q.safetensors")
        self.svc.quarantine(rec.id, reason="测试隔离")
        self.assertEqual(len(self.svc.list()), 0)
        items = self.svc.list(include_quarantined=True)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["state"], "quarantined")

    def test_get_returns_single(self):
        self._register("a.safetensors", display_name="Alpha")
        rec = self._register("b.safetensors", display_name="Beta")
        item = self.svc.get(rec.id)
        self.assertEqual(item["name"], "Beta")

    def test_get_missing_raises_404(self):
        with self.assertRaises(AppError) as cm:
            self.svc.get(999)
        self.assertEqual(cm.exception.status, 404)
        self.assertEqual(cm.exception.code, "LORA_ASSET_NOT_FOUND")


class TestUpdate(LoraLibraryServiceBase):
    def test_update_cleans_and_expands_aliases(self):
        self._weight_file("miku.safetensors")
        rec = self._register("miku.safetensors", display_name="Miku")
        self._seed_profile(
            "illustrious", "miku", "Hatsune Miku",
            aliases=["初音ミク"], triggers=["miku"], lora_name="miku.safetensors",
        )
        result = self.svc.update(rec.id, aliases=["  hatsune miku ", "初音未来", "初音ミク", ""])
        aliases = result["aliases"]
        # 清洗：去空白、空串丢弃；角色别名表扩展；按匹配键去重
        self.assertIn("hatsune miku", aliases)
        self.assertIn("初音未来", aliases)
        self.assertIn("初音ミク", aliases)
        self.assertIn("miku", aliases)
        # 持久化到 profile_store
        stored = self.profile_store.get("illustrious", "miku")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.profile["aliases"], aliases)

    def test_update_triggers_and_role(self):
        self._weight_file("hoshino.safetensors")
        rec = self._register("hoshino.safetensors", display_name="Hoshino")
        self._seed_profile(
            "illustrious", "hoshino", "Hoshino Mirai", lora_name="hoshino.safetensors",
        )
        result = self.svc.update(rec.id, triggers=[" hoshino ", "teacher", "teacher"], role=[" student ", "student"])
        self.assertEqual(result["triggers"], ["hoshino", "teacher"])
        self.assertEqual(result["role"], ["student"])

    def test_update_disabled_flag(self):
        rec = self._register("a.safetensors")
        item = self.svc.update(rec.id, disabled=True)
        self.assertTrue(item["disabled"])
        item2 = self.svc.update(rec.id, disabled=False)
        self.assertFalse(item2["disabled"])
        asset = self.asset_store.get_by_id(rec.id)
        self.assertFalse(asset.disabled)

    def test_update_creates_profile_when_missing(self):
        rec = self._register("new_asset.safetensors", display_name="New Asset")
        result = self.svc.update(rec.id, aliases=["newname"], triggers=["trigger1"])
        self.assertIn("newname", result["aliases"])
        stored = self.profile_store.get("illustrious", "new_asset")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.profile["lora"], "new_asset.safetensors")

    def test_update_missing_asset_raises(self):
        with self.assertRaises(AppError):
            self.svc.update(999, aliases=["x"])


class TestQuarantineRestoreDelete(LoraLibraryServiceBase):
    def test_quarantine_moves_file_into_deleted_loras(self):
        path = self._weight_file("isolate_me.safetensors")
        rec = self._register("isolate_me.safetensors", display_name="Isolate Me")
        result = self.svc.quarantine(rec.id, reason="手动隔离")
        self.assertEqual(result["state"], "quarantined")
        self.assertEqual(result["quarantine_reason"], "手动隔离")
        relpath = result["quarantine_relpath"]
        self.assertTrue(relpath.startswith("deleted_loras/"))
        self.assertTrue(relpath.endswith("_isolate_me.safetensors"))
        # 原文件已移入隔离目录，内容保留
        self.assertFalse(path.exists())
        moved = self.tmp / relpath
        self.assertTrue(moved.is_file())
        self.assertEqual(moved.read_bytes(), b"lora-weight-bytes")
        # DB 登记相对路径
        asset = self.asset_store.get_by_id(rec.id)
        self.assertEqual(asset.quarantine_relpath, relpath)

    def test_restore_returns_file_to_original_place(self):
        path = self._weight_file("restore_me.safetensors")
        rec = self._register("restore_me.safetensors")
        self.svc.quarantine(rec.id)
        self.assertFalse(path.exists())
        result = self.svc.restore(rec.id)
        self.assertEqual(result["state"], "installed")
        self.assertIsNone(result["quarantine_relpath"])
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_bytes(), b"lora-weight-bytes")

    def test_restore_rejects_out_of_bounds_path(self):
        rec = self._register("escape.safetensors")
        self.db.connection.execute(
            "UPDATE lora_assets SET state='quarantined', quarantine_relpath=? WHERE id=?",
            ("../escape.safetensors", rec.id),
        )
        with self.assertRaises(AppError) as cm:
            self.svc.restore(rec.id)
        self.assertEqual(cm.exception.code, "LORA_RESTORE_PATH_UNSAFE")
        # 越界拒绝后状态不变，仍在隔离
        asset = self.asset_store.get_by_id(rec.id)
        self.assertEqual(asset.state, "quarantined")

    def test_restore_not_quarantined_raises(self):
        rec = self._register("active.safetensors")
        with self.assertRaises(AppError) as cm:
            self.svc.restore(rec.id)
        self.assertEqual(cm.exception.code, "LORA_NOT_QUARANTINED")

    def test_delete_removes_index_keeps_file_recoverable(self):
        path = self._weight_file("obsolete.safetensors")
        rec = self._register("obsolete.safetensors", display_name="Obsolete")
        self.svc.delete(rec.id)
        # 索引已删
        self.assertIsNone(self.asset_store.get_by_id(rec.id))
        # 权重文件未删除，移入 deleted_loras 可恢复
        self.assertFalse(path.exists())
        deleted_files = list((self.tmp / "deleted_loras").glob("*_obsolete.safetensors"))
        self.assertEqual(len(deleted_files), 1)
        self.assertEqual(deleted_files[0].read_bytes(), b"lora-weight-bytes")

    def test_quarantine_tolerates_unavailable_directory(self):
        shutil.rmtree(self.loras_dir)
        rec = self._register("ghost.safetensors")
        result = self.svc.quarantine(rec.id)
        self.assertEqual(result["state"], "quarantined")
        self.assertTrue(result["quarantine_relpath"].startswith("deleted_loras/"))

    def test_delete_tolerates_missing_file(self):
        rec = self._register("gone.safetensors")
        self.svc.delete(rec.id)
        self.assertIsNone(self.asset_store.get_by_id(rec.id))

    def test_quarantine_missing_asset_raises(self):
        with self.assertRaises(AppError):
            self.svc.quarantine(999)


class TestMatchPreview(LoraLibraryServiceBase):
    async def test_match_preview_structure_and_public_fields(self):
        self._weight_file("hoshino_mirai.safetensors")
        self._seed_profile(
            "illustrious", "hoshino mirai", "Hoshino Mirai",
            aliases=["Hoshino Mirai", "hoshino", "ホシノ"],
            triggers=["hoshino"],
            lora_name="hoshino_mirai.safetensors",
        )
        result = await self.svc.match_preview("画一个 hoshino 与蓝天", family="illustrious", limit=8)
        self.assertEqual(result["prompt"], "画一个 hoshino 与蓝天")
        self.assertEqual(result["family"], "illustrious")
        self.assertGreaterEqual(len(result["matches"]), 1)
        m = result["matches"][0]
        self.assertEqual(m["key"], "hoshino mirai")
        self.assertEqual(m["character"], "Hoshino Mirai")
        self.assertEqual(m["name"], "hoshino_mirai.safetensors")
        self.assertIn("hoshino", m["aliases"])
        self.assertIn("hoshino", m["triggers"])
        self.assertGreaterEqual(m["score"], 0)
        self.assertIn("relevance", m)
        self.assertIn("connector", m)
        self.assertIn("note", m)
        blob = json.dumps(result, ensure_ascii=False)
        for forbidden in ("http", "token", "authorization", "cookie", "C:", ":\\\\"):
            self.assertNotIn(forbidden, blob)

    async def test_match_preview_empty_prompt_raises(self):
        with self.assertRaises(AppError) as cm:
            await self.svc.match_preview("  ", family="illustrious")
        self.assertEqual(cm.exception.status, 422)

    async def test_match_preview_invalid_family_raises(self):
        with self.assertRaises(AppError):
            await self.svc.match_preview("hoshino", family="pokemon")

    async def test_match_preview_empty_library_returns_empty_matches(self):
        result = await self.svc.match_preview("some prompt", family="illustrious")
        self.assertEqual(result["matches"], [])
        self.assertFalse(result["multi"])
        self.assertFalse(result["connector_found"])

    async def test_match_preview_uses_injected_service_factory(self):
        profiles = {
            "illustrious": {
                "hoshino": {
                    "lora": "hoshino.safetensors",
                    "character": "Hoshino Mirai",
                    "aliases": ["hoshino"],
                    "trigger_words": ["hoshino"],
                    "source": "local",
                }
            }
        }
        svc = LoraLibraryService(
            db=self.db,
            data_root=self.tmp,
            asset_store=self.asset_store,
            profile_store=self.profile_store,
            service_factory=lambda: LoraService(profiles),
        )
        result = await svc.match_preview("hoshino", family="illustrious")
        self.assertGreaterEqual(len(result["matches"]), 1)
        self.assertEqual(result["matches"][0]["key"], "hoshino")


if __name__ == "__main__":
    unittest.main(verbosity=2)

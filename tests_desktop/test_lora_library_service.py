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


class FakeLlmMatchService:
    """可注入 LoraLibraryService 的假 LLM 匹配服务。"""

    def __init__(self, enabled: bool = True, matched_index=0):
        self.enabled = enabled
        self.matched_index = matched_index
        self.calls: list[tuple[str, str, int]] = []

    def llm_enabled(self) -> bool:
        return self.enabled

    async def match(self, *, prompt, candidates, family="illustrious", revision=0, limit=5):
        self.calls.append((prompt, family, revision))
        if not self.enabled or not candidates or self.matched_index is None:
            return {"matched": None, "reason": "无候选", "used_cache": False}
        idx = min(int(self.matched_index), len(candidates) - 1)
        return {"matched": candidates[idx], "confidence": 0.95, "reason": "LLM 命中", "used_cache": False}


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
    def test_update_cleans_aliases_without_regenerating_removed_values(self):
        self._weight_file("miku.safetensors")
        rec = self._register("miku.safetensors", display_name="Miku")
        self._seed_profile(
            "illustrious", "miku", "Hatsune Miku",
            aliases=["初音ミク"], triggers=["miku"], lora_name="miku.safetensors",
        )
        result = self.svc.update(rec.id, aliases=["  hatsune miku ", "初音未来", "初音ミク", ""])
        aliases = result["aliases"]
        # User edits replace aliases exactly; expansion belongs to import,
        # otherwise deleting a generated nickname can never persist.
        self.assertIn("hatsune miku", aliases)
        self.assertIn("初音未来", aliases)
        self.assertIn("初音ミク", aliases)
        self.assertNotIn("miku", aliases)
        # 持久化到 profile_store
        stored = self.profile_store.get("illustrious", "miku")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.profile["aliases"], aliases)

    def test_update_aliases_replaces_removed_values(self):
        self._weight_file("replace_alias.safetensors")
        rec = self._register("replace_alias.safetensors", display_name="Replace Alias")
        self._seed_profile(
            "illustrious", "replace alias", "Replace Alias",
            aliases=["obsolete_alias", "keep_alias"],
            lora_name="replace_alias.safetensors",
        )
        result = self.svc.update(rec.id, aliases=["keep_alias", "new_alias"])
        self.assertIn("new_alias", result["aliases"])
        self.assertNotIn("obsolete_alias", result["aliases"])

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
    def test_quarantine_moves_file_into_quarantine_dir(self):
        path = self._weight_file("isolate_me.safetensors")
        rec = self._register("isolate_me.safetensors", display_name="Isolate Me")
        result = self.svc.quarantine(rec.id, reason="手动隔离")
        self.assertEqual(result["state"], "quarantined")
        self.assertEqual(result["quarantine_reason"], "手动隔离")
        relpath = result["quarantine_relpath"]
        self.assertTrue(relpath.startswith(".winefox-quarantine/"))
        self.assertTrue(relpath.endswith("_isolate_me.safetensors"))
        # 原文件已移入同磁盘隔离目录，内容保留
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

    def test_inventory_asset_with_local_dir_can_be_quarantined_and_restored(self):
        local_dir = self.tmp / "inventory_local"
        local_dir.mkdir(parents=True, exist_ok=True)
        path = local_dir / "inventory_file.safetensors"
        path.write_bytes(b"lora-weight-bytes")
        rec = self.asset_store.register(
            "illustrious", "inventory_file.safetensors",
            comfy_key="inventory_file.safetensors", display_name="Inventory File",
            source="inventory",
        )
        events = []
        svc = LoraLibraryService(
            db=self.db,
            data_root=self.tmp,
            asset_store=self.asset_store,
            profile_store=self.profile_store,
            settings_fn=lambda: {
                "lora_download": {"mode": "local", "local_dir": str(local_dir)}
            },
            on_profiles_changed=lambda: events.append("changed"),
        )
        self.assertEqual(svc.quarantine(rec.id)["state"], "quarantined")
        self.assertEqual(svc.restore(rec.id)["state"], "installed")
        self.assertTrue(path.is_file())
        self.assertEqual(events, ["changed", "changed"])

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

    def test_delete_is_disabled_and_keeps_index_and_file(self):
        path = self._weight_file("obsolete.safetensors")
        rec = self._register("obsolete.safetensors", display_name="Obsolete")
        with self.assertRaises(AppError) as cm:
            self.svc.delete(rec.id)
        self.assertEqual(cm.exception.code, "LORA_PERMANENT_DELETE_DISABLED")
        self.assertIsNotNone(self.asset_store.get_by_id(rec.id))
        self.assertTrue(path.is_file())

    def test_quarantine_rejects_when_file_missing(self):
        shutil.rmtree(self.loras_dir)
        rec = self._register("ghost.safetensors")
        with self.assertRaises(AppError) as cm:
            self.svc.quarantine(rec.id)
        self.assertEqual(cm.exception.code, "LORA_QUARANTINE_FILE_MISSING")
        # 移动失败/找不到文件不得标记 DB 隔离
        asset = self.asset_store.get_by_id(rec.id)
        self.assertEqual(asset.state, "installed")

    def test_quarantine_uses_local_dir_root_and_restores(self):
        local_dir = self.tmp / "local_loras"
        local_dir.mkdir(parents=True, exist_ok=True)
        svc = LoraLibraryService(
            db=self.db,
            data_root=self.tmp,
            asset_store=self.asset_store,
            profile_store=self.profile_store,
            settings_fn=lambda: {"lora_download": {"local_dir": str(local_dir)}},
        )
        path = local_dir / "mizuki.safetensors"
        path.write_bytes(b"lora-weight-bytes")
        rec = self.asset_store.register(
            "illustrious", "mizuki.safetensors",
            comfy_key="mizuki.safetensors", display_name="Mizuki", source="local",
        )
        result = svc.quarantine(rec.id, reason="本地隔离")
        self.assertEqual(result["state"], "quarantined")
        relpath = result["quarantine_relpath"]
        self.assertTrue(relpath.startswith(".winefox-quarantine/"))
        # 隔离目录在 local_dir 下（与源同磁盘），原文件已移走
        self.assertTrue((local_dir / relpath).is_file())
        self.assertFalse(path.exists())
        restored = svc.restore(rec.id)
        self.assertEqual(restored["state"], "installed")
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_bytes(), b"lora-weight-bytes")

    def test_restore_missing_file_keeps_quarantined(self):
        path = self._weight_file("gone.safetensors")
        rec = self._register("gone.safetensors")
        result = self.svc.quarantine(rec.id)
        relpath = result["quarantine_relpath"]
        (self.tmp / relpath).unlink()
        with self.assertRaises(AppError) as cm:
            self.svc.restore(rec.id)
        self.assertEqual(cm.exception.code, "LORA_RESTORE_FILE_MISSING")
        # 隔离文件不存在时保持隔离状态
        asset = self.asset_store.get_by_id(rec.id)
        self.assertEqual(asset.state, "quarantined")

    def test_delete_missing_file_is_still_disabled(self):
        rec = self._register("gone.safetensors")
        with self.assertRaises(AppError) as cm:
            self.svc.delete(rec.id)
        self.assertEqual(cm.exception.code, "LORA_PERMANENT_DELETE_DISABLED")
        self.assertIsNotNone(self.asset_store.get_by_id(rec.id))

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
        self.assertIn("strength_model", m)
        self.assertIn("strength_clip", m)
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


class TestMatchPreviewLlm(LoraLibraryServiceBase):
    def test_family_revision_is_sum_not_max(self):
        self._seed_profile(
            "illustrious", "first", "First", lora_name="first.safetensors",
        )
        self._seed_profile(
            "illustrious", "second", "Second", lora_name="second.safetensors",
        )
        first = self.profile_store.get("illustrious", "first")
        self.profile_store.save("illustrious", "first", dict(first.profile))
        self.assertEqual(self.svc._family_revision("illustrious"), 3)

    async def test_rules_match_take_precedence_over_llm(self):
        self._weight_file("hoshino_mirai.safetensors")
        self._seed_profile(
            "illustrious", "hoshino mirai", "Hoshino Mirai",
            aliases=["Hoshino Mirai", "hoshino"], triggers=["hoshino"],
            lora_name="hoshino_mirai.safetensors",
        )
        llm = FakeLlmMatchService(enabled=True)
        self.svc.llm_service = llm
        result = await self.svc.match_preview("画一个 hoshino 与蓝天", family="illustrious")
        self.assertGreaterEqual(len(result["matches"]), 1)
        self.assertEqual(result["matches"][0]["source"], "rule")
        # 规则命中时不调用 LLM
        self.assertEqual(llm.calls, [])

    async def test_llm_rescue_when_rules_empty(self):
        self._weight_file("hoshino_mirai.safetensors")
        self._seed_profile(
            "illustrious", "hoshino mirai", "Hoshino Mirai",
            aliases=["Hoshino Mirai", "hoshino"], triggers=["hoshino"],
            lora_name="hoshino_mirai.safetensors",
        )
        llm = FakeLlmMatchService(enabled=True, matched_index=0)
        self.svc.llm_service = llm
        result = await self.svc.match_preview("一只蓝色小猫在草地上玩耍", family="illustrious")
        self.assertEqual(len(result["matches"]), 1)
        m = result["matches"][0]
        self.assertEqual(m["source"], "llm")
        self.assertEqual(m["character"], "Hoshino Mirai")
        self.assertEqual(m["name"], "hoshino_mirai.safetensors")
        self.assertIn("llm", m["relevance"].lower())
        self.assertGreaterEqual(m["score"], 0)
        # revision 传入 LLM（save 后 revision 递增）
        self.assertGreaterEqual(llm.calls[0][2], 1)
        blob = json.dumps(result, ensure_ascii=False)
        for forbidden in ("http", "authorization", "cookie", "C:", ":\\\\"):
            self.assertNotIn(forbidden, blob)

    async def test_llm_disabled_degrades_to_rules(self):
        self._weight_file("hoshino_mirai.safetensors")
        self._seed_profile(
            "illustrious", "hoshino mirai", "Hoshino Mirai",
            aliases=["Hoshino Mirai", "hoshino"], triggers=["hoshino"],
            lora_name="hoshino_mirai.safetensors",
        )
        llm = FakeLlmMatchService(enabled=False)
        self.svc.llm_service = llm
        result = await self.svc.match_preview("一只蓝色小猫在草地上玩耍", family="illustrious")
        self.assertEqual(result["matches"], [])
        # 不可用时完全降级到规则，无 LLM 项
        self.assertEqual(llm.calls, [])

    async def test_llm_no_match_returns_empty(self):
        self._weight_file("hoshino_mirai.safetensors")
        self._seed_profile(
            "illustrious", "hoshino mirai", "Hoshino Mirai",
            aliases=["Hoshino Mirai", "hoshino"], triggers=["hoshino"],
            lora_name="hoshino_mirai.safetensors",
        )
        llm = FakeLlmMatchService(enabled=True, matched_index=None)
        self.svc.llm_service = llm
        result = await self.svc.match_preview("一只蓝色小猫在草地上玩耍", family="illustrious")
        self.assertEqual(result["matches"], [])

    async def test_llm_candidates_exclude_disabled_assets(self):
        path = self._weight_file("disabled_hero.safetensors")
        rec = self._register("disabled_hero.safetensors", display_name="Disabled Hero")
        self._seed_profile(
            "illustrious", "disabled hero", "Disabled Hero",
            aliases=["disabledhero"], lora_name="disabled_hero.safetensors",
        )
        self.svc.update(rec.id, disabled=True)
        llm = FakeLlmMatchService(enabled=True, matched_index=0)
        self.svc.llm_service = llm
        result = await self.svc.match_preview("一只蓝色小猫在草地上玩耍", family="illustrious")
        # 禁用资产不进入 LLM 候选，也就不会有 LLM 命中
        self.assertEqual(result["matches"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

# -*- coding: utf-8 -*-
"""LoRA 库统一数据层测试：迁移 0003 / LoraAssetStore / LoraDownloadJobStore /
validate_public_json / JSON 档案幂等导入。

运行：``python -m unittest tests_desktop.test_lora_library_storage -v``
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winefox_core.storage import (  # noqa: E402
    AppEventStore,
    Database,
    LoraAssetStore,
    LoraDownloadJobStore,
    LoraProfileStore,
)
from winefox_core.storage_models import (  # noqa: E402
    InvalidStateTransitionError,
    LoraAssetState,
    LoraDownloadState,
    ValidationError,
    validate_public_json,
)
from desktop_runtime.lora_profiles import import_lora_profiles_into_db  # noqa: E402


class LoraLibraryStorageBase(unittest.TestCase):
    _ADVANCE_PATH = (
        LoraDownloadState.RESOLVING,
        LoraDownloadState.DOWNLOADING,
        LoraDownloadState.PROFILING,
        LoraDownloadState.REFRESHING,
    )

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="lora_lib_")
        self.tmp = Path(self._tmpdir.name)
        self.db = Database(self.tmp / "winefox.db")
        self.applied = self.db.initialize()
        self.assets = LoraAssetStore(self.db)
        self.jobs = LoraDownloadJobStore(self.db)
        self.events = AppEventStore(self.db)

    def tearDown(self):
        self.db.close()
        self._tmpdir.cleanup()

    def _advance(self, result_id: str, target: LoraDownloadState) -> None:
        """按合法路径推进下载任务到 target（RESOLVING..REFRESHING 之一）。"""
        for state in self._ADVANCE_PATH:
            self.jobs.update_state(result_id, state)
            if state is target:
                return


# ---------------------------------------------------------------------------
# 迁移 0003
# ---------------------------------------------------------------------------

class TestMigration0003(LoraLibraryStorageBase):
    def test_migration_applied_and_tables_present(self):
        self.assertIn("0003_lora_library", self.applied)
        names = {
            r[0]
            for r in self.db.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertIn("lora_assets", names)
        self.assertIn("lora_download_jobs", names)

    def test_repeat_initialize_idempotent(self):
        again = self.db.initialize()
        self.assertEqual(again, self.applied)


# ---------------------------------------------------------------------------
# validate_public_json：凭据 / 绝对路径拒绝
# ---------------------------------------------------------------------------

class TestValidatePublicJson(unittest.TestCase):
    def test_plain_json_accepted(self):
        validate_public_json(
            {
                "name": "Anmi Style",
                "triggers": ["anmi", "1girl"],
                "count": 3,
                "nested": {"ok": [1, 2, {"fine": True}]},
            }
        )
        validate_public_json("plain string")
        validate_public_json([1, 2, 3])

    def test_url_without_credentials_accepted(self):
        validate_public_json(
            "https://civitai.com/models/123?q=anmi&type=Model"
        )

    def test_url_with_secret_query_rejected(self):
        for url in (
            "https://civitai.com/api/download/models/1?token=secret123",
            "https://example.com/x?access_token=abc",
            "https://example.com/x?api_key=abc&other=1",
            "https://example.com/x?apikey=abc",
            "https://example.com/x?secret=zzz",
            "https://example.com/x?a=1&token=zzz",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValidationError):
                    validate_public_json(url)

    def test_url_with_userinfo_rejected(self):
        with self.assertRaises(ValidationError):
            validate_public_json("https://user:pass@example.com/file.safetensors")

    def test_header_and_absolute_path_rejected(self):
        with self.assertRaises(ValidationError):
            validate_public_json({"h": "Cookie: session=abc"})
        with self.assertRaises(ValidationError):
            validate_public_json({"h": "Authorization: Bearer abc"})
        with self.assertRaises(ValidationError):
            validate_public_json({"p": "C:\\Users\\x\\secret.safetensors"})
        with self.assertRaises(ValidationError):
            validate_public_json({"p": "\\\\server\\share\\a.safetensors"})

    def test_nested_values_checked(self):
        payload = {"deep": {"list": ["https://x/download?token=leak"]}}
        with self.assertRaises(ValidationError):
            validate_public_json(payload)


# ---------------------------------------------------------------------------
# LoraAssetStore
# ---------------------------------------------------------------------------

class TestLoraAssetStore(LoraLibraryStorageBase):
    def test_register_and_get(self):
        rec = self.assets.register(
            "illustrious",
            "anmi_style_v2.safetensors",
            comfy_key="anmi_style_v2.safetensors",
            display_name="Anmi Style v2",
            source="civitai",
            model_id=1001,
            version_id=99,
            preview_key="cache/lora_previews/ab12.png",
            info={"sha256": "a" * 64, "size_bytes": 12345},
        )
        self.assertEqual(rec.state, "installed")
        self.assertEqual(rec.comfy_key, "anmi_style_v2.safetensors")
        self.assertEqual(rec.info["size_bytes"], 12345)
        got = self.assets.get("illustrious", "anmi_style_v2.safetensors")
        self.assertEqual(got.id, rec.id)
        self.assertEqual(self.assets.get_by_id(rec.id).display_name, "Anmi Style v2")
        self.assertIsNone(self.assets.get("illustrious", "missing.safetensors"))

    def test_register_upsert_by_family_filename(self):
        self.assets.register("illustrious", "a.safetensors", display_name="v1")
        self.assets.register("illustrious", "a.safetensors", display_name="v2")
        rows = self.db.connection.execute(
            "SELECT COUNT(*) FROM lora_assets WHERE model_family='illustrious' AND filename='a.safetensors'"
        ).fetchone()[0]
        self.assertEqual(rows, 1)
        self.assertEqual(self.assets.get("illustrious", "a.safetensors").display_name, "v2")
        # 不同 family 同名 -> 两行
        self.assets.register("anima", "a.safetensors")
        self.assertEqual(self.assets.count(model_family="anima"), 1)
        self.assertEqual(self.assets.count(), 2)

    def test_list_and_filenames_filters(self):
        self.assets.register("illustrious", "a.safetensors", source="civitai")
        self.assets.register("illustrious", "b.safetensors", source="local")
        self.assets.register("anima", "c.safetensors")
        self.assertEqual(len(self.assets.list(model_family="illustrious")), 2)
        self.assertEqual(
            set(self.assets.filenames(model_family="illustrious")),
            {"a.safetensors", "b.safetensors"},
        )
        self.assertEqual(self.assets.filenames(model_family="anima"), ["c.safetensors"])
        self.assertEqual(self.assets.count(), 3)

    def test_find_by_model_id(self):
        self.assets.register("illustrious", "x.safetensors", model_id=77)
        found = self.assets.find_by_model_id(77)
        self.assertEqual([r.filename for r in found], ["x.safetensors"])
        self.assertEqual(self.assets.find_by_model_id(999), [])

    def test_preview_and_disabled(self):
        rec = self.assets.register("illustrious", "p.safetensors")
        self.assertTrue(self.assets.set_preview(rec.id, "cache\\lora_previews\\xy.png"))
        self.assertEqual(
            self.assets.get_by_id(rec.id).preview_key, "cache/lora_previews/xy.png"
        )
        self.assertTrue(self.assets.set_disabled(rec.id, "用户禁用"))
        asset = self.assets.get_by_id(rec.id)
        self.assertTrue(asset.disabled)
        self.assertEqual(asset.disable_reason, "用户禁用")
        self.assertEqual(len(self.assets.list(include_disabled=False)), 0)
        self.assertTrue(self.assets.clear_disabled(rec.id))
        self.assertFalse(self.assets.get_by_id(rec.id).disabled)

    def test_quarantine_and_restore_only_original_place(self):
        rec = self.assets.register("illustrious", "q.safetensors")
        quarantined = self.assets.quarantine(
            rec.id, "deleted_loras/2026_xyz_q.safetensors"
        )
        self.assertEqual(quarantined.state, LoraAssetState.QUARANTINED.value)
        self.assertEqual(
            quarantined.quarantine_relpath, "deleted_loras/2026_xyz_q.safetensors"
        )
        # 已隔离不能重复隔离
        with self.assertRaises(InvalidStateTransitionError):
            self.assets.quarantine(rec.id, "deleted_loras/another.safetensors")
        restored = self.assets.restore(rec.id)
        self.assertEqual(restored.state, LoraAssetState.INSTALLED.value)
        self.assertIsNone(restored.quarantine_relpath)
        # 未隔离不能恢复
        with self.assertRaises(InvalidStateTransitionError):
            self.assets.restore(rec.id)
        # 隔离状态计入 count(state)
        self.assets.quarantine(rec.id, "deleted_loras/x.safetensors")
        self.assertEqual(self.assets.count(state="quarantined"), 1)
        self.assertEqual(self.assets.count(state="installed"), 0)

    def test_delete_only_removes_index(self):
        rec = self.assets.register("illustrious", "d.safetensors")
        self.assertTrue(self.assets.delete(rec.id))
        self.assertIsNone(self.assets.get_by_id(rec.id))
        self.assertFalse(self.assets.delete(rec.id))

    def test_validation(self):
        with self.assertRaises(ValidationError):
            self.assets.register("illustrious", "")
        with self.assertRaises(ValidationError):
            self.assets.register("illustrious", "sub/a.safetensors")
        with self.assertRaises(ValidationError):
            self.assets.register("illustrious", "..\\a.safetensors")
        with self.assertRaises(ValidationError):
            self.assets.register("", "a.safetensors")
        # preview / info 含凭据或绝对路径
        with self.assertRaises(ValidationError):
            self.assets.register("illustrious", "a.safetensors", preview_key="C:\\abs\\x.png")
        with self.assertRaises(ValidationError):
            self.assets.register(
                "illustrious", "a.safetensors",
                info={"url": "https://x/download?token=leak"},
            )


# ---------------------------------------------------------------------------
# LoraDownloadJobStore
# ---------------------------------------------------------------------------

class TestLoraDownloadJobStore(LoraLibraryStorageBase):
    def test_enqueue_idempotent(self):
        job = self.jobs.enqueue(
            "res-1", family="illustrious", alias="anmi",
            source="civitai",
            source_ref={"model_id": 5, "name": "Anmi", "version_id": 2},
        )
        self.assertEqual(job.state, LoraDownloadState.QUEUED.value)
        self.assertEqual(job.alias, "anmi")
        dup = self.jobs.enqueue("res-1", source_ref={})
        self.assertEqual(dup.result_id, "res-1")
        self.assertEqual(dup.state, LoraDownloadState.QUEUED.value)
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM lora_download_jobs WHERE result_id='res-1'"
            ).fetchone()[0],
            1,
        )

    def test_full_state_machine(self):
        job = self.jobs.enqueue("sm-1")
        for target in (
            LoraDownloadState.RESOLVING,
            LoraDownloadState.DOWNLOADING,
            LoraDownloadState.PROFILING,
            LoraDownloadState.REFRESHING,
        ):
            job = self.jobs.update_state(job.result_id, target)
            self.assertEqual(job.state, target.value)
        done = self.jobs.update_state(
            "sm-1",
            LoraDownloadState.COMPLETED,
            result={"filename": "anmi.safetensors", "sha256": "a" * 64},
        )
        self.assertEqual(done.state, LoraDownloadState.COMPLETED.value)
        self.assertEqual(done.result["filename"], "anmi.safetensors")
        self.assertIsNotNone(done.started_at)
        self.assertIsNotNone(done.finished_at)

    def test_illegal_transition_rolls_back(self):
        self.jobs.enqueue("bad-1")
        self.jobs.update_state("bad-1", LoraDownloadState.RESOLVING)
        with self.assertRaises(InvalidStateTransitionError):
            self.jobs.update_state("bad-1", LoraDownloadState.COMPLETED)
        # 回滚：无部分写入
        row = self.db.connection.execute(
            "SELECT state, finished_at FROM lora_download_jobs WHERE result_id='bad-1'"
        ).fetchone()
        self.assertEqual(row["state"], "resolving")
        self.assertIsNone(row["finished_at"])
        # 终态后任何迁移非法
        self.jobs.update_state("bad-1", LoraDownloadState.DOWNLOADING)
        self.jobs.update_state("bad-1", LoraDownloadState.PROFILING)
        self.jobs.update_state("bad-1", LoraDownloadState.COMPLETED)
        with self.assertRaises(InvalidStateTransitionError):
            self.jobs.update_state("bad-1", LoraDownloadState.REFRESHING)
        with self.assertRaises(InvalidStateTransitionError):
            self.jobs.update_state("bad-1", LoraDownloadState.FAILED)
        # 不存在的任务
        with self.assertRaises(InvalidStateTransitionError):
            self.jobs.update_state("missing", LoraDownloadState.FAILED)

    def test_progress_finish_and_active_count(self):
        self.jobs.enqueue("act-1")
        self.jobs.enqueue("act-2")
        self._advance("act-2", LoraDownloadState.DOWNLOADING)
        self.assertEqual(self.jobs.active_count(), 2)
        self.jobs.set_progress("act-2", {"percent": 42, "downloaded": 100})
        self.assertEqual(
            self.jobs.get("act-2").progress["percent"], 42
        )
        self.jobs.update_state("act-2", LoraDownloadState.PROFILING)
        self.jobs.update_state("act-2", LoraDownloadState.REFRESHING)
        self.jobs.finish("act-2", {"filename": "x.safetensors"})
        self.assertEqual(self.jobs.get("act-2").state, "completed")
        self.assertEqual(self.jobs.active_count(), 1)

    def test_requeue_from_terminal(self):
        self.jobs.enqueue("retry-1")
        self._advance("retry-1", LoraDownloadState.DOWNLOADING)
        # interrupted 场景：模拟重启
        self.jobs.update_state("retry-1", LoraDownloadState.INTERRUPTED)
        requeued = self.jobs.requeue("retry-1")
        self.assertEqual(requeued.state, LoraDownloadState.QUEUED.value)
        self.assertIsNone(requeued.error)
        # failed -> queued
        self.jobs.update_state("retry-1", LoraDownloadState.RESOLVING)
        self.jobs.update_state("retry-1", LoraDownloadState.FAILED, error="net")
        self.assertEqual(self.jobs.requeue("retry-1").state, "queued")
        # queued 不能 requeue
        with self.assertRaises(InvalidStateTransitionError):
            self.jobs.requeue("retry-1")

    def test_mark_interrupted_atomic(self):
        self.jobs.enqueue("i-queued")
        self.jobs.enqueue("i-resolving")
        self._advance("i-resolving", LoraDownloadState.RESOLVING)
        self.jobs.enqueue("i-downloading")
        self._advance("i-downloading", LoraDownloadState.DOWNLOADING)
        self.jobs.enqueue("i-profiling")
        self._advance("i-profiling", LoraDownloadState.PROFILING)
        self.jobs.enqueue("i-refreshing")
        self._advance("i-refreshing", LoraDownloadState.REFRESHING)
        self.jobs.enqueue("i-completed")
        self._advance("i-completed", LoraDownloadState.REFRESHING)
        self.jobs.update_state("i-completed", LoraDownloadState.COMPLETED)
        affected = self.jobs.mark_interrupted()
        ids = {r.result_id: r.state for r in affected}
        self.assertEqual(
            ids,
            {
                "i-queued": "interrupted",
                "i-resolving": "interrupted",
                "i-downloading": "interrupted",
                "i-profiling": "interrupted",
                "i-refreshing": "interrupted",
            },
        )
        # 终态不受影响
        self.assertEqual(self.jobs.get("i-completed").state, "completed")
        self.assertEqual(self.jobs.active_count(), 0)
        # 事件已记录
        events = self.events.list_events(event_type="lora.downloads_interrupted")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].payload["count"], 5)
        # 再次 mark 幂等（无活动任务）
        self.assertEqual(self.jobs.mark_interrupted(), [])

    def test_list_and_delete(self):
        self.jobs.enqueue("l-1", family="illustrious")
        self.jobs.enqueue("l-2", family="anima")
        self.jobs.enqueue("l-3", family="illustrious")
        self._advance("l-3", LoraDownloadState.REFRESHING)
        self.jobs.update_state("l-3", LoraDownloadState.COMPLETED)
        self.assertEqual(len(self.jobs.list()), 3)
        self.assertEqual(len(self.jobs.list(state=LoraDownloadState.QUEUED)), 2)
        self.assertEqual(len(self.jobs.list(family="anima")), 1)
        self.assertTrue(self.jobs.delete("l-1"))
        self.assertIsNone(self.jobs.get("l-1"))
        self.assertFalse(self.jobs.delete("l-1"))

    def test_secret_material_rejected(self):
        with self.assertRaises(ValidationError):
            self.jobs.enqueue(
                "s-1",
                source_ref={"url": "https://civitai.com/api/download/models/1?token=abc"},
            )
        self.jobs.enqueue("s-2")
        with self.assertRaises(ValidationError):
            self.jobs.update_state(
                "s-2", LoraDownloadState.RESOLVING,
                result={"url": "https://x/download?access_token=zz"},
            )
        with self.assertRaises(ValidationError):
            self.jobs.enqueue("s-3", source_ref={"h": "Cookie: session=1"})
        # part_files 只允许相对路径
        with self.assertRaises(ValidationError):
            self.jobs.update_state(
                "s-2", LoraDownloadState.RESOLVING,
                part_files=["C:\\abs\\x.part"],
            )
        with self.assertRaises(ValidationError):
            self.jobs.enqueue("")

    def test_part_files_relative_only(self):
        self.jobs.enqueue("pf-1")
        self._advance("pf-1", LoraDownloadState.DOWNLOADING)
        job = self.jobs.set_progress("pf-1", part_files=[".\\tmp\\a.part", "tmp/b.part"])
        self.assertEqual(job.part_files, ["tmp/a.part", "tmp/b.part"])


# ---------------------------------------------------------------------------
# JSON 档案幂等导入
# ---------------------------------------------------------------------------

class TestImportProfilesIntoDb(LoraLibraryStorageBase):
    def _seed_json(self, root: Path, extra: dict | None = None) -> None:
        legacy = root / "legacy" / "lora_profiles"
        legacy.mkdir(parents=True, exist_ok=True)
        profiles = {
            "anmi style": {
                "lora": "anmi_style.safetensors",
                "character": "Anmi",
                "aliases": ["anmi", "anmi_style"],
                "trigger_words": ["anmi"],
                "source": "legacy",
            }
        }
        if extra:
            profiles.update(extra)
        (legacy / "lora_profiles_illustrious.json").write_text(
            json.dumps(profiles, ensure_ascii=False), encoding="utf-8"
        )

    def test_imports_into_sqlite_idempotent(self):
        root = self.tmp / "data_root"
        root.mkdir()
        self._seed_json(root)
        imported = import_lora_profiles_into_db(
            self.db, root, app_events=self.events
        )
        self.assertGreaterEqual(imported, 1)
        store = LoraProfileStore(self.db)
        rec = store.get("illustrious", "anmi style")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.profile["lora"], "anmi_style.safetensors")
        events1 = len(self.events.list_events(event_type="lora.profiles.imported"))

        # 再次导入：幂等，行数不变，不新增事件
        imported2 = import_lora_profiles_into_db(
            self.db, root, app_events=self.events
        )
        self.assertEqual(imported2, imported)
        rows = self.db.connection.execute(
            "SELECT COUNT(*) FROM lora_profiles WHERE model_family='illustrious'"
        ).fetchone()[0]
        self.assertEqual(rows, 1)
        events2 = len(self.events.list_events(event_type="lora.profiles.imported"))
        self.assertEqual(events2, events1)

    def test_new_entries_record_event(self):
        root = self.tmp / "data_root2"
        root.mkdir()
        self._seed_json(root)
        import_lora_profiles_into_db(self.db, root, app_events=self.events)
        # 新增一条后再导入 -> 事件 +1
        self._seed_json(root, {"nagi": {"lora": "nagi.safetensors", "character": "Nagi"}})
        import_lora_profiles_into_db(self.db, root, app_events=self.events)
        events = self.events.list_events(event_type="lora.profiles.imported")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1].payload["added"], 1)

    def test_invalid_entries_do_not_abort_import(self):
        root = self.tmp / "data_root3"
        root.mkdir()
        legacy = root / "legacy" / "lora_profiles"
        legacy.mkdir(parents=True)
        (legacy / "lora_profiles_illustrious.json").write_text(
            json.dumps(
                {
                    "bad": "not-a-dict",  # 会被跳过
                    "good": {"lora": "good.safetensors", "character": "Good"},
                }
            ),
            encoding="utf-8",
        )
        imported = import_lora_profiles_into_db(self.db, root)
        self.assertEqual(imported, 1)
        self.assertIsNotNone(LoraProfileStore(self.db).get("illustrious", "good"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

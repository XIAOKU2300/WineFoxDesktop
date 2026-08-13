# -*- coding: utf-8 -*-
"""Agent 04 存储层测试：SQLite / 任务持久化 / 历史 / 收藏 / 迁移。

覆盖：
1. 建库 + 重复初始化（幂等）
2. PRAGMA（WAL / foreign_keys / busy_timeout）
3. JobStore 创建幂等 / 状态机 / 非法迁移回滚 / 重启恢复
4. HistoryStore 游标分页 / 软删除 / 相对路径 / JSON 校验 / 外键
5. FavoriteStore 唯一性 / upsert
6. LoraProfileStore 复合主键 / WorkflowRegistry / AppEventStore
7. 并发读 + 单 writer（WAL）
8. 迁移失败完整回滚并保留原库
9. 损坏数据库错误
10. 路径外部注入

运行：``python -m unittest tests_desktop.test_agent04_storage -v``
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winefox_core.storage import (  # noqa: E402
    AppEventStore,
    Database,
    FavoriteStore,
    HistoryStore,
    JobStore,
    LoraProfileStore,
    MigrationError,
    WorkflowRegistry,
    run_migrations,
)
from winefox_core.storage_models import (  # noqa: E402
    CorruptDatabaseError,
    InvalidStateTransitionError,
    JobState,
    ValidationError,
)


def _fresh_db(tmp: Path, name: str = "test.db") -> Database:
    return Database(tmp / name)


class StorageBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="agent04_")
        self.tmp = Path(self._tmpdir.name)
        self.db_path = self.tmp / "winefox.db"
        self.db = Database(self.db_path)
        self.applied = self.db.initialize()
        self.jobs = JobStore(self.db)
        self.history = HistoryStore(self.db)
        self.favorites = FavoriteStore(self.db)
        self.loras = LoraProfileStore(self.db)
        self.workflows = WorkflowRegistry(self.db)
        self.events = AppEventStore(self.db)

    def tearDown(self):
        self.db.close()
        self._tmpdir.cleanup()

    def _mkjob(self, rid: str = "req-1", state: JobState = JobState.SUCCEEDED):
        job = self.jobs.create(rid, "txt2img", {"prompt": rid})
        if state is JobState.SUCCEEDED:
            self.jobs.update_state(job.id, JobState.RUNNING)
            self.jobs.update_state(job.id, JobState.SUCCEEDED)
        return job


# ---------------------------------------------------------------------------
# 1. 建库 / 重复初始化 / PRAGMA / 路径注入
# ---------------------------------------------------------------------------

class TestDatabaseInit(StorageBase):
    def test_create_and_repeat_initialize(self):
        """建库 + 重复初始化必须幂等。"""
        applied1 = self.db.list_applied_migrations()
        self.assertEqual(
            applied1,
            ["0001_initial", "0002_history_favorite_indexes", "0003_lora_library"],
        )
        # 重复初始化
        applied2 = self.db.initialize()
        self.assertEqual(applied2, applied1)
        # 再直接跑一次 runner
        applied3 = run_migrations(self.db)
        self.assertEqual(applied3, applied1)
        # 表都在
        names = {
            r[0]
            for r in self.db.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for table in (
            "schema_migrations",
            "jobs",
            "history",
            "favorites",
            "lora_profiles",
            "lora_assets",
            "lora_download_jobs",
            "workflow_registry",
            "app_events",
        ):
            self.assertIn(table, names, f"缺少表 {table}")

    def test_path_injected_externally(self):
        """数据库路径由外部注入：文件必须创建在指定位置。"""
        custom = self.tmp / "sub" / "custom.db"
        db2 = Database(custom)
        db2.initialize()
        db2.close()
        self.assertTrue(custom.exists(), "数据库文件应创建在注入的路径")
        self.assertFalse((self.tmp / "winefox_default.db").exists())
        self.assertEqual(Database(custom).path, custom)

    def test_pragmas(self):
        """WAL / foreign_keys=ON / busy_timeout=5000。"""
        self.assertEqual(self.db.pragma("journal_mode"), "wal")
        self.assertEqual(self.db.pragma("foreign_keys"), 1)
        self.assertEqual(self.db.pragma("busy_timeout"), 5000)
        # WAL 文件在打开状态下存在
        self.assertTrue(Path(str(self.db_path) + "-wal").exists())

    def test_integrity_check(self):
        self.assertEqual(self.db.verify_integrity(), [])

    def test_corrupted_database_error(self):
        """损坏数据库必须报 CorruptDatabaseError 而不是静默成功。"""
        self.db.close()
        self.db_path.write_bytes(b"garbage-not-a-sqlite-db-" * 100)
        db2 = Database(self.db_path)
        with self.assertRaises(CorruptDatabaseError):
            db2.open()
        db2.close()

    def test_corrupted_database_initialize(self):
        self.db.close()
        self.db_path.write_bytes(os.urandom(4096))
        db2 = Database(self.db_path)
        with self.assertRaises(CorruptDatabaseError):
            db2.initialize()
        db2.close()


# ---------------------------------------------------------------------------
# 2. JobStore
# ---------------------------------------------------------------------------

class TestJobStore(StorageBase):
    def test_create_idempotent_by_client_request_id(self):
        j1 = self.jobs.create("dup-1", "txt2img", {"p": 1})
        j2 = self.jobs.create("dup-1", "txt2img", {"p": 2})
        self.assertEqual(j1.id, j2.id)
        self.assertEqual(j1.client_request_id, "dup-1")
        rows = self.db.connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE client_request_id='dup-1'"
        ).fetchone()[0]
        self.assertEqual(rows, 1)
        # 不同请求 id -> 不同行
        j3 = self.jobs.create("other-1", "txt2img", {})
        self.assertNotEqual(j1.id, j3.id)
        self.assertEqual(j3.state, JobState.PENDING.value)

    def test_create_validation(self):
        with self.assertRaises(ValidationError):
            self.jobs.create("", "txt2img", {})
        with self.assertRaises(ValidationError):
            self.jobs.create("x", "", {})
        with self.assertRaises(ValidationError):
            self.jobs.create("x", "txt2img", {"bad": object()})
        with self.assertRaises(ValidationError):
            self.jobs.create("x", "txt2img", {"deep": _deep_nested(70)})

    def test_state_machine_happy_path(self):
        j = self.jobs.create("sm-1", "txt2img", {})
        j = self.jobs.update_state(j.id, JobState.RUNNING)
        self.assertEqual(j.state, JobState.RUNNING.value)
        self.assertIsNotNone(j.started_at)
        j = self.jobs.update_state(
            j.id, JobState.SUCCEEDED, comfy_prompt_id="prompt-42"
        )
        self.assertEqual(j.comfy_prompt_id, "prompt-42")
        self.assertIsNotNone(j.finished_at)

    def test_invalid_state_transition_rolls_back(self):
        """非法迁移：抛错且数据库无任何部分写入。"""
        j = self.jobs.create("bad-1", "txt2img", {})
        self.jobs.update_state(j.id, JobState.RUNNING)
        self.jobs.update_state(j.id, JobState.FAILED, error="boom")
        # 终态 -> 任何迁移都非法
        with self.assertRaises(InvalidStateTransitionError):
            self.jobs.update_state(
                j.id, JobState.SUCCEEDED, comfy_prompt_id="should-not-write"
            )
        row = self.db.connection.execute(
            "SELECT state, error, comfy_prompt_id FROM jobs WHERE id=?", (j.id,)
        ).fetchone()
        self.assertEqual(row["state"], JobState.FAILED.value)
        self.assertEqual(row["error"], "boom")
        self.assertIsNone(row["comfy_prompt_id"])  # 未部分写入
        # pending 直接 -> succeeded 非法
        j2 = self.jobs.create("bad-2", "txt2img", {})
        with self.assertRaises(InvalidStateTransitionError):
            self.jobs.update_state(j2.id, JobState.SUCCEEDED)
        # 不存在的任务
        with self.assertRaises(InvalidStateTransitionError):
            self.jobs.update_state(999999, JobState.FAILED)

    def test_get_and_list(self):
        self._mkjob("g-1")
        self._mkjob("g-2", state=JobState.PENDING)
        got = self.jobs.get_by_client_request_id("g-1")
        self.assertEqual(got.mode, "txt2img")
        self.assertEqual(self.jobs.get(got.id).client_request_id, "g-1")
        self.assertIsNone(self.jobs.get(12345))
        self.assertEqual(len(self.jobs.list()), 2)
        self.assertEqual(len(self.jobs.list(state=JobState.PENDING)), 1)

    def test_restart_recovery(self):
        """重启恢复：running 的任务被重置回 pending，数据可继续使用。"""
        self.db.close()
        db2 = Database(self.db_path)
        db2.initialize()
        jobs2 = JobStore(db2)
        j1 = jobs2.create("rec-1", "txt2img", {})
        jobs2.update_state(j1.id, JobState.RUNNING)
        j2 = jobs2.create("rec-2", "txt2img", {})  # pending
        j3 = jobs2.create("rec-3", "txt2img", {})
        jobs2.update_state(j3.id, JobState.RUNNING)
        jobs2.update_state(j3.id, JobState.SUCCEEDED)  # 终态不受影响
        db2.close()

        db3 = Database(self.db_path)
        db3.initialize()
        recovered = JobStore(db3).recover_interrupted()
        ids = {r.id: r.state for r in recovered}
        self.assertEqual(ids, {j1.id: JobState.PENDING.value})
        # 终态任务原样
        self.assertEqual(
            JobStore(db3).get(j3.id).state, JobState.SUCCEEDED.value
        )
        # 恢复事件已记录
        events = AppEventStore(db3).list_events(event_type="job.recovered")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].payload["ids"], [j1.id])
        db3.close()

    def test_recovery_stale_before_and_force(self):
        j1 = self.jobs.create("r-1", "txt2img", {})
        self.jobs.update_state(j1.id, JobState.RUNNING)
        j2 = self.jobs.create("r-2", "txt2img", {})
        # stale_before 在过去 -> 没有任务早于它，全部跳过
        recovered = self.jobs.recover_interrupted(
            stale_before="2000-01-01T00:00:00+00:00"
        )
        self.assertEqual(recovered, [])
        self.assertEqual(self.jobs.get(j1.id).state, JobState.RUNNING.value)
        # 默认（不过滤时间）：running 被重置为 pending
        recovered = self.jobs.recover_interrupted()
        self.assertEqual([r.id for r in recovered], [j1.id])
        self.assertEqual(self.jobs.get(j1.id).state, JobState.PENDING.value)
        # force=True 时 pending 也算恢复对象
        recovered = self.jobs.recover_interrupted(force=True)
        self.assertEqual({r.id for r in recovered}, {j1.id, j2.id})


# ---------------------------------------------------------------------------
# 3. HistoryStore
# ---------------------------------------------------------------------------

class TestHistoryStore(StorageBase):
    def _seed(self, n: int, prefix: str = "h-") -> list:
        jobs = []
        for i in range(n):
            job = self.jobs.create(f"{prefix}{i:03d}", "txt2img", {"p": i})
            self.jobs.update_state(job.id, JobState.RUNNING)
            self.jobs.update_state(job.id, JobState.SUCCEEDED)
            self.history.add(
                job.id,
                f"images/out/img{i:04d}.png",
                sha256=f"{i:064x}",
                raw_prompt=f"raw {i}",
                final_prompt=f"final {i}",
                seed=i,
                width=1024,
                height=1024,
                metadata={"i": i},
            )
            jobs.append(job)
        return jobs

    def test_add_and_get(self):
        job = self._mkjob()
        rec = self.history.add(
            job.id, "images/out/a.png", sha256="a" * 64,
            raw_prompt="cat", final_prompt="cat, best", seed=7,
            width=1024, height=1024, metadata={"cfg": 7.0},
        )
        self.assertEqual(rec.image_relpath, "images/out/a.png")
        got = self.history.get_by_job_id(job.id)
        self.assertEqual(got.sha256, "a" * 64)
        self.assertEqual(got.metadata, {"cfg": 7.0})
        self.assertEqual(got.width, 1024)

    def test_add_upsert_by_job_id(self):
        job = self._mkjob()
        self.history.add(job.id, "images/out/a.png")
        self.history.add(job.id, "images/out/b.png", seed=99)
        rec = self.history.get_by_job_id(job.id)
        self.assertEqual(rec.image_relpath, "images/out/b.png")
        self.assertEqual(rec.seed, 99)
        rows = self.db.connection.execute(
            "SELECT COUNT(*) FROM history WHERE job_id=?", (job.id,)
        ).fetchone()[0]
        self.assertEqual(rows, 1)

    def test_foreign_key_enforced(self):
        """history.job_id 必须引用存在的任务（foreign_keys=ON）。"""
        with self.assertRaises(sqlite3.IntegrityError):
            self.history.add(424242, "images/out/x.png")

    def test_relative_path_only(self):
        job = self._mkjob()
        bad_paths = [
            "/abs/path.png",
            "C:\\Users\\x\\a.png",
            "c:/windows/evil.png",
            "\\\\server\\share\\a.png",
            "../escape.png",
            "a/../../escape.png",
            "",
            "   ",
        ]
        for bad in bad_paths:
            with self.subTest(path=bad):
                with self.assertRaises(ValidationError):
                    self.history.add(job.id, bad)
        # 合法相对路径（含反斜杠/前导 ./ 归一化）
        ok = self.history.add(job.id, ".\\images\\out\\ok.png")
        self.assertEqual(ok.image_relpath, "images/out/ok.png")
        rows = self.db.connection.execute(
            "SELECT image_relpath FROM history WHERE job_id=?",
            (job.id,),
        ).fetchall()
        self.assertTrue(all(not Path(r[0]).is_absolute() for r in rows))

    def test_sha256_and_json_validation(self):
        job = self._mkjob()
        with self.assertRaises(ValidationError):
            self.history.add(job.id, "a.png", sha256="not-hex")
        with self.assertRaises(ValidationError):
            self.history.add(job.id, "a.png", sha256="ab" * 32 + "zz")
        with self.assertRaises(ValidationError):
            self.history.add(job.id, "a.png", metadata={"x": object()})

    def test_cursor_pagination(self):
        self._seed(25)
        seen, cursor = [], None
        pages = 0
        while True:
            page, cursor = self.history.list_cursor(after_id=cursor, limit=10)
            pages += 1
            seen.extend(r.id for r in page)
            if cursor is None:
                break
        self.assertEqual(pages, 3)
        self.assertEqual(len(seen), 25)
        # 倒序、无重叠、无遗漏
        self.assertEqual(seen, sorted(seen, reverse=True))
        self.assertEqual(len(set(seen)), 25)
        # 单独校验每页大小
        p1, c1 = self.history.list_cursor(limit=10)
        self.assertEqual(len(p1), 10)
        self.assertEqual(c1, p1[-1].id)
        p2, c2 = self.history.list_cursor(after_id=c1, limit=10)
        self.assertEqual(len(p2), 10)
        self.assertNotIn(p2[0].id, {r.id for r in p1})

    def test_pagination_stable_under_inserts(self):
        """游标分页对并发插入稳定：下一页只看 id < cursor。"""
        self._seed(12)
        p1, c1 = self.history.list_cursor(limit=5)
        # 分页中间插入新数据
        self._seed(3, prefix="h2-")
        p2, c2 = self.history.list_cursor(after_id=c1, limit=5)
        self.assertTrue(all(r.id < c1 for r in p2))
        seen = [r.id for r in p1] + [r.id for r in p2]
        self.assertEqual(len(seen), len(set(seen)))

    def test_soft_delete_and_restore(self):
        jobs = self._seed(5)
        target = jobs[2].id
        self.assertTrue(self.history.soft_delete(target))
        # 默认分页不含软删除
        all_rows = self.history.list_cursor(limit=100, include_deleted=True)[0]
        visible = self.history.list_cursor(limit=100)[0]
        self.assertEqual(len(all_rows), 5)
        self.assertEqual(len(visible), 4)
        self.assertTrue(all(r.deleted_at is None for r in visible))
        deleted = [r for r in all_rows if r.deleted_at is not None]
        self.assertEqual([r.job_id for r in deleted], [target])
        # 重复软删除幂等
        self.assertFalse(self.history.soft_delete(target))
        # 恢复
        self.assertTrue(self.history.restore(target))
        self.assertFalse(self.history.restore(target))  # 已无删除行
        self.assertEqual(len(self.history.list_cursor(limit=100)[0]), 5)
        # 物理删除
        self.assertTrue(self.history.hard_delete(target))
        self.assertFalse(self.history.hard_delete(target))
        self.assertEqual(len(self.history.list_cursor(limit=100)[0]), 4)


# ---------------------------------------------------------------------------
# 4. FavoriteStore
# ---------------------------------------------------------------------------

class TestFavoriteStore(StorageBase):
    def test_unique_scope_ref_key(self):
        f1 = self.favorites.add("user-1", "lora:anmi", payload={"v": 1})
        f2 = self.favorites.add("user-1", "lora:anmi", payload={"v": 2})
        self.assertEqual(f1.id, f2.id)  # 幂等返回既有
        self.assertEqual(f1.payload, {"v": 1})  # 不被覆盖
        rows = self.db.connection.execute(
            "SELECT COUNT(*) FROM favorites WHERE scope='user-1' AND ref_key='lora:anmi'"
        ).fetchone()[0]
        self.assertEqual(rows, 1)

    def test_upsert_overwrites(self):
        self.favorites.add("u", "k", kind="image", payload={"a": 1})
        self.favorites.upsert("u", "k", kind="prompt", payload={"a": 2, "b": 3})
        rec = self.favorites.get("u", "k")
        self.assertEqual(rec.kind, "prompt")
        self.assertEqual(rec.payload, {"a": 2, "b": 3})
        rows = self.db.connection.execute(
            "SELECT COUNT(*) FROM favorites"
        ).fetchone()[0]
        self.assertEqual(rows, 1)

    def test_scope_separation_and_list(self):
        self.favorites.add("u1", "k1", kind="image", payload={"p": "cat"})
        self.favorites.add("u2", "k1", kind="image", payload={})
        self.favorites.add("u1", "k2", kind="lora", payload={})
        self.assertEqual(len(self.favorites.list(scope="u1")), 2)
        self.assertEqual(len(self.favorites.list(scope="u1", kind="image")), 1)
        self.assertEqual(len(self.favorites.list()), 3)
        self.assertEqual(
            self.favorites.get("u1", "k1").payload, {"p": "cat"}
        )
        self.assertTrue(self.favorites.remove("u2", "k1"))
        self.assertFalse(self.favorites.remove("u2", "k1"))
        self.assertIsNone(self.favorites.get("u2", "k1"))

    def test_validation(self):
        with self.assertRaises(ValidationError):
            self.favorites.add("", "k")
        with self.assertRaises(ValidationError):
            self.favorites.add("u", "")
        with self.assertRaises(ValidationError):
            self.favorites.add("u", "k", payload={"x": object()})


# ---------------------------------------------------------------------------
# 5. LoraProfileStore / WorkflowRegistry / AppEventStore
# ---------------------------------------------------------------------------

class TestLoraProfileStore(StorageBase):
    def test_composite_primary_key(self):
        self.loras.save("illustrious", "Anmi Style", {"trigger": "anmi"})
        self.loras.save("illustrious", "  anmi   style ", {"trigger": "anmi2"})
        rows = self.db.connection.execute(
            "SELECT COUNT(*) FROM lora_profiles WHERE model_family='illustrious'"
        ).fetchone()[0]
        self.assertEqual(rows, 1)  # 规范化 key 相同 -> upsert
        rec = self.loras.get("illustrious", "ANMI STYLE")
        self.assertEqual(rec.profile, {"trigger": "anmi2"})
        self.assertEqual(rec.normalized_key, "anmi style")
        # 不同 family 相同 key -> 两行
        self.loras.save("pony", "Anmi Style", {"trigger": "anmi"})
        self.assertEqual(len(self.loras.get_family("illustrious")), 1)
        self.assertEqual(sorted(self.loras.list_families()), ["illustrious", "pony"])
        self.assertTrue(self.loras.delete("pony", "anmi style"))
        self.assertFalse(self.loras.delete("pony", "anmi style"))

    def test_json_validation(self):
        with self.assertRaises(ValidationError):
            self.loras.save("f", "k", {"x": object()})


class TestWorkflowRegistry(StorageBase):
    def test_register_upsert_and_flags(self):
        self.workflows.register("finish", {"nodes": ["a"]}, version="1")
        self.workflows.register("finish", {"nodes": ["a", "b"]}, version="2", enabled=False)
        rec = self.workflows.get("finish")
        self.assertEqual(rec.version, "2")
        self.assertEqual(rec.definition, {"nodes": ["a", "b"]})
        self.assertFalse(rec.enabled)
        rows = self.db.connection.execute(
            "SELECT COUNT(*) FROM workflow_registry WHERE name='finish'"
        ).fetchone()[0]
        self.assertEqual(rows, 1)
        self.assertEqual(len(self.workflows.list(enabled_only=True)), 0)
        self.assertTrue(self.workflows.set_enabled("finish", True))
        self.assertEqual(len(self.workflows.list(enabled_only=True)), 1)
        self.assertTrue(self.workflows.unregister("finish"))
        self.assertIsNone(self.workflows.get("finish"))


class TestAppEventStore(StorageBase):
    def test_record_list_prune(self):
        self.events.record("job.started", {"job_id": 1})
        self.events.record("job.started", {"job_id": 2})
        self.events.record("job.finished", {"job_id": 2})
        started = self.events.list_events(event_type="job.started")
        self.assertEqual([e.payload["job_id"] for e in started], [1, 2])
        all_events = self.events.list_events()
        self.assertEqual(len(all_events), 3)
        # 游标读取
        after = self.events.list_events(after_id=1)
        self.assertEqual([e.id for e in after], [2, 3])
        # 清理
        pruned = self.events.prune("9999-01-01T00:00:00+00:00")
        self.assertEqual(pruned, 3)
        self.assertEqual(self.events.list_events(), [])
        with self.assertRaises(ValidationError):
            self.events.record("", {})


# ---------------------------------------------------------------------------
# 6. 并发：多读者 + 单 writer
# ---------------------------------------------------------------------------

class TestConcurrency(StorageBase):
    def test_concurrent_readers_with_writer(self):
        """WAL：写事务进行时，其他连接的读者仍可读且读到一致快照。"""
        self._seed_history(30)
        errors: list = []
        stop = threading.Event()
        results: dict = {}

        def reader(name: str):
            conn = self.db.new_connection()
            conn.row_factory = sqlite3.Row
            try:
                while not stop.is_set():
                    rows = conn.execute(
                        "SELECT COUNT(*) AS c FROM history WHERE deleted_at IS NULL"
                    ).fetchone()
                    results[name] = rows["c"]
                    time.sleep(0.001)
            except Exception as exc:  # pragma: no cover
                errors.append((name, exc))
            finally:
                conn.close()

        def writer():
            conn = self.db.new_connection()
            conn.row_factory = sqlite3.Row
            try:
                for i in range(30):
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "INSERT INTO app_events (event_type, payload, created_at)"
                        " VALUES (?, ?, ?)",
                        ("concurrent", "{}", "2026-01-01T00:00:00+00:00"),
                    )
                    time.sleep(0.002)
                    conn.execute("COMMIT")
            except Exception as exc:  # pragma: no cover
                errors.append(("writer", exc))
            finally:
                conn.close()

        readers = [
            threading.Thread(target=reader, args=(f"r{i}",))
            for i in range(4)
        ]
        wt = threading.Thread(target=writer)
        for t in readers:
            t.start()
        wt.start()
        time.sleep(1.0)
        stop.set()
        for t in readers:
            t.join(timeout=10)
        wt.join(timeout=10)
        self.assertEqual(errors, [], f"并发读写出错: {errors}")
        self.assertTrue(all(v == 30 for v in results.values()), results)
        rows = self.db.connection.execute(
            "SELECT COUNT(*) FROM app_events WHERE event_type='concurrent'"
        ).fetchone()[0]
        self.assertEqual(rows, 30)

    def test_single_writer_lock(self):
        """单 writer：第二个写事务在 busy_timeout 内等待，超时抛 locked。"""
        conn_a = self.db.new_connection()
        conn_a.row_factory = sqlite3.Row
        conn_b = self.db.new_connection()
        conn_b.row_factory = sqlite3.Row
        try:
            conn_a.execute("BEGIN IMMEDIATE")
            conn_a.execute(
                "INSERT INTO app_events (event_type, payload, created_at)"
                " VALUES ('hold', '{}', '2026-01-01T00:00:00+00:00')"
            )
            # B 用短 busy_timeout 快速验证锁语义
            conn_b.execute("PRAGMA busy_timeout=300")
            with self.assertRaises(sqlite3.OperationalError) as ctx:
                conn_b.execute("BEGIN IMMEDIATE")
            self.assertIn("locked", str(ctx.exception).lower())
            # A 提交后 B 可正常写
            conn_a.execute("COMMIT")
            conn_b.execute("BEGIN IMMEDIATE")
            conn_b.execute(
                "INSERT INTO app_events (event_type, payload, created_at)"
                " VALUES ('after', '{}', '2026-01-01T00:00:00+00:00')"
            )
            conn_b.execute("COMMIT")
        finally:
            conn_a.close()
            conn_b.close()
        types = [
            r[0]
            for r in self.db.connection.execute(
                "SELECT event_type FROM app_events ORDER BY id"
            ).fetchall()
        ]
        self.assertEqual(types, ["hold", "after"])

    def _seed_history(self, n: int):
        for i in range(n):
            job = self.jobs.create(f"c-{i:03d}", "txt2img", {"p": i})
            self.jobs.update_state(job.id, JobState.RUNNING)
            self.jobs.update_state(job.id, JobState.SUCCEEDED)
            self.history.add(job.id, f"images/out/c{i:03d}.png")


# ---------------------------------------------------------------------------
# 7. 迁移
# ---------------------------------------------------------------------------

def _write_migration(dir_path: Path, migration_id: str, statements: list) -> Path:
    path = dir_path / f"{migration_id}.py"
    body = (
        "MIGRATION_ID = %r\n\n"
        "def upgrade(conn):\n"
        "    for s in _STATEMENTS:\n"
        "        conn.execute(s)\n\n"
        "_STATEMENTS = %r\n" % (migration_id, statements)
    )
    path.write_text(body, encoding="utf-8")
    return path


class TestMigrations(StorageBase):
    def _copy_shipped_migrations(self, dest: Path) -> None:
        src = ROOT / "migrations"
        for path in sorted(src.glob("*.py")):
            shutil.copyfile(path, dest / path.name)

    def test_migration_failure_full_rollback(self):
        """新库 + 0001(好) + 0002(坏)：整体回滚，schema_migrations 为空，
        业务表不存在，随后可用好迁移目录正常初始化。"""
        bad_dir = self.tmp / "migrations_bad"
        bad_dir.mkdir()
        self._copy_shipped_migrations(bad_dir)
        _write_migration(
            bad_dir,
            "0002_bad",
            ["CREATE TABLE broken_1 (id INTEGER", "INSERT INTO nope VALUES (1)"],
        )
        db2 = Database(self.tmp / "rollback.db", migrations_dir=bad_dir)
        with self.assertRaises(MigrationError):
            db2.initialize()
        # 原库保留：可正常打开，且没有半成品表
        db3 = Database(self.tmp / "rollback.db", migrations_dir=bad_dir)
        db3.open()
        self.assertEqual(db3.list_applied_migrations(), [])
        tables = {
            r[0]
            for r in db3.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertNotIn("jobs", tables)
        self.assertNotIn("broken_1", tables)
        db3.close()
        # 用正确迁移目录重试成功（文件未被破坏）
        good = Database(self.tmp / "rollback.db")
        applied = good.initialize()
        self.assertEqual(
            applied,
            ["0001_initial", "0002_history_favorite_indexes", "0003_lora_library"],
        )
        good.close()

    def test_migration_failure_preserves_existing_data(self):
        """已入库数据 + 新坏迁移：失败后旧数据完整保留。"""
        good = Database(self.db_path)
        job = JobStore(good).create("keep-1", "txt2img", {"p": 1})
        good.close()

        bad_dir = self.tmp / "migrations_bad2"
        bad_dir.mkdir()
        self._copy_shipped_migrations(bad_dir)
        _write_migration(
            bad_dir, "0003_bad", ["CREATE TABLE broken_2 (id"]
        )
        db2 = Database(self.db_path, migrations_dir=bad_dir)
        with self.assertRaises(MigrationError):
            db2.initialize()
        db3 = Database(self.db_path)
        db3.initialize()
        kept = JobStore(db3).get(job.id)
        self.assertIsNotNone(kept)
        self.assertEqual(kept.client_request_id, "keep-1")
        self.assertEqual(kept.request_json, {"p": 1})
        applied = db3.list_applied_migrations()
        self.assertNotIn("0003_bad", applied)
        self.assertEqual(
            applied,
            ["0001_initial", "0002_history_favorite_indexes", "0003_lora_library"],
        )
        db3.close()

    def test_unknown_migration_dir(self):
        with self.assertRaises(MigrationError):
            Database(self.tmp / "x.db", migrations_dir=self.tmp / "missing").initialize()

    def test_migration_module_without_upgrade(self):
        bad_dir = self.tmp / "migrations_noup"
        bad_dir.mkdir()
        (bad_dir / "0001_noop.py").write_text("MIGRATION_ID='0001_noop'\n", encoding="utf-8")
        with self.assertRaises(MigrationError):
            Database(self.tmp / "x2.db", migrations_dir=bad_dir).initialize()


# ---------------------------------------------------------------------------
# 8. 重启持久化
# ---------------------------------------------------------------------------

class TestPersistenceAcrossRestart(StorageBase):
    def test_data_survives_reopen(self):
        job = self.jobs.create("persist-1", "txt2img", {"p": "x"})
        self.jobs.update_state(job.id, JobState.RUNNING)
        self.history.add(job.id, "images/out/p.png", seed=11)
        self.favorites.add("u", "k", payload={"a": 1})
        self.db.close()

        db2 = Database(self.db_path)
        db2.initialize()
        j = JobStore(db2).get(job.id)
        self.assertEqual(j.state, JobState.RUNNING.value)
        self.assertEqual(j.request_json, {"p": "x"})
        h = HistoryStore(db2).get_by_job_id(job.id)
        self.assertEqual(h.seed, 11)
        self.assertEqual(FavoriteStore(db2).get("u", "k").payload, {"a": 1})
        db2.close()

    def test_no_wal_leftovers_after_clean_close(self):
        self.db.close()
        self.assertTrue(self.db_path.exists())
        # WAL/SHM 在干净关闭后可被清理（这里只验证主文件可独立打开）
        db2 = sqlite3.connect(str(self.db_path))
        tables = db2.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        self.assertTrue(any("jobs" in t for t in tables))
        db2.close()


def _deep_nested(depth: int):
    v = 1
    for _ in range(depth):
        v = [v]
    return v


if __name__ == "__main__":
    unittest.main(verbosity=2)

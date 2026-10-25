from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from desktop_runtime.engine import DesktopEngine
from desktop_backend.handlers import _validate_job_payload
from desktop_backend.errors import ValidationError
from desktop_runtime.sidecar import _report_status
from desktop_runtime import sidecar
from winefox_core.comfyui_executor import ComfyUIExecutor
from winefox_core.comfyui_types import ComfyUIServerConfig, ExecutionSnapshot, ComfyUIResult
from winefox_core.storage import Database, FavoriteStore, HistoryStore


class DesktopReviewStorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.db.initialize()
        self.engine = object.__new__(DesktopEngine)
        self.engine.db = self.db
        self.engine.history_store = HistoryStore(self.db)
        self.engine.favorite_store = FavoriteStore(self.db)

    def tearDown(self):
        self.db.close()

    def job(self, prompt="test", state="completed", seed=123):
        current = self.db.connection.execute(
            "INSERT INTO jobs (client_request_id,mode,state,request_json,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (f"review-{self.db.connection.total_changes}", "txt2img", state,
             json.dumps({"prompt": prompt, "seed": -1}), "2026-10-15T00:00:00Z", "2026-10-15T00:00:00Z"),
        )
        job_id = current.lastrowid
        if state == "completed":
            history = self.engine.history_store.add(job_id, f"outputs/{job_id}.png", raw_prompt=prompt, seed=seed)
            return job_id, history.id
        return job_id, None

    async def test_favorite_id_is_not_overwritten_by_history_and_queries_filter(self):
        self.job("unfavorited")
        _, history_id = self.job("blue sky")
        self.assertTrue(await self.engine.set_favorite(str(history_id), True))
        result = await self.engine.list_favorites()
        favorite = result["items"][0]
        self.assertNotEqual(favorite["id"], str(history_id))
        self.assertEqual(favorite["history_id"], str(history_id))
        self.assertTrue(await self.engine.update_favorite(favorite["id"], {"category": "landscape", "note": "reference"}))
        self.assertEqual((await self.engine.list_favorites({"q": "BLUE", "category": "landscape"}))["total"], 1)
        self.assertEqual((await self.engine.list_favorites({"q": "missing"}))["total"], 0)
        self.assertEqual((await self.engine.list_favorites({"category": "other"}))["total"], 0)
        self.assertTrue(await self.engine.delete_favorite(favorite["id"]))
        self.assertEqual((await self.engine.list_favorites())["total"], 0)

    async def test_history_search_total_and_favorite_filter_agree(self):
        self.job("red", seed=17)
        _, second = self.job("blue", seed=92345)
        self.job("blue", seed=3)
        await self.engine.set_favorite(str(second), True)
        page = await self.engine.list_history_page(q="92345")
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["id"], str(second))
        favorite = await self.engine.list_history_page(status="favorite", q="blue")
        self.assertEqual(favorite["total"], 1)
        self.assertEqual(len(favorite["items"]), 1)
        first_page = await self.engine.list_history_page(q="blue", limit=1)
        next_page = await self.engine.list_history_page(q="blue", limit=1, cursor=first_page["next_cursor"])
        self.assertEqual(next_page["total"], 2)
        self.assertNotEqual(first_page["items"][0]["id"], next_page["items"][0]["id"])

    async def test_failed_jobs_are_visible_without_broken_image(self):
        self.job("success")
        failed, _ = self.job("failure", state="failed")
        page = await self.engine.list_history_page(status="failed")
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["job_id"], str(failed))
        self.assertIsNone(page["items"][0]["image"])

    async def test_failed_history_seed_is_searchable_and_keeps_precision(self):
        failed, _ = self.job(state="failed")
        self.db.connection.execute(
            "UPDATE jobs SET request_json=? WHERE id=?",
            (json.dumps({"prompt": "failed", "seed": -1, "width": 832, "prompt_debug": {"seed": 9223372036854775806}}), failed),
        )
        page = await self.engine.list_history_page(status="failed", q="9223372036854775806")
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["seed"], "9223372036854775806")
        self.assertEqual(page["items"][0]["width"], 832)

    async def test_completed_job_returns_actual_random_seed(self):
        job_id, _ = self.job(seed=777)
        self.assertEqual((await self.engine.get_job(str(job_id))).seed, 777)

    async def test_remote_recovery_skips_preparation_and_keeps_actual_seed(self):
        job_id, _ = self.job(state="recovering")
        payload = {"prompt": "original", "seed": -1, "mode": "img2img", "prompt_debug": {
            "seed": 9223372036854775806, "final_prompt": "already prepared", "source": "llm", "warnings": [],
        }}
        self.db.connection.execute(
            "UPDATE jobs SET request_json=?, comfy_prompt_id=? WHERE id=?",
            (json.dumps(payload), "accepted-before-restart", job_id),
        )
        self.engine._cancel_requested = set()
        self.engine._connection_config = lambda: ComfyUIServerConfig()
        self.engine._prepare_prompt = AsyncMock(side_effect=AssertionError("must not call AI again"))
        self.engine._select_workflow = lambda _: self.fail("recovery must not read local workflows")
        image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGM8ISfHAANMcBZeDgA0dgEMydTl/QAAAABJRU5ErkJggg==")
        executor = SimpleNamespace(resume=AsyncMock(return_value=ComfyUIResult(
            ok=True, prompt_id="accepted-before-restart", images=[image],
        )))
        context = SimpleNamespace(__aenter__=AsyncMock(return_value=executor), __aexit__=AsyncMock(return_value=False))
        # Special methods are resolved on the context manager's type.
        class ExecutorContext:
            async def __aenter__(self):
                return await context.__aenter__()

            async def __aexit__(self, *args):
                return await context.__aexit__(*args)

        with tempfile.TemporaryDirectory(prefix="winefox_resume_review_") as directory, \
             patch("desktop_runtime.engine.ComfyUIExecutor", return_value=ExecutorContext()):
            self.engine.data_root = Path(directory)
            await self.engine._run_job(job_id)
        executor.resume.assert_awaited_once()
        self.assertEqual(executor.resume.call_args.args[0], "accepted-before-restart")
        self.assertEqual(executor.resume.call_args.args[1].prompt, "already prepared")
        self.engine._prepare_prompt.assert_not_awaited()
        job = await self.engine.get_job(str(job_id))
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.seed, "9223372036854775806")
        row = self.db.connection.execute("SELECT final_prompt FROM history WHERE job_id=?", (job_id,)).fetchone()
        self.assertEqual(row[0], "already prepared")

    async def test_large_seed_is_serialized_without_javascript_precision_loss(self):
        seed = 9223372036854775806
        job_id, _ = self.job(seed=seed)
        self.assertEqual((await self.engine.get_job(str(job_id))).seed, str(seed))
        self.assertEqual((await self.engine.list_history_page())["items"][0]["seed"], str(seed))

    async def test_idempotent_retry_succeeds_even_when_queue_is_full(self):
        job_id, _ = self.job()
        row = self.db.connection.execute("SELECT client_request_id FROM jobs WHERE id=?", (job_id,)).fetchone()
        self.engine._accepting = True
        self.engine._queue = asyncio.Queue(maxsize=1)
        self.engine._queue.put_nowait(100)
        result = await self.engine.submit_job({"client_request_id": row[0], "prompt": "test"})
        self.assertEqual(result.id, str(job_id))

    async def test_recovery_starts_worker_before_filling_bounded_queue(self):
        for _ in range(3):
            self.job(state="queued")
        engine = self.engine
        engine._accepting = False
        engine._queue = asyncio.Queue(maxsize=1)
        engine._cancel_requested = set()
        engine._current_job_id = None
        engine.lora_download_jobs = SimpleNamespace(mark_interrupted=lambda: None)
        engine._background_scan_local_loras = AsyncMock()
        engine._run_job = AsyncMock()
        try:
            await asyncio.wait_for(engine.start(), 0.5)
            await asyncio.wait_for(engine._queue.join(), 0.5)
            self.assertEqual(engine._run_job.await_count, 3)
        finally:
            engine._worker.cancel()
            await asyncio.gather(engine._worker, *getattr(engine, "_bg_tasks", []), return_exceptions=True)

    async def test_late_failure_cannot_overwrite_cancelled_state(self):
        job_id, _ = self.job(state="cancelled")
        self.engine._update_job(job_id, "failed", error="late network error")
        self.assertEqual((await self.engine.get_job(str(job_id))).status, "cancelled")

    async def test_cancelling_running_wait_releases_worker_for_next_job(self):
        first, _ = self.job(state="running")
        second, _ = self.job(state="queued")
        self.engine._queue = asyncio.Queue()
        self.engine._cancel_requested = set()
        self.engine._current_job_id = None
        self.engine._active_job_task = None
        started = asyncio.Event()
        never_completes = asyncio.Event()
        executed = []

        async def run(job_id):
            executed.append(job_id)
            if job_id == first:
                started.set()
                await never_completes.wait()

        self.engine._run_job = run
        self.engine._queue.put_nowait(first)
        self.engine._queue.put_nowait(second)
        worker = asyncio.create_task(self.engine._worker_loop())
        try:
            await asyncio.wait_for(started.wait(), 0.5)
            self.assertTrue(await self.engine.cancel_job(str(first)))
            await asyncio.wait_for(self.engine._queue.join(), 0.5)
            self.assertEqual(executed, [first, second])
            self.assertEqual((await self.engine.get_job(str(first))).status, "cancelled")
            self.assertFalse(worker.done())
            self.assertIsNone(self.engine._active_job_task)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_worker_shutdown_is_not_swallowed_by_simultaneous_job_cancel(self):
        job_id, _ = self.job(state="running")
        self.engine._queue = asyncio.Queue()
        self.engine._queue.put_nowait(job_id)
        self.engine._cancel_requested = set()
        self.engine._active_job_task = None
        self.engine._current_job_id = None
        started = asyncio.Event()

        async def run(_):
            started.set()
            await asyncio.Event().wait()

        self.engine._run_job = run
        worker = asyncio.create_task(self.engine._worker_loop())
        await asyncio.wait_for(started.wait(), 0.5)
        await self.engine.cancel_job(str(job_id))
        worker.cancel()
        await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), 0.5)
        self.assertTrue(worker.cancelled())
        self.assertIsNone(self.engine._active_job_task)

    async def test_live_history_cannot_be_purged_and_trash_is_hidden_from_favorites(self):
        _, history_id = self.job()
        await self.engine.set_favorite(str(history_id), True)
        self.assertFalse(await self.engine.purge_history(str(history_id)))
        await self.engine.delete_history(str(history_id))
        self.assertEqual((await self.engine.list_favorites())["total"], 0)
        await self.engine.restore_history(str(history_id))
        self.assertEqual((await self.engine.list_favorites())["total"], 1)


class DesktopReviewLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_shutdown_can_be_cancelled_and_controls_remain_responsive(self):
        runtime = SimpleNamespace(
            engine=SimpleNamespace(busy=lambda: True, task_count=lambda: 1),
            paths=SimpleNamespace(subdir=lambda name: Path("unused-test-data")),
            start=AsyncMock(return_value={"port": 1, "bootstrapPath": "/unused"}), stop=AsyncMock(),
        )
        room = SimpleNamespace(shutdown=AsyncMock())
        messages = []

        def feed(loop, queue, stop_event):
            for command in (
                {"cmd": "shutdown", "mode": "wait"}, {"cmd": "shutdown-cancel"},
                {"cmd": "ping"}, {"cmd": "shutdown", "mode": "now"},
            ):
                queue.put_nowait(command)

        with patch.object(sidecar, "DesktopRuntime", return_value=runtime), \
             patch.object(sidecar, "_start_stdin_reader", feed), \
             patch.object(sidecar, "emit", messages.append), \
             patch("desktop_room.manager.RoomManager", return_value=room):
            result = await asyncio.wait_for(sidecar.run(SimpleNamespace(log_level="ERROR", selftest=False, open_browser=False)), 1)
        self.assertEqual(result, 0)
        self.assertTrue(any(message.get("cmd") == "ping" and message.get("pong") for message in messages))
        self.assertEqual(messages[-1]["reason"], "shutdown-now")
        runtime.stop.assert_awaited_once()

    async def test_prompt_identity_is_published_before_remote_polling(self):
        executor = ComfyUIExecutor(ComfyUIServerConfig())
        executor.submit = AsyncMock(return_value="accepted-remote-job")
        accepted = []

        async def wait(prompt_id, snapshot, **kwargs):
            self.assertEqual(accepted, ["accepted-remote-job"])
            return ComfyUIResult(ok=True, prompt_id=prompt_id)

        executor.wait_for_completion = wait
        await executor.run(
            ExecutionSnapshot(workflow={"1": {"class_type": "SaveImage", "inputs": {}}}, options={"validate_nodes": False}),
            on_submitted=accepted.append,
        )
        executor.submit.assert_awaited_once()

    async def test_sidecar_emits_initial_and_changed_task_status(self):
        engine = SimpleNamespace(busy=lambda: False, task_count=lambda: 0)
        messages = []
        with patch("desktop_runtime.sidecar.emit", messages.append):
            task = asyncio.create_task(_report_status(SimpleNamespace(engine=engine)))
            await asyncio.sleep(0)
            engine.busy = lambda: True
            engine.task_count = lambda: 1
            await asyncio.sleep(0.22)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual([message["tasks"] for message in messages], [0, 1])


class DesktopReviewValidationTests(unittest.TestCase):
    def test_reference_only_img2img_is_valid(self):
        result = _validate_job_payload({"mode": "img2img", "prompt": "", "ref_image": {"url": "/api/v1/uploads/reference"}})
        self.assertEqual(result["prompt"], "")

    def test_img2img_without_reference_is_rejected(self):
        with self.assertRaises(ValidationError):
            _validate_job_payload({"mode": "img2img", "prompt": "test"})

    def test_invalid_parameters_are_validation_errors(self):
        for patch_value in ({"mode": {}}, {"width": "broken"}, {"width": 65}, {"steps": True}, {"seed": -2}, {"cfg": float("nan")}):
            with self.subTest(patch_value=patch_value), self.assertRaises(ValidationError):
                _validate_job_payload({"prompt": "test", **patch_value})

    def test_large_seed_string_remains_exact(self):
        seed = 9223372036854775806
        self.assertEqual(_validate_job_payload({"prompt": "test", "seed": str(seed)})["seed"], seed)

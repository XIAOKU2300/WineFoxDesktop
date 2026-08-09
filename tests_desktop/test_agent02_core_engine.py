# -*- coding: utf-8 -*-
"""Agent 02 — winefox_core 单元测试（unittest + 内存 fake）。

覆盖：冻结类型校验与 JSON 往返、稳定错误码与异常映射、正常状态链、
client_request_id 幂等、队列满、queued 取消、running detach、异常映射、
prompt_id 恢复、无 prompt_id 的未知提交、stop 拒绝新任务、EventSink 顺序、
非法状态迁移、CoreEngine 门面。

运行：python -m unittest discover -s tests_desktop -v
"""

from __future__ import annotations

import asyncio
import copy
import ssl
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winefox_core.errors import (  # noqa: E402
    COMFYUI_AUTH_FAILED,
    COMFYUI_OFFLINE,
    COMFYUI_TLS_FAILED,
    COMFYUI_TIMEOUT,
    CHECKPOINT_NOT_FOUND,
    CONFIG_INVALID,
    DISK_SPACE_LOW,
    INVALID_IMAGE,
    INTERNAL_ERROR,
    JOB_NOT_CANCELLABLE,
    JOB_NOT_DELETABLE,
    JOB_NOT_FAVORITABLE,
    JOB_NOT_FOUND,
    JOB_NOT_CANCELLABLE as _,
    LORA_NOT_FOUND,
    QUEUE_FULL,
    SECRET_STORE_FAILED,
    UNKNOWN_SUBMISSION_STATE,
    UPLOAD_TOO_LARGE,
    WORKFLOW_NODE_MISSING,
    WORKFLOW_NOT_FOUND,
    BACKEND_RESTARTED,
    AppError,
    classify_exception,
    KNOWN_ERROR_CODES,
)
from winefox_core.jobs import ALLOWED_TRANSITIONS, JobManager
from winefox_core.models import (
    ACTIVE_JOB_STATES,
    EVENT_ENGINE_STARTED,
    EVENT_ENGINE_STOPPED,
    EVENT_JOB_AUDITING,
    EVENT_JOB_CANCELLED,
    EVENT_JOB_COMPLETED,
    EVENT_JOB_DETACHED,
    EVENT_JOB_DOWNLOADING,
    EVENT_JOB_FAILED,
    EVENT_JOB_PREPARING,
    EVENT_JOB_QUEUED,
    EVENT_JOB_RECOVERING,
    EVENT_JOB_RUNNING,
    EVENT_JOB_SUBMITTING,
    EVENT_JOB_FAVORITED,
    AUDIT_SCORE_KEYS,
    ComfyHistoryEntry,
    ComfyImageRef,
    ComfyQueueSnapshot,
    ComfySubmission,
    ComfyUIStatus,
    CoreEvent,
    GenerationMode,
    GenerationRequest,
    GenerationResult,
    ImageAuditResult,
    JobRecord,
    JobSnapshot,
    JobState,
    LoraSelection,
    ModelFamily,
    WorkflowBuild,
)
from winefox_core.engine import CoreEngine
from winefox_core.ports import (
    AuditPort,
    CatalogPort,
    Clock,
    ComfyUIPort,
    ConfigStorePort,
    EventSink,
    ImageStorePort,
    JobStorePort,
    PromptPort,
    SecretStorePort,
)

UTC = timezone.utc

REQUIRED_ERROR_CODES = [
    "COMFYUI_OFFLINE", "COMFYUI_AUTH_FAILED", "COMFYUI_TLS_FAILED", "COMFYUI_TIMEOUT",
    "WORKFLOW_NOT_FOUND", "WORKFLOW_NODE_MISSING", "CHECKPOINT_NOT_FOUND",
    "LORA_NOT_FOUND", "INVALID_IMAGE", "UPLOAD_TOO_LARGE", "QUEUE_FULL",
    "JOB_NOT_CANCELLABLE", "DISK_SPACE_LOW", "CONFIG_INVALID",
    "SECRET_STORE_FAILED", "BACKEND_RESTARTED", "UNKNOWN_SUBMISSION_STATE",
]


def make_request(crid: str = "req-1", **overrides: Any) -> GenerationRequest:
    values: Dict[str, Any] = dict(
        client_request_id=crid,
        mode=GenerationMode.TXT2IMG,
        raw_prompt="1girl, cat ears, masterpiece",
        source_upload_ids=[],
        model_family=ModelFamily.ILLUSTRIOUS,
    )
    values.update(overrides)
    return GenerationRequest(**values)


def make_success_entry(output_count: int = 1) -> ComfyHistoryEntry:
    return ComfyHistoryEntry(
        status="success",
        completed=True,
        outputs=[ComfyImageRef(f"out_{i}.png", subfolder="", image_type="output") for i in range(output_count)],
    )


# ---------------------------------------------------------------------------
# 内存 fakes
# ---------------------------------------------------------------------------

class FakeClock(Clock):
    """tick 时钟：每次 now()/monotonic() 调用严格递增，保证排序确定性。"""

    def __init__(self) -> None:
        self._base = datetime(2025, 1, 1, tzinfo=UTC)
        self._tick = 0

    def now(self) -> datetime:
        self._tick += 1
        return self._base + timedelta(microseconds=self._tick)

    def monotonic(self) -> float:
        self._tick += 1
        return 1000.0 + self._tick / 1_000_000.0

    def advance(self, seconds: float) -> None:
        self._tick += int(seconds * 1_000_000)


class OrderLog:
    """共享顺序日志：store.save 与 events.emit 都会追加 (kind, ...)。"""

    def __init__(self) -> None:
        self.entries: List[tuple] = []


class FakeJobStore(JobStorePort):
    def __init__(self, order: OrderLog, clock: FakeClock) -> None:
        self._records: Dict[str, JobRecord] = {}
        self._order = order
        self._clock = clock

    def save(self, record: JobRecord) -> None:
        self._records[record.id] = record
        self._order.entries.append(("save", record.id, record.state, self._clock.monotonic()))

    def get(self, job_id: str) -> Optional[JobRecord]:
        return self._records.get(job_id)

    def get_by_client_request_id(self, client_request_id: str) -> Optional[JobRecord]:
        for record in self._records.values():
            if record.client_request_id == client_request_id:
                return record
        return None

    def list(self, limit: int = 200, states=None) -> List[JobRecord]:
        items = [r for r in self._records.values() if states is None or r.state in set(states)]
        items.sort(key=lambda r: (r.created_at, r.id))
        return items[:limit]

    def next_queued(self) -> Optional[JobRecord]:
        items = [r for r in self._records.values() if r.state == JobState.QUEUED]
        items.sort(key=lambda r: (r.created_at, r.id))
        return items[0] if items else None

    def delete(self, job_id: str) -> None:
        self._records.pop(job_id, None)

    @property
    def records(self) -> Dict[str, JobRecord]:
        return self._records


class FakeEventSink(EventSink):
    def __init__(self, order: OrderLog, clock: FakeClock) -> None:
        self.events: List[CoreEvent] = []
        self._order = order
        self._clock = clock

    def emit(self, event: CoreEvent) -> None:
        self.events.append(event)
        self._order.entries.append(("emit", event.job_id, event.type, self._clock.monotonic()))

    def job_events(self, job_id: str) -> List[str]:
        return [e.type for e in self.events if e.job_id == job_id]


class FakeComfy(ComfyUIPort):
    def __init__(self) -> None:
        self.history: Dict[str, Optional[ComfyHistoryEntry]] = {}
        self.queue_running: List[str] = []
        self.queue_pending: List[str] = []
        self.submit_error: Optional[BaseException] = None
        self.history_error: Optional[BaseException] = None
        self.submit_block: Optional[asyncio.Event] = None
        self.interrupt_calls: List[str] = []
        self.submissions: List[str] = []
        self.image_bytes = b"fake-png-bytes"
        self.probe_status = ComfyUIStatus(online=True, version="0.3.5", device="cuda", node_count=120, latency_ms=4.2)
        # True 时 submit 立即登记成功历史（自动完成）；False 时保持 running 直到测试手动登记
        self.auto_complete = True

    async def probe(self) -> ComfyUIStatus:
        return self.probe_status

    async def submit(self, workflow: Dict[str, Any], client_id: str) -> ComfySubmission:
        self.submissions.append(client_id)
        if self.submit_block is not None:
            await self.submit_block.wait()
        if self.submit_error is not None:
            raise self.submit_error
        prompt_id = f"pid-{len(self.submissions):03d}"
        self.queue_running.append(prompt_id)
        if self.auto_complete:
            self.history[prompt_id] = make_success_entry()
        return ComfySubmission(prompt_id=prompt_id, queue_remaining=max(len(self.queue_pending), 0))

    async def fetch_history(self, prompt_id: str) -> Optional[ComfyHistoryEntry]:
        if self.history_error is not None:
            raise self.history_error
        if prompt_id not in self.history:
            return None
        entry = self.history[prompt_id]
        if entry is not None and entry.completed and prompt_id in self.queue_running:
            self.queue_running.remove(prompt_id)
        return entry

    async def queue_snapshot(self) -> ComfyQueueSnapshot:
        return ComfyQueueSnapshot(running=list(self.queue_running), pending=list(self.queue_pending))

    async def fetch_image_bytes(self, image: ComfyImageRef) -> bytes:
        return self.image_bytes

    async def upload_image(self, source_path: str) -> str:
        return "uploaded.png"

    async def interrupt(self) -> None:
        self.interrupt_calls.append("interrupt")


class FakePrompt(PromptPort):
    def __init__(self) -> None:
        self.build_error: Optional[BaseException] = None
        self.builds: List[GenerationRequest] = []

    def build_workflow(self, request: GenerationRequest) -> WorkflowBuild:
        self.builds.append(request)
        if self.build_error is not None:
            raise self.build_error
        return WorkflowBuild(
            workflow={"3": {"class_type": "KSampler", "inputs": {"seed": request.seed or 42}}},
            final_prompt=request.raw_prompt,
            seed=request.seed if request.seed is not None else 42,
            width=request.width or 512,
            height=request.height or 512,
            applied_loras=list(request.loras),
            meta={"family": request.model_family.value},
        )


class FakeImageStore(ImageStorePort):
    def __init__(self) -> None:
        self.images: Dict[str, bytes] = {}
        self.metadata: Dict[str, Dict[str, Any]] = {}
        self._counter = 0
        self.store_error: Optional[BaseException] = None

    def store_image(self, job_id: str, data: bytes, metadata: Dict[str, Any]) -> str:
        if self.store_error is not None:
            raise self.store_error
        self._counter += 1
        image_id = f"img-{self._counter:03d}"
        self.images[image_id] = data
        self.metadata[image_id] = dict(metadata)
        return image_id

    def read_image(self, image_id: str) -> Optional[bytes]:
        return self.images.get(image_id)

    def delete_image(self, image_id: str) -> None:
        self.images.pop(image_id, None)
        self.metadata.pop(image_id, None)


class FakeConfig(ConfigStorePort):
    def __init__(self, initial: Optional[Dict[str, Any]] = None) -> None:
        self.data: Dict[str, Any] = dict(initial or {})
        self.updates: List[tuple] = []

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def get_all(self) -> Dict[str, Any]:
        return dict(self.data)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    def update_section(self, section: str, values: Dict[str, Any]) -> None:
        for key, value in values.items():
            self.data[f"{section}.{key}"] = value
        self.updates.append((section, dict(values)))


class FakeSecrets(SecretStorePort):
    def __init__(self) -> None:
        self.data: Dict[str, str] = {}
        self.fail = False

    def get(self, key: str) -> Optional[str]:
        if self.fail:
            raise AppError(SECRET_STORE_FAILED, "fake secret store failure")
        return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        if self.fail:
            raise AppError(SECRET_STORE_FAILED, "fake secret store failure")
        self.data[key] = value

    def delete(self, key: str) -> None:
        if self.fail:
            raise AppError(SECRET_STORE_FAILED, "fake secret store failure")
        self.data.pop(key, None)


class FakeCatalog(CatalogPort):
    def __init__(self) -> None:
        self.checkpoints: List[Any] = []
        self.loras: List[Any] = []
        self.workflows: List[Any] = []
        self.error: Optional[BaseException] = None

    async def list_checkpoints(self, family=None) -> List[Any]:
        if self.error:
            raise self.error
        return self.checkpoints

    async def list_loras(self, query=None, family=None) -> List[Any]:
        if self.error:
            raise self.error
        return self.loras

    async def list_workflows(self) -> List[Any]:
        if self.error:
            raise self.error
        return self.workflows


class FakeAudit(AuditPort):
    def __init__(self) -> None:
        self.calls: List[tuple] = []
        self.result = ImageAuditResult(
            scores={"drawings": 0.9, "hentai": 0.0, "neutral": 0.1, "porn": 0.0, "sexy": 0.0},
            binary_nsfw=0.0,
            backend="fake",
        )
        self.error: Optional[BaseException] = None

    async def audit_image(self, image_id: str, data: bytes, metadata: Dict[str, Any]) -> ImageAuditResult:
        self.calls.append((image_id, data, metadata))
        if self.error is not None:
            raise self.error
        return self.result


class EngineHarness:
    """组装 JobManager 或 CoreEngine 所需全部 fakes。"""

    def __init__(self, max_queue: int = 20, poll_interval: float = 0.05, audit: bool = True) -> None:
        self.clock = FakeClock()
        self.order = OrderLog()
        self.store = FakeJobStore(self.order, self.clock)
        self.events = FakeEventSink(self.order, self.clock)
        self.comfy = FakeComfy()
        self.prompt = FakePrompt()
        self.images = FakeImageStore()
        self.config = FakeConfig()
        self.secrets = FakeSecrets()
        self.catalog = FakeCatalog()
        self.audit = FakeAudit() if audit else None
        self.max_queue = max_queue
        self.poll_interval = poll_interval

    def make_manager(self) -> JobManager:
        return JobManager(
            jobs=self.store,
            comfy=self.comfy,
            prompt=self.prompt,
            images=self.images,
            events=self.events,
            clock=self.clock,
            audit=self.audit,
            max_queue=self.max_queue,
            poll_interval_seconds=self.poll_interval,
        )

    def make_engine(self) -> CoreEngine:
        return CoreEngine(
            comfy=self.comfy,
            prompt=self.prompt,
            catalog=self.catalog,
            jobs=self.store,
            images=self.images,
            secrets=self.secrets,
            config=self.config,
            events=self.events,
            clock=self.clock,
            audit=self.audit,
        )


async def wait_for_state(manager: JobManager, job_id: str, states, timeout: float = 5.0) -> JobSnapshot:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        snapshot = await manager.get_job(job_id)
        if snapshot.state in set(states):
            return snapshot
        if loop.time() > deadline:
            raise AssertionError(f"job {job_id} 未在 {timeout}s 内到达 {states}，当前 {snapshot.state}: {snapshot.progress_text}")
        await asyncio.sleep(0.01)


def job_state_events(manager: JobManager, job_id: str) -> List[str]:
    return [e.type for e in manager._events.events if e.job_id == job_id]  # type: ignore[attr-defined]


def lifecycle_events(sink: FakeEventSink, job_id: str) -> List[str]:
    """仅 job.* 生命周期事件（过滤 queue.changed 等伴随事件）。"""
    return [e.type for e in sink.events if e.job_id == job_id and e.type.startswith("job.")]


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------

class TestModels(unittest.IsolatedAsyncioTestCase):
    def test_request_round_trip(self) -> None:
        request = make_request(
            seed=0,  # seed 0 合法
            width=1024,
            height=512,
            workflow_id="finish_optimized_api.json",
            loras=[LoraSelection("char_lora.safetensors", 0.8, 0.6)],
            negative_prompt="bad quality",
        )
        restored = GenerationRequest.from_dict(request.to_dict())
        self.assertEqual(restored, request)

    def test_request_seed_zero_allowed(self) -> None:
        request = make_request(seed=0)
        self.assertEqual(request.seed, 0)

    def test_request_rejects_negative_seed(self) -> None:
        with self.assertRaises(ValueError):
            make_request(seed=-1)

    def test_request_rejects_non_multiple_of_8(self) -> None:
        with self.assertRaises(ValueError):
            make_request(width=1004)
        with self.assertRaises(ValueError):
            make_request(height=513)

    def test_request_rejects_non_positive_dimension(self) -> None:
        with self.assertRaises(ValueError):
            make_request(width=0)
        with self.assertRaises(ValueError):
            make_request(height=-8)

    def test_request_accepts_none_dimensions(self) -> None:
        request = make_request(width=None, height=None)
        self.assertIsNone(request.width)

    def test_request_rejects_bad_mode_and_family(self) -> None:
        with self.assertRaises(ValueError):
            GenerationRequest.from_dict(make_request().to_dict() | {"mode": "video"})
        with self.assertRaises(ValueError):
            GenerationRequest.from_dict(make_request().to_dict() | {"model_family": "flux"})

    def test_request_rejects_empty_fields(self) -> None:
        with self.assertRaises(ValueError):
            make_request(client_request_id="  ")
        with self.assertRaises(ValueError):
            make_request(raw_prompt="")
        with self.assertRaises(ValueError):
            make_request(source_upload_ids=[""])

    def test_lora_strength_bounds(self) -> None:
        with self.assertRaises(ValueError):
            LoraSelection("x.safetensors", strength_model=4.5)
        with self.assertRaises(ValueError):
            LoraSelection("x.safetensors", strength_clip=-4.5)
        with self.assertRaises(ValueError):
            LoraSelection("x.safetensors", strength_model=float("nan"))
        with self.assertRaises(ValueError):
            LoraSelection("   ")
        # 边界值合法
        LoraSelection("x.safetensors", strength_model=4.0, strength_clip=-4.0)

    def test_result_validation_and_round_trip(self) -> None:
        result = GenerationResult(
            history_id="h1", image_id="img-1", final_prompt="p", seed=7,
            width=512, height=512, elapsed_seconds=1.5,
            audit={"scores": {"porn": 0.0}},
            metadata={"prompt_id": "pid-001", "image_ids": ["img-1"]},
        )
        self.assertEqual(GenerationResult.from_dict(result.to_dict()), result)
        with self.assertRaises(ValueError):
            GenerationResult(history_id="h1", image_id="img-1", final_prompt="p", seed=7, width=513, height=512, elapsed_seconds=1.5)
        with self.assertRaises(ValueError):
            GenerationResult(history_id="h1", image_id="img-1", final_prompt="p", seed=7, width=512, height=512, elapsed_seconds=-1.0)

    def test_result_metadata_must_be_serializable(self) -> None:
        loop: List[Any] = []
        loop.append(loop)  # 循环引用 → 不可序列化
        with self.assertRaises(ValueError):
            GenerationResult(
                history_id="h1", image_id="img-1", final_prompt="p", seed=1,
                width=512, height=512, elapsed_seconds=0.1,
                metadata={"loop": loop},
            )

    def test_snapshot_round_trip(self) -> None:
        snapshot = JobSnapshot(
            id="j1", state=JobState.RUNNING, queue_position=None,
            progress_text="x", comfy_prompt_id="pid-1",
            result=None, error=None,
        )
        self.assertEqual(JobSnapshot.from_dict(snapshot.to_dict()), snapshot)

    def test_job_record_round_trip(self) -> None:
        record = JobRecord(
            id="j1", client_request_id="c1", request=make_request("c1"),
            state=JobState.FAILED,
            error=AppError(COMFYUI_OFFLINE, "offline", {"host": "x"}, True),
            comfy_prompt_id="pid-1",
            started_at=datetime(2025, 1, 1, tzinfo=UTC),
            finished_at=datetime(2025, 1, 1, 0, 1, tzinfo=UTC),
            favorite=True,
        )
        restored = JobRecord.from_dict(record.to_dict())
        self.assertEqual(restored.id, record.id)
        self.assertEqual(restored.state, JobState.FAILED)
        self.assertEqual(restored.error.code, COMFYUI_OFFLINE)
        self.assertTrue(restored.error.retryable)
        self.assertEqual(restored.request, record.request)
        self.assertTrue(restored.favorite)
        # 未知字段 → 严格拒绝
        data = record.to_dict()
        data["bogus"] = 1
        with self.assertRaises(ValueError):
            JobRecord.from_dict(data)

    def test_audit_result(self) -> None:
        audit = ImageAuditResult(scores={"porn": 0.99, "neutral": 0.01}, backend="http")
        self.assertEqual(ImageAuditResult.from_dict(audit.to_dict()), audit)
        with self.assertRaises(ValueError):
            ImageAuditResult(scores={"porn": 1.5})

    def test_event_round_trip(self) -> None:
        event = CoreEvent(EVENT_JOB_COMPLETED, job_id="j1", payload={"result": {"ok": True}})
        self.assertEqual(CoreEvent.from_dict(event.to_dict()), event)

    def test_job_state_sets(self) -> None:
        self.assertEqual(len(TERMINAL_JOB_STATES := {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED, JobState.DETACHED}), 4)  # noqa: E501
        self.assertEqual(
            set(ACTIVE_JOB_STATES),
            {JobState.SUBMITTING, JobState.RUNNING, JobState.DOWNLOADING, JobState.AUDITING},
        )
        self.assertTrue(JobState.COMPLETED.is_terminal)
        self.assertFalse(JobState.RUNNING.is_terminal)
        self.assertTrue(JobState.QUEUED.is_active is False)


class TestErrors(unittest.TestCase):
    def test_required_codes_present(self) -> None:
        for code in REQUIRED_ERROR_CODES:
            self.assertIn(code, KNOWN_ERROR_CODES, f"缺少稳定错误码 {code}")

    def test_app_error_round_trip(self) -> None:
        error = AppError(COMFYUI_OFFLINE, "无法连接", {"host": "127.0.0.1:8188"}, True)
        restored = AppError.from_dict(error.to_dict())
        self.assertEqual(restored.code, error.code)
        self.assertEqual(restored.message, error.message)
        self.assertEqual(restored.details, error.details)
        self.assertTrue(restored.retryable)

    def test_unknown_code_rejected(self) -> None:
        with self.assertRaises(ValueError):
            AppError("NOT_A_REAL_CODE", "x")

    def test_classify_connection_error(self) -> None:
        error = classify_exception(ConnectionError("connection refused"))
        self.assertEqual(error.code, COMFYUI_OFFLINE)
        self.assertTrue(error.retryable)

    def test_classify_timeout(self) -> None:
        self.assertEqual(classify_exception(TimeoutError("slow")).code, COMFYUI_TIMEOUT)
        self.assertEqual(classify_exception(asyncio.TimeoutError()).code, COMFYUI_TIMEOUT)

    def test_classify_tls(self) -> None:
        self.assertEqual(classify_exception(ssl.SSLError("cert")).code, COMFYUI_TLS_FAILED)

    def test_classify_disk_space(self) -> None:
        error = OSError(28, "No space left on device")
        self.assertEqual(classify_exception(error).code, DISK_SPACE_LOW)

    def test_classify_aiohttp_by_module_name(self) -> None:
        # 不得导入 aiohttp：用同名假类验证模块名文本识别
        fake = types.SimpleNamespace()
        error = classify_exception(fake)
        self.assertEqual(error.code, INTERNAL_ERROR)
        class _FakeClientError(Exception):
            pass
        _FakeClientError.__module__ = "aiohttp.client"
        self.assertEqual(classify_exception(_FakeClientError("x")).code, COMFYUI_OFFLINE)

    def test_classify_app_error_passthrough(self) -> None:
        original = AppError(SECRET_STORE_FAILED, "boom")
        self.assertIs(classify_exception(original), original)

    def test_classify_fallback(self) -> None:
        self.assertEqual(classify_exception(ValueError("bad")).code, INTERNAL_ERROR)


class TestJobManagerFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.harness = EngineHarness()
        self.manager = self.harness.make_manager()

    async def asyncTearDown(self) -> None:
        if self.manager.running:
            await self.manager.stop()

    async def test_normal_state_chain(self) -> None:
        await self.manager.start()
        snapshot = await self.manager.submit(make_request("chain-1"))
        completed = await wait_for_state(self.manager, snapshot.id, {JobState.COMPLETED})

        self.assertEqual(completed.state, JobState.COMPLETED)
        self.assertIsNotNone(completed.comfy_prompt_id)
        self.assertIsNone(completed.error)
        self.assertIsNotNone(completed.result)
        self.assertEqual(completed.result.image_id, "img-001")
        self.assertEqual(completed.result.final_prompt, "1girl, cat ears, masterpiece")
        self.assertEqual(completed.result.seed, 42)
        self.assertEqual(completed.result.width, 512)
        self.assertEqual(completed.result.audit["backend"], "fake")
        self.assertEqual(completed.result.metadata["prompt_id"], "pid-001")
        self.assertEqual(completed.result.metadata["image_ids"], ["img-001"])
        self.assertTrue(self.harness.images.read_image("img-001") == b"fake-png-bytes")

        # 状态链事件顺序（job.* 维度）
        expected = [
            EVENT_JOB_QUEUED, EVENT_JOB_PREPARING, EVENT_JOB_SUBMITTING,
            EVENT_JOB_RUNNING, EVENT_JOB_DOWNLOADING, EVENT_JOB_AUDITING,
            EVENT_JOB_COMPLETED,
        ]
        self.assertEqual(lifecycle_events(self.harness.events, snapshot.id), expected)

    async def test_queued_snapshot_has_position(self) -> None:
        await self.manager.start()
        first = await self.manager.submit(make_request("pos-1"))
        second = await self.manager.submit(make_request("pos-2"))
        # worker 可能已经处理 first；只验证 second 的排队位置语义
        second_snap = await self.manager.get_job(second.id)
        if second_snap.state in (JobState.QUEUED, JobState.PREPARING):
            self.assertGreaterEqual(second_snap.queue_position, 1)
        first_snap = await self.manager.get_job(first.id)
        self.assertEqual(first_snap.id, first.id)

    async def test_client_request_id_idempotent(self) -> None:
        await self.manager.start()
        first = await self.manager.submit(make_request("same-id"))
        second = await self.manager.submit(make_request("same-id"))
        self.assertEqual(first.id, second.id)
        await wait_for_state(self.manager, first.id, {JobState.COMPLETED})
        self.assertEqual(len(self.harness.store.records), 1)

    async def test_queue_full(self) -> None:
        block = asyncio.Event()
        manager = JobManager(
            jobs=self.harness.store, comfy=self.harness.comfy, prompt=self.harness.prompt,
            images=self.harness.images, events=self.harness.events, clock=self.harness.clock,
            audit=self.harness.audit, max_queue=2, poll_interval_seconds=0.05,
        )
        self.manager = manager
        self.harness.comfy.submit_block = block
        await manager.start()
        first = await manager.submit(make_request("full-1"))
        # 等待 worker 卡在 submit（submitting 状态，占 1 个容量）
        await wait_for_state(manager, first.id, {JobState.SUBMITTING})
        await manager.submit(make_request("full-2"))  # queued，占满
        with self.assertRaises(AppError) as ctx:
            await manager.submit(make_request("full-3"))
        self.assertEqual(ctx.exception.code, QUEUE_FULL)
        self.assertTrue(ctx.exception.retryable)
        # 解除阻塞：auto_complete 会登记历史产出，全部完成
        block.set()
        await wait_for_state(manager, first.id, {JobState.COMPLETED})
        second_id = self.harness.store.get_by_client_request_id("full-2").id
        await wait_for_state(manager, second_id, {JobState.COMPLETED})

    async def test_cancel_queued(self) -> None:
        await self.manager.start(start_worker=False)
        snapshot = await self.manager.submit(make_request("cancel-q"))
        self.assertEqual(snapshot.state, JobState.QUEUED)
        cancelled = await self.manager.cancel(snapshot.id)
        self.assertEqual(cancelled.state, JobState.CANCELLED)
        self.assertIsNone(cancelled.comfy_prompt_id)
        events = lifecycle_events(self.harness.events, snapshot.id)
        self.assertEqual(events, [EVENT_JOB_QUEUED, EVENT_JOB_CANCELLED])
        # 新 manager（同一 store）启动 worker 后不会拾取已取消任务
        fresh = JobManager(
            jobs=self.harness.store, comfy=self.harness.comfy, prompt=self.harness.prompt,
            images=self.harness.images, events=self.harness.events, clock=self.harness.clock,
            audit=self.harness.audit, max_queue=20, poll_interval_seconds=0.05,
        )
        await fresh.start()
        try:
            await asyncio.sleep(0.2)
            self.assertEqual((await fresh.get_job(snapshot.id)).state, JobState.CANCELLED)
        finally:
            await fresh.stop()

    async def test_cancel_running_detaches(self) -> None:
        self.harness.comfy.auto_complete = False
        await self.manager.start()
        snapshot = await self.manager.submit(make_request("cancel-r"))
        # 历史一直返回 None 且任务在队列中 → 停在 running
        running = await wait_for_state(self.manager, snapshot.id, {JobState.RUNNING})
        self.assertIsNotNone(running.comfy_prompt_id)
        detached = await self.manager.cancel(snapshot.id)
        self.assertEqual(detached.state, JobState.DETACHED)
        self.assertIn("interrupt", self.harness.comfy.interrupt_calls)
        events = lifecycle_events(self.harness.events, snapshot.id)
        self.assertEqual(events[-1], EVENT_JOB_DETACHED)
        # 终态不再流转
        await asyncio.sleep(0.15)
        self.assertEqual((await self.manager.get_job(snapshot.id)).state, JobState.DETACHED)
        # detached 任务不可再次取消
        with self.assertRaises(AppError) as ctx:
            await self.manager.cancel(snapshot.id)
        self.assertEqual(ctx.exception.code, JOB_NOT_CANCELLABLE)

    async def test_exception_mapping_offline(self) -> None:
        self.harness.comfy.submit_error = ConnectionError("refused")
        await self.manager.start()
        snapshot = await self.manager.submit(make_request("fail-1"))
        failed = await wait_for_state(self.manager, snapshot.id, {JobState.FAILED})
        self.assertEqual(failed.error["code"], COMFYUI_OFFLINE)
        self.assertTrue(failed.error["retryable"])
        self.assertEqual(lifecycle_events(self.harness.events, snapshot.id)[-1], EVENT_JOB_FAILED)

    async def test_workflow_build_failure(self) -> None:
        from winefox_core.errors import workflow_not_found
        self.harness.prompt.build_error = workflow_not_found("missing.json")
        await self.manager.start()
        snapshot = await self.manager.submit(make_request("fail-2"))
        failed = await wait_for_state(self.manager, snapshot.id, {JobState.FAILED})
        self.assertEqual(failed.error["code"], WORKFLOW_NOT_FOUND)

    async def test_history_error_fails_job(self) -> None:
        self.harness.comfy.auto_complete = False
        await self.manager.start()
        snapshot = await self.manager.submit(make_request("fail-3"))
        running = await wait_for_state(self.manager, snapshot.id, {JobState.RUNNING})
        self.harness.comfy.history[running.comfy_prompt_id] = ComfyHistoryEntry(
            status="error", completed=False, error="CUDA out of memory"
        )
        failed = await wait_for_state(self.manager, snapshot.id, {JobState.FAILED})
        self.assertIn("CUDA out of memory", failed.error["message"])

    async def test_store_before_emit_order(self) -> None:
        await self.manager.start()
        snapshot = await self.manager.submit(make_request("order-1"))
        await wait_for_state(self.manager, snapshot.id, {JobState.COMPLETED})
        state_events = [
            EVENT_JOB_QUEUED, EVENT_JOB_PREPARING, EVENT_JOB_SUBMITTING,
            EVENT_JOB_RUNNING, EVENT_JOB_DOWNLOADING, EVENT_JOB_AUDITING,
            EVENT_JOB_COMPLETED,
        ]
        state_by_event = {e: e.split(".", 1)[1] for e in state_events}
        for event_type in state_events:
            emit_ts = next(
                ts for kind, jid, etype, ts in self.harness.order.entries
                if kind == "emit" and jid == snapshot.id and etype == event_type
            )
            save_ts = max(
                ts for kind, jid, st, ts in self.harness.order.entries
                if kind == "save" and jid == snapshot.id and st.value == state_by_event[event_type]
            )
            self.assertLessEqual(save_ts, emit_ts, f"{event_type} 必须先写 JobStore 再通知 EventSink")

    async def test_illegal_transitions_rejected(self) -> None:
        from winefox_core.errors import invalid_state_transition
        record = JobRecord(id="j", client_request_id="c", request=make_request("c"), state=JobState.QUEUED)
        with self.assertRaises(AppError) as ctx:
            self.manager._transition(record, JobState.COMPLETED)  # type: ignore[attr-defined]
        self.assertEqual(ctx.exception.code, "INVALID_STATE_TRANSITION")
        # 严格矩阵：终态无出口
        record.state = JobState.COMPLETED
        with self.assertRaises(AppError):
            self.manager._transition(record, JobState.QUEUED)  # type: ignore[attr-defined]
        # 全部合法迁移都能通过
        for from_state, to_states in ALLOWED_TRANSITIONS.items():
            for to_state in to_states:
                candidate = JobRecord(id="x", client_request_id="c", request=make_request("c"), state=from_state)
                self.manager._transition(candidate, to_state)  # type: ignore[attr-defined]

    async def test_unknown_submission_without_prompt_id(self) -> None:
        # 持久化中存在 RUNNING 但无 prompt_id（崩溃窗口的损坏记录）
        record = JobRecord(
            id="corrupt-1", client_request_id="c1", request=make_request("c1"),
            state=JobState.RUNNING, comfy_prompt_id=None,
            created_at=self.harness.clock.now(), updated_at=self.harness.clock.now(),
        )
        self.harness.store.save(record)
        await self.manager.start()
        failed = await wait_for_state(self.manager, "corrupt-1", {JobState.FAILED})
        self.assertEqual(failed.error["code"], UNKNOWN_SUBMISSION_STATE)
        events = lifecycle_events(self.harness.events, "corrupt-1")
        self.assertIn(EVENT_JOB_RECOVERING, events)
        self.assertEqual(events[-1], EVENT_JOB_FAILED)

    async def test_prompt_id_recovery_completes(self) -> None:
        record = JobRecord(
            id="recover-1", client_request_id="c2", request=make_request("c2", seed=9),
            state=JobState.RUNNING, comfy_prompt_id="pid-777",
            started_at=self.harness.clock.now(),
            created_at=self.harness.clock.now(), updated_at=self.harness.clock.now(),
        )
        self.harness.store.save(record)
        self.harness.comfy.history["pid-777"] = make_success_entry()
        await self.manager.start()
        completed = await wait_for_state(self.manager, "recover-1", {JobState.COMPLETED})
        self.assertEqual(completed.result.image_id, "img-001")
        self.assertEqual(completed.result.metadata["recovered"], True)
        self.assertEqual(completed.result.seed, 9)
        events = lifecycle_events(self.harness.events, "recover-1")
        self.assertIn(EVENT_JOB_RECOVERING, events)
        self.assertEqual(events[-1], EVENT_JOB_COMPLETED)

    async def test_recovery_running_still_in_queue(self) -> None:
        record = JobRecord(
            id="recover-2", client_request_id="c3", request=make_request("c3"),
            state=JobState.RUNNING, comfy_prompt_id="pid-888",
            started_at=self.harness.clock.now(),
            created_at=self.harness.clock.now(), updated_at=self.harness.clock.now(),
        )
        self.harness.store.save(record)
        # 历史无记录，但队列快照显示仍在执行
        self.harness.comfy.queue_running.append("pid-888")
        await self.manager.start()
        running = await wait_for_state(self.manager, "recover-2", {JobState.RUNNING})
        self.assertEqual(running.comfy_prompt_id, "pid-888")
        # 之后产出完成
        self.harness.comfy.history["pid-888"] = make_success_entry()
        await wait_for_state(self.manager, "recover-2", {JobState.COMPLETED})

    async def test_recovery_unknown_submission_with_prompt_id(self) -> None:
        record = JobRecord(
            id="recover-3", client_request_id="c4", request=make_request("c4"),
            state=JobState.RUNNING, comfy_prompt_id="pid-999",
            started_at=self.harness.clock.now(),
            created_at=self.harness.clock.now(), updated_at=self.harness.clock.now(),
        )
        self.harness.store.save(record)
        # 历史与队列都找不到 → UNKNOWN_SUBMISSION_STATE
        await self.manager.start()
        failed = await wait_for_state(self.manager, "recover-3", {JobState.FAILED})
        self.assertEqual(failed.error["code"], UNKNOWN_SUBMISSION_STATE)

    async def test_recovery_requeues_preparing(self) -> None:
        record = JobRecord(
            id="prep-1", client_request_id="c5", request=make_request("c5"),
            state=JobState.PREPARING,
            created_at=self.harness.clock.now(), updated_at=self.harness.clock.now(),
        )
        self.harness.store.save(record)
        await self.manager.start()
        completed = await wait_for_state(self.manager, "prep-1", {JobState.COMPLETED})
        self.assertEqual(completed.state, JobState.COMPLETED)
        events = lifecycle_events(self.harness.events, "prep-1")
        self.assertEqual(events[0], EVENT_JOB_RECOVERING)
        self.assertIn(EVENT_JOB_QUEUED, events)

    async def test_stop_rejects_new_jobs(self) -> None:
        await self.manager.start()
        await self.manager.stop()
        self.assertFalse(self.manager.accepting)
        with self.assertRaises(AppError) as ctx:
            await self.manager.submit(make_request("after-stop"))
        self.assertEqual(ctx.exception.code, "ENGINE_STOPPED")

    async def test_double_start_rejected(self) -> None:
        await self.manager.start()
        with self.assertRaises(AppError) as ctx:
            await self.manager.start()
        self.assertEqual(ctx.exception.code, "ENGINE_ALREADY_RUNNING")

    async def test_multiple_jobs_single_worker_sequential(self) -> None:
        await self.manager.start()
        first = await self.manager.submit(make_request("seq-1"))
        second = await self.manager.submit(make_request("seq-2"))
        await wait_for_state(self.manager, first.id, {JobState.COMPLETED})
        await wait_for_state(self.manager, second.id, {JobState.COMPLETED})
        # 单 worker：提交顺序 = 完成顺序（开始时间有序）
        first_record = self.harness.store.records[first.id]
        second_record = self.harness.store.records[second.id]
        self.assertLessEqual(first_record.started_at, second_record.started_at)

    async def test_get_job_not_found(self) -> None:
        with self.assertRaises(AppError) as ctx:
            await self.manager.get_job("nope")
        self.assertEqual(ctx.exception.code, JOB_NOT_FOUND)

    async def test_favorite_and_delete_history(self) -> None:
        block = asyncio.Event()
        self.harness.comfy.submit_block = block
        await self.manager.start()
        snapshot = await self.manager.submit(make_request("hist-1"))
        queued = await self.manager.submit(make_request("hist-2"))  # worker 卡住 → 稳定 queued
        # 未完成任务不可收藏 / 删除
        with self.assertRaises(AppError) as ctx:
            await self.manager.favorite(queued.id)
        self.assertEqual(ctx.exception.code, JOB_NOT_FAVORITABLE)
        with self.assertRaises(AppError) as ctx:
            await self.manager.delete_history(queued.id)
        self.assertEqual(ctx.exception.code, JOB_NOT_DELETABLE)
        # 历史列表只含终态（hist-2 仍在 queued）
        history = await self.manager.list_history()
        self.assertNotIn(queued.id, [h.id for h in history])
        # 放行并完成 hist-1
        block.set()
        await wait_for_state(self.manager, snapshot.id, {JobState.COMPLETED})
        # 收藏 / 取消收藏
        await self.manager.favorite(snapshot.id)
        self.assertTrue(self.harness.store.records[snapshot.id].favorite)
        self.assertIn(EVENT_JOB_FAVORITED, lifecycle_events(self.harness.events, snapshot.id))
        await self.manager.favorite(snapshot.id, favorite=False)
        self.assertFalse(self.harness.store.records[snapshot.id].favorite)
        # 删除
        deleted = await self.manager.delete_history(snapshot.id)
        self.assertEqual(deleted.id, snapshot.id)
        with self.assertRaises(AppError) as ctx:
            await self.manager.get_job(snapshot.id)
        self.assertEqual(ctx.exception.code, JOB_NOT_FOUND)
        # 被删任务的图片随删除清理（hist-2 的图片不受影响）
        self.assertNotIn("img-001", self.harness.images.images)
        self.assertIn("img-002", self.harness.images.images)
        # 清理 hist-2 避免 teardown 挂起
        await wait_for_state(self.manager, queued.id, {JobState.COMPLETED, JobState.FAILED})


class TestCoreEngine(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.harness = EngineHarness()
        self.engine = self.harness.make_engine()

    async def asyncTearDown(self) -> None:
        if self.engine.state == "running":
            await self.engine.stop()

    async def test_lifecycle_and_status(self) -> None:
        self.assertEqual(self.engine.state, "stopped")
        await self.engine.start()
        self.assertEqual(self.engine.state, "running")
        status = self.engine.status()
        self.assertEqual(status.state, "running")
        self.assertEqual(status.worker_count, 1)
        started_event = [e for e in self.harness.events.events if e.type == EVENT_ENGINE_STARTED]
        self.assertEqual(len(started_event), 1)
        await self.engine.stop()
        self.assertEqual(self.engine.state, "stopped")
        stopped_event = [e for e in self.harness.events.events if e.type == EVENT_ENGINE_STOPPED]
        self.assertEqual(len(stopped_event), 1)

    async def test_double_start(self) -> None:
        await self.engine.start()
        with self.assertRaises(AppError) as ctx:
            await self.engine.start()
        self.assertEqual(ctx.exception.code, "ENGINE_ALREADY_RUNNING")

    async def test_probe_comfyui(self) -> None:
        await self.engine.start()
        status = await self.engine.probe_comfyui()
        self.assertTrue(status["online"])
        self.assertEqual(status["version"], "0.3.5")

    async def test_probe_comfyui_offline(self) -> None:
        await self.engine.start()
        self.harness.comfy.probe_status = ComfyUIStatus(online=False, error="refused")
        status = await self.engine.probe_comfyui()
        self.assertFalse(status["online"])

    async def test_list_catalog(self) -> None:
        await self.engine.start()
        self.harness.catalog.checkpoints = []
        self.harness.catalog.loras = []
        self.harness.catalog.workflows = []
        catalog = await self.engine.list_catalog()
        self.assertIn("checkpoints", catalog)
        self.assertIn("loras", catalog)
        self.assertIn("workflows", catalog)

    async def test_submit_get_cancel_history_via_engine(self) -> None:
        block = asyncio.Event()
        self.harness.comfy.submit_block = block
        await self.engine.start()
        snapshot = await self.engine.submit_generation(make_request("engine-1"))
        got = await self.engine.get_job(snapshot.id)
        self.assertEqual(got.id, snapshot.id)
        # worker 卡在提交时，第二个任务停留在 queued → 可取消
        queued = await self.engine.submit_generation(make_request("engine-cancel"))
        self.assertEqual(queued.state, JobState.QUEUED)
        cancelled = await self.engine.cancel_job(queued.id)
        self.assertEqual(cancelled.state, JobState.CANCELLED)
        # 放行第一个任务（auto_complete 自动登记产出）
        self.harness.clock.advance(1.0)  # 保证完成时间晚于取消时间，排序确定
        block.set()
        await wait_for_state(self.engine._manager, snapshot.id, {JobState.COMPLETED})  # type: ignore[attr-defined]
        history = await self.engine.list_history(limit=10)
        self.assertEqual([h.id for h in history][0], snapshot.id)
        favorited = await self.engine.favorite_history(snapshot.id)
        self.assertIsNotNone(favorited.result)
        await self.engine.delete_history(snapshot.id)

    async def test_submit_from_dict(self) -> None:
        await self.engine.start()
        payload = make_request("dict-1").to_dict()
        snapshot = await self.engine.submit_generation(payload)
        self.assertEqual(snapshot.state, JobState.QUEUED)
        with self.assertRaises(AppError) as ctx:
            await self.engine.submit_generation({"client_request_id": "bad", "mode": "nope", "raw_prompt": "", "source_upload_ids": [], "model_family": "anima"})
        self.assertEqual(ctx.exception.code, "INVALID_REQUEST")

    async def test_update_settings(self) -> None:
        await self.engine.start()
        applied = await self.engine.update_settings({"max_queue": 5, "poll_interval_seconds": 0.2})
        self.assertEqual(applied["applied"]["max_queue"], 5)
        self.assertEqual(self.engine._manager.max_queue, 5)  # type: ignore[attr-defined]
        # 单 worker 约束
        with self.assertRaises(AppError) as ctx:
            await self.engine.update_settings({"queue_worker_count": 3})
        self.assertEqual(ctx.exception.code, CONFIG_INVALID)
        # 未知键
        with self.assertRaises(AppError) as ctx:
            await self.engine.update_settings({"comfyui.server_address": "http://x"})
        self.assertEqual(ctx.exception.code, CONFIG_INVALID)
        # 非法值
        with self.assertRaises(AppError) as ctx:
            await self.engine.update_settings({"max_queue": 0})
        self.assertEqual(ctx.exception.code, CONFIG_INVALID)

    async def test_stop_rejects_submissions(self) -> None:
        await self.engine.start()
        await self.engine.stop()
        with self.assertRaises(AppError) as ctx:
            await self.engine.submit_generation(make_request("stopped"))
        self.assertEqual(ctx.exception.code, "ENGINE_STOPPED")

    async def test_engine_settings_from_config(self) -> None:
        self.harness.config.set("core.max_queue", 3)
        self.harness.config.set("core.poll_interval_seconds", 0.5)
        engine = self.harness.make_engine()
        self.assertEqual(engine._manager.max_queue, 3)  # type: ignore[attr-defined]
        self.assertEqual(engine._manager.poll_interval_seconds, 0.5)

    async def test_config_fallback_on_invalid(self) -> None:
        self.harness.config.set("core.max_queue", "bogus")
        engine = self.harness.make_engine()
        self.assertEqual(engine._manager.max_queue, 20)  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main(verbosity=2)

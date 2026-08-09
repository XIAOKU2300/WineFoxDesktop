# -*- coding: utf-8 -*-
"""WineFox Desktop — JobManager 任务状态机。

规则（冻结）：
- 默认最大队列 20、单 worker。
- client_request_id 幂等：重复提交返回既有任务快照。
- 严格状态迁移：非法迁移抛 INVALID_STATE_TRANSITION。
- queued / preparing 可取消 → cancelled；已有 comfy_prompt_id 的活跃任务
  取消只能 → detached（ComfyUI 侧尽力 interrupt，但不保证停止）。
- 重启恢复：preparing → queued 重排；活跃任务按 prompt_id 重新核对
  history/queue；无 prompt_id 的未知提交 → failed(UNKNOWN_SUBMISSION_STATE)。
- stop：停止接单（新提交抛 ENGINE_STOPPED）并停掉 worker；任务状态在每次
  变更时已持久化。
- 状态必须先写 JobStore 再通知 EventSink（先 save 后 emit）。
- worker 单循环驱动：先拾取 queued 任务推进到 running，再逐个轮询
  tracked（running/recovering）任务；recover() 只做状态归类，不阻塞启动。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, List, Optional, Union

from .errors import (
    AppError,
    INTERNAL_ERROR,
    classify_exception,
    engine_stopped,
    invalid_state_transition,
    job_not_cancellable,
    job_not_deletable,
    job_not_favoritable,
    job_not_found,
    queue_full,
    unknown_submission_state,
)
from .models import (
    ACTIVE_JOB_STATES,
    EVENT_JOB_AUDITING,
    EVENT_JOB_CANCELLED,
    EVENT_JOB_COMPLETED,
    EVENT_JOB_DELETED,
    EVENT_JOB_DETACHED,
    EVENT_JOB_DOWNLOADING,
    EVENT_JOB_FAILED,
    EVENT_JOB_FAVORITED,
    EVENT_JOB_PREPARING,
    EVENT_JOB_QUEUED,
    EVENT_JOB_RECOVERING,
    EVENT_JOB_RUNNING,
    EVENT_JOB_SUBMITTING,
    EVENT_QUEUE_CHANGED,
    QUEUE_POSITION_STATES,
    TERMINAL_JOB_STATES,
    CoreEvent,
    GenerationRequest,
    GenerationResult,
    ImageAuditResult,
    JobRecord,
    JobSnapshot,
    JobState,
    WorkflowBuild,
)
from .ports import (
    AuditPort,
    Clock,
    ComfyUIPort,
    EventSink,
    ImageStorePort,
    JobStorePort,
    PromptPort,
)

logger = logging.getLogger("winefox_core.jobs")

DEFAULT_MAX_QUEUE = 20
DEFAULT_POLL_INTERVAL_SECONDS = 1.0


def _allowed_transitions() -> Dict[JobState, frozenset]:
    return {
        JobState.QUEUED: frozenset({JobState.PREPARING, JobState.CANCELLED, JobState.FAILED}),
        JobState.PREPARING: frozenset({JobState.SUBMITTING, JobState.CANCELLED, JobState.QUEUED, JobState.FAILED}),
        JobState.SUBMITTING: frozenset({JobState.RUNNING, JobState.CANCELLED, JobState.FAILED, JobState.RECOVERING}),
        JobState.RUNNING: frozenset({JobState.DOWNLOADING, JobState.DETACHED, JobState.FAILED, JobState.RECOVERING}),
        JobState.DOWNLOADING: frozenset({JobState.AUDITING, JobState.DETACHED, JobState.FAILED, JobState.RECOVERING}),
        JobState.AUDITING: frozenset({JobState.COMPLETED, JobState.DETACHED, JobState.FAILED, JobState.RECOVERING}),
        JobState.RECOVERING: frozenset({JobState.RUNNING, JobState.DOWNLOADING, JobState.DETACHED, JobState.FAILED}),
        JobState.COMPLETED: frozenset(),
        JobState.FAILED: frozenset(),
        JobState.CANCELLED: frozenset(),
        JobState.DETACHED: frozenset(),
    }


ALLOWED_TRANSITIONS = _allowed_transitions()

# 计入队列容量（非终态、非 detached）
_CAPACITY_STATES = tuple(
    state for state in JobState
    if state not in TERMINAL_JOB_STATES
)


class JobManager:
    """单 worker 任务状态机。所有外部能力构造注入。"""

    def __init__(
        self,
        jobs: JobStorePort,
        comfy: ComfyUIPort,
        prompt: PromptPort,
        images: ImageStorePort,
        events: EventSink,
        clock: Clock,
        audit: Optional[AuditPort] = None,
        *,
        max_queue: int = DEFAULT_MAX_QUEUE,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._jobs = jobs
        self._comfy = comfy
        self._prompt = prompt
        self._images = images
        self._events = events
        self._clock = clock
        self._audit = audit
        self._max_queue = int(max_queue)
        self._poll_interval = float(poll_interval_seconds)
        self._lock = asyncio.Lock()
        self._running = False
        self._accepting = False
        self._worker_task: Optional[asyncio.Task] = None
        # 正在被 worker 轮询的任务（running/recovering）；dict 保插入顺序
        self._tracked: Dict[str, bool] = {}
        # job_id -> WorkflowBuild（用于完成时回填 final_prompt/seed/尺寸）
        self._builds: Dict[str, WorkflowBuild] = {}
        # 本次运行中经由恢复路径接管的任务
        self._recovered: Set[str] = set()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    @property
    def accepting(self) -> bool:
        return self._accepting

    @property
    def max_queue(self) -> int:
        return self._max_queue

    @property
    def poll_interval_seconds(self) -> float:
        return self._poll_interval

    def set_max_queue(self, value: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise AppError("CONFIG_INVALID", f"max_queue 必须为正整数: {value!r}", {"max_queue": value})
        self._max_queue = int(value)

    def set_poll_interval(self, value: float) -> None:
        number = float(value)
        if number < 0.05:
            raise AppError("CONFIG_INVALID", f"poll_interval_seconds 必须 >= 0.05: {value!r}", {"value": value})
        self._poll_interval = number

    async def start(self, start_worker: bool = True) -> None:
        """恢复未完成任务并启动单 worker。start_worker=False 供测试注入。"""
        if self._running:
            raise AppError("ENGINE_ALREADY_RUNNING", "JobManager 已在运行")
        self._running = True
        self._accepting = True
        await self.recover()
        if start_worker:
            self._worker_task = asyncio.create_task(self._worker_loop())

    async def stop(self) -> None:
        """停止接单并停掉 worker；已持久化的活跃任务留给下次恢复。"""
        if not self._running:
            return
        self._accepting = False
        self._running = False
        task, self._worker_task = self._worker_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - 兜底
                logger.exception("worker 停止时出现异常")

    # ------------------------------------------------------------------
    # 恢复
    # ------------------------------------------------------------------

    async def recover(self) -> None:
        """重启恢复：preparing → queued；活跃任务按 prompt_id 归类（不阻塞启动）。"""
        # 1) preparing（从未到达 ComfyUI）→ queued 重排
        for record in self._jobs.list(states=[JobState.PREPARING]):
            async with self._lock:
                fresh = self._jobs.get(record.id)
                if fresh is None or fresh.state != JobState.PREPARING:
                    continue
                self._transition(fresh, JobState.QUEUED)
                fresh.attempts += 1
                fresh.progress_text = "后端重启，已重新排队"
                fresh.updated_at = self._clock.now()
                self._jobs.save(fresh)
                self._emit(EVENT_JOB_RECOVERING, fresh, {"from": JobState.PREPARING.value})
                self._emit(EVENT_JOB_QUEUED, fresh)

        # 2) 活跃任务：无 prompt_id → 未知提交失败；有 → recovering（加入跟踪）
        for record in self._jobs.list(states=list(ACTIVE_JOB_STATES)):
            async with self._lock:
                fresh = self._jobs.get(record.id)
                if fresh is None or fresh.state not in ACTIVE_JOB_STATES:
                    continue
                from_state = fresh.state.value
                if not fresh.comfy_prompt_id:
                    self._transition(fresh, JobState.FAILED)
                    fresh.error = unknown_submission_state(fresh.id, "")
                    fresh.progress_text = "失败: 重启后无 prompt_id 的未知提交"
                    fresh.finished_at = self._clock.now()
                    fresh.updated_at = self._clock.now()
                    self._jobs.save(fresh)
                    self._emit(EVENT_JOB_RECOVERING, fresh, {"from": from_state})
                    self._emit(EVENT_JOB_FAILED, fresh, {"error": fresh.error.to_dict()})
                    self._emit(EVENT_QUEUE_CHANGED, fresh)
                else:
                    self._transition(fresh, JobState.RECOVERING)
                    fresh.progress_text = "恢复中: 正在核对 ComfyUI 历史记录"
                    fresh.updated_at = self._clock.now()
                    self._jobs.save(fresh)
                    self._emit(EVENT_JOB_RECOVERING, fresh, {"from": from_state})
                    self._tracked[fresh.id] = True
                    self._recovered.add(fresh.id)

    # ------------------------------------------------------------------
    # 对外操作
    # ------------------------------------------------------------------

    async def submit(self, request: Union[GenerationRequest, Dict[str, Any]]) -> JobSnapshot:
        """提交生成任务；client_request_id 幂等；队列满抛 QUEUE_FULL。"""
        if not self._accepting:
            raise engine_stopped()
        if isinstance(request, dict):
            try:
                request = GenerationRequest.from_dict(request)
            except ValueError as exc:
                raise AppError("INVALID_REQUEST", str(exc)) from exc
        if not isinstance(request, GenerationRequest):
            raise AppError("INVALID_REQUEST", f"期望 GenerationRequest，得到 {type(request).__name__}")
        request.validate()

        async with self._lock:
            existing = self._jobs.get_by_client_request_id(request.client_request_id)
            if existing is not None:
                return self._snapshot(existing)
            if self._capacity_reached():
                raise queue_full(self._max_queue)
            now = self._clock.now()
            record = JobRecord(
                id=uuid.uuid4().hex,
                client_request_id=request.client_request_id,
                request=request,
                state=JobState.QUEUED,
                progress_text="已排队，等待 worker",
                created_at=now,
                updated_at=now,
            )
            self._jobs.save(record)
            self._emit(EVENT_JOB_QUEUED, record, {"client_request_id": request.client_request_id})
            self._emit(EVENT_QUEUE_CHANGED, record, {"queued_count": self._capacity_count()})
            return self._snapshot(record)

    async def get_job(self, job_id: str) -> JobSnapshot:
        record = self._jobs.get(job_id)
        if record is None:
            raise job_not_found(job_id)
        return self._snapshot(record)

    async def cancel(self, job_id: str) -> JobSnapshot:
        """queued/preparing → cancelled；有 prompt_id 的活跃任务 → detached。"""
        interrupt_prompt_id: Optional[str] = None
        snapshot: Optional[JobSnapshot] = None
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                raise job_not_found(job_id)
            now = self._clock.now()
            if record.state in (JobState.QUEUED, JobState.PREPARING):
                self._transition(record, JobState.CANCELLED)
                record.cancelled_at = now
                record.finished_at = now
                record.progress_text = "已取消"
                record.updated_at = now
                self._jobs.save(record)
                self._emit(EVENT_JOB_CANCELLED, record)
                self._emit(EVENT_QUEUE_CHANGED, record, {"queued_count": self._capacity_count()})
                snapshot = self._snapshot(record)
            elif record.state in ACTIVE_JOB_STATES or record.state == JobState.RECOVERING:
                if record.comfy_prompt_id:
                    # 已有 prompt_id：只能 detached（ComfyUI 侧尽力 interrupt）
                    self._transition(record, JobState.DETACHED)
                    record.detached_at = now
                    record.finished_at = now
                    record.progress_text = "已脱离跟踪（ComfyUI 侧任务不保证停止）"
                    record.updated_at = now
                    self._jobs.save(record)
                    self._emit(EVENT_JOB_DETACHED, record, {"prompt_id": record.comfy_prompt_id})
                    self._emit(EVENT_QUEUE_CHANGED, record, {"queued_count": self._capacity_count()})
                    interrupt_prompt_id = record.comfy_prompt_id
                    self._tracked.pop(job_id, None)
                    self._builds.pop(job_id, None)
                    snapshot = self._snapshot(record)
                elif record.state == JobState.SUBMITTING:
                    # 尚未拿到 prompt_id：提交仍在途，按 cancelled 处理
                    self._transition(record, JobState.CANCELLED)
                    record.cancelled_at = now
                    record.finished_at = now
                    record.progress_text = "已取消（提交在途）"
                    record.updated_at = now
                    self._jobs.save(record)
                    self._emit(EVENT_JOB_CANCELLED, record)
                    self._emit(EVENT_QUEUE_CHANGED, record, {"queued_count": self._capacity_count()})
                    snapshot = self._snapshot(record)
                else:
                    raise job_not_cancellable(job_id, record.state.value)
            else:
                raise job_not_cancellable(job_id, record.state.value)

        # 锁外尽力 interrupt（不阻塞取消返回）
        if interrupt_prompt_id:
            await self._best_effort_interrupt(interrupt_prompt_id)
        return snapshot  # type: ignore[return-value]

    async def list_history(self, limit: int = 50) -> List[JobSnapshot]:
        records = self._jobs.list(limit=max(limit, 0), states=list(TERMINAL_JOB_STATES))
        records.sort(key=lambda r: (r.finished_at or r.updated_at, r.id), reverse=True)
        return [self._snapshot(r) for r in records[:max(limit, 0)]]

    async def favorite(self, job_id: str, favorite: bool = True) -> JobSnapshot:
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                raise job_not_found(job_id)
            if record.state != JobState.COMPLETED:
                raise job_not_favoritable(job_id, record.state.value)
            record.favorite = bool(favorite)
            record.favorited_at = self._clock.now() if favorite else None
            record.updated_at = self._clock.now()
            self._jobs.save(record)
            self._emit(EVENT_JOB_FAVORITED, record, {"favorite": bool(favorite)})
            return self._snapshot(record)

    async def delete_history(self, job_id: str) -> JobSnapshot:
        snapshot: Optional[JobSnapshot] = None
        image_ids: List[str] = []
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                raise job_not_found(job_id)
            if record.state not in TERMINAL_JOB_STATES:
                raise job_not_deletable(job_id, record.state.value)
            snapshot = self._snapshot(record)
            if record.result is not None:
                image_ids.append(record.result.image_id)
                image_ids.extend(record.result.metadata.get("image_ids", []))
            self._jobs.delete(job_id)
            self._emit(EVENT_JOB_DELETED, record)
        # 锁外清理图片（尽力而为）
        for image_id in dict.fromkeys(image_ids):
            try:
                self._images.delete_image(image_id)
            except Exception:  # pragma: no cover - 清理失败不阻塞删除
                logger.warning("删除图片失败: %s", image_id, exc_info=True)
        return snapshot  # type: ignore[return-value]

    def counts(self) -> Dict[str, int]:
        records = self._jobs.list(limit=10000)
        queued = sum(1 for r in records if r.state in QUEUE_POSITION_STATES)
        active = sum(
            1 for r in records
            if r.state in ACTIVE_JOB_STATES or r.state == JobState.RECOVERING
        )
        return {"jobs_queued": queued, "jobs_active": active}

    # ------------------------------------------------------------------
    # worker 单循环：先拾取 queued，再逐个轮询 tracked
    # ------------------------------------------------------------------

    async def _worker_loop(self) -> None:
        while True:
            if not self._running:
                break
            record = self._jobs.next_queued()
            if record is not None:
                try:
                    await self._process(record.id)
                except asyncio.CancelledError:
                    raise
                except Exception:  # pragma: no cover - 兜底，不允许 worker 崩溃
                    logger.exception("处理任务 %s 时出现未预期异常", record.id)
                continue
            tracked = list(self._tracked)  # 插入顺序轮询（FIFO）
            if tracked:
                for job_id in tracked:
                    if job_id not in self._tracked:
                        continue  # 本轮已被其它路径终结
                    try:
                        await self._poll_once(job_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # pragma: no cover - 兜底
                        logger.exception("轮询任务 %s 时出现未预期异常", job_id)
                # 轮询一轮后休息一个间隔，避免忙循环饿死事件循环
                await asyncio.sleep(self._poll_interval)
                continue
            await asyncio.sleep(self._poll_interval)

    async def _process(self, job_id: str) -> None:
        """queued → preparing → submitting → running（加入 tracked，轮询交给 worker）。"""
        # queued → preparing
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is None or record.state != JobState.QUEUED:
                return
            self._transition(record, JobState.PREPARING)
            record.started_at = record.started_at or self._clock.now()
            record.progress_text = "正在构建工作流"
            record.updated_at = self._clock.now()
            self._jobs.save(record)
            self._emit(EVENT_JOB_PREPARING, record)

        try:
            build = self._prompt.build_workflow(record.request)
        except AppError as exc:
            await self._fail(job_id, exc)
            return
        except Exception as exc:
            await self._fail(job_id, classify_exception(exc))
            return

        # preparing → submitting
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is None or record.state != JobState.PREPARING:
                return
            self._transition(record, JobState.SUBMITTING)
            record.progress_text = "正在提交到 ComfyUI"
            record.updated_at = self._clock.now()
            self._jobs.save(record)
            self._emit(EVENT_JOB_SUBMITTING, record)

        try:
            submission = await self._comfy.submit(build.workflow, client_id=job_id)
        except AppError as exc:
            await self._fail(job_id, exc)
            return
        except Exception as exc:
            await self._fail(job_id, classify_exception(exc))
            return

        # submitting → running
        abandoned_prompt_id: Optional[str] = None
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return
            if record.state != JobState.SUBMITTING:
                # 提交在途时被取消：ComfyUI 侧尽力中断
                abandoned_prompt_id = submission.prompt_id
            else:
                record.comfy_prompt_id = submission.prompt_id
                self._transition(record, JobState.RUNNING)
                record.progress_text = "ComfyUI 执行中"
                record.updated_at = self._clock.now()
                self._jobs.save(record)
                self._emit(EVENT_JOB_RUNNING, record, {"prompt_id": submission.prompt_id})
                self._tracked[job_id] = True
                self._builds[job_id] = build
        if abandoned_prompt_id:
            await self._best_effort_interrupt(abandoned_prompt_id)

    async def _poll_once(self, job_id: str) -> None:
        """单步轮询 tracked 任务；成功产出时执行 downloading → auditing → completed。"""
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                self._untrack(job_id)
                return
            if record.state not in (JobState.RUNNING, JobState.RECOVERING):
                self._untrack(job_id)  # 已终态（被取消/detached 等）
                return
            prompt_id = record.comfy_prompt_id
        if not prompt_id:
            await self._fail(job_id, unknown_submission_state(job_id, ""))
            self._untrack(job_id)
            return

        try:
            entry = await self._comfy.fetch_history(prompt_id)
        except AppError as exc:
            await self._fail(job_id, exc)
            self._untrack(job_id)
            return
        except Exception as exc:
            await self._fail(job_id, classify_exception(exc))
            self._untrack(job_id)
            return

        if entry is None:
            in_queue = False
            try:
                in_queue = (await self._comfy.queue_snapshot()).contains(prompt_id)
            except Exception:  # pragma: no cover - 队列快照失败按不在队列处理
                logger.warning("获取 ComfyUI 队列快照失败: %s", prompt_id, exc_info=True)
            async with self._lock:
                record = self._jobs.get(job_id)
                if record is None or record.state not in (JobState.RUNNING, JobState.RECOVERING):
                    self._untrack(job_id)
                    return
                if not in_queue:
                    await self._fail_locked(record, unknown_submission_state(job_id, prompt_id))
                    self._untrack(job_id)
                    return
                if record.state == JobState.RECOVERING:
                    self._transition(record, JobState.RUNNING)
                    record.progress_text = "恢复后确认任务仍在 ComfyUI 队列中"
                    record.updated_at = self._clock.now()
                    self._jobs.save(record)
                    self._emit(EVENT_JOB_RUNNING, record, {"prompt_id": prompt_id})
            return  # 仍在执行，下轮再查

        if entry.status == "error":
            await self._fail(
                job_id,
                AppError(
                    INTERNAL_ERROR,
                    entry.error or "ComfyUI 执行失败",
                    {"prompt_id": prompt_id},
                    retryable=False,
                ),
            )
            self._untrack(job_id)
            return
        if not entry.completed or not entry.outputs:
            return  # 未完成，下轮再查

        # success 且有产出：执行下载/审核/完成段（有界）
        build = self._builds.get(job_id)
        if build is None:
            record = self._jobs.get(job_id)
            try:
                build = self._prompt.build_workflow(record.request) if record is not None else None
            except Exception:  # 重建失败不影响完成
                build = None
        try:
            await self._finish_download(job_id, build, entry.outputs, prompt_id)
        finally:
            self._untrack(job_id)

    async def _finish_download(
        self,
        job_id: str,
        build: Optional[WorkflowBuild],
        outputs: List[Any],
        prompt_id: str,
    ) -> None:
        """downloading → auditing → completed（有界；任一步被取消则中止）。"""
        # → downloading
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is None or record.state not in (JobState.RUNNING, JobState.RECOVERING):
                return
            self._transition(record, JobState.DOWNLOADING)
            record.progress_text = "正在下载图片"
            record.updated_at = self._clock.now()
            self._jobs.save(record)
            self._emit(EVENT_JOB_DOWNLOADING, record, {"prompt_id": prompt_id})

        downloaded: List[bytes] = []
        try:
            for ref in outputs:
                downloaded.append(await self._comfy.fetch_image_bytes(ref))
        except AppError as exc:
            await self._fail(job_id, exc)
            return
        except Exception as exc:
            await self._fail(job_id, classify_exception(exc))
            return

        if not downloaded:
            await self._fail(job_id, AppError(INTERNAL_ERROR, "ComfyUI 无图片产出", {"prompt_id": prompt_id}))
            return

        metadata: Dict[str, Any] = {
            "prompt_id": prompt_id,
            "client_request_id": None,
            "output_count": len(downloaded),
            "recovered": job_id in self._recovered,
        }
        async with self._lock:
            current = self._jobs.get(job_id)
            if current is not None:
                metadata["client_request_id"] = current.client_request_id
        try:
            image_ids: List[str] = []
            for data in downloaded:
                image_ids.append(self._images.store_image(job_id, data, dict(metadata)))
            primary_image_id = image_ids[0]
            metadata["image_ids"] = list(image_ids)
            metadata["primary_image_id"] = primary_image_id
        except Exception as exc:
            await self._fail(job_id, classify_exception(exc))
            return

        # → auditing
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is None or record.state != JobState.DOWNLOADING:
                return  # 下载期间被取消
            self._transition(record, JobState.AUDITING)
            record.progress_text = "正在审核图片"
            record.updated_at = self._clock.now()
            self._jobs.save(record)
            self._emit(EVENT_JOB_AUDITING, record)

        if self._audit is not None:
            try:
                audit_result = await self._audit.audit_image(primary_image_id, downloaded[0], metadata)
            except AppError as exc:
                await self._fail(job_id, exc)
                return
            except Exception as exc:
                await self._fail(job_id, classify_exception(exc))
                return
        else:
            audit_result = ImageAuditResult.empty()

        # → completed
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is None or record.state != JobState.AUDITING:
                return  # 审核期间被取消
            now = self._clock.now()
            started_at = record.started_at or now
            elapsed = max((now - started_at).total_seconds(), 0.0)
            result = GenerationResult(
                history_id=job_id,
                image_id=primary_image_id,
                final_prompt=build.final_prompt if build is not None else record.request.raw_prompt,
                seed=build.seed if build is not None else record.request.seed,
                width=build.width if build is not None else record.request.width,
                height=build.height if build is not None else record.request.height,
                elapsed_seconds=elapsed,
                audit=audit_result.to_dict(),
                metadata=metadata,
            )
            self._transition(record, JobState.COMPLETED)
            record.result = result
            record.finished_at = now
            record.progress_text = "完成"
            record.updated_at = now
            self._jobs.save(record)
            self._emit(EVENT_JOB_COMPLETED, record, {"result": result.to_dict()})
            self._emit(EVENT_QUEUE_CHANGED, record, {"queued_count": self._capacity_count()})

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _untrack(self, job_id: str) -> None:
        self._tracked.pop(job_id, None)
        self._builds.pop(job_id, None)
        self._recovered.discard(job_id)

    async def _fail(self, job_id: str, error: AppError) -> None:
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return
            await self._fail_locked(record, error)

    async def _fail_locked(self, record: JobRecord, error: AppError) -> None:
        """必须在持有 _lock 时调用；终态不再改写。"""
        if record.state in TERMINAL_JOB_STATES:
            return
        self._transition(record, JobState.FAILED)
        record.error = error
        record.progress_text = f"失败: {error.message}"
        record.finished_at = self._clock.now()
        record.updated_at = self._clock.now()
        self._jobs.save(record)
        self._emit(EVENT_JOB_FAILED, record, {"error": error.to_dict()})
        self._emit(EVENT_QUEUE_CHANGED, record, {"queued_count": self._capacity_count()})

    async def _best_effort_interrupt(self, prompt_id: str) -> None:
        try:
            await self._comfy.interrupt()
        except Exception:  # pragma: no cover - 尽力而为
            logger.warning("ComfyUI interrupt 失败: %s", prompt_id, exc_info=True)

    def _transition(self, record: JobRecord, to_state: JobState) -> None:
        allowed = ALLOWED_TRANSITIONS.get(record.state, frozenset())
        if to_state not in allowed:
            raise invalid_state_transition(record.id, record.state.value, to_state.value)
        record.state = to_state

    def _emit(self, event_type: str, record: JobRecord, payload: Optional[Dict[str, Any]] = None) -> None:
        data = dict(payload or {})
        data.setdefault("state", record.state.value)
        self._events.emit(
            CoreEvent(event_type, job_id=record.id, payload=data, timestamp=self._clock.now())
        )

    def _capacity_count(self) -> int:
        return len(self._jobs.list(limit=10000, states=list(_CAPACITY_STATES)))

    def _capacity_reached(self) -> bool:
        return self._capacity_count() >= self._max_queue

    def _snapshot(self, record: JobRecord) -> JobSnapshot:
        queue_position: Optional[int] = None
        if record.state in QUEUE_POSITION_STATES:
            peers = self._jobs.list(limit=10000, states=list(QUEUE_POSITION_STATES))
            peers.sort(key=lambda r: (r.created_at, r.id))
            for index, peer in enumerate(peers):
                if peer.id == record.id:
                    queue_position = index + 1
                    break
        return JobSnapshot(
            id=record.id,
            state=record.state,
            queue_position=queue_position,
            progress_text=record.progress_text,
            comfy_prompt_id=record.comfy_prompt_id,
            result=record.result,
            error=record.error.to_dict() if isinstance(record.error, AppError) else record.error,
        )


__all__ = ["JobManager", "ALLOWED_TRANSITIONS", "DEFAULT_MAX_QUEUE", "DEFAULT_POLL_INTERVAL_SECONDS"]

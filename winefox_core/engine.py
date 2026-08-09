# -*- coding: utf-8 -*-
"""WineFox Desktop — CoreEngine 门面。

start/stop/probe_comfyui/list_catalog/submit_generation/get_job/cancel_job/
list_history/favorite_history/delete_history/update_settings。全部外部能力
（ComfyUIPort / PromptPort / CatalogPort / JobStorePort / ImageStorePort /
SecretStorePort / ConfigStorePort / EventSink / Clock / AuditPort）构造注入。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from .errors import (
    AppError,
    CONFIG_INVALID,
    classify_exception,
    engine_stopped,
    invalid_request,
)
from .jobs import (
    DEFAULT_MAX_QUEUE,
    DEFAULT_POLL_INTERVAL_SECONDS,
    JobManager,
)
from .models import (
    EVENT_ENGINE_STARTED,
    EVENT_ENGINE_STOPPED,
    CoreEvent,
    EngineStatus,
    GenerationRequest,
    JobSnapshot,
)
from .ports import (
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

logger = logging.getLogger("winefox_core.engine")

# update_settings 接受的 core 段键（其它配置段由 Agent 03 负责）
CORE_SETTING_KEYS = frozenset({
    "max_queue",
    "poll_interval_seconds",
    "queue_worker_count",
})


class CoreEngine:
    """WineFox Desktop 核心门面。"""

    def __init__(
        self,
        *,
        comfy: ComfyUIPort,
        prompt: PromptPort,
        catalog: CatalogPort,
        jobs: JobStorePort,
        images: ImageStorePort,
        secrets: SecretStorePort,
        config: ConfigStorePort,
        events: EventSink,
        clock: Clock,
        audit: Optional[AuditPort] = None,
    ) -> None:
        self._comfy = comfy
        self._prompt = prompt
        self._catalog = catalog
        self._jobs = jobs
        self._images = images
        self._secrets = secrets
        self._config = config
        self._events = events
        self._clock = clock
        self._audit = audit
        self._state = "stopped"
        self._started_at: Optional[datetime] = None
        self._stopped_at: Optional[datetime] = None

        max_queue = self._config_value("core.max_queue", DEFAULT_MAX_QUEUE, int)
        poll_interval = self._config_value(
            "core.poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS, float
        )
        self._manager = JobManager(
            jobs=jobs,
            comfy=comfy,
            prompt=prompt,
            images=images,
            events=events,
            clock=clock,
            audit=audit,
            max_queue=max_queue,
            poll_interval_seconds=poll_interval,
        )

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    async def start(self) -> None:
        if self._state == "running":
            raise AppError("ENGINE_ALREADY_RUNNING", "引擎已在运行")
        if self._state == "starting":
            raise AppError("ENGINE_ALREADY_RUNNING", "引擎正在启动")
        self._state = "starting"
        try:
            await self._manager.start()
            self._state = "running"
            self._started_at = self._clock.now()
            self._stopped_at = None
            self._events.emit(
                CoreEvent(EVENT_ENGINE_STARTED, payload={"worker_count": 1}, timestamp=self._started_at)
            )
        except Exception:
            self._state = "stopped"
            raise

    async def stop(self) -> None:
        if self._state == "stopped":
            return
        self._state = "stopping"
        await self._manager.stop()
        self._state = "stopped"
        self._stopped_at = self._clock.now()
        self._events.emit(
            CoreEvent(EVENT_ENGINE_STOPPED, payload={"persisted": True}, timestamp=self._stopped_at)
        )

    def status(self) -> EngineStatus:
        counts = self._manager.counts()
        return EngineStatus(
            state=self._state,
            started_at=self._started_at,
            stopped_at=self._stopped_at,
            jobs_queued=counts["jobs_queued"],
            jobs_active=counts["jobs_active"],
            worker_count=1,
        )

    # ------------------------------------------------------------------
    # 查询 / 操作
    # ------------------------------------------------------------------

    async def probe_comfyui(self) -> Dict[str, Any]:
        try:
            status = await self._comfy.probe()
        except AppError as exc:
            return {"online": False, "error": exc.to_dict(), "latency_ms": None}
        except Exception as exc:
            mapped = classify_exception(exc)
            return {"online": False, "error": mapped.to_dict(), "latency_ms": None}
        return status.to_dict()

    async def list_catalog(self) -> Dict[str, Any]:
        checkpoints, loras, workflows = await asyncio.gather(
            self._catalog.list_checkpoints(),
            self._catalog.list_loras(),
            self._catalog.list_workflows(),
        )
        return {
            "checkpoints": [item.to_dict() for item in checkpoints],
            "loras": [item.to_dict() for item in loras],
            "workflows": [item.to_dict() for item in workflows],
        }

    async def submit_generation(
        self, request: Union[GenerationRequest, Dict[str, Any]]
    ) -> JobSnapshot:
        try:
            return await self._manager.submit(request)
        except ValueError as exc:  # GenerationRequest.from_dict 校验失败
            raise invalid_request(str(exc)) from exc

    async def get_job(self, job_id: str) -> JobSnapshot:
        return await self._manager.get_job(job_id)

    async def cancel_job(self, job_id: str) -> JobSnapshot:
        return await self._manager.cancel(job_id)

    async def list_history(self, limit: int = 50) -> List[JobSnapshot]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise invalid_request(f"limit 必须为非负整数: {limit!r}")
        return await self._manager.list_history(limit)

    async def favorite_history(self, job_id: str, favorite: bool = True) -> JobSnapshot:
        return await self._manager.favorite(job_id, bool(favorite))

    async def delete_history(self, job_id: str) -> JobSnapshot:
        return await self._manager.delete_history(job_id)

    async def update_settings(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        """校验并应用 core 段设置；非法键/值抛 CONFIG_INVALID。"""
        if not isinstance(settings, dict):
            raise invalid_request("settings 必须为 dict")
        unknown = set(settings) - CORE_SETTING_KEYS
        if unknown:
            raise AppError(
                CONFIG_INVALID,
                f"未知设置项: {sorted(unknown)}（core 段仅支持 {sorted(CORE_SETTING_KEYS)}）",
                {"unknown": sorted(unknown)},
            )
        applied: Dict[str, Any] = {}
        for key, value in settings.items():
            if key == "max_queue":
                if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                    raise AppError(CONFIG_INVALID, f"max_queue 必须为正整数: {value!r}")
                self._manager.set_max_queue(value)
                applied[key] = value
            elif key == "poll_interval_seconds":
                try:
                    number = float(value)
                except (TypeError, ValueError) as exc:
                    raise AppError(CONFIG_INVALID, f"poll_interval_seconds 非法: {value!r}") from exc
                if number < 0.05:
                    raise AppError(CONFIG_INVALID, "poll_interval_seconds 必须 >= 0.05")
                self._manager.set_poll_interval(number)
                applied[key] = number
            elif key == "queue_worker_count":
                if value != 1:
                    raise AppError(
                        CONFIG_INVALID,
                        f"queue_worker_count 固定为 1（单 worker），收到 {value!r}",
                    )
                applied[key] = 1
        if applied:
            self._config.update_section("core", applied)
        return {"applied": applied}

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _config_value(self, key: str, default: Any, cast: type) -> Any:
        value = self._config.get(key, default)
        if value is None:
            return default
        try:
            if cast is int:
                if isinstance(value, bool):
                    raise ValueError
                return int(value)
            return cast(value)
        except (TypeError, ValueError):
            logger.warning("配置 %s 非法（%r），回退默认值 %r", key, value, default)
            return default


__all__ = ["CoreEngine", "CORE_SETTING_KEYS"]

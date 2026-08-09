# -*- coding: utf-8 -*-
"""WineFox Desktop — 端口契约（Protocol）。

平台无关：接口中不得出现 AstrBot event、aiohttp request、Tauri 或 UI 绝对路径。
所有端口由对应 Agent（03-07）实现，经 CoreEngine 构造注入。

实现约定：
- 端口方法可抛 AppError（见 errors.py）；其它异常由调用方按 classify_exception 映射。
- 所有 async 端口方法都可在事件循环中安全 await。
- EventSink.emit 为同步方法：实现方必须自身不阻塞（内部排队或立即投递）。
"""

from __future__ import annotations

import abc
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from .models import (
    ComfyHistoryEntry,
    ComfyImageRef,
    ComfyQueueSnapshot,
    ComfySubmission,
    ComfyUIStatus,
    CoreEvent,
    GenerationRequest,
    ImageAuditResult,
    JobRecord,
    JobState,
    LoraInfo,
    ModelFamily,
    ModelInfo,
    WorkflowBuild,
    WorkflowInfo,
)


class Clock(abc.ABC):
    """时间源：测试可注入假时钟。"""

    def now(self) -> datetime:
        """当前 UTC aware 时间。"""
        raise NotImplementedError

    def monotonic(self) -> float:
        """单调时钟秒数（用于耗时统计，不受系统时间调整影响）。"""
        raise NotImplementedError


class ConfigStorePort(abc.ABC):
    """配置存储（Agent 03 实现）。点分 key，如 'core.max_queue'。"""

    def get(self, key: str, default: Any = None) -> Any:
        raise NotImplementedError

    def get_all(self) -> Dict[str, Any]:
        raise NotImplementedError

    def set(self, key: str, value: Any) -> None:
        raise NotImplementedError

    def update_section(self, section: str, values: Dict[str, Any]) -> None:
        raise NotImplementedError


class SecretStorePort(abc.ABC):
    """密钥存储（Agent 03 实现；失败抛 SECRET_STORE_FAILED）。"""

    def get(self, key: str) -> Optional[str]:
        raise NotImplementedError

    def set(self, key: str, value: str) -> None:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError


class JobStorePort(abc.ABC):
    """任务持久化（Agent 04 实现）。JobRecord 见 models.py。"""

    def save(self, record: JobRecord) -> None:
        raise NotImplementedError

    def get(self, job_id: str) -> Optional[JobRecord]:
        raise NotImplementedError

    def get_by_client_request_id(self, client_request_id: str) -> Optional[JobRecord]:
        raise NotImplementedError

    def list(
        self,
        limit: int = 200,
        states: Optional[Sequence[JobState]] = None,
    ) -> List[JobRecord]:
        """按创建时间升序返回；states 为 None 表示全部。"""
        raise NotImplementedError

    def next_queued(self) -> Optional[JobRecord]:
        """返回最早的 queued 任务（worker 拾取）。"""
        raise NotImplementedError

    def delete(self, job_id: str) -> None:
        raise NotImplementedError


class ImageStorePort(abc.ABC):
    """图片存储（Agent 04 实现）。store_image 返回 image_id。"""

    def store_image(self, job_id: str, data: bytes, metadata: Dict[str, Any]) -> str:
        raise NotImplementedError

    def read_image(self, image_id: str) -> Optional[bytes]:
        raise NotImplementedError

    def delete_image(self, image_id: str) -> None:
        raise NotImplementedError


class ComfyUIPort(abc.ABC):
    """ComfyUI HTTP 适配层（Agent 05 实现）。"""

    async def probe(self) -> ComfyUIStatus:
        raise NotImplementedError

    async def submit(self, workflow: Dict[str, Any], client_id: str) -> ComfySubmission:
        """POST /prompt；校验失败抛 WORKFLOW_NODE_MISSING / CHECKPOINT_NOT_FOUND 等。"""
        raise NotImplementedError

    async def fetch_history(self, prompt_id: str) -> Optional[ComfyHistoryEntry]:
        """GET /history/{prompt_id}；未完成返回 None 或 pending 条目。"""
        raise NotImplementedError

    async def queue_snapshot(self) -> ComfyQueueSnapshot:
        """GET /queue。"""
        raise NotImplementedError

    async def fetch_image_bytes(self, image: ComfyImageRef) -> bytes:
        """GET /view；失败抛 INVALID_IMAGE。"""
        raise NotImplementedError

    async def upload_image(self, source_path: str) -> str:
        """POST /upload，返回上传后的文件名；失败抛 INVALID_IMAGE / UPLOAD_TOO_LARGE。"""
        raise NotImplementedError

    async def interrupt(self) -> None:
        """POST /interrupt（尽力而为，失败可忽略）。"""
        raise NotImplementedError


class PromptPort(abc.ABC):
    """提示词/工作流构建（Agent 06 实现）。"""

    def build_workflow(self, request: GenerationRequest) -> WorkflowBuild:
        """把 GenerationRequest 编译为可提交的 API 工作流。

        失败抛：WORKFLOW_NOT_FOUND / WORKFLOW_NODE_MISSING /
        CHECKPOINT_NOT_FOUND / LORA_NOT_FOUND / CONFIG_INVALID。
        """
        raise NotImplementedError


class CatalogPort(abc.ABC):
    """模型/工作流目录（Agent 05/06 实现）。"""

    async def list_checkpoints(self, family: Optional[ModelFamily] = None) -> List[ModelInfo]:
        raise NotImplementedError

    async def list_loras(
        self,
        query: Optional[str] = None,
        family: Optional[ModelFamily] = None,
    ) -> List[LoraInfo]:
        raise NotImplementedError

    async def list_workflows(self) -> List[WorkflowInfo]:
        raise NotImplementedError


class AuditPort(abc.ABC):
    """图片审核（Agent 07/10 实现；CoreEngine 可注入 None 跳过）。"""

    async def audit_image(
        self, image_id: str, data: bytes, metadata: Dict[str, Any]
    ) -> ImageAuditResult:
        raise NotImplementedError


class EventSink(abc.ABC):
    """事件出口（Agent 08 UI / Agent 07 API 实现）。同步、非阻塞。"""

    def emit(self, event: CoreEvent) -> None:
        raise NotImplementedError


__all__ = [
    "Clock", "ConfigStorePort", "SecretStorePort", "JobStorePort",
    "ImageStorePort", "ComfyUIPort", "PromptPort", "CatalogPort",
    "AuditPort", "EventSink",
]

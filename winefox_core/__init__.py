# -*- coding: utf-8 -*-
"""WineFox Desktop — 平台无关核心包。

领域类型 / 稳定错误码 / 端口契约 / 任务状态机 / CoreEngine 门面。
本包不导入 astrbot、aiohttp、Tauri 或具体 SQLite；外部能力一律经
ports.py 的 Protocol 注入。
"""

from .errors import AppError, classify_exception, KNOWN_ERROR_CODES, RETRYABLE_CODES
from .models import (
    GenerationMode,
    GenerationRequest,
    GenerationResult,
    ImageAuditResult,
    JobRecord,
    JobSnapshot,
    JobState,
    LoraSelection,
    ModelFamily,
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
from .jobs import JobManager, ALLOWED_TRANSITIONS
from .engine import CoreEngine

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "AppError", "classify_exception", "KNOWN_ERROR_CODES", "RETRYABLE_CODES",
    "GenerationMode", "GenerationRequest", "GenerationResult", "ImageAuditResult",
    "JobRecord", "JobSnapshot", "JobState", "LoraSelection", "ModelFamily",
    "AuditPort", "CatalogPort", "Clock", "ComfyUIPort", "ConfigStorePort",
    "EventSink", "ImageStorePort", "JobStorePort", "PromptPort", "SecretStorePort",
    "JobManager", "ALLOWED_TRANSITIONS", "CoreEngine",
]

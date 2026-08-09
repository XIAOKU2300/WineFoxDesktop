# -*- coding: utf-8 -*-
"""WineFox Desktop — 平台无关领域类型（冻结契约）。

本模块只依赖标准库，不导入 astrbot / aiohttp / Tauri / SQLite。
所有冻结类型（GenerationRequest / LoraSelection / GenerationResult /
JobState / JobSnapshot / AppError 见 errors.py）必须保持字段与语义稳定，
其它 Agent（03-10）按此契约实现各自适配层。

序列化约定：
- to_dict() / from_dict() 严格往返；from_dict 遇到未知字段或非法值抛
  ValueError（消息以 "invalid" 开头）。
- 时间一律 timezone-aware UTC，ISO-8601 字符串。
- metadata / audit 必须 JSON 可序列化（json.dumps 往返校验）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------

class GenerationMode(str, Enum):
    TXT2IMG = "txt2img"
    IMG2IMG = "img2img"


class ModelFamily(str, Enum):
    ANIMA = "anima"
    ILLUSTRIOUS = "illustrious"


class JobState(str, Enum):
    QUEUED = "queued"
    PREPARING = "preparing"
    SUBMITTING = "submitting"
    RUNNING = "running"
    DOWNLOADING = "downloading"
    AUDITING = "auditing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DETACHED = "detached"
    RECOVERING = "recovering"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_JOB_STATES

    @property
    def is_active(self) -> bool:
        return self in ACTIVE_JOB_STATES


# 终态：不会再被 worker 拾取或自动流转
TERMINAL_JOB_STATES: frozenset = frozenset(
    {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED, JobState.DETACHED}
)

# 活跃态：重启后需要恢复追踪
ACTIVE_JOB_STATES: frozenset = frozenset(
    {JobState.SUBMITTING, JobState.RUNNING, JobState.DOWNLOADING, JobState.AUDITING}
)

# 排队位置有意义的状态
QUEUE_POSITION_STATES: frozenset = frozenset(
    {JobState.QUEUED, JobState.PREPARING}
)


# ---------------------------------------------------------------------------
# 核心事件类型常量（EventSink 契约）
# ---------------------------------------------------------------------------

EVENT_ENGINE_STARTED = "engine.started"
EVENT_ENGINE_STOPPED = "engine.stopped"
EVENT_QUEUE_CHANGED = "queue.changed"
EVENT_JOB_QUEUED = "job.queued"
EVENT_JOB_PREPARING = "job.preparing"
EVENT_JOB_SUBMITTING = "job.submitting"
EVENT_JOB_RUNNING = "job.running"
EVENT_JOB_DOWNLOADING = "job.downloading"
EVENT_JOB_AUDITING = "job.auditing"
EVENT_JOB_COMPLETED = "job.completed"
EVENT_JOB_FAILED = "job.failed"
EVENT_JOB_CANCELLED = "job.cancelled"
EVENT_JOB_DETACHED = "job.detached"
EVENT_JOB_RECOVERING = "job.recovering"
EVENT_JOB_PROGRESS = "job.progress"
EVENT_JOB_FAVORITED = "job.favorited"
EVENT_JOB_DELETED = "job.deleted"


# ---------------------------------------------------------------------------
# 校验辅助
# ---------------------------------------------------------------------------

def _json_serializable(value: Any, label: str) -> None:
    """metadata/audit 等自由字典必须能无损 JSON 序列化。"""
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:  # 含循环引用 / NaN
        raise ValueError(
            f"invalid {label}: value must be JSON serializable: {exc}"
        ) from exc


def _require_non_empty_text(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid {label}: expected str, got {type(value).__name__}")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"invalid {label}: must not be empty")
    if any(ord(ch) < 0x20 for ch in stripped):
        raise ValueError(f"invalid {label}: control characters are not allowed")
    return stripped


def _validate_dimension(value: Optional[int], label: str) -> None:
    """尺寸：正数且为 8 的倍数。"""
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"invalid {label}: expected int, got {type(value).__name__}")
    if value <= 0:
        raise ValueError(f"invalid {label}: must be a positive integer, got {value}")
    if value % 8 != 0:
        raise ValueError(f"invalid {label}: must be a multiple of 8, got {value}")


def _validate_seed(value: Optional[int]) -> None:
    """seed 允许 0；必须是非负整数。"""
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"invalid seed: expected int, got {type(value).__name__}")
    if value < 0 or value >= 2**63:
        raise ValueError(f"invalid seed: must be in [0, 2**63), got {value}")


# LoRA 强度合理边界（含负数用于反向/去权重场景）
LORA_STRENGTH_MIN = -4.0
LORA_STRENGTH_MAX = 4.0


def _validate_lora_strength(value: float, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"invalid {label}: expected number")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"invalid {label}: must be finite")
    if not (LORA_STRENGTH_MIN <= number <= LORA_STRENGTH_MAX):
        raise ValueError(
            f"invalid {label}: must be within "
            f"[{LORA_STRENGTH_MIN}, {LORA_STRENGTH_MAX}], got {number}"
        )
    return number


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"invalid datetime: missing timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _strict_dict(value: Any, label: str, required: tuple, allowed: tuple) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"invalid {label}: expected dict")
    unknown = set(value) - set(allowed)
    if unknown:
        raise ValueError(f"invalid {label}: unknown fields {sorted(unknown)}")
    missing = [name for name in required if name not in value]
    if missing:
        raise ValueError(f"invalid {label}: missing fields {missing}")
    return value


# ---------------------------------------------------------------------------
# 冻结类型
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LoraSelection:
    """冻结契约：LoRA 选择。"""

    name: str
    strength_model: float = 1.0
    strength_clip: float = 1.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _require_non_empty_text(self.name, "lora.name"))
        object.__setattr__(self, "strength_model", _validate_lora_strength(self.strength_model, "lora.strength_model"))
        object.__setattr__(self, "strength_clip", _validate_lora_strength(self.strength_clip, "lora.strength_clip"))

    def validate(self) -> "LoraSelection":
        return self

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "strength_model": self.strength_model,
            "strength_clip": self.strength_clip,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "LoraSelection":
        data = _strict_dict(value, "LoraSelection", ("name",), ("name", "strength_model", "strength_clip"))
        return cls(
            name=data["name"],
            strength_model=data.get("strength_model", 1.0),
            strength_clip=data.get("strength_clip", 1.0),
        )


@dataclass(frozen=True)
class GenerationRequest:
    """冻结契约：一次文生图 / 图生图请求。"""

    client_request_id: str
    mode: GenerationMode
    raw_prompt: str
    source_upload_ids: List[str]
    model_family: ModelFamily
    workflow_id: Optional[str] = None
    loras: List[LoraSelection] = field(default_factory=list)
    seed: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    negative_prompt: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "client_request_id", _require_non_empty_text(self.client_request_id, "client_request_id"))
        object.__setattr__(self, "raw_prompt", _require_non_empty_text(self.raw_prompt, "raw_prompt"))
        if not isinstance(self.mode, GenerationMode):
            raise ValueError(f"invalid mode: {self.mode!r}")
        if not isinstance(self.model_family, ModelFamily):
            raise ValueError(f"invalid model_family: {self.model_family!r}")
        if not isinstance(self.source_upload_ids, list):
            raise ValueError("invalid source_upload_ids: expected list")
        for idx, upload_id in enumerate(self.source_upload_ids):
            _require_non_empty_text(upload_id, f"source_upload_ids[{idx}]")
        if self.workflow_id is not None:
            _require_non_empty_text(self.workflow_id, "workflow_id")
        if not isinstance(self.loras, list):
            raise ValueError("invalid loras: expected list")
        object.__setattr__(self, "loras", list(self.loras))
        for lora in self.loras:
            if not isinstance(lora, LoraSelection):
                raise ValueError(f"invalid loras item: expected LoraSelection, got {type(lora).__name__}")
        _validate_seed(self.seed)
        _validate_dimension(self.width, "width")
        _validate_dimension(self.height, "height")
        if self.negative_prompt is not None:
            if not isinstance(self.negative_prompt, str):
                raise ValueError("invalid negative_prompt: expected str or None")
            if any(ord(ch) < 0x20 for ch in self.negative_prompt):
                raise ValueError("invalid negative_prompt: control characters are not allowed")

    def validate(self) -> "GenerationRequest":
        return self

    def to_dict(self) -> dict:
        return {
            "client_request_id": self.client_request_id,
            "mode": self.mode.value,
            "raw_prompt": self.raw_prompt,
            "source_upload_ids": list(self.source_upload_ids),
            "model_family": self.model_family.value,
            "workflow_id": self.workflow_id,
            "loras": [lora.to_dict() for lora in self.loras],
            "seed": self.seed,
            "width": self.width,
            "height": self.height,
            "negative_prompt": self.negative_prompt,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "GenerationRequest":
        data = _strict_dict(
            value,
            "GenerationRequest",
            ("client_request_id", "mode", "raw_prompt", "source_upload_ids", "model_family"),
            (
                "client_request_id", "mode", "raw_prompt", "source_upload_ids",
                "model_family", "workflow_id", "loras", "seed", "width",
                "height", "negative_prompt",
            ),
        )
        try:
            mode = GenerationMode(data["mode"])
            family = ModelFamily(data["model_family"])
        except ValueError as exc:
            raise ValueError(f"invalid GenerationRequest: {exc}") from exc
        return cls(
            client_request_id=data["client_request_id"],
            mode=mode,
            raw_prompt=data["raw_prompt"],
            source_upload_ids=list(data["source_upload_ids"]),
            model_family=family,
            workflow_id=data.get("workflow_id"),
            loras=[LoraSelection.from_dict(item) for item in data.get("loras", [])],
            seed=data.get("seed"),
            width=data.get("width"),
            height=data.get("height"),
            negative_prompt=data.get("negative_prompt"),
        )


@dataclass(frozen=True)
class GenerationResult:
    """冻结契约：一次成功生成的产物描述。"""

    history_id: str
    image_id: str
    final_prompt: str
    seed: Optional[int]
    width: int
    height: int
    elapsed_seconds: float
    audit: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "history_id", _require_non_empty_text(self.history_id, "history_id"))
        object.__setattr__(self, "image_id", _require_non_empty_text(self.image_id, "image_id"))
        object.__setattr__(self, "final_prompt", str(self.final_prompt or ""))
        _validate_seed(self.seed)
        _validate_dimension(self.width, "width")
        _validate_dimension(self.height, "height")
        if not isinstance(self.elapsed_seconds, (int, float)) or isinstance(self.elapsed_seconds, bool):
            raise ValueError("invalid elapsed_seconds: expected number")
        elapsed = float(self.elapsed_seconds)
        if elapsed < 0 or elapsed != elapsed:
            raise ValueError(f"invalid elapsed_seconds: must be >= 0, got {self.elapsed_seconds}")
        if not isinstance(self.audit, dict):
            raise ValueError("invalid audit: expected dict")
        if not isinstance(self.metadata, dict):
            raise ValueError("invalid metadata: expected dict")
        _json_serializable(self.audit, "audit")
        _json_serializable(self.metadata, "metadata")

    def validate(self) -> "GenerationResult":
        return self

    def to_dict(self) -> dict:
        return {
            "history_id": self.history_id,
            "image_id": self.image_id,
            "final_prompt": self.final_prompt,
            "seed": self.seed,
            "width": self.width,
            "height": self.height,
            "elapsed_seconds": self.elapsed_seconds,
            "audit": self.audit,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "GenerationResult":
        data = _strict_dict(
            value,
            "GenerationResult",
            ("history_id", "image_id", "final_prompt", "seed", "width", "height", "elapsed_seconds"),
            ("history_id", "image_id", "final_prompt", "seed", "width", "height", "elapsed_seconds", "audit", "metadata"),
        )
        return cls(
            history_id=data["history_id"],
            image_id=data["image_id"],
            final_prompt=data["final_prompt"],
            seed=data["seed"],
            width=data["width"],
            height=data["height"],
            elapsed_seconds=data["elapsed_seconds"],
            audit=dict(data.get("audit") or {}),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True)
class JobSnapshot:
    """冻结契约：任务的公开视图（API / UI 使用）。"""

    id: str
    state: JobState
    queue_position: Optional[int] = None
    progress_text: Optional[str] = None
    comfy_prompt_id: Optional[str] = None
    result: Optional[GenerationResult] = None
    error: Optional[Dict[str, Any]] = None  # AppError.to_dict()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "state": self.state.value,
            "queue_position": self.queue_position,
            "progress_text": self.progress_text,
            "comfy_prompt_id": self.comfy_prompt_id,
            "result": self.result.to_dict() if self.result is not None else None,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "JobSnapshot":
        data = _strict_dict(value, "JobSnapshot", ("id", "state"), tuple(JobSnapshot.__dataclass_fields__))
        try:
            state = JobState(data["state"])
        except ValueError as exc:
            raise ValueError(f"invalid JobSnapshot: {exc}") from exc
        result = None
        if data.get("result") is not None:
            result = GenerationResult.from_dict(data["result"])
        return cls(
            id=data["id"],
            state=state,
            queue_position=data.get("queue_position"),
            progress_text=data.get("progress_text"),
            comfy_prompt_id=data.get("comfy_prompt_id"),
            result=result,
            error=data.get("error"),
        )


# ---------------------------------------------------------------------------
# 内部持久化记录（JobStorePort 存储单位；非冻结契约但必须可序列化）
# ---------------------------------------------------------------------------

@dataclass
class JobRecord:
    """任务的持久化记录。state 变更先写 JobStore 再通知 EventSink。"""

    id: str
    client_request_id: str
    request: GenerationRequest
    state: JobState = JobState.QUEUED
    queue_position: Optional[int] = None
    progress_text: Optional[str] = None
    comfy_prompt_id: Optional[str] = None
    result: Optional[GenerationResult] = None
    error: Optional["Any"] = None  # AppError
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    detached_at: Optional[datetime] = None
    favorited_at: Optional[datetime] = None
    favorite: bool = False
    attempts: int = 0

    def to_dict(self) -> dict:
        from .errors import AppError  # 局部导入避免循环依赖

        return {
            "id": self.id,
            "client_request_id": self.client_request_id,
            "request": self.request.to_dict(),
            "state": self.state.value,
            "queue_position": self.queue_position,
            "progress_text": self.progress_text,
            "comfy_prompt_id": self.comfy_prompt_id,
            "result": self.result.to_dict() if self.result is not None else None,
            "error": self.error.to_dict() if isinstance(self.error, AppError) else self.error,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "started_at": _iso(self.started_at) if self.started_at else None,
            "finished_at": _iso(self.finished_at) if self.finished_at else None,
            "cancelled_at": _iso(self.cancelled_at) if self.cancelled_at else None,
            "detached_at": _iso(self.detached_at) if self.detached_at else None,
            "favorited_at": _iso(self.favorited_at) if self.favorited_at else None,
            "favorite": bool(self.favorite),
            "attempts": int(self.attempts),
        }

    @classmethod
    def from_dict(cls, value: Any) -> "JobRecord":
        from .errors import AppError  # 局部导入避免循环依赖

        data = _strict_dict(
            value,
            "JobRecord",
            ("id", "client_request_id", "request", "state", "created_at", "updated_at"),
            set(cls.__dataclass_fields__),
        )
        try:
            state = JobState(data["state"])
        except ValueError as exc:
            raise ValueError(f"invalid JobRecord: {exc}") from exc
        error = data.get("error")
        if isinstance(error, dict):
            error = AppError.from_dict(error)
        return cls(
            id=data["id"],
            client_request_id=data["client_request_id"],
            request=GenerationRequest.from_dict(data["request"]),
            state=state,
            queue_position=data.get("queue_position"),
            progress_text=data.get("progress_text"),
            comfy_prompt_id=data.get("comfy_prompt_id"),
            result=GenerationResult.from_dict(data["result"]) if data.get("result") else None,
            error=error,
            created_at=_parse_iso(data["created_at"]),
            updated_at=_parse_iso(data["updated_at"]),
            started_at=_parse_iso(data["started_at"]) if data.get("started_at") else None,
            finished_at=_parse_iso(data["finished_at"]) if data.get("finished_at") else None,
            cancelled_at=_parse_iso(data["cancelled_at"]) if data.get("cancelled_at") else None,
            detached_at=_parse_iso(data["detached_at"]) if data.get("detached_at") else None,
            favorited_at=_parse_iso(data["favorited_at"]) if data.get("favorited_at") else None,
            favorite=bool(data.get("favorite", False)),
            attempts=int(data.get("attempts", 0)),
        )

    def snapshot(self) -> JobSnapshot:
        return JobSnapshot(
            id=self.id,
            state=self.state,
            queue_position=self.queue_position,
            progress_text=self.progress_text,
            comfy_prompt_id=self.comfy_prompt_id,
            result=self.result,
            error=self.error.to_dict() if self.error is not None else None,
        )


# ---------------------------------------------------------------------------
# 非冻结支持类型（端口签名 / 目录 / 审计 / 状态）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CoreEvent:
    """EventSink 载荷。type 取 EVENT_* 常量。"""

    type: str
    job_id: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        _json_serializable(self.payload, "event.payload")
        return {
            "type": self.type,
            "job_id": self.job_id,
            "payload": self.payload,
            "timestamp": _iso(self.timestamp),
        }

    @classmethod
    def from_dict(cls, value: Any) -> "CoreEvent":
        data = _strict_dict(value, "CoreEvent", ("type",), ("type", "job_id", "payload", "timestamp"))
        return cls(
            type=data["type"],
            job_id=data.get("job_id"),
            payload=dict(data.get("payload") or {}),
            timestamp=_parse_iso(data["timestamp"]) if data.get("timestamp") else datetime.now(timezone.utc),
        )


@dataclass(frozen=True)
class ComfyImageRef:
    filename: str
    subfolder: str = ""
    image_type: str = "output"  # output / temp / input

    def __post_init__(self) -> None:
        object.__setattr__(self, "filename", _require_non_empty_text(self.filename, "image.filename"))
        object.__setattr__(self, "subfolder", str(self.subfolder or ""))
        if self.image_type not in ("output", "temp", "input"):
            raise ValueError(f"invalid image_type: {self.image_type!r}")

    def to_dict(self) -> dict:
        return {"filename": self.filename, "subfolder": self.subfolder, "type": self.image_type}

    @classmethod
    def from_dict(cls, value: Any) -> "ComfyImageRef":
        data = _strict_dict(value, "ComfyImageRef", ("filename",), ("filename", "subfolder", "type"))
        return cls(data["filename"], data.get("subfolder", ""), data.get("type", "output"))


@dataclass(frozen=True)
class ComfyHistoryEntry:
    status: str  # success / error / pending
    completed: bool
    outputs: List[ComfyImageRef] = field(default_factory=list)
    error: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "completed": self.completed,
            "outputs": [ref.to_dict() for ref in self.outputs],
            "error": self.error,
        }


@dataclass(frozen=True)
class ComfyQueueSnapshot:
    running: List[str] = field(default_factory=list)
    pending: List[str] = field(default_factory=list)

    def contains(self, prompt_id: str) -> bool:
        return prompt_id in self.running or prompt_id in self.pending


@dataclass(frozen=True)
class ComfySubmission:
    prompt_id: str
    queue_remaining: Optional[int] = None
    node_errors: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class ComfyUIStatus:
    online: bool
    version: Optional[str] = None
    device: Optional[str] = None
    node_count: Optional[int] = None
    latency_ms: Optional[float] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "online": self.online,
            "version": self.version,
            "device": self.device,
            "node_count": self.node_count,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }


@dataclass(frozen=True)
class WorkflowBuild:
    """PromptPort 的产出：可直接提交给 ComfyUI 的 API 工作流。"""

    workflow: Dict[str, Any]
    final_prompt: str
    seed: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    applied_loras: List[LoraSelection] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.workflow, dict) or not self.workflow:
            raise ValueError("invalid WorkflowBuild: workflow must be a non-empty dict")
        _json_serializable(self.workflow, "workflow")
        _json_serializable(self.meta, "workflow.meta")


@dataclass(frozen=True)
class ModelInfo:
    name: str
    family: Optional[ModelFamily] = None
    filename: Optional[str] = None
    size_bytes: Optional[int] = None
    downloaded: bool = True
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "family": self.family.value if self.family else None,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "downloaded": self.downloaded,
            "meta": self.meta,
        }


@dataclass(frozen=True)
class LoraInfo:
    name: str
    family: Optional[ModelFamily] = None
    trigger_words: List[str] = field(default_factory=list)
    base_model: Optional[str] = None
    downloaded: bool = True
    source: str = "local"  # local / civitai
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "family": self.family.value if self.family else None,
            "trigger_words": list(self.trigger_words),
            "base_model": self.base_model,
            "downloaded": self.downloaded,
            "source": self.source,
            "meta": self.meta,
        }


@dataclass(frozen=True)
class WorkflowInfo:
    id: str
    filename: str
    title: Optional[str] = None
    mode: Optional[GenerationMode] = None
    model_family: Optional[ModelFamily] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "filename": self.filename,
            "title": self.title,
            "mode": self.mode.value if self.mode else None,
            "model_family": self.model_family.value if self.model_family else None,
            "meta": self.meta,
        }


class AuditVerdict(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"
    REVIEW = "review"


# 与插件 IMAGE_AUDIT_SCORE_KEYS 一致的稳定五分类
AUDIT_SCORE_KEYS = ("drawings", "hentai", "neutral", "porn", "sexy")


@dataclass(frozen=True)
class ImageAuditResult:
    """五分类 NSFW 审核结果（兼容原插件 scores 结构）。"""

    scores: Dict[str, float] = field(default_factory=dict)
    binary_nsfw: float = 0.0
    backend: str = "none"
    verdict: AuditVerdict = AuditVerdict.ALLOW

    def __post_init__(self) -> None:
        # 归一化为完整五分类字典，保证往返一致
        normalized = {}
        for key in AUDIT_SCORE_KEYS:
            value = float(self.scores.get(key, 0.0) or 0.0)
            if value < 0.0 or value > 1.0:
                raise ValueError(f"invalid audit score {key}: must be in [0, 1]")
            normalized[key] = value
        object.__setattr__(self, "scores", normalized)
        object.__setattr__(self, "binary_nsfw", float(self.binary_nsfw))
        if not isinstance(self.verdict, AuditVerdict):
            raise ValueError(f"invalid verdict: {self.verdict!r}")

    def to_dict(self) -> dict:
        return {
            "scores": {key: float(self.scores.get(key, 0.0) or 0.0) for key in AUDIT_SCORE_KEYS},
            "binary_nsfw": float(self.binary_nsfw),
            "backend": self.backend,
            "verdict": self.verdict.value,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ImageAuditResult":
        data = _strict_dict(value, "ImageAuditResult", ("scores",), ("scores", "binary_nsfw", "backend", "verdict"))
        try:
            verdict = AuditVerdict(data.get("verdict", "allow"))
        except ValueError as exc:
            raise ValueError(f"invalid ImageAuditResult: {exc}") from exc
        return cls(
            scores=dict(data.get("scores") or {}),
            binary_nsfw=float(data.get("binary_nsfw", 0.0) or 0.0),
            backend=str(data.get("backend", "none")),
            verdict=verdict,
        )

    @classmethod
    def empty(cls) -> "ImageAuditResult":
        return cls(
            scores={key: 0.0 for key in AUDIT_SCORE_KEYS},
            backend="none",
            verdict=AuditVerdict.ALLOW,
        )


@dataclass(frozen=True)
class EngineStatus:
    state: str = "stopped"  # stopped / starting / running / stopping
    started_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None
    jobs_queued: int = 0
    jobs_active: int = 0
    worker_count: int = 1

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "started_at": _iso(self.started_at) if self.started_at else None,
            "stopped_at": _iso(self.stopped_at) if self.stopped_at else None,
            "jobs_queued": self.jobs_queued,
            "jobs_active": self.jobs_active,
            "worker_count": self.worker_count,
        }


__all__ = [
    "GenerationMode", "ModelFamily", "JobState", "AuditVerdict",
    "TERMINAL_JOB_STATES", "ACTIVE_JOB_STATES", "QUEUE_POSITION_STATES",
    "EVENT_ENGINE_STARTED", "EVENT_ENGINE_STOPPED", "EVENT_QUEUE_CHANGED",
    "EVENT_JOB_QUEUED", "EVENT_JOB_PREPARING", "EVENT_JOB_SUBMITTING",
    "EVENT_JOB_RUNNING", "EVENT_JOB_DOWNLOADING", "EVENT_JOB_AUDITING",
    "EVENT_JOB_COMPLETED", "EVENT_JOB_FAILED", "EVENT_JOB_CANCELLED",
    "EVENT_JOB_DETACHED", "EVENT_JOB_RECOVERING", "EVENT_JOB_PROGRESS",
    "EVENT_JOB_FAVORITED", "EVENT_JOB_DELETED",
    "LoraSelection", "GenerationRequest", "GenerationResult", "JobSnapshot",
    "JobRecord", "CoreEvent", "ComfyImageRef", "ComfyHistoryEntry",
    "ComfyQueueSnapshot", "ComfySubmission", "ComfyUIStatus", "WorkflowBuild",
    "ModelInfo", "LoraInfo", "WorkflowInfo", "ImageAuditResult",
    "AUDIT_SCORE_KEYS", "EngineStatus", "LORA_STRENGTH_MIN", "LORA_STRENGTH_MAX",
]

# -*- coding: utf-8 -*-
"""WineFox Desktop 存储模型与 JSON 校验。

本模块只依赖标准库，定义：
- 时间戳/JSON 工具函数（入库前校验）
- JobState 枚举与合法状态迁移表
- 各表的记录 dataclass（行 <-> 对象）

约定：
- 所有时间戳均为 UTC ISO-8601 字符串（``datetime.now(timezone.utc).isoformat()``）。
- JSON 字段入库前必须通过 :func:`validate_json` 校验，拒绝非 JSON 序列化数据。
- 文件路径只允许相对路径（见 :func:`normalize_relative_path`）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

__all__ = [
    "utcnow_iso",
    "validate_json",
    "validate_public_json",
    "normalize_relative_path",
    "JobState",
    "ALLOWED_STATE_TRANSITIONS",
    "LoraAssetState",
    "LoraDownloadState",
    "DOWNLOAD_ACTIVE_STATES",
    "DOWNLOAD_TERMINAL_STATES",
    "DOWNLOAD_STATE_TRANSITIONS",
    "assert_download_transition",
    "JobRecord",
    "HistoryRecord",
    "FavoriteRecord",
    "LoraProfileRecord",
    "LoraAssetRecord",
    "LoraDownloadJobRecord",
    "WorkflowRecord",
    "AppEventRecord",
    "StorageError",
    "ValidationError",
    "DuplicateError",
    "InvalidStateTransitionError",
    "MigrationError",
    "CorruptDatabaseError",
]


# ---------------------------------------------------------------------------
# 基础异常
# ---------------------------------------------------------------------------

class StorageError(Exception):
    """存储层统一基类异常。"""


class ValidationError(StorageError, ValueError):
    """数据校验失败（JSON 非法 / 路径非法 / 状态非法）。"""


class DuplicateError(StorageError):
    """唯一约束冲突。"""


class InvalidStateTransitionError(ValidationError):
    """任务状态迁移不合法。"""


class MigrationError(StorageError):
    """数据库迁移失败（已完整回滚，原库保留）。"""


class CorruptDatabaseError(StorageError):
    """数据库文件损坏或无法打开。"""


# ---------------------------------------------------------------------------
# 时间与 JSON 工具
# ---------------------------------------------------------------------------

def utcnow_iso() -> str:
    """当前 UTC 时间的 ISO-8601 字符串（带时区，可排序）。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


_JSON_SCALARS = (str, int, float, bool, type(None))

# JSON 深度/大小限制，防止异常数据写库
_MAX_JSON_DEPTH = 64
_MAX_JSON_CHARS = 2_000_000


def _json_depth(value: Any, depth: int = 0) -> int:
    if depth > _MAX_JSON_DEPTH:
        return depth
    if isinstance(value, dict):
        if not value:
            return depth + 1
        return max(_json_depth(k, depth + 1) for k in value.keys()) if any(
            isinstance(k, (dict, list)) for k in value.keys()
        ) else max((_json_depth(v, depth + 1) for v in value.values()), default=depth + 1)
    if isinstance(value, (list, tuple)):
        if not value:
            return depth + 1
        return max((_json_depth(v, depth + 1) for v in value), default=depth + 1)
    return depth


def validate_json(value: Any, *, name: str = "payload") -> None:
    """入库前校验 JSON 可序列化性。

    抛出 :class:`ValidationError` 的情况：非法 JSON 类型、循环引用、
    深度/体积超限。
    """
    if value is None:
        return
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValidationError(f"{name} 不是可序列化的 JSON: {exc}") from exc
        if len(text) > _MAX_JSON_CHARS:
            raise ValidationError(f"{name} JSON 过大（>{_MAX_JSON_CHARS} 字符）")
        if _json_depth(value) > _MAX_JSON_DEPTH:
            raise ValidationError(f"{name} JSON 嵌套过深（>{_MAX_JSON_DEPTH} 层）")
        return
    if isinstance(value, _JSON_SCALARS):
        return
    raise ValidationError(
        f"{name} 必须是 dict/list/标量，得到 {type(value).__name__}"
    )


def json_dumps(value: Any) -> str:
    """校验后序列化（无空格、UTF-8 保留）。"""
    validate_json(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_loads(text: Optional[str], default: Any = None) -> Any:
    """反序列化，失败时返回 default（不抛错，容忍历史脏数据）。"""
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError, RecursionError):
        return default


# ---------------------------------------------------------------------------
# 公开 JSON 校验（禁止持久化带凭据的 URL / Cookie / Authorization / 绝对路径）
# ---------------------------------------------------------------------------

_SECRET_QUERY_PARAM_RE = re.compile(
    r"(?i)(?:access[_-]?token|api[_-]?key|apikey|token|secret|password|"
    r"passwd|auth|signature|sig|credential|sign)(?:[&]|=)[^&\s;]+"
)
_CREDENTIAL_USERINFO_RE = re.compile(r"(?i)^https?://[^/@\s]+:[^/@\s]+@")
_URL_RE = re.compile(r"(?i)\bhttps?://\S+")
_HEADER_SECRET_RE = re.compile(r"(?i)^(?:cookie|authorization|proxy-authorization)\s*:\s*\S+")
_ABS_PATH_RE = re.compile(r"(?:^|[^\w])(?:[A-Za-z]:[\\/]|\\\\|[\\/])[^\r\n\"'<>|?*]+")


def validate_public_json(value: Any, *, name: str = "payload") -> None:
    """校验 JSON 可序列化性，且不允许出现凭据泄漏模式。

    在 :func:`validate_json` 的基础上追加扫描：递归检查每个字符串，若它
    看起来像 URL（https?://），则拒绝含查询参数凭据（token/api_key 等）或
    ``user:pass@`` 的 URL；以 ``Cookie:/Authorization:`` 开头的整串拒绝；
    形如 Windows 盘符 / UNC / 根目录的绝对路径字符串拒绝。

    用于会持久化下载源/结果/信息的字段（source_ref / result / info），
    防止带 token 的下载 URL、Cookie、Authorization、绝对路径落入数据库。
    """
    validate_json(value, name=name)

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for item in node.values():
                _walk(item)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _walk(item)
        elif isinstance(node, str):
            text = node
            if _URL_RE.search(text):
                if _CREDENTIAL_USERINFO_RE.match(text):
                    raise ValidationError(f"{name} 包含带凭据的 URL（user:pass@）")
                if _SECRET_QUERY_PARAM_RE.search(text):
                    raise ValidationError(f"{name} 的 URL 包含敏感查询参数（token/api_key 等）")
            elif _HEADER_SECRET_RE.match(text.strip()):
                raise ValidationError(f"{name} 包含 Cookie/Authorization 头")
            elif _ABS_PATH_RE.match(text.strip()):
                raise ValidationError(f"{name} 包含绝对路径: {text[:80]!r}")

    _walk(value)


# ---------------------------------------------------------------------------
# 相对路径
# ---------------------------------------------------------------------------

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def normalize_relative_path(path: Union[str, Path], *, name: str = "path") -> str:
    """校验并规范化文件相对路径。

    规则：
    - 拒绝绝对路径（POSIX 前导 ``/``、Windows 盘符 ``C:\\``、UNC ``\\\\``）；
    - 拒绝 ``..`` 越界（``a/../b`` 在规范化后仍包含 ``..`` 即拒绝）；
    - 统一为 POSIX 风格（``\\`` -> ``/``），去掉首尾空白与多余分隔符。

    返回规范化后的相对路径字符串，非法时抛出 :class:`ValidationError`。
    """
    if isinstance(path, Path):
        text = str(path)
    else:
        text = str(path or "").strip()
    if not text:
        raise ValidationError(f"{name} 不能为空")
    text = text.replace("\\", "/")
    if text.startswith("/") or _WINDOWS_DRIVE_RE.match(text) or text.startswith("//"):
        raise ValidationError(f"{name} 必须是相对路径: {text!r}")
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValidationError(f"{name} 不允许包含 .. 越界: {text!r}")
    normalized = "/".join(parts)
    if not normalized:
        raise ValidationError(f"{name} 规范化后为空: {text!r}")
    return normalized


# ---------------------------------------------------------------------------
# 任务状态机
# ---------------------------------------------------------------------------

class JobState(str, Enum):
    """任务生命周期状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    def __str__(self) -> str:  # pragma: no cover - 便利方法
        return self.value


ALLOWED_STATE_TRANSITIONS: Dict[JobState, set] = {
    JobState.PENDING: {JobState.RUNNING, JobState.CANCELLED, JobState.FAILED},
    # running -> pending 仅用于应用重启后的任务恢复（recover_interrupted）
    JobState.RUNNING: {JobState.PENDING, JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED},
    JobState.SUCCEEDED: set(),
    JobState.FAILED: set(),
    JobState.CANCELLED: set(),
}

# 非终止状态（应用重启后可恢复）
RECOVERABLE_STATES = {JobState.PENDING, JobState.RUNNING}


def assert_transition(old: JobState, new: JobState) -> None:
    """校验状态迁移合法性，非法时抛出 :class:`InvalidStateTransitionError`。"""
    old_s, new_s = JobState(old), JobState(new)
    if new_s not in ALLOWED_STATE_TRANSITIONS.get(old_s, set()):
        raise InvalidStateTransitionError(
            f"非法状态迁移: {old_s.value} -> {new_s.value}"
        )


# ---------------------------------------------------------------------------
# LoRA 资产 / 下载任务状态
# ---------------------------------------------------------------------------

class LoraAssetState(str, Enum):
    """本地库资产状态。隔离不删除权重文件，只移动并记录隔离后相对路径。"""

    INSTALLED = "installed"
    QUARANTINED = "quarantined"

    def __str__(self) -> str:  # pragma: no cover - 便利方法
        return self.value


class LoraDownloadState(str, Enum):
    """LoRA 下载任务状态机。"""

    QUEUED = "queued"
    RESOLVING = "resolving"
    DOWNLOADING = "downloading"
    PROFILING = "profiling"
    REFRESHING = "refreshing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"

    def __str__(self) -> str:  # pragma: no cover - 便利方法
        return self.value


DOWNLOAD_ACTIVE_STATES = frozenset({
    LoraDownloadState.QUEUED,
    LoraDownloadState.RESOLVING,
    LoraDownloadState.DOWNLOADING,
    LoraDownloadState.PROFILING,
    LoraDownloadState.REFRESHING,
})

DOWNLOAD_TERMINAL_STATES = frozenset({
    LoraDownloadState.COMPLETED,
    LoraDownloadState.FAILED,
    LoraDownloadState.CANCELLED,
    LoraDownloadState.INTERRUPTED,
})

DOWNLOAD_STATE_TRANSITIONS: Dict[LoraDownloadState, set] = {
    LoraDownloadState.QUEUED: {
        LoraDownloadState.RESOLVING,
        LoraDownloadState.CANCELLED,
        LoraDownloadState.FAILED,
        LoraDownloadState.INTERRUPTED,
    },
    LoraDownloadState.RESOLVING: {
        LoraDownloadState.DOWNLOADING,
        LoraDownloadState.CANCELLED,
        LoraDownloadState.FAILED,
        LoraDownloadState.INTERRUPTED,
    },
    LoraDownloadState.DOWNLOADING: {
        LoraDownloadState.PROFILING,
        LoraDownloadState.CANCELLED,
        LoraDownloadState.FAILED,
        LoraDownloadState.INTERRUPTED,
    },
    LoraDownloadState.PROFILING: {
        LoraDownloadState.REFRESHING,
        LoraDownloadState.COMPLETED,
        LoraDownloadState.CANCELLED,
        LoraDownloadState.FAILED,
        LoraDownloadState.INTERRUPTED,
    },
    LoraDownloadState.REFRESHING: {
        LoraDownloadState.COMPLETED,
        LoraDownloadState.CANCELLED,
        LoraDownloadState.FAILED,
        LoraDownloadState.INTERRUPTED,
    },
    LoraDownloadState.COMPLETED: set(),
    LoraDownloadState.FAILED: {LoraDownloadState.QUEUED},  # 重试
    LoraDownloadState.CANCELLED: {LoraDownloadState.QUEUED},  # 重新排队
    LoraDownloadState.INTERRUPTED: {LoraDownloadState.QUEUED},  # 用户重试
}


def assert_download_transition(
    old: LoraDownloadState, new: LoraDownloadState
) -> None:
    """校验下载任务状态迁移，非法时抛出 :class:`InvalidStateTransitionError`。"""
    old_s, new_s = LoraDownloadState(old), LoraDownloadState(new)
    if new_s not in DOWNLOAD_STATE_TRANSITIONS.get(old_s, set()):
        raise InvalidStateTransitionError(
            f"非法下载任务状态迁移: {old_s.value} -> {new_s.value}"
        )


# ---------------------------------------------------------------------------
# 记录 dataclass
# ---------------------------------------------------------------------------

@dataclass
class JobRecord:
    """jobs 表一行。"""

    id: int
    client_request_id: str
    mode: str
    state: str
    request_json: Dict[str, Any]
    comfy_prompt_id: Optional[str] = None
    error: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None

    @property
    def state_enum(self) -> JobState:
        return JobState(self.state)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "client_request_id": self.client_request_id,
            "mode": self.mode,
            "state": self.state,
            "request": self.request_json,
            "comfy_prompt_id": self.comfy_prompt_id,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass
class HistoryRecord:
    """history 表一行（图片生成历史，软删除）。"""

    id: int
    job_id: int
    image_relpath: str
    sha256: Optional[str] = None
    raw_prompt: str = ""
    final_prompt: str = ""
    seed: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    deleted_at: Optional[str] = None
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "image_relpath": self.image_relpath,
            "sha256": self.sha256,
            "raw_prompt": self.raw_prompt,
            "final_prompt": self.final_prompt,
            "seed": self.seed,
            "width": self.width,
            "height": self.height,
            "metadata": self.metadata,
            "deleted_at": self.deleted_at,
            "created_at": self.created_at,
        }


@dataclass
class FavoriteRecord:
    """favorites 表一行（(scope, ref_key) 唯一）。"""

    id: int
    scope: str
    ref_key: str
    kind: str = "generic"
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "scope": self.scope,
            "ref_key": self.ref_key,
            "kind": self.kind,
            "payload": self.payload,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class LoraProfileRecord:
    """lora_profiles 表一行（(model_family, normalized_key) 复合主键）。"""

    model_family: str
    normalized_key: str
    display_key: str
    profile: Dict[str, Any] = field(default_factory=dict)
    updated_at: str = ""
    revision: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_family": self.model_family,
            "normalized_key": self.normalized_key,
            "display_key": self.display_key,
            "profile": self.profile,
            "updated_at": self.updated_at,
            "revision": self.revision,
        }


@dataclass
class LoraAssetRecord:
    """lora_assets 表一行（(model_family, filename) 唯一）。

    只记录索引与相对路径；权重文件本身不受本层删除/改写。隔离时
    quarantine_relpath 记录隔离后的相对路径，恢复只能回原位。
    """

    id: int
    model_family: str
    filename: str
    comfy_key: str = ""
    display_name: str = ""
    source: str = "local"
    model_id: Optional[int] = None
    version_id: Optional[int] = None
    preview_key: Optional[str] = None
    state: str = "installed"
    quarantine_relpath: Optional[str] = None
    disabled: bool = False
    disable_reason: Optional[str] = None
    info: Dict[str, Any] = field(default_factory=dict)
    installed_at: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""

    @property
    def state_enum(self) -> LoraAssetState:
        return LoraAssetState(self.state)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "model_family": self.model_family,
            "filename": self.filename,
            "comfy_key": self.comfy_key,
            "display_name": self.display_name,
            "source": self.source,
            "model_id": self.model_id,
            "version_id": self.version_id,
            "preview_key": self.preview_key,
            "state": self.state,
            "quarantine_relpath": self.quarantine_relpath,
            "disabled": self.disabled,
            "disable_reason": self.disable_reason,
            "info": self.info,
            "installed_at": self.installed_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class LoraDownloadJobRecord:
    """lora_download_jobs 表一行（result_id 主键）。

    source_ref / result / progress 均为已验证的公开 JSON（不含带 token 的
    下载 URL、Cookie、Authorization、绝对路径）。
    """

    result_id: str
    state: str = "queued"
    family: str = "illustrious"
    alias: str = ""
    source: str = "civitai"
    source_ref: Dict[str, Any] = field(default_factory=dict)
    progress: Dict[str, Any] = field(default_factory=dict)
    result: Dict[str, Any] = field(default_factory=dict)
    part_files: List[str] = field(default_factory=list)
    error: Optional[str] = None
    request_timeout: int = 60
    created_at: str = ""
    updated_at: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None

    @property
    def state_enum(self) -> LoraDownloadState:
        return LoraDownloadState(self.state)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "result_id": self.result_id,
            "state": self.state,
            "family": self.family,
            "alias": self.alias,
            "source": self.source,
            "source_ref": self.source_ref,
            "progress": self.progress,
            "result": self.result,
            "part_files": list(self.part_files),
            "error": self.error,
            "request_timeout": self.request_timeout,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass
class WorkflowRecord:
    """workflow_registry 表一行（name 唯一）。"""

    id: int
    name: str
    version: str = "1"
    kind: str = "workflow"
    definition: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "kind": self.kind,
            "definition": self.definition,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class AppEventRecord:
    """app_events 表一行（追加式事件日志）。"""

    id: int
    event_type: str
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "event_type": self.event_type,
            "payload": self.payload,
            "created_at": self.created_at,
        }

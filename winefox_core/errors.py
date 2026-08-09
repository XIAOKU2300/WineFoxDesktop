# -*- coding: utf-8 -*-
"""WineFox Desktop — 稳定错误码与 AppError。

错误码是冻结契约（字符串常量，稳定不变化）。至少包含任务规格要求的 17 个
基础码；额外补充了内部流转与请求校验使用的辅助码。任何 Agent 不得修改已有
码的含义；新增码必须走 __all__ 与 KNOWN_ERROR_CODES。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# 稳定错误码（冻结）
# ---------------------------------------------------------------------------

# ComfyUI 连接层
COMFYUI_OFFLINE = "COMFYUI_OFFLINE"            # 无法连接 / 连接被拒
COMFYUI_AUTH_FAILED = "COMFYUI_AUTH_FAILED"    # 认证失败（401 / 令牌无效）
COMFYUI_TLS_FAILED = "COMFYUI_TLS_FAILED"      # TLS / 证书错误
COMFYUI_TIMEOUT = "COMFYUI_TIMEOUT"            # 请求超时

# 工作流 / 资源
WORKFLOW_NOT_FOUND = "WORKFLOW_NOT_FOUND"      # 工作流文件不存在
WORKFLOW_NODE_MISSING = "WORKFLOW_NODE_MISSING"  # 工作流缺少必要节点 / 节点类不存在
CHECKPOINT_NOT_FOUND = "CHECKPOINT_NOT_FOUND"  # checkpoint 模型缺失
LORA_NOT_FOUND = "LORA_NOT_FOUND"              # LoRA 文件缺失

# 输入 / 上传
INVALID_IMAGE = "INVALID_IMAGE"                # 图片无效 / 无法解码
UPLOAD_TOO_LARGE = "UPLOAD_TOO_LARGE"          # 上传体积超限

# 队列 / 任务
QUEUE_FULL = "QUEUE_FULL"                      # 队列已满（默认上限 20）
JOB_NOT_CANCELLABLE = "JOB_NOT_CANCELLABLE"    # 任务当前状态不可取消
JOB_NOT_FAVORITABLE = "JOB_NOT_FAVORITABLE"    # 任务当前状态不可收藏
JOB_NOT_FOUND = "JOB_NOT_FOUND"                # 任务不存在
JOB_NOT_DELETABLE = "JOB_NOT_DELETABLE"        # 任务当前状态不可删除

# 配置 / 密钥 / 后端
DISK_SPACE_LOW = "DISK_SPACE_LOW"              # 磁盘空间不足
CONFIG_INVALID = "CONFIG_INVALID"              # 配置非法
SECRET_STORE_FAILED = "SECRET_STORE_FAILED"    # 密钥存取失败
BACKEND_RESTARTED = "BACKEND_RESTARTED"        # 本端重启导致任务中断
UNKNOWN_SUBMISSION_STATE = "UNKNOWN_SUBMISSION_STATE"  # prompt_id 无法在历史/队列中定位

# 引擎生命周期 / 内部
ENGINE_STOPPED = "ENGINE_STOPPED"              # 引擎已停止，拒绝接单
ENGINE_ALREADY_RUNNING = "ENGINE_ALREADY_RUNNING"
INVALID_REQUEST = "INVALID_REQUEST"            # 请求 / 设置校验失败
INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"  # 任务状态机非法迁移
AUDIT_FAILED = "AUDIT_FAILED"                  # 审核通道失败
INTERNAL_ERROR = "INTERNAL_ERROR"              # 兜底内部错误

KNOWN_ERROR_CODES: frozenset = frozenset({
    COMFYUI_OFFLINE, COMFYUI_AUTH_FAILED, COMFYUI_TLS_FAILED, COMFYUI_TIMEOUT,
    WORKFLOW_NOT_FOUND, WORKFLOW_NODE_MISSING, CHECKPOINT_NOT_FOUND,
    LORA_NOT_FOUND, INVALID_IMAGE, UPLOAD_TOO_LARGE, QUEUE_FULL,
    JOB_NOT_CANCELLABLE, JOB_NOT_FAVORITABLE, JOB_NOT_FOUND, JOB_NOT_DELETABLE, DISK_SPACE_LOW,
    CONFIG_INVALID, SECRET_STORE_FAILED, BACKEND_RESTARTED,
    UNKNOWN_SUBMISSION_STATE, ENGINE_STOPPED, ENGINE_ALREADY_RUNNING,
    INVALID_REQUEST, INVALID_STATE_TRANSITION, AUDIT_FAILED, INTERNAL_ERROR,
})

# 可重试码（瞬态故障，UI 可提示"稍后重试"）
RETRYABLE_CODES: frozenset = frozenset({
    COMFYUI_OFFLINE, COMFYUI_TIMEOUT, COMFYUI_TLS_FAILED, QUEUE_FULL,
    DISK_SPACE_LOW, SECRET_STORE_FAILED, INTERNAL_ERROR,
})


@dataclass(frozen=True)
class AppError(Exception):
    """统一错误类型：稳定 code + 人类可读 message + 结构化 details。

    - code: KNOWN_ERROR_CODES 中的稳定字符串
    - retryable: 是否建议重试（瞬时故障为 True）
    - details: 任意 JSON 可序列化字典
    """

    code: str
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    retryable: bool = False

    def __post_init__(self) -> None:
        if self.code not in KNOWN_ERROR_CODES:
            raise ValueError(f"unknown error code: {self.code!r}")
        if not isinstance(self.message, str):
            raise ValueError("AppError.message must be a str")
        if not isinstance(self.details, dict):
            raise ValueError("AppError.details must be a dict")
        try:
            import json

            json.dumps(self.details, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"AppError.details must be JSON serializable: {exc}") from exc

    def __str__(self) -> str:
        return f"[{self.code}] {self.message or '(no message)'}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "retryable": bool(self.retryable),
        }

    @classmethod
    def from_dict(cls, value: Any) -> "AppError":
        if not isinstance(value, dict):
            raise ValueError("invalid AppError: expected dict")
        if "code" not in value:
            raise ValueError("invalid AppError: missing 'code'")
        return cls(
            code=value["code"],
            message=str(value.get("message") or ""),
            details=dict(value.get("details") or {}),
            retryable=bool(value.get("retryable", False)),
        )


# ---------------------------------------------------------------------------
# 便捷构造器
# ---------------------------------------------------------------------------

def comfyui_offline(detail: str = "", retryable: bool = True) -> AppError:
    return AppError(COMFYUI_OFFLINE, detail or "无法连接 ComfyUI", {"detail": detail}, retryable)


def comfyui_auth_failed(detail: str = "") -> AppError:
    return AppError(COMFYUI_AUTH_FAILED, detail or "ComfyUI 认证失败", {"detail": detail})


def comfyui_tls_failed(detail: str = "", retryable: bool = True) -> AppError:
    return AppError(COMFYUI_TLS_FAILED, detail or "ComfyUI TLS/证书错误", {"detail": detail}, retryable)


def comfyui_timeout(detail: str = "", retryable: bool = True) -> AppError:
    return AppError(COMFYUI_TIMEOUT, detail or "ComfyUI 请求超时", {"detail": detail}, retryable)


def workflow_not_found(workflow_id: str) -> AppError:
    return AppError(WORKFLOW_NOT_FOUND, f"工作流不存在: {workflow_id}", {"workflow_id": workflow_id})


def workflow_node_missing(detail: str = "") -> AppError:
    return AppError(WORKFLOW_NODE_MISSING, detail or "工作流缺少必要节点", {"detail": detail})


def checkpoint_not_found(name: str) -> AppError:
    return AppError(CHECKPOINT_NOT_FOUND, f"checkpoint 不存在: {name}", {"name": name})


def lora_not_found(name: str) -> AppError:
    return AppError(LORA_NOT_FOUND, f"LoRA 不存在: {name}", {"name": name})


def invalid_image(detail: str = "") -> AppError:
    return AppError(INVALID_IMAGE, detail or "图片无效", {"detail": detail})


def upload_too_large(detail: str = "") -> AppError:
    return AppError(UPLOAD_TOO_LARGE, detail or "上传体积超限", {"detail": detail})


def queue_full(max_queue: int) -> AppError:
    return AppError(QUEUE_FULL, f"队列已满（上限 {max_queue}）", {"max_queue": max_queue}, retryable=True)


def job_not_cancellable(job_id: str, state: str) -> AppError:
    return AppError(JOB_NOT_CANCELLABLE, f"任务不可取消: {job_id} (state={state})", {"job_id": job_id, "state": state})


def job_not_favoritable(job_id: str, state: str) -> AppError:
    return AppError(JOB_NOT_FAVORITABLE, f"任务不可收藏: {job_id} (state={state})", {"job_id": job_id, "state": state})


def job_not_found(job_id: str) -> AppError:
    return AppError(JOB_NOT_FOUND, f"任务不存在: {job_id}", {"job_id": job_id})


def job_not_deletable(job_id: str, state: str) -> AppError:
    return AppError(JOB_NOT_DELETABLE, f"任务不可删除: {job_id} (state={state})", {"job_id": job_id, "state": state})


def disk_space_low(detail: str = "", retryable: bool = True) -> AppError:
    return AppError(DISK_SPACE_LOW, detail or "磁盘空间不足", {"detail": detail}, retryable)


def config_invalid(detail: str = "") -> AppError:
    return AppError(CONFIG_INVALID, detail or "配置非法", {"detail": detail})


def secret_store_failed(detail: str = "") -> AppError:
    return AppError(SECRET_STORE_FAILED, detail or "密钥存储失败", {"detail": detail})


def backend_restarted(job_id: str) -> AppError:
    return AppError(BACKEND_RESTARTED, f"后端重启导致任务中断: {job_id}", {"job_id": job_id})


def unknown_submission_state(job_id: str, prompt_id: str) -> AppError:
    return AppError(
        UNKNOWN_SUBMISSION_STATE,
        f"无法在 ComfyUI 历史/队列中定位任务: job={job_id} prompt_id={prompt_id}",
        {"job_id": job_id, "prompt_id": prompt_id},
    )


def engine_stopped() -> AppError:
    return AppError(ENGINE_STOPPED, "引擎已停止，拒绝接收新任务")


def invalid_request(detail: str = "") -> AppError:
    return AppError(INVALID_REQUEST, detail or "请求校验失败", {"detail": detail})


def invalid_state_transition(job_id: str, from_state: str, to_state: str) -> AppError:
    return AppError(
        INVALID_STATE_TRANSITION,
        f"非法状态迁移: {from_state} -> {to_state} (job={job_id})",
        {"job_id": job_id, "from": from_state, "to": to_state},
    )


def audit_failed(detail: str = "") -> AppError:
    return AppError(AUDIT_FAILED, detail or "图片审核失败", {"detail": detail})


def internal_error(detail: str = "") -> AppError:
    return AppError(INTERNAL_ERROR, detail or "内部错误", {"detail": detail})


# ---------------------------------------------------------------------------
# 异常映射（不得导入 aiohttp；按类名/模块名文本识别外部库异常）
# ---------------------------------------------------------------------------

def _exc_module_name(exc: BaseException) -> str:
    cls = exc.__class__
    module = getattr(cls, "__module__", "") or ""
    return f"{module}.{cls.__name__}"


def classify_exception(exc: BaseException, fallback_code: str = INTERNAL_ERROR) -> AppError:
    """把任意异常映射为稳定 AppError。AppError 直接透传。"""
    if isinstance(exc, AppError):
        return exc

    detail = str(exc) or exc.__class__.__name__
    module_name = _exc_module_name(exc)
    exc_name = exc.__class__.__name__

    # TLS / 证书
    if exc_name in ("SSLError", "CertificateError", "SSLZeroReturnError") or module_name.startswith("ssl."):
        return comfyui_tls_failed(detail)

    # 超时
    if isinstance(exc, TimeoutError) or exc_name == "TimeoutError":
        return comfyui_timeout(detail)

    # 磁盘空间
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:  # ENOSPC
        return disk_space_low(detail)

    # 连接失败（aiohttp 按模块名识别，避免直接导入）
    if isinstance(exc, ConnectionError) or exc_name in (
        "ClientConnectorError", "ClientConnectionError", "ClientOSError",
        "ServerDisconnectedError", "ClientError",
    ) or module_name.startswith("aiohttp"):
        return comfyui_offline(detail)

    # 文件缺失
    if isinstance(exc, FileNotFoundError):
        return AppError(WORKFLOW_NOT_FOUND, f"文件不存在: {detail}", {"detail": detail}, retryable=False)

    return AppError(fallback_code, f"{exc_name}: {detail}", {"exception": exc_name}, retryable=(fallback_code in RETRYABLE_CODES))


__all__ = [
    "AppError", "classify_exception", "KNOWN_ERROR_CODES", "RETRYABLE_CODES",
    "COMFYUI_OFFLINE", "COMFYUI_AUTH_FAILED", "COMFYUI_TLS_FAILED", "COMFYUI_TIMEOUT",
    "WORKFLOW_NOT_FOUND", "WORKFLOW_NODE_MISSING", "CHECKPOINT_NOT_FOUND", "LORA_NOT_FOUND",
    "INVALID_IMAGE", "UPLOAD_TOO_LARGE", "QUEUE_FULL", "JOB_NOT_CANCELLABLE",
    "JOB_NOT_FAVORITABLE",
    "JOB_NOT_FOUND", "JOB_NOT_DELETABLE", "DISK_SPACE_LOW", "CONFIG_INVALID",
    "SECRET_STORE_FAILED", "BACKEND_RESTARTED", "UNKNOWN_SUBMISSION_STATE",
    "ENGINE_STOPPED", "ENGINE_ALREADY_RUNNING", "INVALID_REQUEST",
    "INVALID_STATE_TRANSITION", "AUDIT_FAILED", "INTERNAL_ERROR",
    "comfyui_offline", "comfyui_auth_failed", "comfyui_tls_failed", "comfyui_timeout",
    "workflow_not_found", "workflow_node_missing", "checkpoint_not_found",
    "lora_not_found", "invalid_image", "upload_too_large", "queue_full",
    "job_not_cancellable", "job_not_favoritable", "job_not_found", "job_not_deletable", "disk_space_low",
    "config_invalid", "secret_store_failed", "backend_restarted",
    "unknown_submission_state", "engine_stopped", "invalid_request",
    "invalid_state_transition", "audit_failed", "internal_error",
]

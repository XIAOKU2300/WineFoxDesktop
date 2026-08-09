# -*- coding: utf-8 -*-
"""WineFox Desktop — ComfyUI 网络/执行层共享类型（Agent 05）。

平台无关：不依赖 AstrBot、不导入 astrbot.logger，使用标准 logging。
本模块只包含纯数据类型与异常，不含任何网络逻辑（见 comfyui_transport.py /
comfyui_executor.py）。

Agent 02（core-engine）尚未交付的共享类型一律不在此定义；
需要时通过仓库根目录的 _agent_stubs.py（未提交 shim）桥接。
"""
from __future__ import annotations

import dataclasses
import enum
import logging
import pathlib
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger("winefox.comfyui.types")

# ---------------------------------------------------------------------------
# 常量（与旧 comfyui_api.py 对齐的默认值，但口径统一为显式配置）
# ---------------------------------------------------------------------------
DEFAULT_CONNECT_TIMEOUT = 10.0   # 旧插件 20s → 收紧
DEFAULT_READ_TIMEOUT = 120.0     # 旧插件 300s（生成轮询期下载）
DEFAULT_TOTAL_TIMEOUT = 600.0    # 覆盖长任务 view 下载
DEFAULT_MAX_JSON_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_IMAGE_BYTES = 48 * 1024 * 1024
DEFAULT_MAX_UPLOAD_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_IMAGE_PIXELS = 100_000_000  # Pillow decompression-bomb 防护阈值
DEFAULT_GET_RETRIES = 3
DEFAULT_GET_BACKOFF_BASE = 0.5
DEFAULT_GET_BACKOFF_MAX = 4.0
DEFAULT_USER_AGENT = "WineFoxDesktop/0.1 comfyui-client"


class AuthMode(str, enum.Enum):
    NONE = "none"
    BEARER = "bearer"
    BASIC = "basic"


class ErrorKind(str, enum.Enum):
    """结构化错误分类（对 UI / 日志友好，不暴露绝对路径）。"""

    CONNECTION = "connection"            # 连不上（连接建立前失败）
    AUTH = "auth"                        # 401/403
    TLS = "tls"                          # 证书验证失败（自签名等）
    TIMEOUT = "timeout"                  # connect/read/total 超时
    VALIDATION = "validation"            # 400 prompt_outputs_failed_validation
    MISSING_NODE = "missing_node"        # object_info 里缺工作流节点
    QUEUE_SUBMIT_UNKNOWN = "queue_submit_unknown"  # POST /prompt 状态不明
    PROMPT_LOST = "prompt_lost"          # 队列与历史中都找不到 prompt_id
    EXECUTION_ERROR = "execution_error"  # history status_str == error
    GENERATION_TIMEOUT = "generation_timeout"  # 轮询耗尽
    PAYLOAD_TOO_LARGE = "payload_too_large"    # JSON/图片超出大小上限
    IMAGE_DECODE = "image_decode"        # Pillow 解码失败 / 解压炸弹
    DOWNLOAD = "download"                # /view 下载失败
    FILE = "file"                        # 本地文件问题（不存在/超限/非法名）
    INTERNAL = "internal"


# 安全：这些目录名是相对 ComfyUI models/ 的提示，绝不返回服务器绝对路径。
MODEL_DIR_HINTS: Mapping[str, str] = {
    "lora_name": "models\\loras",
    "lora": "models\\loras",
    "vae_name": "models\\vae",
    "clip_name": "models\\text_encoders",
    "unet_name": "models\\diffusion_models",
    "ckpt_name": "models\\checkpoints",
    "control_net_name": "models\\controlnet",
    "controlnet_name": "models\\controlnet",
    "checkpoint_name": "models\\checkpoints",
}


class ComfyUIError(Exception):
    """所有 ComfyUI 层错误的基础类型。"""

    kind: ErrorKind = ErrorKind.INTERNAL
    status: Optional[int] = None
    detail: str = ""
    status_unknown: bool = False  # 写请求后连接中断，无法确认服务器是否已接收

    def __init__(
        self,
        message: str,
        *,
        kind: Optional[ErrorKind] = None,
        status: Optional[int] = None,
        detail: str = "",
        cause: Optional[BaseException] = None,
        status_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        if kind is not None:
            self.kind = kind
        self.status = status
        self.detail = detail
        self.status_unknown = status_unknown
        if cause is not None:
            self.__cause__ = cause

    @property
    def user_message(self) -> str:
        """给 UI 的安全文案（不含绝对路径、不含密钥）。"""
        return str(self)


class ComfyUIConnectionError(ComfyUIError):
    kind = ErrorKind.CONNECTION


class ComfyUIAuthError(ComfyUIError):
    kind = ErrorKind.AUTH


class ComfyUITLSError(ComfyUIError):
    kind = ErrorKind.TLS


class ComfyUITimeoutError(ComfyUIError):
    kind = ErrorKind.TIMEOUT


class ComfyUIValidationError(ComfyUIError):
    kind = ErrorKind.VALIDATION


class ComfyUIMissingNodeError(ComfyUIError):
    kind = ErrorKind.MISSING_NODE


class ComfyUIQueueSubmitUnknownError(ComfyUIError):
    kind = ErrorKind.QUEUE_SUBMIT_UNKNOWN


class ComfyUIPromptLostError(ComfyUIError):
    kind = ErrorKind.PROMPT_LOST


class ComfyUIExecutionError(ComfyUIError):
    kind = ErrorKind.EXECUTION_ERROR


class ComfyUIGenerationTimeoutError(ComfyUIError):
    kind = ErrorKind.GENERATION_TIMEOUT


class ComfyUIPayloadTooLargeError(ComfyUIError):
    kind = ErrorKind.PAYLOAD_TOO_LARGE


class ComfyUIImageDecodeError(ComfyUIError):
    kind = ErrorKind.IMAGE_DECODE


class ComfyUIDownloadError(ComfyUIError):
    kind = ErrorKind.DOWNLOAD


class ComfyUIFileError(ComfyUIError):
    kind = ErrorKind.FILE


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class ComfyUIAuth:
    """Bearer / Basic / 自定义 Header 认证配置。"""

    mode: AuthMode = AuthMode.NONE
    bearer_token: str = ""
    username: str = ""
    password: str = ""

    @classmethod
    def bearer(cls, token: str) -> "ComfyUIAuth":
        return cls(mode=AuthMode.BEARER, bearer_token=(token or "").strip())

    @classmethod
    def basic(cls, username: str, password: str) -> "ComfyUIAuth":
        return cls(mode=AuthMode.BASIC, username=username or "", password=password or "")

    @property
    def secret_values(self) -> Tuple[str, ...]:
        """用于日志脱敏的值集合。"""
        out: List[str] = []
        if self.bearer_token:
            out.append(self.bearer_token)
        if self.password:
            out.append(self.password)
        return tuple(v for v in out if len(v) >= 4)


@dataclasses.dataclass(frozen=True)
class ComfyUIServerConfig:
    """ComfyUI 服务器连接配置。

    安全默认：TLS 默认验证（verify_tls=True）。只有显式设置
    verify_tls=False 才关闭验证（transport 会记录 WARNING）。
    """

    server_address: str = "127.0.0.1:8188"
    auth: ComfyUIAuth = dataclasses.field(default_factory=ComfyUIAuth)
    extra_headers: Mapping[str, str] = dataclasses.field(default_factory=dict)
    verify_tls: bool = True
    ca_bundle: Optional[str] = None      # 自定义 CA（PEM 文件路径）
    timeout_connect: float = DEFAULT_CONNECT_TIMEOUT
    timeout_read: float = DEFAULT_READ_TIMEOUT
    timeout_total: float = DEFAULT_TOTAL_TIMEOUT
    max_json_bytes: int = DEFAULT_MAX_JSON_BYTES
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    max_image_pixels: int = DEFAULT_MAX_IMAGE_PIXELS
    get_retries: int = DEFAULT_GET_RETRIES
    get_backoff_base: float = DEFAULT_GET_BACKOFF_BASE
    get_backoff_max: float = DEFAULT_GET_BACKOFF_MAX
    user_agent: str = DEFAULT_USER_AGENT

    def redacted(self) -> "ComfyUIServerConfig":
        """返回用于日志的脱敏副本（隐藏 token/password）。"""
        return dataclasses.replace(self, auth=dataclasses.replace(self.auth, bearer_token="***", password="***"))


# ---------------------------------------------------------------------------
# 上传 / 探测 / 队列结果
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class UploadedImage:
    """/upload/image 返回的服务器侧定位信息（已防逃逸清洗）。"""

    name: str
    subfolder: str = ""
    type: str = "input"


@dataclasses.dataclass(frozen=True)
class ModelInventory:
    checkpoints: Tuple[str, ...] = ()
    loras: Tuple[str, ...] = ()
    vae: Tuple[str, ...] = ()
    controlnets: Tuple[str, ...] = ()
    upscalers: Tuple[str, ...] = ()
    unet: Tuple[str, ...] = ()
    text_encoders: Tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class QueueSnapshot:
    running: int = 0
    pending: int = 0
    running_ids: Tuple[str, ...] = ()
    pending_ids: Tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class ServerProbe:
    """probe() 的结构化结果。"""

    reachable: bool
    latency_ms: float = -1.0
    object_info: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    models: ModelInventory = dataclasses.field(default_factory=ModelInventory)
    queue: QueueSnapshot = dataclasses.field(default_factory=QueueSnapshot)
    system_stats: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    warnings: Tuple[str, ...] = ()

    @property
    def node_count(self) -> int:
        return len(self.object_info)


# ---------------------------------------------------------------------------
# 执行快照（不可变输入）
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class ExecutionSnapshot:
    """一次生成的全部输入，不可变。

    与旧插件不同：旧插件把运行时状态挂在共享的 ComfyUI 实例
    （runtime_* 系列属性），并发/复用时互相污染；这里所有输入都打包在
    一个 frozen snapshot 里，executor 不读取任何共享可变状态。
    """

    workflow: Mapping[str, Any]          # API 格式工作流 {node_id: {class_type, inputs}}
    prompt: str = ""                     # 正向提示词（写入 meta）
    negative_prompt: str = ""
    source_image: Optional[str] = None   # 本地图片路径（图生图/视频）
    source_image_bytes: Optional[bytes] = None  # 或直接给字节
    upload_subfolder: Optional[str] = None
    output_node_id: Optional[str] = None  # 优先取该节点的 images；None=首个含 images 的节点
    client_id: Optional[str] = None      # None → executor 自动生成
    resume_prompt_id: Optional[str] = None  # 已有 prompt_id 时恢复轮询
    options: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    # -- options 支持的键（全部可选）---------------------------------------
    # validate_nodes: bool = True            提交前用 object_info 校验节点存在
    # poll_attempts: int = 300               轮询上限
    # poll_interval_base: float = 0.8        基础轮询间隔（秒）
    # poll_interval_max: float = 10.0
    # queue_missing_grace_seconds: float = 30.0  提交后宽限期
    # queue_missing_confirmations: int = 3   连续 N 次队列+历史都找不到才判定丢失
    # verify_upload_image: bool = True       Pillow 预解码校验上传图片
    # decode_check: bool = True              下载后 Pillow 解码校验
    # image_type: str = "output"             /view 的 type 参数
    # meta_prefix: str = ""                  meta 键前缀（预留）
    # client_id_prefix: str = ""             client_id 前缀（调试用）

    def get_option(self, key: str, default: Any = None) -> Any:
        return (self.options or {}).get(key, default)

    def clone_with(self, **changes: Any) -> "ExecutionSnapshot":
        return dataclasses.replace(self, **changes)


@dataclasses.dataclass
class ComfyUIResult:
    """结构化执行结果（替代旧插件 (bytes, error, meta) 裸元组）。"""

    ok: bool = False
    images: List[bytes] = dataclasses.field(default_factory=list)
    prompt_id: Optional[str] = None
    error: Optional[ComfyUIError] = None
    meta: Dict[str, Any] = dataclasses.field(default_factory=dict)
    warnings: List[str] = dataclasses.field(default_factory=list)

    def as_core_result(self) -> Optional[Any]:
        """适配 Agent 02（core-engine）的通用结果类型。

        依赖 _agent_stubs.py（未提交 shim）；Agent 02 交付后自然切换。
        """
        try:  # pragma: no cover - 依赖外部 shim
            from _agent_stubs import CoreResult  # type: ignore

            return CoreResult(
                ok=self.ok,
                data=None if not self.ok else {"images": list(self.images), "prompt_id": self.prompt_id},
                error=None if self.ok else self.error.user_message,
                error_kind=None if self.ok else self.error.kind.value,
                meta=dict(self.meta),
            )
        except Exception:
            return None

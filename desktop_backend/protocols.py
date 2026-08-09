"""业务核心注入协议（Agent 07 定义的稳定接口）。

后端不 import 任何插件/引擎实现；Agent 02/03/04/05 按本文件实现真实对象并注入
``create_app(engine=..., config=..., uploads=...)`` 或 CLI ``--engine``。

实现方须知：
- 所有方法均为 async（除标注外）。
- 不得在返回的 dict 中携带绝对路径；图片一律用 ID 表达。
- 抛 ``desktop_backend.errors.AppError`` 携带业务错误；其余异常会被映射为 500。
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from .models import HistoryEntry, ImageData, JobRecord


@runtime_checkable
class CoreEngine(Protocol):
    """绘图业务核心（Agent 02 实现；封装插件 ComfyUIPlugin 能力）。"""

    async def probe_comfyui(self, payload: Optional[dict] = None) -> dict:
        """探测 ComfyUI 可用性。返回 {"ok": bool, "server": str, "latency_ms": int, "error": str|None, ...}。"""

    async def get_catalog(self) -> dict:
        """模型/工作流目录。返回 {"loras": [...], "checkpoints": [...], "workflows": [...], ...}。"""

    async def submit_job(self, payload: dict) -> JobRecord:
        """提交生成任务。payload 已由后端做基本校验（prompt 非空等）。返回初始 JobRecord。"""

    async def get_job(self, job_id: str) -> Optional[JobRecord]:
        """查询任务；不存在返回 None。"""

    async def cancel_job(self, job_id: str) -> bool:
        """取消任务；返回是否取消成功（不存在/已结束返回 False）。"""

    async def list_history(
        self,
        limit: int = 50,
        offset: int = 0,
        favorite_only: bool = False,
    ) -> list[HistoryEntry]:
        """历史记录列表，按创建时间倒序。"""

    async def get_history_image(self, history_id: str) -> Optional[ImageData]:
        """按历史 ID 取图片内容；不存在返回 None。"""

    async def set_favorite(self, history_id: str, favorite: bool) -> bool:
        """设置收藏状态；历史不存在返回 False。"""

    async def delete_history(self, history_id: str) -> bool:
        """通过 CoreEngine 删除历史（含图片与元数据）；不存在返回 False。"""

    async def diagnostics(self) -> dict:
        """诊断信息：版本、目录、ComfyUI 状态、队列、最近错误等。"""


@runtime_checkable
class ConfigStore(Protocol):
    """配置/密钥存储（Agent 03 实现）。

    - ``get_settings`` 只返回「已配置」的键（不并 schema 默认值），敏感值请以明文返回，
      脱敏由后端统一处理（见 desktop_backend.settings.mask_settings）。
    - ``patch_settings`` 负责类型校验；直接改 PATCH 密钥类键会被后端拒绝（走 /secrets）。
    """

    async def get_settings(self) -> dict: ...

    async def patch_settings(self, patch: dict) -> dict:
        """应用部分更新，返回更新后的配置（已配置视图）。非法类型抛 AppError(422)。"""

    async def put_secret(self, name: str, value: str) -> None:
        """写入/覆盖密钥；name 非法抛 AppError(422)。"""

    async def delete_secret(self, name: str) -> bool:
        """删除密钥；不存在返回 False。"""


@runtime_checkable
class UploadStore(Protocol):
    """上传文件存储（Agent 04 实现）。只暴露 ID，不暴露路径。"""

    async def save(
        self,
        upload_id: str,
        data: bytes,
        *,
        filename: str,
        content_type: str,
        kind: str,
    ) -> None:
        """保存上传内容；失败抛 AppError。"""

    async def get(self, upload_id: str) -> Optional[ImageData]:
        """按 ID 取回上传内容（可选实现，供引擎消费）。"""

    async def delete(self, upload_id: str) -> bool:
        """删除上传（可选实现）。"""


@runtime_checkable
class EngineFactory(Protocol):
    """CLI ``--engine module:factory`` 的工厂签名。"""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """返回 CoreEngine 实例（可为 async 工厂）。"""

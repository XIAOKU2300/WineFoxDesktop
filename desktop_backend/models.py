"""跨模块数据模型（Protocol 的返回类型，Agent 02/04 按此实现）。

JSON 序列化统一使用 snake_case 字段名。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class JobRecord:
    """一次生成任务。status: queued | running | done | failed | cancelled"""

    id: str
    status: str
    created_at: float
    prompt: str = ""
    updated_at: Optional[float] = None
    result: Optional[dict] = None  # 完成后的产出，如 {"imageIds": [...]}
    error: Optional[str] = None
    meta: Optional[dict] = None
    progress: Optional[float] = None
    queue_position: Optional[int] = None
    image: Optional[dict] = None
    seed: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "prompt": self.prompt,
            "result": self.result,
            "error": self.error,
            "meta": self.meta,
            "progress": self.progress,
            "queue_position": self.queue_position,
            "image": self.image,
            "seed": self.seed,
        }


@dataclass
class HistoryEntry:
    """历史记录。image_id 是历史图片的 ID（不是路径），经
    ``GET /api/v1/history/{id}/image`` 按 ID 取图。"""

    id: str
    prompt: str
    created_at: Any
    image_id: Optional[str] = None
    favorite: bool = False
    tags: list = field(default_factory=list)
    meta: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "created_at": self.created_at,
            "image_id": self.image_id,
            "favorite": self.favorite,
            "tags": list(self.tags or []),
            "meta": self.meta,
        }


@dataclass
class ImageData:
    """二进制图片内容（历史图 / 上传校验用）。"""

    data: bytes
    content_type: str = "image/png"
    filename: Optional[str] = None

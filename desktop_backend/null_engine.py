"""空引擎：未注入真实 CoreEngine 时的兜底实现（业务路由返回 503）。

CLI 未传 ``--engine`` 时使用；真实集成（Agent 09）必须注入 Agent 02 的引擎。
"""

from __future__ import annotations

from .errors import AppError


class NullEngine:
    async def probe_comfyui(self, payload=None):
        raise AppError(503, "core_engine_not_configured", "核心引擎未注入")

    async def get_catalog(self):
        raise AppError(503, "core_engine_not_configured", "核心引擎未注入")

    async def submit_job(self, payload):
        raise AppError(503, "core_engine_not_configured", "核心引擎未注入")

    async def get_job(self, job_id):
        raise AppError(503, "core_engine_not_configured", "核心引擎未注入")

    async def cancel_job(self, job_id):
        raise AppError(503, "core_engine_not_configured", "核心引擎未注入")

    async def list_history(self, limit=50, offset=0, favorite_only=False):
        raise AppError(503, "core_engine_not_configured", "核心引擎未注入")

    async def get_history_image(self, history_id):
        raise AppError(503, "core_engine_not_configured", "核心引擎未注入")

    async def set_favorite(self, history_id, favorite):
        raise AppError(503, "core_engine_not_configured", "核心引擎未注入")

    async def delete_history(self, history_id):
        raise AppError(503, "core_engine_not_configured", "核心引擎未注入")

    async def diagnostics(self):
        raise AppError(503, "core_engine_not_configured", "核心引擎未注入")


class NullConfig:
    async def get_settings(self):
        raise AppError(503, "not_configured", "配置存储未注入")

    async def patch_settings(self, patch):
        raise AppError(503, "not_configured", "配置存储未注入")

    async def put_secret(self, name, value):
        raise AppError(503, "not_configured", "配置存储未注入")

    async def delete_secret(self, name):
        raise AppError(503, "not_configured", "配置存储未注入")


class NullUploadStore:
    async def save(self, upload_id, data, *, filename, content_type, kind):
        raise AppError(503, "not_configured", "上传存储未注入")

    async def get(self, upload_id):
        return None

    async def delete(self, upload_id):
        return False

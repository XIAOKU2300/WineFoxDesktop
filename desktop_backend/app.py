"""应用装配：路由 + 安全中间件 + 错误映射 + 访问日志。

中间件顺序（列表首位为最外层）：
1. security_headers  —— 所有响应（含错误响应）统一安全头
2. error_mapper      —— AppError/HTTPException -> JSON envelope
3. access_log        —— 访问日志（bootstrap/health 不记录）
4. security_gate     —— Host/Origin/Cookie/CSRF 校验
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from aiohttp import web

from . import DEFAULT_MAX_UPLOAD_BYTES, DEFAULT_STATIC_DIR
from .errors import AppError, error_to_response, is_api_path
from .handlers import CTX_KEY, register_routes
from .security import (
    API_PREFIX,
    BOOTSTRAP_PREFIX,
    SecurityManager,
    WRITE_METHODS,
)
from .static import register_static

logger = logging.getLogger("winefox")
access_logger = logging.getLogger("winefox.access")

CSP_DEFAULT = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "media-src 'self' blob:; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'; "
    "frame-src 'none'; "
    "form-action 'self'"
)

# bootstrap 与 health 不进访问日志（health 会被 UI 高频轮询）
_ACCESS_LOG_SKIP = (BOOTSTRAP_PREFIX,)


def _skip_access_log(path: str) -> bool:
    return path.startswith(_ACCESS_LOG_SKIP) or path == "/api/v1/health"


def _apply_security_headers(resp, path: str, csp: str) -> None:
    """给任意响应（含 HTTPException）附加统一安全头。"""
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Content-Security-Policy", csp)
    if is_api_path(path):
        resp.headers.setdefault("Cache-Control", "no-store")
    else:
        resp.headers.setdefault("Cache-Control", "no-cache, no-store, must-revalidate")
    resp.headers.setdefault("Server", "WineFox")


def _make_middlewares(
    security: SecurityManager,
    csp: str,
    *,
    max_upload_bytes: int,
) -> list:
    @web.middleware
    async def security_headers(request: web.Request, handler):
        try:
            resp = await handler(request)
        except web.HTTPException as exc:
            # 3.14 起不允许把 HTTPException 当响应返回；加头后重新抛出
            _apply_security_headers(exc, request.path, csp)
            raise
        _apply_security_headers(resp, request.path, csp)
        return resp

    @web.middleware
    async def error_mapper(request: web.Request, handler):
        try:
            return await handler(request)
        except AppError as exc:
            return error_to_response(request, exc)
        except web.HTTPException as exc:
            if is_api_path(request.path):
                return error_to_response(request, exc)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("未处理异常: %s", exc)
            return error_to_response(request, exc)

    @web.middleware
    async def access_log(request: web.Request, handler):
        start = time.monotonic()
        resp = await handler(request)
        if not _skip_access_log(request.path):
            access_logger.info(
                '%s "%s %s %s" %s %.1fms',
                request.remote or "-",
                request.method,
                request.path,
                request.version,
                resp.status,
                (time.monotonic() - start) * 1000,
            )
        return resp

    @web.middleware
    async def security_gate(request: web.Request, handler):
        path = request.path
        if path.startswith(BOOTSTRAP_PREFIX) or not path.startswith(API_PREFIX):
            return await handler(request)  # 静态资源 / bootstrap 不做 API 校验
        security.check_host(request)
        security.check_origin(request, required=False)  # 存在即必须合法
        if path == "/api/v1/health":
            return await handler(request)  # health 免会话，便于 UI 探测端口
        security.check_session(request)
        if request.method in WRITE_METHODS:
            security.check_csrf(request)
        return await handler(request)

    return [security_headers, error_mapper, access_log, security_gate]


def create_app(
    *,
    engine: Any = None,
    config: Any = None,
    uploads: Any = None,
    static_dir: Optional[str] = DEFAULT_STATIC_DIR,
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    csp: Optional[str] = None,
    extra_allowed_hosts: tuple[str, ...] = (),
) -> web.Application:
    """装配后端应用。

    engine/config/uploads 为 Protocol 实现（Agent 02/03/04）；None 时业务路由返回
    503（便于仅静态/健康检查场景）。
    """
    security = SecurityManager(extra_allowed_hosts=extra_allowed_hosts)
    csp = csp or CSP_DEFAULT
    app = web.Application(
        client_max_size=max_upload_bytes + 64 * 1024,  # multipart 头部余量
        middlewares=_make_middlewares(security, csp, max_upload_bytes=max_upload_bytes),
    )
    app[CTX_KEY] = {
        "engine": engine,
        "config": config,
        "uploads": uploads,
        "security": security,
        "max_upload_bytes": int(max_upload_bytes),
        "started_at": time.monotonic(),
    }
    register_routes(app)
    register_static(app, static_dir)

    async def _on_shutdown(app_: web.Application) -> None:
        logger.info("应用关闭中…")

    app.on_shutdown.append(_on_shutdown)
    return app

"""统一错误类型与错误映射。

所有 API 错误都走 JSON envelope::

    {"ok": false, "error": {"code": "...", "message": "...", "details": ...}}

成功响应统一为 ``{"ok": true, "data": ...}``。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web

logger = logging.getLogger("winefox.errors")


class AppError(Exception):
    """业务错误：携带 HTTP status + 机器可读 code。"""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = code
        self.message = message
        self.details = details


class ValidationError(AppError):
    """请求参数校验失败（422）。"""

    def __init__(self, message: str = "请求参数校验失败", details: Any = None) -> None:
        super().__init__(422, "validation_error", message, details)


def envelope_ok(data: Any = None) -> dict:
    return {"ok": True, "data": data}


def envelope_err(status: int, code: str, message: str, details: Any = None) -> dict:
    error = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"ok": False, "error": error}


# aiohttp 原生 HTTPException -> 业务 code 映射
_HTTP_CODE_MAP = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    406: "not_acceptable",
    408: "request_timeout",
    409: "conflict",
    411: "length_required",
    412: "precondition_failed",
    413: "payload_too_large",
    414: "uri_too_long",
    415: "unsupported_media_type",
    416: "range_not_satisfiable",
    422: "validation_error",
    429: "too_many_requests",
    500: "internal_error",
    501: "not_implemented",
    502: "bad_gateway",
    503: "service_unavailable",
    504: "timeout",
}


def _json_response(body: dict, status: int) -> web.Response:
    return web.json_response(body, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False))


def error_to_response(request: web.Request, exc: Exception) -> web.Response:
    """把异常映射为 JSON envelope 响应（仅对 API/bootstrap 路径）。"""
    if isinstance(exc, AppError):
        return _json_response(envelope_err(exc.status, exc.code, exc.message, exc.details), exc.status)
    if isinstance(exc, web.HTTPException):
        code = _HTTP_CODE_MAP.get(exc.status, "http_error")
        message = getattr(exc, "reason", None) or str(exc.status)
        return _json_response(envelope_err(exc.status, code, message), exc.status)
    logger.exception("未处理异常: %s", exc)
    return _json_response(envelope_err(500, "internal_error", "服务器内部错误"), 500)


def is_api_path(path: str) -> bool:
    return path.startswith("/api/v1") or path.startswith("/bootstrap")

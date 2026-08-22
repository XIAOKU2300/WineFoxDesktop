"""静态目录服务（桌面 UI，Agent 08）。

- 目录通过参数注入，默认 ``desktop_ui``（相对进程 CWD）；
- 严格路径解析 + 包含性检查，杜绝 ``..``/编码穿越；
- 未匹配的 /api/v1 与 /bootstrap 路径返回 JSON 404 envelope。
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from urllib.parse import unquote

from aiohttp import web

from .errors import AppError, is_api_path

logger = logging.getLogger("winefox.static")

STATIC_CTX: web.AppKey = web.AppKey("winefox_static", Path)


def resolve_static_path(static_root: Path, rel: str) -> Path | None:
    """安全解析静态文件路径；越界返回 None。"""
    if not rel:
        rel = "index.html"
    if "\\" in rel:
        return None
    parts = [unquote(p) for p in rel.split("/")]
    if any(p in ("", ".", "..") for p in parts):
        # 空段（如 //）或 .. 段直接拒绝
        if any(p == ".." for p in parts):
            return None
    root = static_root.resolve()
    target = (root / Path(*parts)).resolve()
    if target != root and root not in target.parents:
        return None
    if not target.is_file():
        return None
    return target


def make_static_handler(static_root: Path):
    async def static_handler(request: web.Request) -> web.Response:
        if is_api_path(request.path):
            raise AppError(404, "not_found", "接口不存在")
        rel = request.match_info.get("rel", "") or ""
        target = resolve_static_path(static_root, rel)
        if target is None:
            raise web.HTTPNotFound(text="Not Found")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        # 协商缓存：资源文件名不带哈希，升级后内容会变化，因此统一用
        # no-cache + ETag —— WebView 切换视图时命中 304 省去重复传输；
        # index.html 每次启动重新校验，不存在旧版本滞留问题。
        try:
            stat = target.stat()
            etag = f'W/"{int(stat.st_mtime)}-{stat.st_size}"'
        except OSError:
            etag = ""
        headers = {
            "Content-Type": ctype,
            "Cache-Control": "no-cache",
        }
        if etag:
            headers["ETag"] = etag
            if request.headers.get("If-None-Match") == etag:
                return web.Response(status=304, headers=headers)
        return web.FileResponse(target, headers=headers)

    return static_handler


def register_static(app: web.Application, static_dir: str | Path | None) -> None:
    if static_dir is None:
        # 仍注册兜底 handler，保证 /api 404 envelope 行为一致
        async def none_handler(request: web.Request) -> web.Response:
            if is_api_path(request.path):
                raise AppError(404, "not_found", "接口不存在")
            raise web.HTTPNotFound(text="Not Found")

        app.router.add_get("/", none_handler)
        app.router.add_get("/{rel:.*}", none_handler)
        return

    root = Path(static_dir).resolve()
    if not root.is_dir():
        logger.warning("静态目录不存在: %s（仅提供 API）", root)
    app[STATIC_CTX] = root
    handler = make_static_handler(root)
    # 负向断言：/api/v1 与 /bootstrap 前缀不落入静态兜底，
    # 使 aiohttp 路由能为 API 路径正确抛出 405/404
    app.router.add_get("/", handler)
    app.router.add_get(r"/{rel:(?!api/v1/|bootstrap/).*}", handler)

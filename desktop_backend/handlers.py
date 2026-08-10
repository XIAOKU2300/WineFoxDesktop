"""冻结 API 的请求处理。

冻结接口：
    GET    /api/v1/health
    GET    /api/v1/settings          PATCH  /api/v1/settings
    PUT    /api/v1/secrets/{name}    DELETE /api/v1/secrets/{name}
    POST   /api/v1/comfyui/probe
    GET    /api/v1/catalog
    POST   /api/v1/uploads
    POST   /api/v1/jobs              GET /api/v1/jobs/{id}  POST /api/v1/jobs/{id}/cancel
    GET    /api/v1/history           GET /api/v1/history/{id}/image
    POST   /api/v1/history/{id}/favorite   DELETE /api/v1/history/{id}/favorite
    DELETE /api/v1/history/{id}
    GET    /api/v1/diagnostics
"""

from __future__ import annotations

import json
import logging
import re
import time
import io
from typing import Any

from aiohttp import web

from . import PROTOCOL_VERSION, VERSION
from .errors import AppError, ValidationError, envelope_ok
from .models import HistoryEntry, ImageData, JobRecord
from .security import SESSION_COOKIE_NAME
from .settings import mask_settings, validate_settings_patch
from .uploads import parse_upload

logger = logging.getLogger("winefox.api")

_SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

# 内存池：进程内共享的服务依赖
CTX_KEY: web.AppKey[dict] = web.AppKey("winefox", dict)


def _ctx(request: web.Request) -> dict:
    return request.app[CTX_KEY]


async def _json_body(request: web.Request, *, required: bool = True) -> Any:
    """读取 JSON body 并做基本校验。"""
    text = (await request.text()).strip()
    if not text:
        if required:
            raise ValidationError("请求体不能为空")
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AppError(400, "invalid_json", "JSON 解析失败") from exc


def _json(data: Any = None, status: int = 200) -> web.Response:
    return web.json_response(
        envelope_ok(data),
        status=status,
        dumps=lambda o: json.dumps(o, ensure_ascii=False),
    )


# ---------------- bootstrap ----------------

async def handle_bootstrap(request: web.Request) -> web.Response:
    """GET /bootstrap/{token}：一次性换会话 Cookie。"""
    security = _ctx(request)["security"]
    token = request.match_info["token"]
    session = security.consume_bootstrap(token)
    if session is None:
        raise AppError(404, "bootstrap_not_found", "bootstrap 令牌无效或已使用")
    wants_html = "text/html" in request.headers.get("Accept", "")
    resp = web.Response(status=302, headers={"Location": "/"}) if wants_html else _json({"session": "established"})
    resp.set_cookie(
        SESSION_COOKIE_NAME,
        session,
        httponly=True,
        samesite="Strict",
        path="/",
    )
    logger.info("bootstrap 成功，会话已建立")
    return resp


# ---------------- health / settings / secrets ----------------

async def handle_health(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    return _json(
        {
            "status": "ok",
            "version": VERSION,
            "protocol": PROTOCOL_VERSION,
            "uptime_seconds": round(time.monotonic() - ctx["started_at"], 3),
        }
    )


async def handle_get_settings(request: web.Request) -> web.Response:
    config = _ctx(request).get("config")
    if config is None:
        raise AppError(503, "not_configured", "配置存储未注入")
    settings = await config.get_settings()
    return _json(mask_settings(settings))


async def handle_patch_settings(request: web.Request) -> web.Response:
    config = _ctx(request).get("config")
    if config is None:
        raise AppError(503, "not_configured", "配置存储未注入")
    patch = validate_settings_patch(await _json_body(request))
    updated = await config.patch_settings(patch)
    return _json(mask_settings(updated))


async def handle_put_secret(request: web.Request) -> web.Response:
    config = _ctx(request).get("config")
    if config is None:
        raise AppError(503, "not_configured", "配置存储未注入")
    name = request.match_info["name"]
    if not _SECRET_NAME_RE.match(name):
        raise ValidationError("密钥名称仅允许字母/数字/._-，长度 1-128")
    body = await _json_body(request)
    if not isinstance(body, dict) or "value" not in body or not isinstance(body["value"], str):
        raise ValidationError('密钥请求体需要 {"value": "..."}')
    await config.put_secret(name, body["value"])
    return _json({"name": name, "stored": True})


async def handle_delete_secret(request: web.Request) -> web.Response:
    config = _ctx(request).get("config")
    if config is None:
        raise AppError(503, "not_configured", "配置存储未注入")
    name = request.match_info["name"]
    if not _SECRET_NAME_RE.match(name):
        raise ValidationError("密钥名称仅允许字母/数字/._-，长度 1-128")
    deleted = await config.delete_secret(name)
    if not deleted:
        raise AppError(404, "secret_not_found", f"密钥不存在: {name}")
    return _json({"name": name, "deleted": True})


async def handle_get_secrets(request: web.Request) -> web.Response:
    config = _ctx(request).get("config")
    if config is None or not hasattr(config, "secret_statuses"):
        return _json({"items": []})
    return _json(await config.secret_statuses())


# ---------------- comfyui / catalog ----------------

async def handle_probe(request: web.Request) -> web.Response:
    engine = _ctx(request).get("engine")
    if engine is None:
        raise AppError(503, "core_engine_not_configured", "核心引擎未注入")
    payload: dict = {}
    body = await _json_body(request, required=False)
    if body is not None:
        if not isinstance(body, dict):
            raise ValidationError("probe 请求体需要 JSON 对象")
        payload = body
    result = await engine.probe_comfyui(payload)
    return _json(result)


async def handle_catalog(request: web.Request) -> web.Response:
    engine = _ctx(request).get("engine")
    if engine is None:
        raise AppError(503, "core_engine_not_configured", "核心引擎未注入")
    return _json(await engine.get_catalog())


# ---------------- uploads ----------------

async def handle_upload(request: web.Request) -> web.Response:
    uploads = _ctx(request).get("uploads")
    if uploads is None:
        raise AppError(503, "not_configured", "上传存储未注入")
    max_bytes = _ctx(request)["max_upload_bytes"]
    parsed = await parse_upload(request, max_bytes)
    data = parsed.pop("data")
    try:
        await uploads.save(
            parsed["uploadId"],
            data,
            filename=parsed["filename"],
            content_type=parsed["contentType"],
            kind=parsed["kind"],
        )
    except AppError:
        raise
    except Exception as exc:
        logger.exception("上传保存失败")
        raise AppError(500, "upload_store_error", "上传保存失败") from exc
    parsed["name"] = parsed["filename"]
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            parsed["width"], parsed["height"] = image.size
    except Exception:
        pass
    # 只返回 ID 和本地 API URL，绝不返回绝对路径
    return _json(parsed, status=201)


async def handle_get_upload(request: web.Request) -> web.Response:
    uploads = _ctx(request).get("uploads")
    if uploads is None:
        raise AppError(503, "not_configured", "上传存储未注入")
    image = await uploads.get(request.match_info["id"])
    if image is None:
        raise AppError(404, "upload_not_found", "上传不存在或已失效")
    return web.Response(body=image.data, content_type=image.content_type or "application/octet-stream")


# ---------------- jobs ----------------

def _validate_job_payload(body: Any) -> dict:
    if not isinstance(body, dict):
        raise ValidationError("任务请求体需要 JSON 对象")
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValidationError("prompt 必须是非空字符串")
    if "upload_ids" in body and body["upload_ids"] is not None:
        ids = body["upload_ids"]
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise ValidationError("upload_ids 需要字符串数组")
    return body


async def handle_submit_job(request: web.Request) -> web.Response:
    engine = _ctx(request).get("engine")
    if engine is None:
        raise AppError(503, "core_engine_not_configured", "核心引擎未注入")
    payload = _validate_job_payload(await _json_body(request))
    job = await engine.submit_job(payload)
    if not isinstance(job, JobRecord):
        raise AppError(500, "invalid_engine_result", "引擎返回了无效的任务记录")
    return _json(job.to_dict(), status=202)


async def handle_get_job(request: web.Request) -> web.Response:
    engine = _ctx(request).get("engine")
    if engine is None:
        raise AppError(503, "core_engine_not_configured", "核心引擎未注入")
    job = await engine.get_job(request.match_info["id"])
    if job is None:
        raise AppError(404, "job_not_found", "任务不存在")
    return _json(job.to_dict())


async def handle_cancel_job(request: web.Request) -> web.Response:
    engine = _ctx(request).get("engine")
    if engine is None:
        raise AppError(503, "core_engine_not_configured", "核心引擎未注入")
    ok = await engine.cancel_job(request.match_info["id"])
    if not ok:
        raise AppError(404, "job_not_found", "任务不存在或已结束")
    return _json({"id": request.match_info["id"], "cancelled": True})


# ---------------- history ----------------

def _history_query(request: web.Request) -> tuple[int, int, bool]:
    def _int_param(name: str, default: int, lo: int, hi: int) -> int:
        raw = request.query.get(name)
        if raw is None or raw == "":
            return default
        try:
            val = int(raw)
        except ValueError:
            raise ValidationError(f"参数 {name} 需要整数")
        return max(lo, min(val, hi))

    limit = _int_param("limit", 50, 1, 500)
    offset = _int_param("offset", 0, 0, 10_000_000)
    favorite = request.query.get("favorite", "").lower() in {"1", "true", "yes"}
    return limit, offset, favorite


async def handle_list_history(request: web.Request) -> web.Response:
    engine = _ctx(request).get("engine")
    if engine is None:
        raise AppError(503, "core_engine_not_configured", "核心引擎未注入")
    limit, offset, favorite = _history_query(request)
    if hasattr(engine, "list_history_page"):
        page = await engine.list_history_page(
            limit=limit,
            offset=offset,
            favorite_only=favorite,
            cursor=request.query.get("cursor"),
            q=request.query.get("q"),
            status=request.query.get("status"),
            trash=request.query.get("trash"),
        )
        page.pop("_rows", None)
        return _json(page)
    items = await engine.list_history(limit=limit, offset=offset, favorite_only=favorite)
    return _json([it.to_dict() if isinstance(it, HistoryEntry) else it for it in items])


async def handle_history_image(request: web.Request) -> web.Response:
    engine = _ctx(request).get("engine")
    if engine is None:
        raise AppError(503, "core_engine_not_configured", "核心引擎未注入")
    img = await engine.get_history_image(request.match_info["id"])
    if img is None:
        raise AppError(404, "history_not_found", "历史记录不存在")
    if not isinstance(img, ImageData):
        raise AppError(500, "invalid_engine_result", "引擎返回了无效的图片数据")
    return web.Response(body=img.data, content_type=img.content_type or "image/png")


async def handle_set_favorite(request: web.Request) -> web.Response:
    engine = _ctx(request).get("engine")
    if engine is None:
        raise AppError(503, "core_engine_not_configured", "核心引擎未注入")
    hid = request.match_info["id"]
    ok = await engine.set_favorite(hid, True)
    if not ok:
        raise AppError(404, "history_not_found", "历史记录不存在")
    return _json({"id": hid, "favorite": True})


async def handle_unset_favorite(request: web.Request) -> web.Response:
    engine = _ctx(request).get("engine")
    if engine is None:
        raise AppError(503, "core_engine_not_configured", "核心引擎未注入")
    hid = request.match_info["id"]
    ok = await engine.set_favorite(hid, False)
    if not ok:
        raise AppError(404, "history_not_found", "历史记录不存在")
    return _json({"id": hid, "favorite": False})


async def handle_delete_history(request: web.Request) -> web.Response:
    engine = _ctx(request).get("engine")
    if engine is None:
        raise AppError(503, "core_engine_not_configured", "核心引擎未注入")
    hid = request.match_info["id"]
    ok = await engine.delete_history(hid)  # 删除必须走 CoreEngine
    if not ok:
        raise AppError(404, "history_not_found", "历史记录不存在")
    return _json({"id": hid, "deleted": True})


async def handle_restore_history(request: web.Request) -> web.Response:
    engine = _ctx(request).get("engine")
    if engine is None or not hasattr(engine, "restore_history"):
        raise AppError(501, "not_implemented", "当前引擎不支持恢复历史")
    hid = request.match_info["id"]
    if not await engine.restore_history(hid):
        raise AppError(404, "history_not_found", "历史记录不存在")
    return _json({"id": hid, "restored": True})


async def handle_purge_history(request: web.Request) -> web.Response:
    engine = _ctx(request).get("engine")
    if engine is None or not hasattr(engine, "purge_history"):
        raise AppError(501, "not_implemented", "当前引擎不支持彻底删除历史")
    hid = request.match_info["id"]
    if not await engine.purge_history(hid):
        raise AppError(404, "history_not_found", "历史记录不存在")
    return _json({"id": hid, "purged": True})


async def handle_categories(request: web.Request) -> web.Response:
    engine = _ctx(request).get("engine")
    if engine is None or not hasattr(engine, "categories"):
        return _json({"items": []})
    return _json(await engine.categories())


async def handle_favorites(request: web.Request) -> web.Response:
    engine = _ctx(request).get("engine")
    if engine is None or not hasattr(engine, "list_favorites"):
        return _json({"items": [], "total": 0})
    return _json(await engine.list_favorites(request.query))


async def handle_create_favorite(request: web.Request) -> web.Response:
    engine = _ctx(request).get("engine")
    if engine is None or not hasattr(engine, "create_favorite"):
        raise AppError(501, "not_implemented", "当前引擎不支持提示词收藏")
    body = await _json_body(request)
    if not isinstance(body, dict):
        raise ValidationError("收藏内容需要 JSON 对象")
    return _json(await engine.create_favorite(body), status=201)


async def handle_update_favorite(request: web.Request) -> web.Response:
    engine = _ctx(request).get("engine")
    if engine is None or not hasattr(engine, "update_favorite"):
        raise AppError(501, "not_implemented", "当前引擎不支持编辑收藏")
    body = await _json_body(request)
    if not isinstance(body, dict):
        raise ValidationError("收藏内容需要 JSON 对象")
    if not await engine.update_favorite(request.match_info["id"], body):
        raise AppError(404, "favorite_not_found", "收藏不存在")
    return _json({"id": request.match_info["id"], "updated": True})


async def handle_delete_favorite(request: web.Request) -> web.Response:
    engine = _ctx(request).get("engine")
    if engine is None or not hasattr(engine, "delete_favorite"):
        raise AppError(501, "not_implemented", "当前引擎不支持删除收藏")
    if not await engine.delete_favorite(request.match_info["id"]):
        raise AppError(404, "favorite_not_found", "收藏不存在")
    return _json({"id": request.match_info["id"], "deleted": True})


# ---------------- diagnostics ----------------

def _normalize_diagnostics_contract(result: Any) -> dict[str, Any]:
    """Keep the public diagnostics payload stable across engine adapters."""
    if not isinstance(result, dict):
        raise AppError(500, "invalid_engine_result", "诊断引擎返回了无效数据")
    payload = dict(result)
    version = str(payload.get("version") or VERSION)

    app = payload.get("app") if isinstance(payload.get("app"), dict) else {}
    payload["app"] = {
        **app,
        "name": str(app.get("name") or "WineFox Desktop"),
        "version": str(app.get("version") or version),
        "build": str(app.get("build") or "desktop"),
    }
    plugin = payload.get("plugin") if isinstance(payload.get("plugin"), dict) else {}
    payload["plugin"] = {
        **plugin,
        "name": str(plugin.get("name") or "astrbot_plugin_comfyui_pro_UltraPlusMax"),
        "version": str(plugin.get("version") or version),
    }

    comfy = payload.get("comfyui") if isinstance(payload.get("comfyui"), dict) else {}
    connected = bool(comfy.get("connected", comfy.get("online", comfy.get("status") == "ok")))
    payload["comfyui"] = {
        "connected": connected,
        "status": "ok" if connected else str(comfy.get("status") or "fail"),
        "url": str(comfy.get("url") or comfy.get("server") or ""),
        "version": str(comfy.get("version") or ""),
        "latency_ms": comfy.get("latency_ms"),
        "queue": comfy.get("queue") if isinstance(comfy.get("queue"), dict) else {},
        "error": comfy.get("error"),
    }

    nodes = payload.get("nodes")
    if isinstance(nodes, list):
        nodes = {"total": len(nodes), "items": nodes, "missing": []}
    elif not isinstance(nodes, dict):
        nodes = {}
    missing = nodes.get("missing")
    if not isinstance(missing, list):
        missing = list(payload.get("missing_nodes") or [])
    payload["nodes"] = {
        **nodes,
        "total": nodes.get("total"),
        "missing": missing,
        "status": str(nodes.get("status") or ("warn" if missing else "ok")),
        "detail": str(nodes.get("detail") or ""),
    }

    catalog = payload.get("catalog") if isinstance(payload.get("catalog"), dict) else {}
    payload["catalog"] = {
        **catalog,
        "workflows": int(catalog.get("workflows") or 0),
        "valid_workflows": int(catalog.get("valid_workflows") or 0),
        "compatible_workflows": catalog.get("compatible_workflows"),
        "checkpoints": int(catalog.get("checkpoints") or 0),
        "loras": int(catalog.get("loras") or 0),
        "lora_profiles": int(catalog.get("lora_profiles") or 0),
        "status": str(catalog.get("status") or "unknown"),
    }

    health = payload.get("config_health")
    if not isinstance(health, list):
        health = payload.get("checks") if isinstance(payload.get("checks"), list) else []
    payload["config_health"] = [item for item in health if isinstance(item, dict)]
    failures = payload.get("failures")
    if not isinstance(failures, list):
        failures = payload.get("errors") if isinstance(payload.get("errors"), list) else []
    payload["failures"] = [item for item in failures if isinstance(item, dict)]
    log_tail = payload.get("log_tail") if isinstance(payload.get("log_tail"), dict) else {}
    lines = log_tail.get("lines") if isinstance(log_tail.get("lines"), list) else []
    payload["log_tail"] = {
        "available": bool(log_tail.get("available", lines)),
        "lines": [str(line) for line in lines],
        "truncated": bool(log_tail.get("truncated")),
        "detail": str(log_tail.get("detail") or ("暂无运行日志" if not lines else "")),
    }
    return payload


async def handle_diagnostics(request: web.Request) -> web.Response:
    engine = _ctx(request).get("engine")
    if engine is None:
        raise AppError(503, "core_engine_not_configured", "核心引擎未注入")
    result = _normalize_diagnostics_contract(await engine.diagnostics())
    return _json(result)


async def handle_diagnostics_export(request: web.Request) -> web.Response:
    engine = _ctx(request).get("engine")
    if engine is None:
        raise AppError(503, "core_engine_not_configured", "核心引擎未注入")
    result = _normalize_diagnostics_contract(await engine.diagnostics())
    return _json({
        "filename": "winefox-diagnostics.json",
        "content": json.dumps(result, ensure_ascii=False, indent=2),
    })


# ---------------- 路由表 ----------------

def register_routes(app: web.Application) -> None:
    app.router.add_get("/bootstrap/{token}", handle_bootstrap)
    app.router.add_get("/api/v1/health", handle_health)
    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/api/v1/settings", handle_get_settings)
    app.router.add_patch("/api/v1/settings", handle_patch_settings)
    app.router.add_put("/api/v1/settings", handle_patch_settings)
    app.router.add_get("/api/v1/secrets", handle_get_secrets)
    app.router.add_put("/api/v1/secrets/{name}", handle_put_secret)
    app.router.add_delete("/api/v1/secrets/{name}", handle_delete_secret)
    app.router.add_post("/api/v1/comfyui/probe", handle_probe)
    app.router.add_get("/api/v1/comfyui", handle_probe)
    app.router.add_post("/api/v1/probe", handle_probe)
    app.router.add_get("/api/v1/catalog", handle_catalog)
    app.router.add_get("/api/v1/models", handle_catalog)
    app.router.add_post("/api/v1/uploads", handle_upload)
    app.router.add_get("/api/v1/uploads/{id}", handle_get_upload)
    app.router.add_post("/api/v1/jobs", handle_submit_job)
    app.router.add_get("/api/v1/jobs/{id}", handle_get_job)
    app.router.add_post("/api/v1/jobs/{id}/cancel", handle_cancel_job)
    app.router.add_get("/api/v1/history", handle_list_history)
    app.router.add_get("/api/v1/history/{id}/image", handle_history_image)
    app.router.add_post("/api/v1/history/{id}/favorite", handle_set_favorite)
    app.router.add_delete("/api/v1/history/{id}/favorite", handle_unset_favorite)
    app.router.add_delete("/api/v1/history/{id}", handle_delete_history)
    app.router.add_post("/api/v1/history/{id}/unfavorite", handle_unset_favorite)
    app.router.add_post("/api/v1/history/{id}/trash", handle_delete_history)
    app.router.add_post("/api/v1/history/{id}/restore", handle_restore_history)
    app.router.add_post("/api/v1/history/{id}/purge", handle_purge_history)
    app.router.add_get("/api/v1/categories", handle_categories)
    app.router.add_get("/api/v1/favorites", handle_favorites)
    app.router.add_post("/api/v1/favorites", handle_create_favorite)
    app.router.add_put("/api/v1/favorites/{id}", handle_update_favorite)
    app.router.add_delete("/api/v1/favorites/{id}", handle_delete_favorite)
    app.router.add_get("/api/v1/diagnostics", handle_diagnostics)
    app.router.add_post("/api/v1/diagnostics/export", handle_diagnostics_export)

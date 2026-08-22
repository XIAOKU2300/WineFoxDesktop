"""环境中心（ComfyUI setup）的冻结 API。

冻结接口（docs/desktop_setup_migration.md 第 5 节）：
    GET    /api/v1/setup/summary           环境总览（根、节点、模型、Bridge、Python、状态码）
    GET    /api/v1/setup/capabilities      能力/可安装项列表（manifest 驱动）
    POST   /api/v1/setup/scans             发起（或复用缓存）只读扫描
    POST   /api/v1/setup/plans             按 profile/custom/repair/retry 生成安装计划
    POST   /api/v1/setup/jobs              启动执行 job
    GET    /api/v1/setup/jobs              列出最近 job（便于 UI 重连）
    GET    /api/v1/setup/jobs/{id}         job 状态
    GET    /api/v1/setup/jobs/{id}/events  SSE 事件流（退化 1s 轮询 + 指数退避）
    POST   /api/v1/setup/jobs/{id}/cancel  取消
    POST   /api/v1/setup/jobs/{id}/retry   重试失败步骤
    POST   /api/v1/setup/jobs/{id}/rollback 回滚（恢复被覆盖/删除的文件）
    POST   /api/v1/setup/reprobe           强制重探（含 Bridge、catalog、LoRA profiles 刷新）
    GET    /api/v1/setup/remote-kit        远程部署信息（仅 configured，不含凭据值）

复用现有 Local Session / Host / Origin / CSRF / CSP 安全中间件；绝不注册到
desktop_room 房间网关。所有响应均不泄露绝对路径或 token。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from .errors import AppError, ValidationError, envelope_ok
from .handlers import _ctx, _json_body

logger = logging.getLogger("winefox.setup")

# SSE 在 handler 内已开始发送响应头，之后安全中间件无法再补头，故在此显式附加。
_SSE_HEADERS = {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-store",
    "X-Accel-Buffering": "no",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Server": "WineFox",
}

# 事件流的终止事件：收到即结束 SSE 流。
_TERMINAL_EVENTS = {"job_finished", "job_cancelled", "rollback_finished"}
_SSE_KEEPALIVE = 15.0


def _setup(request: web.Request) -> Any:
    manager = _ctx(request).get("setup")
    if manager is None:
        raise AppError(503, "setup_not_configured", "环境中心未注入")
    return manager


def _json(data: Any = None, status: int = 200) -> web.Response:
    return web.json_response(
        envelope_ok(data),
        status=status,
        dumps=lambda o: json.dumps(o, ensure_ascii=False),
    )


# ---------------- summary / capabilities ----------------

async def handle_setup_summary(request: web.Request) -> web.Response:
    manager = _setup(request)
    return _json(await manager.summary())


async def handle_setup_capabilities(request: web.Request) -> web.Response:
    manager = _setup(request)
    manifest = manager.manifest
    capabilities: dict[str, Any] = {
        "tiers": {
            name: [{"step": s.step, "group": s.group} for s in steps]
            for name, steps in manifest.tiers.items()
        },
        "profiles": list(manifest.tiers.keys()),
        "model_groups": {
            group: [
                {
                    "id": m.id,
                    "name": m.name,
                    "kind": m.kind,
                    "dest": m.dest,
                    "min_gb": m.min_gb,
                }
                for m in manifest.models.values()
                if m.group == group
            ]
            for group in manifest.model_groups
        },
        "source_hosts": sorted({s.host for m in manifest.models.values() for s in m.sources}),
        "node_groups": {
            name: {"nodes": list(g.nodes), "pip_packages": list(g.pip_packages)}
            for name, g in manifest.node_groups.items()
        },
        "manual_files": [
            {"name": mf.name, "dest": mf.dest, "note": mf.note} for mf in manifest.manual_files
        ],
        "python": {
            "min_version": list(manifest.python.min_version),
            "probe_requires_pip": manifest.python.probe_requires_pip,
        },
        "bridge": {"version": manifest.bridge.version},
        "allowed_hosts": list(manifest.bridge.allowed_hosts),
    }
    return _json(capabilities)


# ---------------- scans / plans ----------------

async def handle_setup_scan(request: web.Request) -> web.Response:
    manager = _setup(request)
    body = await _json_body(request, required=False)
    root: Path | None = None
    force = False
    if isinstance(body, dict):
        raw_root = body.get("root")
        if raw_root:
            root = Path(str(raw_root))
        force = bool(body.get("force", False))
    return _json(await manager.scan(root=root, force=force))


async def handle_setup_plan(request: web.Request) -> web.Response:
    manager = _setup(request)
    body = await _json_body(request)
    if not isinstance(body, dict):
        raise ValidationError("plan 请求体需要 JSON 对象")
    only_missing = bool(body.get("only_missing", True))
    custom: list[tuple[str, str | None]] | None = None
    raw_custom = body.get("custom")
    if raw_custom is not None:
        if not isinstance(raw_custom, list):
            raise ValidationError("custom 需要数组")
        custom = []
        for step in raw_custom:
            if not isinstance(step, dict):
                raise ValidationError("custom 的每一步需要对象")
            kind = str(step.get("step"))
            if kind not in ("nodes", "models", "bridge"):
                raise ValidationError(f"未知步骤类型: {kind}")
            custom.append((kind, step.get("group")))
    profile = str(body["profile"]) if body.get("profile") else None
    if profile and profile not in manager.manifest.tiers:
        raise ValidationError(f"未知 profile: {profile}")
    plan = await manager.plan(
        profile=profile,
        custom=custom,
        repair=bool(body.get("repair", False)),
        retry_job_id=str(body["retry_of"]) if body.get("retry_of") else None,
        only_missing=only_missing,
    )
    return _json(plan)


# ---------------- jobs ----------------

async def handle_setup_start_job(request: web.Request) -> web.Response:
    manager = _setup(request)
    body = await _json_body(request)
    if not isinstance(body, dict):
        raise ValidationError("job 请求体需要 JSON 对象")
    root = Path(str(body["root"])) if body.get("root") else None
    plan_dict = body.get("plan")
    if not isinstance(plan_dict, dict):
        raise ValidationError("缺少 plan 对象")
    return _json(await manager.start_job(root=root, plan_dict=plan_dict), status=202)


async def handle_setup_list_jobs(request: web.Request) -> web.Response:
    manager = _setup(request)
    jobs = [
        {
            "id": job.id,
            "profile": job.profile,
            "status": job.status,
            "error_code": job.error_code,
            "error_message": job.error_message,
            "steps": [s.to_dict() for s in job.steps],
        }
        for job in manager.jobs()
    ]
    return _json({"items": jobs})


async def handle_setup_get_job(request: web.Request) -> web.Response:
    manager = _setup(request)
    job = manager.get_job(request.match_info["id"])
    if job is None:
        raise AppError(404, "setup_job_not_found", "任务不存在")
    return _json(_job_summary(job))


def _job_summary(job: Any) -> dict[str, Any]:
    return {
        "id": job.id,
        "root": None,
        "profile": job.profile,
        "status": job.status,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "steps": [s.to_dict() for s in job.steps],
    }


async def handle_setup_cancel_job(request: web.Request) -> web.Response:
    manager = _setup(request)
    job_id = request.match_info["id"]
    ok = await manager.cancel_job(job_id)
    if not ok:
        job = manager.get_job(job_id)
        if job is None:
            raise AppError(404, "setup_job_not_found", "任务不存在")
        raise AppError(409, "setup_job_busy", "任务已结束，无法取消")
    return _json({"id": job_id, "cancelled": True})


async def handle_setup_retry_job(request: web.Request) -> web.Response:
    manager = _setup(request)
    result = await manager.retry_job(request.match_info["id"])
    return _json(result, status=202)


async def handle_setup_rollback_job(request: web.Request) -> web.Response:
    manager = _setup(request)
    job_id = request.match_info["id"]
    ok = await manager.rollback_job(job_id)
    if not ok:
        raise AppError(409, "setup_rollback_failed", "回滚失败，请检查文件权限")
    return _json({"id": job_id, "rolled_back": True})


async def handle_setup_job_events(request: web.Request) -> web.Response:
    manager = _setup(request)
    job = manager.get_job(request.match_info["id"])
    if job is None:
        raise AppError(404, "setup_job_not_found", "任务不存在")
    resp = web.StreamResponse(status=200, headers=dict(_SSE_HEADERS))
    await resp.prepare(request)
    queue = job.subscribe()
    try:
        async for event in _iter_events(job, queue):
            await resp.write(("data: %s\n\n" % json.dumps(event, ensure_ascii=False)).encode("utf-8"))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - keep the stream from crashing the connection
        logger.warning("SSE 流异常: %s", exc)
    finally:
        job.unsubscribe(queue)
        await resp.write_eof()
    return resp


async def _iter_events(job: Any, queue: asyncio.Queue):
    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=_SSE_KEEPALIVE)
        except asyncio.TimeoutError:
            if job.status in ("success", "failed", "cancelled", "rolled_back"):
                break  # 已结束且队列清空，安全收尾
            yield {"type": "keepalive", "at": None}
            continue
        yield event
        if event.get("type") in _TERMINAL_EVENTS:
            break


# ---------------- reprobe / remote ----------------

async def handle_setup_reprobe(request: web.Request) -> web.Response:
    manager = _setup(request)
    engine = _ctx(request).get("engine")
    if engine is None:
        raise AppError(503, "core_engine_not_configured", "核心引擎未注入")
    return _json(await manager.reprobe(engine))


async def handle_setup_remote_kit(request: web.Request) -> web.Response:
    manager = _setup(request)
    return _json(await manager.remote_kit())


# ---------------- 镜像测速 / 优选 ----------------

async def handle_setup_mirrors_probe(request: web.Request) -> web.Response:
    """POST /api/v1/setup/mirrors/probe：实测各下载镜像的时延与吞吐。"""
    manager = _setup(request)
    return _json(await manager.probe_mirrors())


async def handle_setup_mirrors_preference(request: web.Request) -> web.Response:
    manager = _setup(request)
    if request.method == "GET":
        return _json({"preferred_host": manager.preferred_host})
    body = await _json_body(request, required=False)
    host = str((body or {}).get("preferred_host") or "").strip()
    manager.set_preferred_host(host)  # 空串表示恢复自动顺序
    return _json({"preferred_host": manager.preferred_host})


# ---------------- 路由表 ----------------

def register_setup_routes(app: web.Application) -> None:
    app.router.add_get("/api/v1/setup/summary", handle_setup_summary)
    app.router.add_get("/api/v1/setup/capabilities", handle_setup_capabilities)
    app.router.add_post("/api/v1/setup/scans", handle_setup_scan)
    app.router.add_post("/api/v1/setup/plans", handle_setup_plan)
    app.router.add_post("/api/v1/setup/jobs", handle_setup_start_job)
    app.router.add_get("/api/v1/setup/jobs", handle_setup_list_jobs)
    app.router.add_get("/api/v1/setup/jobs/{id}", handle_setup_get_job)
    app.router.add_get("/api/v1/setup/jobs/{id}/events", handle_setup_job_events)
    app.router.add_post("/api/v1/setup/jobs/{id}/cancel", handle_setup_cancel_job)
    app.router.add_post("/api/v1/setup/jobs/{id}/retry", handle_setup_retry_job)
    app.router.add_post("/api/v1/setup/jobs/{id}/rollback", handle_setup_rollback_job)
    app.router.add_post("/api/v1/setup/reprobe", handle_setup_reprobe)
    app.router.add_get("/api/v1/setup/remote-kit", handle_setup_remote_kit)
    app.router.add_post("/api/v1/setup/mirrors/probe", handle_setup_mirrors_probe)
    app.router.add_get("/api/v1/setup/mirrors/preference", handle_setup_mirrors_preference)
    app.router.add_put("/api/v1/setup/mirrors/preference", handle_setup_mirrors_preference)

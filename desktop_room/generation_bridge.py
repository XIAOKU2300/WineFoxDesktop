"""房间共享生图：通过房主 WineFox/ComfyUI 执行，访客无需安装任何东西。

设计：
- ``GenerationBackend`` 是鸭子类型接口，桥接房主桌面引擎；测试注入 FakeEngine。
- ``GenerationBridge`` 把房间内的生成请求翻译成引擎负载，跟踪引擎任务状态，
  轮询映射为房间任务状态，完成后把图片落库为附件并广播 job.* 事件与
  message.created（结果消息，type=generation_result，挂附件授权 URL）。
- 队列限制：perMemberQueueLimit / roomQueueLimit（读自房间设置）。
- 幂等：clientRequestId 格式 room:<public>:member:<id>:<uuid>，重复提交返回已有任务。
- img2img 引用图：只允许房间内已授权附件 ID（refAttachmentId），由桥接层读取
  字节写入引擎临时上传区，绝不把本地绝对路径暴露给访客。

WebSocket 事件：job.queued / job.progress / job.completed / job.failed +
message.created（结果消息）。图片从不走 Base64。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time
from pathlib import Path
from typing import Any, Protocol

from aiohttp import web

from . import protocol
from .auth import error_response, resolve_member
from .models import job_public, message_payload
from .protocol import parse_client_request_id
from .rooms import _ChatRateBucket
from .security import contains_lone_surrogate
from .storage import RoomStorage
from .uploads import ImageErrorDetail, _process_image, _store_file, _store_thumb

log = logging.getLogger(__name__)

MAX_PROMPT_LENGTH = 2000
_MAX_STR_LENGTH = 128
_MAX_LORAS = 8
_TERMINAL_ROOM_STATES = {
    protocol.JOB_STATE_COMPLETED,
    protocol.JOB_STATE_FAILED,
    protocol.JOB_STATE_CANCELLED,
}

# 引擎状态 → 房间任务状态 的映射
_ENGINE_QUEUED = {"queued", "recovering", "submitting"}
_ENGINE_RUNNING = {"preparing", "running", "downloading"}


class GenerationError(Exception):
    def __init__(self, message: str, error_code: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.status = status


class GenerationBackend(Protocol):
    """房主生成后端接口（桌面引擎或测试 FakeEngine 的公共子集）。"""

    async def submit(self, payload: dict[str, Any]) -> Any:
        """提交引擎任务，返回带 .id/.status/.queue_position 的对象。"""
        ...

    async def get(self, job_id: str) -> Any | None:
        """查询引擎任务状态；返回 .id/.status/.queue_position/.result/.error。"""
        ...

    async def cancel(self, job_id: str) -> bool:
        """取消引擎任务。已终态返回 False。"""
        ...

    async def history_image(self, history_id: str) -> tuple[bytes, str, str] | None:
        """按 history_id 取生成图片，返回 (bytes, mime, filename)。"""
        ...

    async def save_upload(self, upload_id: str, data: bytes, *, filename: str, content_type: str) -> None:
        """把房间附件字节写入引擎临时上传区（img2img 引用图）。"""
        ...

    async def enhance_prompt(self, prompt: str) -> str:
        """提示词增强（AI 关闭时原样返回）。"""
        ...

    async def analyze_image(self, source_bytes: bytes, *, prompt: str) -> tuple[list[str], dict[str, Any]]:
        """视觉模型分析图片，返回 (tags, meta)。"""
        ...


class GenerationBridge:
    def __init__(self, *, storage: RoomStorage, room_id: int, room_public_id: str,
                 data_dir: Path | None, hub: Any, backend: GenerationBackend,
                 poll_interval: float = 0.4) -> None:
        self._storage = storage
        self._room_id = int(room_id)
        self._room_public_id = room_public_id
        self._data_dir = Path(data_dir) if data_dir else None
        self._hub = hub
        self._backend = backend
        self._poll_interval = max(0.01, float(poll_interval))
        self._trackers: dict[int, asyncio.Task] = {}
        self.backend = backend  # 供取消路由等直接访问

    # ------------------------------------------------------------------
    # 提交
    # ------------------------------------------------------------------
    async def submit(self, *, member, body: dict[str, Any], settings: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        storage = self._storage
        client_request_id = str(body.get("clientRequestId") or "")
        room_public_id, req_member_id = parse_client_request_id(client_request_id)
        if req_member_id != int(member["id"]):
            raise GenerationError("clientRequestId 与当前成员不匹配", "BAD_CLIENT_REQUEST_ID", 400)
        if room_public_id != self._room_public_id:
            raise GenerationError("clientRequestId 与当前房间不匹配", "BAD_CLIENT_REQUEST_ID", 400)
        existing = storage.get_job_by_client_id(client_request_id)
        if existing is not None:
            return job_public(existing), False

        raw_request = body.get("request")
        if not isinstance(raw_request, dict):
            raise GenerationError("缺少生成参数", "BAD_REQUEST", 400)
        snapshot = _sanitize_request(raw_request, settings)
        mode = "img2img" if "refAttachmentId" in snapshot else str(snapshot.get("mode") or "txt2img")
        if mode == "img2img" and not settings.get("allowImg2img"):
            raise GenerationError("房主已关闭图片参考", "IMG2IMG_DISABLED", 403)
        snapshot["mode"] = mode

        per_limit = int(settings.get("perMemberQueueLimit", 1) or 1)
        room_limit = int(settings.get("roomQueueLimit", 10) or 1)
        if storage.member_running_jobs(self._room_id, int(member["id"])) >= per_limit:
            raise GenerationError("个人队列已满，请等待当前任务完成", "QUEUE_LIMIT_MEMBER", 429)
        if storage.room_running_jobs(self._room_id) >= room_limit:
            raise GenerationError("房间队列已满，请稍后再试", "QUEUE_LIMIT_ROOM", 429)

        job_id, is_new = storage.create_job(
            room_id=self._room_id,
            member_id=int(member["id"]),
            client_request_id=client_request_id,
            request_snapshot=snapshot,
        )
        if not is_new:
            row = storage.get_job(job_id)
            return job_public(row), False

        engine_payload = await self._build_engine_payload(snapshot, mode)
        try:
            core = await self._backend.submit(engine_payload)
        except GenerationError:
            raise
        except Exception as exc:
            log.warning("room generation submit failed", exc_info=True)
            storage.set_job_state(job_id, protocol.JOB_STATE_FAILED,
                                  completed_at=None)
            row = storage.get_job(job_id)
            await self._hub.broadcast("job.failed", {
                "job": job_public(row),
                "error": "后端任务提交失败",
                "errorCode": "SUBMIT_FAILED",
            })
            return job_public(row), True

        storage.set_job_state(
            job_id, protocol.JOB_STATE_QUEUED,
            core_job_id=str(core.id),
            queue_position=getattr(core, "queue_position", None),
        )
        row = storage.get_job(job_id)
        await self._hub.broadcast("job.queued", {"job": job_public(row)})
        self._spawn_tracker(job_id, str(core.id))
        return job_public(row), True

    async def _build_engine_payload(self, snapshot: dict[str, Any], mode: str) -> dict[str, Any]:
        payload = dict(snapshot)
        payload.pop("refAttachmentId", None)
        ref_attachment_id = snapshot.get("refAttachmentId")
        if ref_attachment_id is not None:
            att = self._storage.get_attachment(int(ref_attachment_id))
            if att is None or int(att["room_id"]) != self._room_id:
                raise GenerationError("引用附件不存在", "ATTACHMENT_NOT_FOUND", 404)
            rel = att["relative_path"]
            if self._data_dir is None:
                raise GenerationError("图片存储不可用", "STORAGE_UNAVAILABLE", 500)
            path = self._data_dir / rel
            if not path.is_file():
                raise GenerationError("引用附件文件缺失", "ATTACHMENT_NOT_FOUND", 404)
            data = path.read_bytes()
            upload_id = secrets.token_hex(16)
            try:
                await self._backend.save_upload(
                    upload_id, data,
                    filename=f"room-ref-{ref_attachment_id}.png",
                    content_type=att["mime_type"] or "image/png",
                )
            except Exception as exc:  # pragma: no cover - 引擎存储异常
                log.warning("room ref upload failed", exc_info=True)
                raise GenerationError("引用图片暂不可用，请重试", "REF_UPLOAD_FAILED", 502)
            payload["ref_image"] = {"url": f"/uploads/{upload_id}"}
        payload["mode"] = mode
        return payload

    # ------------------------------------------------------------------
    # 查询 / 取消
    # ------------------------------------------------------------------
    async def list_jobs(self, *, member_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._storage.list_room_jobs(self._room_id, member_id, limit)
        return [job_public(r) for r in rows]

    async def cancel(self, room_job_id: int) -> tuple[bool, str]:
        storage = self._storage
        row = storage.get_job(room_job_id)
        if row is None or int(row["room_id"]) != self._room_id:
            return False, "JOB_NOT_FOUND"
        if row["state"] not in (protocol.JOB_STATE_QUEUED, protocol.JOB_STATE_RUNNING):
            return False, "JOB_ALREADY_DONE"
        core_id = row["core_job_id"]
        if core_id:
            try:
                ok = await self._backend.cancel(str(core_id))
            except Exception as exc:
                log.warning("room generation cancel failed", exc_info=True)
                raise GenerationError("取消失败，请重试", "CANCEL_FAILED", 502)
            if not ok:
                return False, "JOB_ALREADY_DONE"
        task = self._trackers.pop(room_job_id, None)
        if task is not None and not task.done():
            task.cancel()
        storage.set_job_state(room_job_id, protocol.JOB_STATE_CANCELLED, core_job_id=core_id)
        row = storage.get_job(room_job_id)
        await self._hub.broadcast("job.failed", {
            "job": job_public(row),
            "error": "任务已取消",
            "errorCode": "JOB_CANCELLED",
        })
        return True, ""

    async def enhance(self, prompt: str) -> str:
        prompt = str(prompt or "").strip()
        if not prompt:
            raise GenerationError("提示词不能为空", "EMPTY_PROMPT", 400)
        if len(prompt) > MAX_PROMPT_LENGTH:
            raise GenerationError("提示词过长", "PROMPT_TOO_LONG", 400)
        try:
            return str(await self._backend.enhance_prompt(prompt))
        except GenerationError:
            raise
        except Exception as exc:
            log.warning("room prompt enhancement failed", exc_info=True)
            raise GenerationError("提示词增强失败", "ENHANCE_FAILED", 502)

    async def analyze(self, attachment_id: int, prompt: str) -> dict[str, Any]:
        att = self._storage.get_attachment(attachment_id)
        if att is None or int(att["room_id"]) != self._room_id:
            raise GenerationError("附件不存在", "ATTACHMENT_NOT_FOUND", 404)
        rel = att["relative_path"]
        if self._data_dir is None:
            raise GenerationError("图片存储不可用", "STORAGE_UNAVAILABLE", 500)
        path = self._data_dir / rel
        if not path.is_file():
            raise GenerationError("附件文件缺失", "ATTACHMENT_NOT_FOUND", 404)
        source_bytes = path.read_bytes()
        try:
            tags, meta = await self._backend.analyze_image(source_bytes, prompt=prompt)
        except Exception as exc:
            log.warning("room vision analysis failed", exc_info=True)
            raise GenerationError("图片分析失败", "ANALYZE_FAILED", 502)
        return {"tags": list(tags or []), "meta": dict(meta or {})}

    async def catalog(self) -> dict[str, Any] | None:
        """读取房主桌面目录（供访客生成控件下拉）；后端未实现时返回 None。"""
        fn = getattr(self.backend, "catalog", None)
        if fn is None:
            return None
        try:
            result = await fn()
        except Exception:
            log.warning("room catalog fetch failed", exc_info=True)
            return None
        return result if isinstance(result, dict) else None

    # ------------------------------------------------------------------
    # 跟踪器
    # ------------------------------------------------------------------
    def _spawn_tracker(self, room_job_id: int, core_job_id: str) -> None:
        task = asyncio.get_running_loop().create_task(self._track(room_job_id, core_job_id))
        self._trackers[room_job_id] = task
        task.add_done_callback(lambda _t: self._trackers.pop(room_job_id, None))

    async def _track(self, room_job_id: int, core_job_id: str) -> None:
        storage = self._storage
        row = storage.get_job(room_job_id)
        last_room_state = str(row["state"]) if row else None
        while True:
            row = storage.get_job(room_job_id)
            if row is None or row["state"] in _TERMINAL_ROOM_STATES:
                return
            try:
                record = await self._backend.get(core_job_id)
            except Exception:
                record = None
            if record is None:
                if row["state"] in (protocol.JOB_STATE_QUEUED, protocol.JOB_STATE_RUNNING):
                    await self._fail(room_job_id, "后端任务丢失，请重试", "BACKEND_JOB_LOST")
                return
            status = str(getattr(record, "status", ""))
            if status in _ENGINE_QUEUED:
                if last_room_state != protocol.JOB_STATE_QUEUED:
                    last_room_state = protocol.JOB_STATE_QUEUED
                    storage.set_job_state(room_job_id, protocol.JOB_STATE_QUEUED,
                                          core_job_id=core_job_id,
                                          queue_position=getattr(record, "queue_position", None))
                    await self._broadcast_job("job.queued", room_job_id)
            elif status in _ENGINE_RUNNING:
                if last_room_state != protocol.JOB_STATE_RUNNING:
                    last_room_state = protocol.JOB_STATE_RUNNING
                    storage.set_job_state(room_job_id, protocol.JOB_STATE_RUNNING,
                                          core_job_id=core_job_id)
                    await self._broadcast_job("job.progress", room_job_id, {"progress": 0.5})
            elif status == "completed":
                await self._finalize(room_job_id, record)
                return
            elif status == "failed":
                # 不把引擎原始错误透传给访客（可能含本地路径/模型名等内部信息）。
                await self._fail(room_job_id, "生成失败，请稍后再试", "GENERATION_FAILED")
                return
            else:  # cancelled / detached
                await self._cancel(room_job_id)
                return
            await asyncio.sleep(self._poll_interval)

    async def _finalize(self, room_job_id: int, record: Any) -> None:
        storage = self._storage
        row = storage.get_job(room_job_id)
        if row is None:
            return
        result = getattr(record, "result", None)
        history_id = result.get("history_id") if isinstance(result, dict) else None
        if not history_id:
            await self._fail(room_job_id, "后端未返回生成结果", "NO_HISTORY")
            return
        try:
            image = await self._backend.history_image(str(history_id))
        except Exception:
            image = None
        if image is None:
            await self._fail(room_job_id, "无法读取生成图片", "IMAGE_UNAVAILABLE")
            return
        data, mime, filename = image
        try:
            stored_mime, main_data, thumb_data, w, h = _process_image(data)
        except ImageErrorDetail as exc:
            await self._fail(room_job_id, exc.message, exc.error_code)
            return
        if self._data_dir is None:
            await self._fail(room_job_id, "图片存储不可用", "STORAGE_UNAVAILABLE")
            return
        rel = _store_file(self._data_dir, stored_mime, main_data)
        thumb_rel = _store_thumb(self._data_dir, thumb_data)
        client_request_id = str(row["client_request_id"])
        result_message_id = storage.add_message(
            room_id=self._room_id,
            sender_id=int(row["member_id"]),
            msg_type=protocol.MSG_GENERATION_RESULT,
            text=None,
            client_message_id=f"{client_request_id}:result",
            generation_job_id=room_job_id,
        )
        storage.add_attachment(
            room_id=self._room_id,
            uploader_id=int(row["member_id"]),
            message_id=result_message_id,
            kind="generated",
            relative_path=rel,
            thumbnail_relpath=thumb_rel,
            mime_type=stored_mime,
            size=len(main_data),
            width=w,
            height=h,
            sha256=hashlib.sha256(main_data).hexdigest(),
        )
        storage.set_job_state(room_job_id, protocol.JOB_STATE_COMPLETED,
                              core_job_id=row["core_job_id"])
        await self._broadcast_job("job.completed", room_job_id)
        msg_row = storage.get_message(result_message_id)
        if msg_row is not None:
            await self._hub.broadcast("message.created", message_payload(msg_row, storage))

    async def _fail(self, room_job_id: int, message: str, error_code: str) -> None:
        row = self._storage.get_job(room_job_id)
        if row is None:
            return
        self._storage.set_job_state(room_job_id, protocol.JOB_STATE_FAILED,
                                    core_job_id=row["core_job_id"])
        await self._broadcast_job("job.failed", room_job_id,
                                  {"error": message, "errorCode": error_code})

    async def _cancel(self, room_job_id: int) -> None:
        row = self._storage.get_job(room_job_id)
        if row is None:
            return
        self._storage.set_job_state(room_job_id, protocol.JOB_STATE_CANCELLED,
                                    core_job_id=row["core_job_id"])
        await self._broadcast_job("job.failed", room_job_id,
                                  {"error": "任务已取消", "errorCode": "JOB_CANCELLED"})

    async def _broadcast_job(self, event: str, room_job_id: int, extra: dict[str, Any] | None = None) -> None:
        row = self._storage.get_job(room_job_id)
        if row is None:
            return
        data: dict[str, Any] = {"job": job_public(row)}
        if extra:
            data.update(extra)
        await self._hub.broadcast(event, data)

    async def stop_all(self) -> None:
        tasks = list(self._trackers.values())
        self._trackers.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class DesktopRoomBackend:
    """生产后端：包装房主桌面引擎（DesktopEngine）。"""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    async def submit(self, payload: dict[str, Any]) -> Any:
        return await self._engine.submit_job(payload)

    async def get(self, job_id: str) -> Any | None:
        return await self._engine.get_job(job_id)

    async def cancel(self, job_id: str) -> bool:
        return await self._engine.cancel_job(job_id)

    async def history_image(self, history_id: str) -> tuple[bytes, str, str] | None:
        image = await self._engine.get_history_image(history_id)
        if image is None:
            return None
        return (image.data, image.content_type or "image/png", image.filename)

    async def save_upload(self, upload_id: str, data: bytes, *, filename: str, content_type: str) -> None:
        await self._engine.uploads.save(
            upload_id, data, filename=filename, content_type=content_type, kind="room_ref"
        )

    async def enhance_prompt(self, prompt: str) -> str:
        result = await self._engine._prepare_prompt({"prompt": prompt})
        return str(result.final_prompt)

    async def analyze_image(self, source_bytes: bytes, *, prompt: str) -> tuple[list[str], dict[str, Any]]:
        return await self._engine._reverse_prompt_with_vision(
            source_bytes, raw_prompt=prompt, current_prompt=prompt, family="room"
        )

    async def catalog(self) -> dict[str, Any]:
        return await self._engine.get_catalog()


# ---------------------------------------------------------------------------
# 请求校验
# ---------------------------------------------------------------------------
def _clamp_int(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _clamp_float(value: Any, lo: float, hi: float, default: float) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


def _sanitize_request(raw: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """白名单拷贝并校验访客生成参数，防止任意键/超界值混入引擎负载。"""
    out: dict[str, Any] = {}
    prompt = str(raw.get("prompt") or "").strip()
    if not prompt:
        raise GenerationError("提示词不能为空", "EMPTY_PROMPT", 400)
    if len(prompt) > MAX_PROMPT_LENGTH:
        raise GenerationError(f"提示词过长（最多 {MAX_PROMPT_LENGTH} 字）", "PROMPT_TOO_LONG", 400)
    if contains_lone_surrogate(prompt):
        raise GenerationError("提示词包含无法识别的字符，请重新输入", "INVALID_CHARACTERS", 400)
    out["prompt"] = prompt

    negative = raw.get("negative_prompt")
    if negative is not None:
        negative = str(negative).strip()
        if len(negative) > MAX_PROMPT_LENGTH:
            raise GenerationError("负面提示词过长", "PROMPT_TOO_LONG", 400)
        if contains_lone_surrogate(negative):
            raise GenerationError("负面提示词包含无法识别的字符，请重新输入", "INVALID_CHARACTERS", 400)
        out["negative_prompt"] = negative

    out["width"] = _clamp_int(raw.get("width"), 64, 2048, 832)
    out["height"] = _clamp_int(raw.get("height"), 64, 2048, 1216)
    out["steps"] = _clamp_int(raw.get("steps"), 1, 100, 20)
    if raw.get("seed") is not None:
        try:
            out["seed"] = int(raw["seed"])
        except (TypeError, ValueError):
            out["seed"] = -1
    if raw.get("cfg") is not None:
        out["cfg"] = _clamp_float(raw.get("cfg"), 0.0, 30.0, 7.0)
    if isinstance(raw.get("batch"), int):
        out["batch"] = max(1, min(8, int(raw["batch"])))

    for key in ("mode", "family", "model", "checkpoint", "scheduler", "sampler", "workflow"):
        val = raw.get(key)
        if isinstance(val, str) and val.strip():
            out[key] = val.strip()[:_MAX_STR_LENGTH]

    if isinstance(raw.get("loras"), list):
        cleaned = [item for item in raw["loras"] if isinstance(item, dict)]
        if cleaned:
            # allowedLoras 为显式列表时强校验（空列表 = 房主未放行任何 LoRA）。
            allowed_loras = settings.get("allowedLoras")
            if allowed_loras is not None:
                allowed_set = {str(x) for x in allowed_loras}
                for item in cleaned:
                    ident = str(item.get("id") or item.get("name") or "")
                    if ident not in allowed_set:
                        raise GenerationError("该 LoRA 未获得房主允许", "LORA_NOT_ALLOWED", 403)
            out["loras"] = cleaned[:_MAX_LORAS]

    ref_id = raw.get("refAttachmentId")
    if ref_id is not None:
        try:
            out["refAttachmentId"] = int(ref_id)
        except (TypeError, ValueError):
            raise GenerationError("refAttachmentId 无效", "BAD_REQUEST", 400)

    # 白名单语义：键未配置（None）→ 默认放行；显式列表（含空列表）→ 严格校验，
    # 使"房主未勾选任何工作流/模型系列"表现为拒绝非默认项而非全放行。
    allowed_families = settings.get("allowedModelFamilies")
    if allowed_families is not None and out.get("family") and str(out["family"]) not in allowed_families:
        raise GenerationError("该模型系列未获得房主允许", "MODEL_NOT_ALLOWED", 403)
    allowed_workflows = settings.get("allowedWorkflowIds")
    if allowed_workflows is not None and out.get("workflow") and str(out["workflow"]) not in allowed_workflows:
        raise GenerationError("该工作流未获得房主允许", "WORKFLOW_NOT_ALLOWED", 403)
    return out


# ---------------------------------------------------------------------------
# HTTP 路由
# ---------------------------------------------------------------------------
def _bridge(request: web.Request) -> GenerationBridge | None:
    return request.app.get("generation_bridge")


def _room_checks(request: web.Request, *, generation: bool) -> web.Response | None:
    storage: RoomStorage = request.app["room_storage"]
    room = storage.get_room(int(request.app["room_id"]))
    if room is None:
        return error_response("房间不存在", "ROOM_NOT_FOUND", 404)
    if room["state"] != protocol.ROOM_STATE_ONLINE:
        return error_response("房间已关闭", "ROOM_CLOSED", 409)
    if generation:
        settings = storage.room_settings_map(int(request.app["room_id"]))
        if not settings.get("allowGeneration"):
            return error_response("房主已关闭生图", "GENERATION_DISABLED", 403)
        if request.app.get("generation_paused"):
            return error_response("房主已暂停生图", "GENERATION_PAUSED", 409)
    return None


async def _generations_post_handler(request: web.Request) -> web.Response:
    member = resolve_member(request)
    if member is None:
        return error_response("未登录或会话已失效", "AUTH_REQUIRED", 401)
    blocked = _room_checks(request, generation=True)
    if blocked is not None:
        return blocked
    bridge = _bridge(request)
    if bridge is None:
        return error_response("生图服务不可用", "GENERATION_UNAVAILABLE", 503)
    try:
        body = await request.json()
    except Exception:
        return error_response("请求体必须是 JSON", "BAD_REQUEST", 400)
    if not isinstance(body, dict):
        return error_response("请求体必须是 JSON", "BAD_REQUEST", 400)
    storage: RoomStorage = request.app["room_storage"]
    settings = storage.room_settings_map(int(request.app["room_id"]))
    try:
        job, created = await bridge.submit(member=member, body=body, settings=settings)
    except GenerationError as exc:
        return error_response(exc.message, exc.error_code, exc.status)
    return web.json_response({"job": job, "created": created}, status=201)


async def _generations_list_handler(request: web.Request) -> web.Response:
    member = resolve_member(request)
    if member is None:
        return error_response("未登录或会话已失效", "AUTH_REQUIRED", 401)
    bridge = _bridge(request)
    if bridge is None:
        return error_response("生图服务不可用", "GENERATION_UNAVAILABLE", 503)
    try:
        limit = max(1, min(200, int(request.query.get("limit", 50))))
    except (TypeError, ValueError):
        limit = 50
    mine = request.query.get("mine") == "1"
    jobs = await bridge.list_jobs(
        member_id=int(member["id"]) if mine else None, limit=limit
    )
    return web.json_response({"jobs": jobs})


async def _generations_get_handler(request: web.Request) -> web.Response:
    member = resolve_member(request)
    if member is None:
        return error_response("未登录或会话已失效", "AUTH_REQUIRED", 401)
    try:
        job_id = int(request.match_info["job_id"])
    except (TypeError, ValueError, KeyError):
        return error_response("任务 ID 无效", "BAD_REQUEST", 400)
    storage: RoomStorage = request.app["room_storage"]
    row = storage.get_job(job_id)
    if row is None or int(row["room_id"]) != int(request.app["room_id"]):
        return error_response("任务不存在", "JOB_NOT_FOUND", 404)
    return web.json_response({"job": job_public(row)})


async def _generations_cancel_handler(request: web.Request) -> web.Response:
    member = resolve_member(request)
    if member is None:
        return error_response("未登录或会话已失效", "AUTH_REQUIRED", 401)
    try:
        job_id = int(request.match_info["job_id"])
    except (TypeError, ValueError, KeyError):
        return error_response("任务 ID 无效", "BAD_REQUEST", 400)
    storage: RoomStorage = request.app["room_storage"]
    row = storage.get_job(job_id)
    if row is None or int(row["room_id"]) != int(request.app["room_id"]):
        return error_response("任务不存在", "JOB_NOT_FOUND", 404)
    is_owner = str(member["role"]) == protocol.ROLE_OWNER
    is_sender = int(row["member_id"]) == int(member["id"])
    if not (is_owner or is_sender):
        return error_response("无权取消该任务", "FORBIDDEN", 403)
    bridge = _bridge(request)
    if bridge is None:
        return error_response("生图服务不可用", "GENERATION_UNAVAILABLE", 503)
    try:
        ok, code = await bridge.cancel(job_id)
    except GenerationError as exc:
        return error_response(exc.message, exc.error_code, exc.status)
    if not ok:
        return error_response("任务已完成或不可取消", code, 409)
    row = storage.get_job(job_id)
    return web.json_response({"job": job_public(row), "cancelled": True})


async def _enhance_handler(request: web.Request) -> web.Response:
    member = resolve_member(request)
    if member is None:
        return error_response("未登录或会话已失效", "AUTH_REQUIRED", 401)
    storage: RoomStorage = request.app["room_storage"]
    room_id = int(request.app["room_id"])
    if not storage.room_settings_map(room_id).get("allowPromptEnhancement"):
        return error_response("房主已关闭提示词增强", "ENHANCEMENT_DISABLED", 403)
    now = time.monotonic()
    if not request.app["prompt_limiter"].try_acquire(int(member["id"]), now, limit=5, window=60.0):
        return error_response("操作过于频繁，请稍后再试", "PROMPT_RATE_LIMITED", 429)
    bridge = _bridge(request)
    if bridge is None:
        return error_response("生图服务不可用", "GENERATION_UNAVAILABLE", 503)
    try:
        body = await request.json()
    except Exception:
        return error_response("请求体必须是 JSON", "BAD_REQUEST", 400)
    if not isinstance(body, dict):
        return error_response("请求体必须是 JSON", "BAD_REQUEST", 400)
    try:
        enhanced = await bridge.enhance(str(body.get("prompt") or ""))
    except GenerationError as exc:
        return error_response(exc.message, exc.error_code, exc.status)
    return web.json_response({"enhanced": enhanced})


async def _analyze_handler(request: web.Request) -> web.Response:
    member = resolve_member(request)
    if member is None:
        return error_response("未登录或会话已失效", "AUTH_REQUIRED", 401)
    storage: RoomStorage = request.app["room_storage"]
    room_id = int(request.app["room_id"])
    if not storage.room_settings_map(room_id).get("allowImg2img"):
        return error_response("房主已关闭图片参考/分析", "IMG2IMG_DISABLED", 403)
    now = time.monotonic()
    if not request.app["prompt_limiter"].try_acquire(int(member["id"]), now, limit=5, window=60.0):
        return error_response("操作过于频繁，请稍后再试", "PROMPT_RATE_LIMITED", 429)
    bridge = _bridge(request)
    if bridge is None:
        return error_response("生图服务不可用", "GENERATION_UNAVAILABLE", 503)
    try:
        body = await request.json()
    except Exception:
        return error_response("请求体必须是 JSON", "BAD_REQUEST", 400)
    if not isinstance(body, dict):
        return error_response("请求体必须是 JSON", "BAD_REQUEST", 400)
    try:
        attachment_id = int(body.get("attachmentId"))
    except (TypeError, ValueError):
        return error_response("attachmentId 无效", "BAD_REQUEST", 400)
    try:
        result = await bridge.analyze(attachment_id, str(body.get("prompt") or ""))
    except GenerationError as exc:
        return error_response(exc.message, exc.error_code, exc.status)
    return web.json_response(result)


def install_generation_routes(app: web.Application) -> None:
    app["generation_paused"] = False
    app["prompt_limiter"] = _ChatRateBucket()
    # 静态路径必须注册在 {job_id} 之前。
    app.router.add_post("/room/v1/generations/enhance", _enhance_handler)
    app.router.add_post("/room/v1/generations/analyze", _analyze_handler)
    app.router.add_post("/room/v1/generations", _generations_post_handler)
    app.router.add_get("/room/v1/generations", _generations_list_handler)
    app.router.add_get("/room/v1/generations/", _generations_list_handler)
    app.router.add_get("/room/v1/generations/{job_id}", _generations_get_handler)
    app.router.add_post("/room/v1/generations/{job_id}", _generations_cancel_handler)

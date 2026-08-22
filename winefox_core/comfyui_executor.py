# -*- coding: utf-8 -*-
"""WineFox Desktop — 平台无关 ComfyUI 工作流执行器（Agent 05）。

在 ComfyUITransport（HTTP/认证/TLS/超时）之上实现 ComfyUI 语义：
- probe：object_info / queue / models(模型+LoRA) / system_stats；
- 上传图片（/upload/image，含大小上限、Pillow 预解码校验、防逃逸）；
- 提交工作流（POST /prompt）→ prompt_id；状态不明绝不重试；
- 轮询 /history + /queue（含 prompt 消失判定），/view 下载结果；
- 已有 prompt_id 时可恢复轮询（resume），不重复提交；
- 输入为不可变 ExecutionSnapshot，不依赖共享 runtime_* 可变状态；
- 返回结构化 ComfyUIResult / ComfyUIError（不含绝对路径）。

不依赖 AstrBot；标准 logging。
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import mimetypes
import random
import re
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .comfyui_transport import ComfyUITransport
from .comfyui_types import (
    MODEL_DIR_HINTS,
    ComfyUIError,
    ComfyUIExecutionError,
    ComfyUIFileError,
    ComfyUIGenerationTimeoutError,
    ComfyUIImageDecodeError,
    ComfyUIMissingNodeError,
    ComfyUIPromptLostError,
    ComfyUIQueueSubmitUnknownError,
    ComfyUIResult,
    ComfyUIServerConfig,
    ComfyUIValidationError,
    ErrorKind,
    ExecutionSnapshot,
    ModelInventory,
    QueueSnapshot,
    ServerProbe,
    UploadedImage,
)

logger = logging.getLogger("winefox.comfyui.executor")

_SAFE_SUFFIX_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_MODEL_FILE_RE = re.compile(r"^[A-Za-z0-9_ .\-()\[\]]+\.(safetensors|ckpt|pt|bin|pth)$", re.IGNORECASE)

# LoRA 忽略名单（与旧插件 IGNORED_LORA_PREFIXES/MARKERS 对齐）
_IGNORED_LORA_PREFIXES = ("intrinsic_lora_",)
_IGNORED_LORA_MARKERS = (
    "controlnet", "control_net", "openpose", "depth", "sdpose", "sam2", "sam3",
    "segment", "segm", "bbox", "yolo", "detection", "preprocessor", "lllite",
    "t2i_adapter", "adapter",
)

_HISTORY_IMAGE_FIELDS = ("images", "image", "gifs", "gif", "previews", "files")
_COMFY_VIEW_TYPES = frozenset({"input", "output", "temp"})


def _is_ignored_lora_name(name: str) -> bool:
    filename = Path(str(name or "").strip()).name.lower()
    return (
        any(filename.startswith(p) for p in _IGNORED_LORA_PREFIXES)
        or any(m in filename for m in _IGNORED_LORA_MARKERS)
    )


def sanitize_comfy_name(value: str, *, allow_subfolder: bool = False) -> str:
    """防路径逃逸：只允许裸文件名（或相对 subfolder）。

    拒绝绝对路径、'..'、反斜杠；name 额外拒绝 '/'。
    返回清洗后的值；非法时抛 ComfyUIFileError（不含原始绝对路径）。
    """
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or raw in (".", ".."):
        raise ComfyUIFileError("上传/引用文件名非法（空或 .）")
    if raw.startswith("/") or raw.startswith("//") or re.match(r"^[A-Za-z]:", raw):
        raise ComfyUIFileError("不允许使用绝对路径文件名")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise ComfyUIFileError("文件名包含路径逃逸成分（..）")
    if len(parts) > 1 and not allow_subfolder:
        raise ComfyUIFileError("文件名不允许包含子目录分隔符")
    if allow_subfolder:
        return "/".join(parts)
    return parts[0]


def _queue_contains_prompt_id(value: Any, prompt_id: str) -> bool:
    """递归检查 /queue 快照中是否包含目标 prompt_id（与旧插件对齐）。"""
    prompt_id = str(prompt_id or "").strip()
    if not prompt_id:
        return False
    if isinstance(value, dict):
        for key in ("prompt_id", "id"):
            if str(value.get(key) or "") == prompt_id:
                return True
        return any(_queue_contains_prompt_id(item, prompt_id) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_queue_contains_prompt_id(item, prompt_id) for item in value)
    return str(value or "") == prompt_id


def _queue_item_prompt_id(value: Any) -> str:
    """Extract a prompt id from mappings and native ComfyUI queue arrays."""
    if isinstance(value, Mapping):
        for key in ("prompt_id", "id"):
            candidate = str(value.get(key) or "").strip()
            if candidate:
                return candidate
        return ""
    if isinstance(value, (list, tuple)):
        # Core ComfyUI uses [queue_number, prompt_id, workflow, ...].
        if len(value) > 1 and not isinstance(value[1], (dict, list, tuple, set)):
            candidate = str(value[1] or "").strip()
            if candidate:
                return candidate
        for item in value:
            candidate = _queue_item_prompt_id(item)
            if candidate:
                return candidate
    return ""


def _is_connection_failure(exc: "ComfyUIError") -> bool:
    """连接级失败（拒绝/超时/DNS）：重试与串行后续请求都无意义。"""
    from .comfyui_types import ErrorKind

    return exc.kind in (ErrorKind.CONNECTION, ErrorKind.TIMEOUT)


class ComfyUIExecutor:
    """ComfyUI 工作流执行器（无共享可变运行时状态）。

    ``run(snapshot)`` 为完整管线；``resume(prompt_id, snapshot)`` 只做
    轮询恢复（不重新上传/提交，用于进程重启后接续任务）。
    """

    def __init__(
        self,
        config: ComfyUIServerConfig,
        *,
        transport: Optional[ComfyUITransport] = None,
    ) -> None:
        self.config = config
        self._owns_transport = transport is None
        self._transport = transport or ComfyUITransport(config)
        self._object_info_cache: Tuple[float, Dict[str, Any]] = (0.0, {})
        self._probe_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> "ComfyUIExecutor":
        await self._transport.start()
        return self

    async def close(self) -> None:
        if self._owns_transport:
            await self._transport.close()

    async def __aenter__(self) -> "ComfyUIExecutor":
        return await self.start()

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    @property
    def transport(self) -> ComfyUITransport:
        return self._transport

    @property
    def base_url(self) -> str:
        return self._transport.base_url

    # ------------------------------------------------------------------
    # 探测：object_info / queue / models / system_stats
    # ------------------------------------------------------------------
    async def fetch_object_info(self, *, force: bool = False) -> Dict[str, Any]:
        """GET /object_info（60s 缓存；force 强制刷新）。"""
        cached_at, cached = self._object_info_cache
        if not force and isinstance(cached, dict) and cached and time.time() - cached_at < 60:
            return cached
        async with self._probe_lock:
            cached_at, cached = self._object_info_cache
            if not force and isinstance(cached, dict) and cached and time.time() - cached_at < 60:
                return cached
            data = await self._transport.get_json("/object_info")
            if isinstance(data, dict):
                self._object_info_cache = (time.time(), data)
                return data
            return {}

    async def fetch_queue(self) -> QueueSnapshot:
        """GET /queue。"""
        data = await self._transport.get_json("/queue")
        if not isinstance(data, dict):
            return QueueSnapshot()
        running = data.get("queue_running") or data.get("running") or []
        pending = data.get("queue_pending") or data.get("pending") or []
        running = running if isinstance(running, list) else []
        pending = pending if isinstance(pending, list) else []
        running_ids = tuple(
            prompt_id for item in running if (prompt_id := _queue_item_prompt_id(item))
        )
        pending_ids = tuple(
            prompt_id for item in pending if (prompt_id := _queue_item_prompt_id(item))
        )
        return QueueSnapshot(
            running=len(running),
            pending=len(pending),
            running_ids=running_ids,
            pending_ids=pending_ids,
        )

    async def fetch_models(self) -> ModelInventory:
        """GET /models（新 API）；失败时回退 /models/loras + object_info 组合框。"""
        checkpoints: List[str] = []
        loras: List[str] = []
        vae: List[str] = []
        controlnets: List[str] = []
        upscalers: List[str] = []
        unet: List[str] = []
        text_encoders: List[str] = []
        try:
            data = await self._transport.get_json("/models")
            if isinstance(data, dict):
                checkpoints = [str(x) for x in (data.get("checkpoints") or [])]
                loras = [str(x) for x in (data.get("loras") or [])]
                vae = [str(x) for x in (data.get("vae") or [])]
                controlnets = [str(x) for x in (data.get("controlnets") or [])]
                upscalers = [str(x) for x in (data.get("upscalers") or [])]
                unet = [str(x) for x in (data.get("unet") or data.get("diffusion_models") or [])]
                text_encoders = [str(x) for x in (data.get("text_encoders") or data.get("clip") or [])]
        except ComfyUIError:
            pass  # 旧版 ComfyUI 无 /models，走回退
        if not loras:
            try:
                raw = await self._transport.get_json("/models/loras")
                if isinstance(raw, list):
                    loras = [
                        str(x).strip() for x in raw
                        if str(x).strip().lower().endswith((".safetensors", ".ckpt", ".pt", ".bin"))
                        and not _is_ignored_lora_name(str(x).strip())
                    ]
            except ComfyUIError:
                pass
        if not loras:
            loras = self._extract_lora_names_from_object_info(await self.fetch_object_info())
        return ModelInventory(
            checkpoints=tuple(dict.fromkeys(checkpoints)),
            loras=tuple(dict.fromkeys(loras)),
            vae=tuple(dict.fromkeys(vae)),
            controlnets=tuple(dict.fromkeys(controlnets)),
            upscalers=tuple(dict.fromkeys(upscalers)),
            unet=tuple(dict.fromkeys(unet)),
            text_encoders=tuple(dict.fromkeys(text_encoders)),
        )

    async def fetch_lora_metadata(
        self,
        filename: str,
        *,
        timeout_seconds: float = 2.0,
    ) -> Dict[str, Any]:
        """Fetch one safetensors metadata header through ComfyUI's HTTP API."""
        safe_name = sanitize_comfy_name(filename, allow_subfolder=True)
        if not safe_name.lower().endswith(".safetensors"):
            return {}
        try:
            value = await asyncio.wait_for(
                self._transport.get_json(
                    "/view_metadata/loras",
                    params={"filename": safe_name},
                    retries=1,
                ),
                timeout=max(0.1, float(timeout_seconds)),
            )
        except (asyncio.TimeoutError, ComfyUIError):
            return {}
        return dict(value) if isinstance(value, Mapping) else {}

    async def fetch_lora_metadata_batch(
        self,
        filenames: List[str],
        *,
        max_items: int = 40,
        concurrency: int = 2,
        timeout_seconds: float = 2.0,
        total_timeout_seconds: float = 10.0,
        minimum_interval_seconds: float = 0.05,
    ) -> Dict[str, Dict[str, Any]]:
        """Fetch a bounded metadata batch without propagating per-file failures."""
        safe_names: List[str] = []
        seen: set[str] = set()
        for filename in filenames:
            try:
                safe_name = sanitize_comfy_name(filename, allow_subfolder=True)
            except ComfyUIError:
                continue
            marker = safe_name.casefold()
            if not safe_name.lower().endswith(".safetensors") or marker in seen:
                continue
            seen.add(marker)
            safe_names.append(safe_name)
            if len(safe_names) >= max(1, int(max_items)):
                break

        results: Dict[str, Dict[str, Any]] = {}
        semaphore = asyncio.Semaphore(max(1, min(int(concurrency), 4)))
        rate_lock = asyncio.Lock()
        next_start = 0.0

        async def fetch_one(name: str) -> None:
            nonlocal next_start
            async with semaphore:
                async with rate_lock:
                    delay = next_start - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    next_start = time.monotonic() + max(0.0, float(minimum_interval_seconds))
                metadata = await self.fetch_lora_metadata(
                    name,
                    timeout_seconds=timeout_seconds,
                )
                if metadata:
                    results[name] = metadata

        tasks = [asyncio.create_task(fetch_one(name)) for name in safe_names]
        if not tasks:
            return results
        done, pending = await asyncio.wait(
            tasks,
            timeout=max(0.1, float(total_timeout_seconds)),
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*done, *pending, return_exceptions=True)
        return results

    def _extract_lora_names_from_object_info(self, object_info: Mapping[str, Any]) -> List[str]:
        names: set = set()

        def collect(value: Any) -> None:
            if isinstance(value, str):
                v = value.strip()
                if v.lower().endswith((".safetensors", ".ckpt", ".pt", ".bin")) and not _is_ignored_lora_name(v):
                    names.add(v)
            elif isinstance(value, list):
                if value and isinstance(value[0], list):
                    for item in value[0]:
                        collect(item)
                else:
                    for item in value:
                        collect(item)
            elif isinstance(value, dict):
                for item in value.values():
                    collect(item)

        for class_name, class_info in (object_info or {}).items():
            if not isinstance(class_info, dict):
                continue
            input_info = class_info.get("input", {}) or {}
            all_inputs: Dict[str, Any] = {}
            for bucket in ("required", "optional"):
                part = input_info.get(bucket, {})
                if isinstance(part, dict):
                    all_inputs.update(part)
            class_lower = str(class_name).lower()
            for input_name, spec in all_inputs.items():
                input_lower = str(input_name).lower()
                if "lora" in input_lower or ("lora" in class_lower and input_lower in {"name", "file"}):
                    collect(spec)
        return sorted(names)

    async def probe(self) -> ServerProbe:
        """探测服务器：object_info / queue / models + 系统状态。

        失败时返回 reachable=False 的 ServerProbe，不抛异常（探测用于 UI 展示）。
        """
        started = time.perf_counter()
        warnings: List[str] = []
        object_info: Dict[str, Any] = {}
        models = ModelInventory()
        queue = QueueSnapshot()
        system_stats: Dict[str, Any] = {}
        try:
            object_info = await self.fetch_object_info(force=True)
        except ComfyUIError as exc:
            warnings.append(f"object_info: {exc.user_message}")
            # 连接级失败（拒绝/超时）时其余三个请求必然同样失败：
            # 串行重打只会把探测拖到几十秒，把启动页卡在暖场。立即短路。
            if _is_connection_failure(exc):
                return ServerProbe(
                    reachable=False,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    warnings=warnings,
                )
        try:
            queue = await self.fetch_queue()
        except ComfyUIError as exc:
            warnings.append(f"queue: {exc.user_message}")
            if _is_connection_failure(exc):
                return ServerProbe(
                    reachable=False,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    warnings=warnings,
                )
        try:
            models = await self.fetch_models()
        except ComfyUIError as exc:
            warnings.append(f"models: {exc.user_message}")
        try:
            raw = await self._transport.get_json("/system_stats")
            if isinstance(raw, dict):
                system_stats = raw
        except ComfyUIError:
            pass
        latency_ms = (time.perf_counter() - started) * 1000.0
        reachable = bool(object_info) or bool(models.checkpoints) or bool(models.loras)
        return ServerProbe(
            reachable=reachable,
            latency_ms=latency_ms,
            object_info=object_info,
            models=models,
            queue=queue,
            system_stats=system_stats,
            warnings=tuple(warnings),
        )

    # ------------------------------------------------------------------
    # 节点校验
    # ------------------------------------------------------------------
    def validate_workflow_nodes(
        self, workflow: Mapping[str, Any], object_info: Mapping[str, Any]
    ) -> List[str]:
        """返回工作流中存在但 object_info 缺失的 class_type（去重排序）。"""
        missing: List[str] = []
        seen: set = set()
        for node_data in (workflow or {}).values():
            if not isinstance(node_data, dict):
                continue
            class_type = str(node_data.get("class_type") or "").strip()
            if not class_type or class_type in seen:
                continue
            if class_type not in object_info:
                seen.add(class_type)
                missing.append(class_type)
        return missing

    # ------------------------------------------------------------------
    # 上传
    # ------------------------------------------------------------------
    async def upload_image(
        self,
        source: Any,
        *,
        subfolder: Optional[str] = None,
        overwrite: bool = True,
        filename: Optional[str] = None,
        verify: Optional[bool] = None,
        max_bytes: Optional[int] = None,
    ) -> UploadedImage:
        """上传图片到 ComfyUI /upload/image。

        source 可为本地路径（str/Path）或 bytes。校验：大小上限、Pillow 可解码、
        filename/subfolder 防逃逸。返回服务器侧定位（同样经防逃逸清洗）。
        """
        cap = int(max_bytes or self.config.max_upload_bytes)
        verify = True if verify is None else bool(verify)

        if isinstance(source, (bytes, bytearray, memoryview)):
            data = bytes(source)
            display_name = str(filename or f"winefox_{uuid.uuid4().hex}.png")
        else:
            path = Path(str(source)).expanduser()
            display_name = str(filename or path.name or "")
            if not path.is_file():
                raise ComfyUIFileError(f"图生图源图片不存在: {display_name}")
            size = path.stat().st_size
            if size > cap:
                raise ComfyUIFileError(
                    f"上传图片超过大小上限（{size} > {cap} bytes）", kind=ErrorKind.PAYLOAD_TOO_LARGE
                )
            data = path.read_bytes()

        if len(data) > cap:
            raise ComfyUIFileError(
                f"上传图片超过大小上限（{len(data)} > {cap} bytes）", kind=ErrorKind.PAYLOAD_TOO_LARGE
            )
        safe_name = sanitize_comfy_name(display_name)
        if not safe_name.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")):
            # 保留原扩展名，但只允许白名单；其余统一按字节流上传并给 .png
            safe_name = f"{Path(safe_name).stem or 'image'}.png"
        safe_subfolder = ""
        if subfolder:
            safe_subfolder = sanitize_comfy_name(subfolder, allow_subfolder=True)

        if verify:
            self._verify_image_bytes(data, label=safe_name)

        suffix = Path(safe_name).suffix or ".png"
        upload_name = f"winefox_img2img_{uuid.uuid4().hex}{suffix}"
        content_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        fields: Dict[str, Any] = {
            "image": (data, upload_name, content_type),
            "type": "input",
            "overwrite": "true" if overwrite else "false",
        }
        if safe_subfolder:
            fields["subfolder"] = safe_subfolder

        result = await self._transport.post_multipart("/upload/image", fields)
        data_out = result if isinstance(result, dict) else {}
        name = sanitize_comfy_name(str(data_out.get("name") or upload_name))
        resp_subfolder = str(data_out.get("subfolder") or safe_subfolder or "").strip()
        resp_subfolder = sanitize_comfy_name(resp_subfolder, allow_subfolder=True) if resp_subfolder else ""
        return UploadedImage(
            name=name,
            subfolder=resp_subfolder,
            type=str(data_out.get("type") or "input"),
        )

    def _verify_image_bytes(self, data: bytes, *, label: str) -> None:
        """Pillow 解码校验（含解压炸弹防护）。"""
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover - 环境缺 Pillow
            logger.debug("[ComfyUIExecutor] Pillow 不可用，跳过图片解码校验: %s", exc)
            return
        if not data:
            raise ComfyUIImageDecodeError(f"图片内容为空: {label}")
        try:
            previous = Image.MAX_IMAGE_PIXELS
            Image.MAX_IMAGE_PIXELS = max(int(self.config.max_image_pixels), 0)
            try:
                with Image.open(io.BytesIO(data)) as img:
                    img.load()
                    w, h = img.size
                    if w <= 0 or h <= 0:
                        raise ComfyUIImageDecodeError(f"图片尺寸非法: {label}")
            finally:
                Image.MAX_IMAGE_PIXELS = previous
        except ComfyUIImageDecodeError:
            raise
        except Exception as exc:
            raise ComfyUIImageDecodeError(
                f"图片解码失败（格式损坏或尺寸超限）: {label}", cause=exc
            )

    # ------------------------------------------------------------------
    # 提交 / 恢复
    # ------------------------------------------------------------------
    async def submit(
        self,
        workflow: Mapping[str, Any],
        *,
        client_id: str,
        timeout: Optional[float] = None,
    ) -> str:
        """POST /prompt → prompt_id。

        明确语义：POST 状态不明（发送后连接断开/超时）绝不重试，
        抛 ComfyUIQueueSubmitUnknownError，提示先查 /queue 确认。
        """
        payload = {"prompt": workflow, "client_id": client_id}
        try:
            result = await self._transport.post_json("/prompt", payload, timeout=timeout and _client_timeout(self.config, timeout) or None)
        except ComfyUIError as exc:
            if exc.status == 400:
                raise self._format_validation_error(exc, workflow, body=exc.detail) from exc
            if exc.status_unknown or exc.kind == ErrorKind.TIMEOUT:
                raise ComfyUIQueueSubmitUnknownError(
                    "工作流已提交但结果状态不明（连接在响应前中断/超时）。"
                    "无法确认 ComfyUI 是否已接收任务，因此不自动重试；"
                    "请稍后在队列/历史中查询该 client_id 或 prompt_id。",
                    kind=ErrorKind.QUEUE_SUBMIT_UNKNOWN,
                    detail=str(exc),
                    cause=exc,
                ) from exc
            raise
        if not isinstance(result, dict) or not str(result.get("prompt_id") or "").strip():
            raise ComfyUIQueueSubmitUnknownError(
                "ComfyUI 未返回 prompt_id（响应格式异常），无法确认任务是否入队，不自动重试",
                kind=ErrorKind.QUEUE_SUBMIT_UNKNOWN,
            )
        prompt_id = str(result["prompt_id"]).strip()
        logger.info("[ComfyUIExecutor] 已提交工作流 client_id=%s prompt_id=%s", client_id, prompt_id)
        return prompt_id

    def _format_validation_error(
        self, exc: ComfyUIError, workflow: Mapping[str, Any], body: str = ""
    ) -> ComfyUIValidationError:
        """解析 400 prompt_outputs_failed_validation → 缺模型提示（相对目录）。"""
        detail = str(body or exc.detail or "")
        missing: List[str] = []
        try:
            data = json.loads(detail)
            node_errors = (data or {}).get("node_errors", {}) or {}
            for node_id, payload in (node_errors or {}).items():
                node = workflow.get(str(node_id), {}) if isinstance(workflow, dict) else {}
                class_type = str((node or {}).get("class_type") or "")
                errors = payload.get("errors") if isinstance(payload, dict) else []
                if not isinstance(errors, list):
                    continue
                for item in errors:
                    if not isinstance(item, dict) or item.get("type") != "value_not_in_list":
                        continue
                    text = str(item.get("details") or item.get("message") or "")
                    for field, filename in re.findall(r"([A-Za-z0-9_]+_name)\s*:\s*'([^']+)'", text):
                        hint = MODEL_DIR_HINTS.get(field.lower()) or MODEL_DIR_HINTS.get(class_type.lower(), "")
                        # 只展示 basename，绝不把服务器端绝对路径带回 UI
                        basename = str(filename).replace("\\", "/").split("/")[-1] or str(filename)
                        entry = f"{basename}"
                        if hint:
                            entry = f"{basename}（应位于 ComfyUI/{hint}）"
                        missing.append(entry)
        except Exception:
            pass
        if missing:
            message = "ComfyUI 工作流校验失败：缺少模型文件\n" + "\n".join(dict.fromkeys(missing))
        else:
            message = "ComfyUI 工作流校验失败（HTTP 400），请检查节点输入"
        return ComfyUIValidationError(
            message, kind=ErrorKind.VALIDATION, status=400, detail=detail[:4000]
        )

    async def resume(self, prompt_id: str, snapshot: ExecutionSnapshot) -> ComfyUIResult:
        """已有 prompt_id 时恢复：只轮询历史/队列并下载结果，不重新提交。"""
        return await self.wait_for_completion(prompt_id, snapshot, resumed=True)

    # ------------------------------------------------------------------
    # 轮询
    # ------------------------------------------------------------------
    async def fetch_history(self, prompt_id: str) -> Dict[str, Any]:
        data = await self._transport.get_json(f"/history/{prompt_id}")
        if not isinstance(data, dict):
            return {}
        if prompt_id in data:
            return data
        if any(key in data for key in ("outputs", "status", "prompt")):
            return {prompt_id: data}
        return data

    async def wait_for_completion(
        self, prompt_id: str, snapshot: ExecutionSnapshot, *, resumed: Optional[bool] = None
    ) -> ComfyUIResult:
        """轮询 /history + /queue 直至完成，下载全部输出图片。

        - history 为空且队列中也找不到 → 宽限期后连续 N 次确认才判丢失；
        - status_str == error → EXECUTION_ERROR（含节点级详情）；
        - 轮询耗尽 → GENERATION_TIMEOUT。
        """
        started = time.perf_counter()
        resumed = bool(snapshot.resume_prompt_id) if resumed is None else resumed
        meta: Dict[str, Any] = {
            "prompt_id": prompt_id,
            "resumed": resumed,
        }
        if snapshot.prompt:
            meta["prompt"] = snapshot.prompt[:2000]
        options = snapshot.options or {}
        poll_attempts = max(int(options.get("poll_attempts", 300)), 1)
        interval_base = max(float(options.get("poll_interval_base", 0.8)), 0.05)
        interval_max = max(float(options.get("poll_interval_max", 10.0)), interval_base)
        grace = max(float(options.get("queue_missing_grace_seconds", 30.0)), 0.0)
        confirmations = max(int(options.get("queue_missing_confirmations", 3)), 1)
        decode_check = bool(options.get("decode_check", True))
        image_type = str(options.get("image_type") or "output")
        submitted_at = time.time()

        missing_seen = 0
        for poll_index in range(poll_attempts):
            history = {}
            history_ok = True
            try:
                history = await self.fetch_history(prompt_id)
            except ComfyUIError as exc:
                history_ok = False
                meta["history_poll_last_error"] = exc.user_message
                logger.warning("[ComfyUIExecutor] history 轮询失败(暂不致命): %s", exc.user_message)
                if exc.kind in (ErrorKind.AUTH, ErrorKind.TLS):
                    return self._result_error(
                        exc, meta, started, snapshot, resume=resumed
                    )

            if history_ok and prompt_id in history:
                missing_seen = 0
                history_item = history.get(prompt_id) or {}
                status = history_item.get("status", {}) or {}
                status_str = str(status.get("status_str") or "")
                if status_str == "error":
                    detail = self._format_history_error(history_item)
                    if detail:
                        meta["execution_error"] = detail
                    error = ComfyUIExecutionError(
                        "ComfyUI 工作流执行失败：" + (detail or "请查看 ComfyUI 控制台日志"),
                        kind=ErrorKind.EXECUTION_ERROR,
                        detail=detail,
                    )
                    return self._result_error(error, meta, started, snapshot, resume=resumed)
                outputs = history_item.get("outputs", {}) or {}
                images = self._collect_output_images(outputs, snapshot.output_node_id)
                if not images:
                    images = self._collect_output_images({"history": history_item}, None)
                if images:
                    try:
                        downloaded = await self._download_images(
                            images, meta, decode_check=decode_check, image_type=image_type
                        )
                    except ComfyUIError as exc:
                        return self._result_error(exc, meta, started, snapshot, resume=resumed)
                    if downloaded:
                        meta["elapsed_seconds"] = time.perf_counter() - started
                        meta["poll_attempts_used"] = poll_index + 1
                        return ComfyUIResult(
                            ok=True,
                            images=downloaded,
                            prompt_id=prompt_id,
                            meta=meta,
                            warnings=list(snapshot.options.get("warnings", ()) or ()),
                        )
            elif history_ok and time.time() - submitted_at >= grace:
                # 历史为空：检查队列，防止误判（提交后有宽限期）
                try:
                    queue = await self.fetch_queue()
                    meta["queue_running_count"] = queue.running
                    meta["queue_pending_count"] = queue.pending
                    if _queue_contains_prompt_id(
                        {"queue_running": queue.running_ids, "queue_pending": queue.pending_ids}, prompt_id
                    ):
                        missing_seen = 0
                    else:
                        missing_seen += 1
                        if missing_seen >= confirmations:
                            meta["queue_prompt_missing"] = True
                            meta["queue_missing_confirmations_required"] = confirmations
                            error = ComfyUIPromptLostError(
                                "ComfyUI 远端队列与历史中都找不到本次 prompt_id，"
                                "任务可能已被 ComfyUI 清空/重启/节点刷新打断；"
                                "请确认 ComfyUI 控制台状态后重试。",
                                kind=ErrorKind.PROMPT_LOST,
                                detail=f"missing={missing_seen}/{confirmations}",
                            )
                            return self._result_error(error, meta, started, snapshot, resume=resumed)
                except ComfyUIError as exc:
                    logger.debug("[ComfyUIExecutor] queue 轮询失败: %s", exc.user_message)
                    meta["queue_poll_last_error"] = exc.user_message

            if poll_index + 1 < poll_attempts:
                await asyncio.sleep(self._poll_delay(poll_index, interval_base, interval_max))

        error = ComfyUIGenerationTimeoutError(
            f"生成超时（{poll_attempts} 次轮询后仍未完成）；任务可能仍在 ComfyUI 队列中运行，"
            "可用已有 prompt_id 恢复轮询。",
            kind=ErrorKind.GENERATION_TIMEOUT,
        )
        return self._result_error(error, meta, started, snapshot, resume=resumed)

    def _poll_delay(self, poll_index: int, base: float, cap: float) -> float:
        delay = base * (1.35 ** min(poll_index, 8))
        delay = min(delay, cap)
        return delay + (time.monotonic() % 0.2)

    def _collect_output_images(
        self, outputs: Mapping[str, Any], output_node_id: Optional[str]
    ) -> List[Dict[str, Any]]:
        """收集所有输出图片（优先 output_node_id 节点）。"""
        if not isinstance(outputs, Mapping):
            return []
        node_id = str(output_node_id or "").strip()
        ordered: List[Any] = []
        preferred_key: Any = None
        if node_id:
            preferred_key = next((key for key in outputs if str(key) == node_id), None)
            if preferred_key is not None:
                ordered.append(outputs.get(preferred_key))
        ordered.extend(value for key, value in outputs.items() if key != preferred_key)

        out: List[Dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for node_out in ordered:
            if not isinstance(node_out, Mapping):
                continue
            containers = [node_out]
            ui = node_out.get("ui")
            if isinstance(ui, Mapping):
                containers.append(ui)
            for container in containers:
                for field in _HISTORY_IMAGE_FIELDS:
                    raw_items = container.get(field)
                    if raw_items is None:
                        continue
                    if isinstance(raw_items, (str, Mapping)):
                        raw_items = [raw_items]
                    if not isinstance(raw_items, (list, tuple)):
                        continue
                    for raw in raw_items:
                        item = (
                            {"filename": raw}
                            if isinstance(raw, str)
                            else dict(raw)
                            if isinstance(raw, Mapping)
                            else {}
                        )
                        filename = str(item.get("filename") or "").strip()
                        if not filename:
                            continue
                        subfolder = str(item.get("subfolder") or "").strip()
                        image_type = str(item.get("type") or "output").strip().lower()
                        marker = (filename, subfolder, image_type)
                        if marker in seen:
                            continue
                        seen.add(marker)
                        out.append(item)
        return out

    async def _download_images(
        self,
        images: List[Dict[str, Any]],
        meta: Dict[str, Any],
        *,
        decode_check: bool,
        image_type: str,
    ) -> List[bytes]:
        downloaded: List[bytes] = []
        failures: List[ComfyUIError] = []
        for img in images:
            try:
                filename = sanitize_comfy_name(str(img.get("filename") or ""))
                subfolder = str(img.get("subfolder") or "").strip()
                subfolder = sanitize_comfy_name(subfolder, allow_subfolder=True) if subfolder else ""
                ref_type = str(img.get("type") or image_type or "output").strip().lower()
                if ref_type not in _COMFY_VIEW_TYPES:
                    ref_type = "output"
                params = {
                    "filename": filename,
                    "subfolder": subfolder,
                    "type": ref_type,
                }
                data = await self._transport.get_bytes("/view", params=params)
                if decode_check:
                    self._verify_image_bytes(data, label=filename)
                downloaded.append(data)
                meta.setdefault("image_filenames", []).append(filename)
            except ComfyUIError as exc:
                if exc.kind in (ErrorKind.AUTH, ErrorKind.TLS):
                    raise
                failures.append(exc)
                logger.warning(
                    "[ComfyUIExecutor] skipped unusable history image reference (%s)",
                    exc.kind.value,
                )
        if failures:
            meta["image_download_failures"] = len(failures)
        if not downloaded:
            if failures:
                raise failures[-1]
            raise ComfyUIImageDecodeError("输出节点没有可下载的图片", kind=ErrorKind.IMAGE_DECODE)
        return downloaded

    def _format_history_error(self, history_item: Mapping[str, Any]) -> str:
        """把 history error 的 node_errors 格式化为可读文本（不暴露绝对路径）。"""
        status = history_item.get("status", {}) or {}
        messages: List[str] = []
        if isinstance(status.get("messages"), list):
            for msg in status["messages"]:
                if not isinstance(msg, list) or len(msg) < 2:
                    continue
                payload = msg[1] if isinstance(msg[1], dict) else {}
                text = str(payload.get("message") or payload.get("exception_message") or "").strip()
                if text and text not in messages:
                    messages.append(text)
        node_errors = status.get("node_errors") if isinstance(status, dict) else None
        if isinstance(node_errors, dict):
            for node_id, payload in node_errors.items():
                if not isinstance(payload, dict):
                    continue
                errors = payload.get("errors")
                if not isinstance(errors, list):
                    continue
                for item in errors:
                    if not isinstance(item, dict):
                        continue
                    text = str(item.get("message") or item.get("details") or "").strip()
                    class_type = str(payload.get("class_type") or "")
                    if text:
                        messages.append(f"节点 {node_id} [{class_type}]: {text}")
        return "；".join(messages)[:3000]

    # ------------------------------------------------------------------
    # 完整管线
    # ------------------------------------------------------------------
    async def run(self, snapshot: ExecutionSnapshot) -> ComfyUIResult:
        """完整执行：上传（如有）→ 提交（或恢复）→ 轮询 → 结果。

        输入只来自不可变 snapshot；本方法不读取/写入任何共享可变状态。
        """
        started = time.perf_counter()
        if not isinstance(snapshot.workflow, dict) or not snapshot.workflow:
            error = ComfyUIError("工作流为空或格式非法", kind=ErrorKind.VALIDATION)
            return self._result_error(error, {}, started, snapshot)
        options = snapshot.options or {}
        meta: Dict[str, Any] = {"workflow_node_count": len(snapshot.workflow)}

        # 1) 节点预检（可关闭）
        if options.get("validate_nodes", True):
            try:
                object_info = await self.fetch_object_info()
                missing = self.validate_workflow_nodes(snapshot.workflow, object_info)
                if missing:
                    error = ComfyUIMissingNodeError(
                        "ComfyUI 缺少工作流需要的节点（请安装对应自定义节点并重启 ComfyUI）:\n"
                        + ", ".join(missing),
                        kind=ErrorKind.MISSING_NODE,
                        detail=";".join(missing),
                    )
                    return self._result_error(error, meta, started, snapshot)
            except ComfyUIError as exc:
                meta["validate_nodes_warning"] = exc.user_message

        # 2) 上传图生图源图（如有；恢复任务时工作流已引用已上传文件，不重复上传）
        upload_info: Optional[UploadedImage] = None
        resume_prompt_id = str(snapshot.resume_prompt_id or "").strip() or None
        if (snapshot.source_image or snapshot.source_image_bytes) and not resume_prompt_id:
            try:
                upload_info = await self.upload_image(
                    snapshot.source_image_bytes if snapshot.source_image_bytes is not None else snapshot.source_image,
                    subfolder=snapshot.upload_subfolder,
                    verify=bool(options.get("verify_upload_image", True)),
                )
                meta["uploaded_image"] = {
                    "name": upload_info.name,
                    "subfolder": upload_info.subfolder,
                }
            except ComfyUIError as exc:
                return self._result_error(exc, meta, started, snapshot)

        # 3) 提交或恢复
        client_id = str(snapshot.client_id or f"winefox_{uuid.uuid4().hex}")
        meta["client_id"] = client_id
        prompt_id = resume_prompt_id
        if prompt_id:
            meta["resumed"] = True
            logger.info("[ComfyUIExecutor] 恢复已有 prompt_id=%s（不重复提交）", prompt_id)
        else:
            try:
                prompt_id = await self.submit(snapshot.workflow, client_id=client_id)
            except ComfyUIError as exc:
                return self._result_error(exc, meta, started, snapshot)
        if not prompt_id:
            return self._result_error(
                ComfyUIQueueSubmitUnknownError("未能获得 prompt_id", kind=ErrorKind.QUEUE_SUBMIT_UNKNOWN),
                meta, started, snapshot,
            )

        # 4) 轮询等待
        wait_snapshot = snapshot.clone_with(resume_prompt_id=prompt_id)
        result = await self.wait_for_completion(
            prompt_id, wait_snapshot, resumed=bool(snapshot.resume_prompt_id)
        )
        result.meta.setdefault("client_id", client_id)
        result.meta.setdefault("workflow_node_count", len(snapshot.workflow))
        if upload_info is not None:
            result.meta.setdefault("uploaded_image", meta["uploaded_image"])
        return result

    # ------------------------------------------------------------------
    # 结果组装
    # ------------------------------------------------------------------
    def _result_error(
        self,
        error: ComfyUIError,
        meta: Dict[str, Any],
        started: float,
        snapshot: ExecutionSnapshot,
        *,
        resume: bool = False,
    ) -> ComfyUIResult:
        meta = dict(meta)
        meta["elapsed_seconds"] = time.perf_counter() - started
        meta.setdefault("prompt_id", snapshot.resume_prompt_id)
        logger.warning(
            "[ComfyUIExecutor] 执行失败 kind=%s resume=%s: %s",
            error.kind.value, resume, error.user_message,
        )
        return ComfyUIResult(ok=False, prompt_id=meta.get("prompt_id"), error=error, meta=meta)


def _client_timeout(config: ComfyUIServerConfig, total: float) -> Any:
    """按配置构造 aiohttp.ClientTimeout（connect/read/total 三分量）。"""
    import aiohttp

    return aiohttp.ClientTimeout(
        total=max(float(total), 0.0),
        connect=max(config.timeout_connect, 0.0),
        sock_connect=max(config.timeout_connect, 0.0),
        sock_read=max(config.timeout_read, 0.0),
    )

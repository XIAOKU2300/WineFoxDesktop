# -*- coding: utf-8 -*-
"""Fake ComfyUI HTTP server (test double) for WineFox Desktop integration tests.

Implements the subset of the ComfyUI HTTP API that the plugin client
(comfyui_api.ComfyUI) actually consumes:

    GET  /object_info
    GET  /models/loras
    POST /upload/image
    POST /prompt
    GET  /queue
    GET  /history/{prompt_id}
    GET  /view?filename=...&subfolder=...&type=...
    POST /free

Fault modes (switchable at runtime through ``POST /__test/mode``):

    normal           - happy path
    auth_401         - every endpoint answers 401 {"error": "unauthorized"}
    connection_drop  - /prompt closes the connection without a response
    missing_node     - /prompt always answers 400 prompt_outputs_failed_validation
    corrupt_image    - /view returns 200 with non-image garbage bytes
    view_500         - /view returns 500 (download retry path)
    queue_lost       - /history and /queue stay empty after a prompt was accepted
    exec_error       - /history reports status_str == "error" after execution

Object info is derived from the *real* workflow/*.json files of the plugin, so
validation exercised by the tests uses the same node classes as production.
Loopback only. No models, no downloads, no third-party calls.
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import struct
import time
import uuid
import zlib
from pathlib import Path
from typing import Dict, List, Optional

from aiohttp import web

MODES = {
    "normal",
    "auth_401",
    "connection_drop",
    "missing_node",
    "corrupt_image",
    "view_500",
    "queue_lost",
    "exec_error",
}

DEFAULT_FAKE_LORAS = [
    "winefox_test_lora_a.safetensors",
    "winefox_test_lora_b.safetensors",
    "winefox_test_style_c.safetensors",
]


def _png_bytes(width: int = 48, height: int = 48, seed: int = 1) -> bytes:
    """Deterministic, small, *non-blank* PNG (gradient + noise) built with zlib.

    No Pillow dependency. The plugin's blank/black-screen guard must NOT flag
    this image, so the gradient uses many distinct pixel values.
    """
    rows = []
    for y in range(height):
        row = bytearray([0])  # filter type 0
        for x in range(width):
            r = (x * 7 + y * 3 + seed) % 256
            g = (x * 3 + y * 11 + seed * 2) % 256
            b = (x * 13 + y * 5 + seed * 3) % 256
            row += bytes((r, g, b))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


class FakeComfyUI:
    """aiohttp application implementing a small ComfyUI-compatible surface."""

    def __init__(
        self,
        workflow_root: Path,
        mode: str = "normal",
        max_upload_bytes: int = 20 * 1024 * 1024,
        exec_delay: float = 0.05,
        latency: float = 0.0,
        timeout_sleep: float = 60.0,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown fake mode: {mode!r}")
        self.workflow_root = Path(workflow_root)
        self.mode = mode
        self.max_upload_bytes = int(max_upload_bytes)
        self.exec_delay = float(exec_delay)
        self.latency = float(latency)
        self.timeout_sleep = float(timeout_sleep)
        self.base_url = ""
        self.app = web.Application()
        self._register_routes()

        # state
        self.object_info: Dict[str, dict] = {}
        self.loras: List[str] = list(DEFAULT_FAKE_LORAS)
        self.tasks: Dict[str, dict] = {}
        self.uploads: List[dict] = []
        self.queue_running: List[dict] = []
        self.queue_pending: List[dict] = []
        self.stats = {
            "object_info": 0,
            "models_loras": 0,
            "upload_image": 0,
            "prompt": 0,
            "queue": 0,
            "history": 0,
            "view": 0,
            "free": 0,
        }
        self._build_object_info()

    # ------------------------------------------------------------------ setup
    def _register_routes(self) -> None:
        self.app.router.add_get("/object_info", self._handle_object_info)
        self.app.router.add_get("/models/loras", self._handle_models_loras)
        self.app.router.add_post("/upload/image", self._handle_upload_image)
        self.app.router.add_post("/prompt", self._handle_prompt)
        self.app.router.add_get("/queue", self._handle_queue)
        self.app.router.add_get("/history/{prompt_id}", self._handle_history)
        self.app.router.add_get("/view", self._handle_view)
        self.app.router.add_post("/free", self._handle_free)
        self.app.router.add_post("/__test/mode", self._handle_set_mode)
        self.app.router.add_post("/__test/reset", self._handle_reset)

    def _build_object_info(self) -> None:
        """Derive object_info from the plugin's real workflow files.

        Every class_type that appears in any workflow/*.json becomes a node
        class. Inputs that are lists (COMBO-style) get the fake lora names, so
        validation of lora fields passes in normal mode.
        """
        collected: Dict[str, dict] = {}
        files = sorted((self.workflow_root / "workflow").glob("*.json")) if (self.workflow_root / "workflow").exists() else []
        for path in files:
            try:
                wf = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(wf, dict):
                continue
            for node in wf.values():
                if not isinstance(node, dict) or not node.get("class_type"):
                    continue
                class_type = str(node["class_type"])
                inputs = node.get("inputs") or {}
                entry = collected.setdefault(
                    class_type,
                    {"input": {"required": {}, "optional": {}}, "output": [], "output_name": [], "name": class_type},
                )
                for name, value in inputs.items():
                    if isinstance(value, list):
                        entry["input"]["required"][name] = [list(self.loras)]
                    elif name.lower() in ("ckpt_name", "model_name", "lora_name", "vae_name", "clip_name"):
                        entry["input"]["required"][name] = [["winefox_test_checkpoint.safetensors"]]
                    else:
                        entry["input"]["required"][name] = [type(value).__name__]
        # A few classes must always exist even if no workflow references them.
        collected.setdefault(
            "KSampler",
            {"input": {"required": {}}, "output": [], "output_name": [], "name": "KSampler"},
        )
        collected.setdefault(
            "LoraLoader",
            {
                "input": {
                    "required": {
                        "model": ["MODEL"],
                        "clip": ["CLIP"],
                        "lora_name": [list(self.loras)],
                        "strength_model": ["FLOAT"],
                        "strength_clip": ["FLOAT"],
                    }
                },
                "output": ["MODEL", "CLIP"],
                "output_name": ["MODEL", "CLIP"],
                "name": "LoraLoader",
            },
        )
        collected.setdefault(
            "CLIPTextEncode",
            {"input": {"required": {"text": ["STRING"], "clip": ["CLIP"]}}, "output": ["CONDITIONING"], "output_name": ["CONDITIONING"], "name": "CLIPTextEncode"},
        )
        collected.setdefault(
            "SaveImage",
            {"input": {"required": {"images": ["IMAGE"], "filename_prefix": ["STRING"]}}, "output": [], "output_name": [], "name": "SaveImage"},
        )
        self.object_info = collected

    # ------------------------------------------------------------ test control
    async def _handle_set_mode(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad json"}, status=400)
        mode = str(data.get("mode") or "").strip()
        if mode not in MODES:
            return web.json_response({"ok": False, "error": f"unknown mode {mode!r}"}, status=400)
        self.mode = mode
        return web.json_response({"ok": True, "mode": self.mode})

    async def _handle_reset(self, request: web.Request) -> web.Response:
        self.tasks.clear()
        self.uploads.clear()
        self.queue_running.clear()
        self.queue_pending.clear()
        for key in self.stats:
            self.stats[key] = 0
        return web.json_response({"ok": True})

    # ---------------------------------------------------------------- helpers
    async def _maybe_401(self) -> None:
        if self.mode == "auth_401":
            raise web.HTTPUnauthorized(text=json.dumps({"error": "unauthorized"}), content_type="application/json")

    async def _maybe_latency(self) -> None:
        if self.latency > 0:
            await asyncio.sleep(self.latency)

    def _prompt_id(self) -> str:
        return uuid.uuid4().hex

    def _validate_prompt(self, workflow: dict) -> Optional[str]:
        """Mirror ComfyUI prompt validation. Returns None when valid."""
        if self.mode == "missing_node":
            return json.dumps(
                {
                    "error": "prompt_outputs_failed_validation",
                    "node_errors": {
                        "1": {
                            "errors": [
                                ["value_not_in_list", {"value": "MissingNodeX", "info": "class_type"}]
                            ]
                        }
                    },
                }
            )
        if not isinstance(workflow, dict):
            return json.dumps({"error": "prompt_outputs_failed_validation", "node_errors": {}})
        for node_id, node in workflow.items():
            if not isinstance(node, dict) or not node.get("class_type"):
                continue
            class_type = str(node["class_type"])
            if class_type not in self.object_info:
                return json.dumps(
                    {
                        "error": "prompt_outputs_failed_validation",
                        "node_errors": {
                            str(node_id): {
                                "errors": [["unknown_class_type", {"value": class_type}]]
                            }
                        },
                    }
                )
        return None

    # --------------------------------------------------------------- handlers
    async def _handle_object_info(self, request: web.Request) -> web.Response:
        await self._maybe_401()
        await self._maybe_latency()
        self.stats["object_info"] += 1
        return web.json_response(self.object_info)

    async def _handle_models_loras(self, request: web.Request) -> web.Response:
        await self._maybe_401()
        await self._maybe_latency()
        self.stats["models_loras"] += 1
        return web.json_response(self.loras)

    async def _handle_upload_image(self, request: web.Request) -> web.Response:
        await self._maybe_401()
        await self._maybe_latency()
        reader = await request.multipart()
        name = ""
        subfolder = ""
        overwrite = "true"
        body = b""
        while True:
            part = await reader.next()
            if part is None:
                break
            field = str(part.name or "")
            if field == "image":
                raw = b""
                while True:
                    chunk = await part.read_chunk(64 * 1024)
                    if not chunk:
                        break
                    raw += chunk
                    if len(raw) > self.max_upload_bytes:
                        return web.json_response(
                            {"error": f"upload exceeds {self.max_upload_bytes} bytes"},
                            status=413,
                        )
                body = raw
                name = str(part.filename or "upload.png")
            else:
                data = await part.read()
                value = data.decode("utf-8", "replace")
                if field == "type":
                    pass
                elif field == "overwrite":
                    overwrite = value
                elif field == "subfolder":
                    subfolder = value
        if not body:
            return web.json_response({"error": "no image part"}, status=400)
        # path traversal guard (aiohttp percent-encodes filenames, so unquote first)
        from urllib.parse import unquote

        decoded = unquote(name)
        if (
            ".." in decoded
            or decoded.startswith("/")
            or decoded.startswith("\\")
            or "/" in decoded
            or "\\" in decoded
            or ":" in decoded
        ):
            return web.json_response({"error": "invalid filename"}, status=400)
        if not name:
            name = "upload.png"
        record = {
            "name": name,
            "subfolder": subfolder,
            "type": "input",
            "overwrite": overwrite,
            "size": len(body),
            "sha256": zlib.crc32(body) & 0xFFFFFFFF,
        }
        self.uploads.append(record)
        self.stats["upload_image"] += 1
        return web.json_response({"name": name, "subfolder": subfolder, "type": "input"})

    async def _handle_prompt(self, request: web.Request) -> web.Response:
        await self._maybe_401()
        if self.mode == "connection_drop":
            # simulate a dropped connection: abort the TCP transport without a
            # response, so the client observes a disconnect, not an HTTP status.
            transport = request.transport
            if transport is not None:
                transport.abort()
            await asyncio.sleep(0.05)
            return web.Response(status=503)  # never delivered after abort
        await self._maybe_latency()
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        workflow = payload.get("prompt") if isinstance(payload, dict) else None
        validation_error = self._validate_prompt(workflow)
        if validation_error:
            return web.json_response(json.loads(validation_error), status=400)
        prompt_id = self._prompt_id()
        self.tasks[prompt_id] = {
            "prompt_id": prompt_id,
            "submitted_at": time.time(),
            "workflow": workflow,
            "client_id": payload.get("client_id", ""),
            "status_str": "pending",
            "outputs": {},
            "error": "",
        }
        self.queue_pending.append({"prompt_id": prompt_id, "client_id": payload.get("client_id", "")})
        self.stats["prompt"] += 1
        asyncio.get_running_loop().create_task(self._finish_task(prompt_id))
        return web.json_response({"prompt_id": prompt_id, "number": self.stats["prompt"], "node_errors": {}})

    async def _finish_task(self, prompt_id: str) -> None:
        if self.exec_delay > 0:
            await asyncio.sleep(self.exec_delay)
        if prompt_id not in self.tasks:
            return
        task = self.tasks[prompt_id]
        self.queue_pending = [t for t in self.queue_pending if t.get("prompt_id") != prompt_id]
        if self.mode == "queue_lost":
            # history entry is deliberately dropped -> client must recover
            self.tasks.pop(prompt_id, None)
            return
        self.queue_running = [t for t in self.queue_running if t.get("prompt_id") != prompt_id]
        if self.mode == "exec_error":
            task["status_str"] = "error"
            task["error"] = "fake execution error: CUDA out of memory (fake)"
            return
        task["status_str"] = "success"
        task["outputs"] = {
            "9": {
                "images": [
                    {
                        "filename": f"ComfyUI_test_{prompt_id[:8]}.png",
                        "subfolder": "",
                        "type": "output",
                    }
                ]
            }
        }

    async def _handle_queue(self, request: web.Request) -> web.Response:
        await self._maybe_401()
        await self._maybe_latency()
        self.stats["queue"] += 1
        return web.json_response(
            {"queue_running": self.queue_running, "queue_pending": self.queue_pending}
        )

    async def _handle_history(self, request: web.Request) -> web.Response:
        await self._maybe_401()
        await self._maybe_latency()
        self.stats["history"] += 1
        prompt_id = request.match_info.get("prompt_id", "")
        task = self.tasks.get(prompt_id)
        if not task:
            return web.json_response({})
        if task["status_str"] == "pending":
            return web.json_response({})
        if task["status_str"] == "error":
            return web.json_response(
                {
                    prompt_id: {
                        "status": {
                            "status_str": "error",
                            "completed": True,
                            "messages": [
                                [
                                    "execution_error",
                                    {
                                        "node_id": "8",
                                        "node_type": "VAEDecode",
                                        "exception_type": "RuntimeError",
                                        "exception_message": task["error"],
                                    },
                                ]
                            ],
                        },
                        "outputs": {},
                    }
                }
            )
        return web.json_response(
            {
                prompt_id: {
                    "status": {"status_str": "success", "completed": True, "messages": []},
                    "outputs": task["outputs"],
                }
            }
        )

    async def _handle_view(self, request: web.Request) -> web.Response:
        await self._maybe_401()
        await self._maybe_latency()
        self.stats["view"] += 1
        filename = str(request.query.get("filename", "") or "")
        subfolder = str(request.query.get("subfolder", "") or "")
        if ".." in filename or ".." in subfolder or filename.startswith("/"):
            return web.json_response({"error": "invalid filename"}, status=400)
        if self.mode == "corrupt_image":
            return web.Response(body=b"this is not a png - corrupted bytes", content_type="application/octet-stream")
        if self.mode == "view_500":
            return web.json_response({"error": "fake view failure"}, status=500)
        return web.Response(body=_png_bytes(), content_type="image/png")

    async def _handle_free(self, request: web.Request) -> web.Response:
        await self._maybe_401()
        self.stats["free"] += 1
        return web.json_response({})

    # ------------------------------------------------------------ server mgmt
    async def start(self) -> str:
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self._runner = runner
        port = runner.addresses[0][1] if runner.addresses else 0
        self.base_url = f"http://127.0.0.1:{port}"
        return self.base_url

    async def stop(self) -> None:
        runner = getattr(self, "_runner", None)
        if runner is not None:
            await runner.cleanup()

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown fake mode: {mode!r}")
        self.mode = mode

    def snapshot(self) -> dict:
        return {
            "mode": self.mode,
            "stats": dict(self.stats),
            "uploads": list(self.uploads),
            "task_count": len(self.tasks),
        }


async def start_fake_comfyui(workflow_root: Path, mode: str = "normal", **kwargs) -> FakeComfyUI:
    """Start a fake ComfyUI on 127.0.0.1:<ephemeral> and return the instance."""
    server = FakeComfyUI(workflow_root=Path(workflow_root), mode=mode, **kwargs)
    await server.start()
    return server

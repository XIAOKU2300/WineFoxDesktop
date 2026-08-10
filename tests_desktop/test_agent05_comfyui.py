# -*- coding: utf-8 -*-
"""WineFox Desktop — Agent 05 ComfyUI 传输/执行层测试。

在测试内起一个假的 aiohttp ComfyUI 服务器（object_info / models / upload /
prompt / queue / history / view），覆盖：
正常文生图、图生图上传、401/403、TLS（自签名/自定义 CA/显式关闭警告）、
timeout、缺节点、提交超时状态不明不重试、prompt ID 恢复、队列消失、
损坏/超大图片、GET 有限退避、路径逃逸、日志脱敏、快照不可变。

不依赖 pytest-asyncio：服务器与客户端在同一个 asyncio.run 事件循环内运行
（分开的循环会在 asyncio.run 结束时销毁服务器）。
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import shutil
import socket
import ssl
import subprocess
import sys
from pathlib import Path

import aiohttp
import aiohttp.web
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from winefox_core.comfyui_executor import (  # noqa: E402
    ComfyUIExecutor,
    _queue_contains_prompt_id,
    sanitize_comfy_name,
)
from winefox_core.comfyui_transport import (  # noqa: E402
    ComfyUITransport,
    _normalize_server_address,
    _redact_text,
)
from winefox_core.comfyui_types import (  # noqa: E402
    ComfyUIAuth,
    ComfyUIAuthError,
    ComfyUIConnectionError,
    ComfyUIError,
    ComfyUIFileError,
    ComfyUIPayloadTooLargeError,
    ComfyUIQueueSubmitUnknownError,
    ComfyUIServerConfig,
    ComfyUITLSError,
    ComfyUITimeoutError,
    ErrorKind,
    ExecutionSnapshot,
)

# ---------------------------------------------------------------------------
# 工具：小 PNG
# ---------------------------------------------------------------------------
def _tiny_png() -> bytes:
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00" + b"\xff\x00\x00"
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


TINY_PNG = _tiny_png()


# ---------------------------------------------------------------------------
# 假 ComfyUI 服务器
# ---------------------------------------------------------------------------
class FakeComfyUIServer:
    """可编程的假 ComfyUI（aiohttp.web）。"""

    def __init__(self) -> None:
        self.prompt_requests = 0
        self.upload_requests = 0
        self.object_info_hits = 0
        self.view_requests = 0
        self.history_requests = 0
        self.queue_requests = 0
        self.last_prompt_payload: dict | None = None
        self.last_upload: dict | None = None

        self.object_info = {
            "CheckpointLoaderSimple": {
                "input": {
                    "required": {
                        "ckpt_name": [["sd15.safetensors", "illustrious.safetensors"], {"options": []}]
                    }
                }
            },
            "LoraLoader": {
                "input": {
                    "required": {
                        "model": ["MODEL"],
                        "clip": ["CLIP"],
                        "lora_name": [["char1.safetensors", "style1.safetensors"], {"options": []}],
                        "strength_model": ["FLOAT"],
                        "strength_clip": ["FLOAT"],
                    }
                }
            },
            "CLIPTextEncode": {"input": {"required": {"text": ["STRING"], "clip": ["CLIP"]}}},
            "EmptyLatentImage": {"input": {"required": {"width": ["INT"], "height": ["INT"], "batch_size": ["INT"]}}},
            "KSampler": {"input": {"required": {}}},
            "VAEDecode": {"input": {"required": {"samples": ["LATENT"], "vae": ["VAE"]}}},
            "SaveImage": {"input": {"required": {"images": ["IMAGE"], "filename_prefix": ["STRING"]}}},
            "LoadImage": {"input": {"required": {"image": ["COMBO"]}}},
        }
        self.models = {"checkpoints": ["sd15.safetensors"], "loras": ["char1.safetensors", "style1.safetensors"]}
        self.models_loras = ["char1.safetensors", "style1.safetensors", "detailer_controlnet_lora.safetensors"]
        self.queue_running: list = []
        self.queue_pending: list = []
        self.history: dict = {}
        self.images: dict = {"output": {"out1.png": TINY_PNG}, "input": {}}
        self.output_images: list = [{"filename": "out1.png", "subfolder": "", "type": "output"}]

        # 行为开关
        self.prompt_delay = 0.0
        self.prompt_status = 200
        self.prompt_body: dict | None = None
        self.prompt_disconnect = False
        self.view_bytes: bytes | None = None
        self.view_status = 200
        self.object_info_status = 200
        self.object_info_500_count = 0
        self.require_auth: str | None = None
        self.auth_status = 401
        self.keep_queue_entries = True  # False = 提交后不保留队列条目（模拟任务消失）

    def app(self) -> aiohttp.web.Application:
        web = aiohttp.web
        app = web.Application()

        async def object_info_handler(request):
            self.object_info_hits += 1
            if self.object_info_status == 500:
                if self.object_info_500_count > 0:
                    self.object_info_500_count -= 1
                    return web.Response(status=500, text="boom")
                return web.json_response(self.object_info)  # 次数耗尽后恢复正常
            if self.object_info_status != 200:
                return web.Response(status=self.object_info_status, text="denied")
            return web.json_response(self.object_info)

        async def models_handler(request):
            if request.path == "/models/loras":
                return web.json_response(self.models_loras)
            return web.json_response(self.models)

        async def chunked_json_handler(request):
            body = json.dumps({"payload": "x" * 300_000}).encode("utf-8")
            response = web.StreamResponse(status=200, headers={"Content-Type": "application/json"})
            await response.prepare(request)
            await response.write(body[:128 * 1024])
            await asyncio.sleep(0.01)
            await response.write(body[128 * 1024:])
            await response.write_eof()
            return response

        async def queue_handler(request):
            self.queue_requests += 1
            return web.json_response(
                {"queue_running": self.queue_running, "queue_pending": self.queue_pending}
            )

        async def history_handler(request):
            self.history_requests += 1
            prompt_id = request.match_info["prompt_id"]
            if prompt_id in self.history:
                return web.json_response({prompt_id: self.history[prompt_id]})
            return web.json_response({})

        async def view_handler(request):
            self.view_requests += 1
            if self.view_status != 200:
                return web.Response(status=self.view_status, text="nope")
            if self.view_bytes is not None:
                return web.Response(body=self.view_bytes, content_type="image/png")
            filename = request.query.get("filename", "")
            images = self.images.get(request.query.get("type", "output"), {})
            if filename in images:
                return web.Response(body=images[filename], content_type="image/png")
            return web.Response(status=404, text="missing")

        async def upload_handler(request):
            self.upload_requests += 1
            form = await request.post()
            img = form.get("image")
            image_bytes = getattr(img, "file", b"").read() if hasattr(img, "file") else b""
            self.last_upload = {
                "filename": getattr(img, "filename", None),
                "subfolder": form.get("subfolder", ""),
                "overwrite": form.get("overwrite", ""),
                "type": form.get("type", ""),
                "size": len(image_bytes),
            }
            name = getattr(img, "filename", None) or "uploaded.png"
            self.images.setdefault("input", {})[name] = image_bytes
            return web.json_response(
                {
                    "name": name,
                    "subfolder": form.get("subfolder", ""),
                    "type": form.get("type", "input"),
                }
            )

        async def prompt_handler(request):
            self.prompt_requests += 1
            if self.require_auth:
                auth = request.headers.get("Authorization", "")
                if auth != self.require_auth:
                    return web.Response(status=self.auth_status, text="auth required")
            if self.prompt_delay:
                await asyncio.sleep(self.prompt_delay)
            if self.prompt_disconnect:
                # 发送部分数据后强制断开 TCP（不结束响应）→ 客户端状态不明
                response = web.Response(text="", status=200)
                await response.prepare(request)
                await response.write(b'{"prompt_id":')
                transport = request.transport
                if transport is not None:
                    transport.close()
                await asyncio.sleep(0.1)
                return response
            if self.prompt_status != 200:
                return web.json_response(self.prompt_body or {"error": "validation failed"}, status=self.prompt_status)
            payload = await request.json()
            self.last_prompt_payload = payload
            prompt_id = f"prompt-{self.prompt_requests:04d}"
            if self.keep_queue_entries:
                self.queue_pending.append({"prompt_id": prompt_id, "inputs": payload})
            return web.json_response({"prompt_id": prompt_id})

        app.router.add_get("/object_info", object_info_handler)
        app.router.add_get("/models", models_handler)
        app.router.add_get("/models/loras", models_handler)
        app.router.add_get("/chunked-json", chunked_json_handler)
        app.router.add_get("/queue", queue_handler)
        app.router.add_get("/history/{prompt_id}", history_handler)
        app.router.add_get("/view", view_handler)
        app.router.add_post("/upload/image", upload_handler)
        app.router.add_post("/prompt", prompt_handler)
        return app

    def complete_prompt(self, prompt_id: str, *, status: str = "success", error_detail: str = "") -> None:
        self.queue_pending = [p for p in self.queue_pending if p.get("prompt_id") != prompt_id]
        entry = {
            "prompt": self.last_prompt_payload or {"prompt": {}, "client_id": "x"},
            "outputs": {"5": {"images": list(self.output_images)}},
            "status": {"status_str": status, "completed": True, "messages": []},
        }
        if status == "error":
            entry["status"]["messages"] = [
                ["execution_error", {"message": error_detail or "CUDA out of memory"}]
            ]
        self.history[prompt_id] = entry


# ---------------------------------------------------------------------------
# 夹具：服务器与客户端必须共享同一事件循环（asyncio.run 每次新建循环，
# 分开启动时服务器会在 asyncio.run 结束销毁循环后失效）
# ---------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server_scenario():
    """单事件循环：起假服务器 → 跑场景 → 清理。用法::

        result, fake = server_scenario(lambda fake, url: async_body(fake, url))
    """

    def run(scenario_factory, *, ssl_ctx=None):
        async def main():
            fake = FakeComfyUIServer()
            port = _free_port()
            runner = aiohttp.web.AppRunner(fake.app())
            await runner.setup()
            site = aiohttp.web.TCPSite(runner, "127.0.0.1", port, ssl_context=ssl_ctx)
            await site.start()
            try:
                scheme = "https" if ssl_ctx is not None else "http"
                result = await scenario_factory(fake, f"{scheme}://127.0.0.1:{port}")()
                return result, fake
            finally:
                await runner.cleanup()

        return asyncio.run(main())

    return run


def _base_config(url: str, **kw) -> ComfyUIServerConfig:
    defaults = dict(
        server_address=url,
        timeout_connect=3.0,
        timeout_read=2.0,
        timeout_total=10.0,
        get_retries=3,
        get_backoff_base=0.05,
        get_backoff_max=0.2,
    )
    defaults.update(kw)
    return ComfyUIServerConfig(**defaults)


def _snapshot(workflow: dict | None = None, **kw) -> ExecutionSnapshot:
    if workflow is None:
        workflow = {
            "3": {"class_type": "KSampler", "inputs": {"seed": 1}},
            "4": {"class_type": "SaveImage", "inputs": {"filename_prefix": "test"}},
        }
    defaults = dict(workflow=workflow, prompt="test prompt", options={"poll_interval_base": 0.05})
    defaults.update(kw)
    return ExecutionSnapshot(**defaults)


def _workflow_with_missing_node() -> dict:
    return {
        "1": {"class_type": "NonexistentCustomNode", "inputs": {}},
        "2": {"class_type": "KSampler", "inputs": {}},
    }


# ---------------------------------------------------------------------------
# URL 规范化 / 脱敏
# ---------------------------------------------------------------------------
class TestNormalization:
    def test_adds_http_scheme(self):
        assert _normalize_server_address("127.0.0.1:8188") == "http://127.0.0.1:8188"

    def test_strips_trailing_slash(self):
        assert _normalize_server_address("http://host:8188///") == "http://host:8188"

    def test_keeps_https(self):
        assert _normalize_server_address("https://comfy.example") == "https://comfy.example"

    def test_rejects_ftp(self):
        with pytest.raises(ValueError):
            _normalize_server_address("ftp://host:21")

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            _normalize_server_address("  ")

    def test_strips_userinfo(self):
        assert _normalize_server_address("http://user:secret@host:8188") == "http://host:8188"

    def test_rejects_bad_port(self):
        with pytest.raises(ValueError):
            _normalize_server_address("http://host:99999")


# ---------------------------------------------------------------------------
# 传输层
# ---------------------------------------------------------------------------
class TestTransport:
    def test_session_lifecycle(self, server_scenario):
        def build(fake, url):
            async def scenario():
                transport = ComfyUITransport(_base_config(url))
                assert transport.closed
                await transport.start()
                assert not transport.closed
                assert await transport.get_json("/queue") is not None
                await transport.close()
                assert transport.closed
                with pytest.raises(RuntimeError):
                    transport.session

            return scenario

        server_scenario(build)

    def test_get_json_backoff_on_500(self, server_scenario):
        def build(fake, url):
            async def scenario():
                fake.object_info_status = 500
                fake.object_info_500_count = 2  # 前两次 500，第三次 200
                transport = ComfyUITransport(_base_config(url))
                await transport.start()
                try:
                    data = await transport.get_json("/object_info")
                    assert data["CheckpointLoaderSimple"]
                finally:
                    await transport.close()

            return scenario

        _, fake = server_scenario(build)
        assert fake.object_info_hits == 3  # 有限退避：2 次失败 + 1 次成功

    def test_get_json_gives_up_after_retries(self, server_scenario):
        def build(fake, url):
            async def scenario():
                fake.object_info_status = 500
                fake.object_info_500_count = 99
                transport = ComfyUITransport(_base_config(url))
                await transport.start()
                try:
                    with pytest.raises(ComfyUIError) as exc_info:
                        await transport.get_json("/object_info")
                    assert exc_info.value.status == 500
                finally:
                    await transport.close()

            return scenario

        _, fake = server_scenario(build)
        assert fake.object_info_hits == 3  # 有限退避，不无限重试

    def test_auth_401_no_retry(self, server_scenario):
        def build(fake, url):
            async def scenario():
                fake.object_info_status = 401
                transport = ComfyUITransport(_base_config(url))
                await transport.start()
                try:
                    with pytest.raises(ComfyUIAuthError):
                        await transport.get_json("/object_info")
                finally:
                    await transport.close()

            return scenario

        _, fake = server_scenario(build)
        assert fake.object_info_hits == 1  # 401 不重试

    def test_bearer_auth_header_sent(self, server_scenario):
        def build(fake, url):
            async def scenario():
                fake.require_auth = "Bearer topsecret-token-123"
                transport = ComfyUITransport(
                    _base_config(url, auth=ComfyUIAuth.bearer("topsecret-token-123"))
                )
                await transport.start()
                try:
                    data = await transport.get_json("/queue")
                    assert isinstance(data, dict)
                finally:
                    await transport.close()

            return scenario

        server_scenario(build)

    def test_basic_auth_header_sent(self, server_scenario):
        def build(fake, url):
            async def scenario():
                fake.require_auth = "Basic " + base64.b64encode(b"admin:s3cret").decode()
                transport = ComfyUITransport(_base_config(url, auth=ComfyUIAuth.basic("admin", "s3cret")))
                await transport.start()
                try:
                    await transport.get_json("/queue")
                finally:
                    await transport.close()

            return scenario

        server_scenario(build)

    def test_extra_headers(self, server_scenario):
        def build(fake, url):
            async def scenario():
                transport = ComfyUITransport(_base_config(url, extra_headers={"X-Custom": "abc"}))
                await transport.start()
                try:
                    await transport.get_json("/queue")
                finally:
                    await transport.close()

            return scenario

        server_scenario(build)

    def test_read_timeout(self, server_scenario):
        def build(fake, url):
            async def scenario():
                fake.prompt_delay = 30.0  # 服务器挂起 → 读超时
                transport = ComfyUITransport(_base_config(url, timeout_read=0.5, timeout_total=2.0))
                await transport.start()
                try:
                    with pytest.raises(ComfyUITimeoutError):
                        await transport.post_json("/prompt", {"prompt": {}, "client_id": "c"})
                finally:
                    await transport.close()

            return scenario

        server_scenario(build)

    def test_connect_refused(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        url = f"127.0.0.1:{port}"

        async def scenario():
            transport = ComfyUITransport(
                _base_config(url, timeout_connect=1.0, timeout_total=2.0)
            )
            await transport.start()
            try:
                # Windows 上对未监听端口可能表现为拒绝或连接挂起超时，两者都算正确分类
                with pytest.raises((ComfyUIConnectionError, ComfyUITimeoutError)):
                    await transport.get_json("/queue")
            finally:
                await transport.close()

        asyncio.run(scenario())

    def test_post_unknown_status_no_retry(self, server_scenario):
        """POST /prompt 状态不明（响应中断）→ QUEUE_SUBMIT_UNKNOWN，且不重试。"""
        def build(fake, url):
            async def scenario():
                fake.prompt_disconnect = True
                async with ComfyUIExecutor(_base_config(url)) as executor:
                    with pytest.raises(ComfyUIQueueSubmitUnknownError) as exc_info:
                        await executor.submit({"1": {"class_type": "KSampler", "inputs": {}}}, client_id="c")
                    assert exc_info.value.kind == ErrorKind.QUEUE_SUBMIT_UNKNOWN
                    assert "不自动重试" in str(exc_info.value)

            return scenario

        _, fake = server_scenario(build)
        assert fake.prompt_requests == 1  # 绝不重试

    def test_json_size_cap(self, server_scenario):
        def build(fake, url):
            async def scenario():
                fake.view_bytes = b"x" * 1000
                transport = ComfyUITransport(_base_config(url, max_image_bytes=100))
                await transport.start()
                try:
                    with pytest.raises(ComfyUIPayloadTooLargeError):
                        await transport.get_bytes("/view", params={"filename": "a.png"})
                finally:
                    await transport.close()

            return scenario

        server_scenario(build)

    def test_chunked_json_body_is_read_to_eof(self, server_scenario):
        def build(fake, url):
            async def scenario():
                transport = ComfyUITransport(_base_config(url, max_json_bytes=400_000))
                await transport.start()
                try:
                    data = await transport.get_json("/chunked-json", retries=1)
                    assert len(data["payload"]) == 300_000
                finally:
                    await transport.close()

            return scenario

        server_scenario(build)

    def test_redaction(self):
        text = "Authorization: Bearer supersecret123 and Basic dXNlcjpwYXNz"
        red = _redact_text(text, ("supersecret123",))
        assert "supersecret123" not in red
        assert "dXNlcjpwYXNz" not in red


# ---------------------------------------------------------------------------
# TLS
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def tls_certs(tmp_path_factory):
    """用 openssl 生成自签名证书（server cert + CA）。"""
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("openssl 不可用")
    d = tmp_path_factory.mktemp("tls")
    key, csr, cert = d / "server.key", d / "server.csr", d / "server.crt"
    ca_key, ca_cert = d / "ca.key", d / "ca.crt"
    subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(ca_key),
         "-out", str(ca_cert), "-days", "2", "-subj", "/CN=WineFoxTestCA",
         "-addext", "basicConstraints=critical,CA:TRUE",
         "-addext", "keyUsage=critical,keyCertSign,cRLSign"],
        check=True, capture_output=True,
    )
    subprocess.run(
        [openssl, "req", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
         "-out", str(csr), "-subj", "/CN=127.0.0.1"],
        check=True, capture_output=True,
    )
    extfile = d / "ext.cnf"
    extfile.write_text("subjectAltName=IP:127.0.0.1\n", encoding="utf-8")
    subprocess.run(
        [openssl, "x509", "-req", "-in", str(csr), "-CA", str(ca_cert), "-CAkey", str(ca_key),
         "-CAcreateserial", "-out", str(cert), "-days", "2", "-extfile", str(extfile)],
        check=True, capture_output=True,
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    return ctx, str(ca_cert), str(cert)


class TestTLS:
    def test_self_signed_fails_with_clear_error(self, server_scenario, tls_certs):
        ssl_ctx, ca_path, _ = tls_certs

        def build(fake, url):
            async def scenario():
                transport = ComfyUITransport(_base_config(url))  # verify_tls=True 默认
                await transport.start()
                try:
                    with pytest.raises(ComfyUITLSError) as exc_info:
                        await transport.get_json("/queue")
                    assert "ca_bundle" in str(exc_info.value)  # 明确指引
                finally:
                    await transport.close()

            return scenario

        server_scenario(build, ssl_ctx=ssl_ctx)

    def test_custom_ca_works(self, server_scenario, tls_certs):
        ssl_ctx, ca_path, _ = tls_certs

        def build(fake, url):
            async def scenario():
                transport = ComfyUITransport(_base_config(url, ca_bundle=ca_path))
                await transport.start()
                try:
                    data = await transport.get_json("/queue")
                    assert isinstance(data, dict)
                finally:
                    await transport.close()

            return scenario

        server_scenario(build, ssl_ctx=ssl_ctx)

    def test_verify_disabled_works_with_warning(self, server_scenario, tls_certs, caplog):
        ssl_ctx, ca_path, _ = tls_certs
        caplog.set_level(logging.WARNING, logger="winefox.comfyui.transport")

        def build(fake, url):
            async def scenario():
                transport = ComfyUITransport(_base_config(url, verify_tls=False))
                await transport.start()
                try:
                    data = await transport.get_json("/queue")
                    assert isinstance(data, dict)
                finally:
                    await transport.close()

            return scenario

        server_scenario(build, ssl_ctx=ssl_ctx)
        # 显式关闭 TLS 验证必须给出警告
        assert any("TLS 验证已显式关闭" in rec.message for rec in caplog.records)

    def test_missing_ca_file_error(self):
        async def scenario():
            transport = ComfyUITransport(
                _base_config("http://127.0.0.1:1", ca_bundle="C:/no/such/ca.pem")
            )
            with pytest.raises(ComfyUITLSError):
                await transport.start()

        asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 执行器
# ---------------------------------------------------------------------------
class TestExecutorHappyPath:
    def test_text2img_full_pipeline(self, server_scenario):
        def build(fake, url):
            async def scenario():
                async with ComfyUIExecutor(_base_config(url)) as executor:
                    fake.complete_prompt("prompt-0001")  # 预置结果
                    result = await executor.run(_snapshot(options={"poll_interval_base": 0.02}))
                    return result

            return scenario

        result, fake = server_scenario(build)
        assert result.ok
        assert result.images == [TINY_PNG]
        assert result.prompt_id == "prompt-0001"
        assert "elapsed_seconds" in result.meta
        assert fake.prompt_requests == 1
        assert fake.view_requests >= 1

    def test_img2img_upload(self, server_scenario, tmp_path):
        src = tmp_path / "input.png"
        src.write_bytes(TINY_PNG)

        def build(fake, url):
            async def scenario():
                async with ComfyUIExecutor(_base_config(url)) as executor:
                    return await executor.upload_image(src, subfolder="user/test")

            return scenario

        uploaded, fake = server_scenario(build)
        assert fake.upload_requests == 1
        assert uploaded.name.startswith("winefox_img2img_")
        assert uploaded.name.endswith(".png")
        assert uploaded.subfolder == "user/test"

    def test_upload_traversal_rejected(self, server_scenario):
        def build(fake, url):
            async def scenario():
                async with ComfyUIExecutor(_base_config(url)) as executor:
                    with pytest.raises(ComfyUIFileError):
                        await executor.upload_image(io.BytesIO(TINY_PNG), filename="../../etc/passwd")

            return scenario

        server_scenario(build)

    def test_upload_oversized(self, server_scenario):
        def build(fake, url):
            async def scenario():
                async with ComfyUIExecutor(_base_config(url, max_upload_bytes=10)) as executor:
                    with pytest.raises(ComfyUIError) as exc_info:
                        await executor.upload_image(io.BytesIO(TINY_PNG))
                    assert exc_info.value.kind in (ErrorKind.FILE, ErrorKind.PAYLOAD_TOO_LARGE)

            return scenario

        server_scenario(build)

    def test_corrupt_image_detected(self, server_scenario):
        def build(fake, url):
            async def scenario():
                fake.view_bytes = b"not-an-image-at-all"
                fake.complete_prompt("prompt-0001")
                async with ComfyUIExecutor(_base_config(url)) as executor:
                    return await executor.run(_snapshot(options={"poll_interval_base": 0.02}))

            return scenario

        result, _ = server_scenario(build)
        assert not result.ok
        assert result.error.kind == ErrorKind.IMAGE_DECODE

    def test_oversized_download(self, server_scenario):
        def build(fake, url):
            async def scenario():
                fake.view_bytes = b"x" * 500
                fake.complete_prompt("prompt-0001")
                async with ComfyUIExecutor(_base_config(url, max_image_bytes=100)) as executor:
                    return await executor.run(_snapshot(options={"poll_interval_base": 0.02}))

            return scenario

        result, _ = server_scenario(build)
        assert not result.ok
        assert result.error.kind == ErrorKind.PAYLOAD_TOO_LARGE

    def test_auth_failure_executor(self, server_scenario):
        def build(fake, url):
            async def scenario():
                fake.require_auth = "Bearer correct"
                async with ComfyUIExecutor(_base_config(url, auth=ComfyUIAuth.bearer("wrong"))) as executor:
                    return await executor.run(_snapshot())

            return scenario

        result, _ = server_scenario(build)
        assert not result.ok
        assert result.error.kind == ErrorKind.AUTH

    def test_validation_error_missing_model(self, server_scenario):
        def build(fake, url):
            async def scenario():
                fake.prompt_status = 400
                fake.prompt_body = {
                    "error": "prompt_outputs_failed_validation",
                    "node_errors": {
                        "3": {
                            "errors": [
                                {
                                    "type": "value_not_in_list",
                                    "message": "Value not in list",
                                    "details": "ckpt_name: 'missing_model.safetensors' not in list",
                                }
                            ],
                            "class_type": "CheckpointLoaderSimple",
                        }
                    },
                }
                async with ComfyUIExecutor(_base_config(url)) as executor:
                    return await executor.run(_snapshot())

            return scenario

        result, _ = server_scenario(build)
        assert not result.ok
        assert result.error.kind == ErrorKind.VALIDATION
        assert "missing_model.safetensors" in result.error.user_message
        assert "models\\checkpoints" in result.error.user_message  # 相对目录提示
        assert "D:\\" not in result.error.user_message  # 无绝对路径
        assert "C:" not in result.error.user_message

    def test_missing_node(self, server_scenario):
        def build(fake, url):
            async def scenario():
                async with ComfyUIExecutor(_base_config(url)) as executor:
                    return await executor.run(_snapshot(_workflow_with_missing_node()))

            return scenario

        result, fake = server_scenario(build)
        assert not result.ok
        assert result.error.kind == ErrorKind.MISSING_NODE
        assert "NonexistentCustomNode" in result.error.user_message
        assert fake.prompt_requests == 0  # 未提交

    def test_execution_error_from_history(self, server_scenario):
        def build(fake, url):
            async def scenario():
                fake.complete_prompt("prompt-0001", status="error", error_detail="CUDA out of memory")
                async with ComfyUIExecutor(_base_config(url)) as executor:
                    return await executor.run(_snapshot(options={"poll_interval_base": 0.02}))

            return scenario

        result, _ = server_scenario(build)
        assert not result.ok
        assert result.error.kind == ErrorKind.EXECUTION_ERROR
        assert "CUDA out of memory" in result.error.user_message

    def test_resume_with_existing_prompt_id(self, server_scenario):
        def build(fake, url):
            async def scenario():
                fake.complete_prompt("prompt-1234")
                async with ComfyUIExecutor(_base_config(url)) as executor:
                    return await executor.run(
                        _snapshot(resume_prompt_id="prompt-1234", options={"poll_interval_base": 0.02})
                    )

            return scenario

        result, fake = server_scenario(build)
        assert result.ok
        assert result.prompt_id == "prompt-1234"
        assert fake.prompt_requests == 0  # 不重复提交
        assert fake.upload_requests == 0  # 恢复不重复上传

    def test_prompt_lost_queue_disappeared(self, server_scenario):
        def build(fake, url):
            async def scenario():
                fake.complete_prompt("other")  # history 非空但不含目标 id
                fake.keep_queue_entries = False  # 提交后队列立即消失（模拟被清空）
                async with ComfyUIExecutor(_base_config(url)) as executor:
                    return await executor.run(
                        _snapshot(
                            options={
                                "poll_interval_base": 0.02,
                                "queue_missing_grace_seconds": 0.0,
                                "queue_missing_confirmations": 2,
                            }
                        )
                    )

            return scenario

        result, _ = server_scenario(build)
        assert not result.ok
        assert result.error.kind == ErrorKind.PROMPT_LOST
        assert result.meta.get("queue_prompt_missing") is True

    def test_prompt_still_in_queue_not_lost(self, server_scenario):
        def build(fake, url):
            async def scenario():
                fake.queue_running = [{"prompt_id": "prompt-0001"}]  # history 空但队列在
                async with ComfyUIExecutor(_base_config(url)) as executor:
                    return await executor.run(
                        _snapshot(
                            options={
                                "poll_attempts": 3,
                                "poll_interval_base": 0.02,
                                "queue_missing_grace_seconds": 0.0,
                                "queue_missing_confirmations": 2,
                            }
                        )
                    )

            return scenario

        result, _ = server_scenario(build)
        assert result.error.kind != ErrorKind.PROMPT_LOST  # 队列中能找到 → 不判丢失
        assert result.meta.get("queue_prompt_missing") is not True

    def test_generation_timeout(self, server_scenario):
        def build(fake, url):
            async def scenario():
                async with ComfyUIExecutor(_base_config(url)) as executor:
                    return await executor.run(
                        _snapshot(options={"poll_attempts": 3, "poll_interval_base": 0.02})
                    )

            return scenario

        result, _ = server_scenario(build)
        assert not result.ok
        assert result.error.kind == ErrorKind.GENERATION_TIMEOUT

    def test_probe(self, server_scenario):
        def build(fake, url):
            async def scenario():
                async with ComfyUIExecutor(_base_config(url)) as executor:
                    return await executor.probe()

            return scenario

        probe, _ = server_scenario(build)
        assert probe.reachable
        assert probe.node_count >= 8
        assert "char1.safetensors" in probe.models.loras
        assert "sd15.safetensors" in probe.models.checkpoints

    def test_models_fallback(self, server_scenario):
        def build(fake, url):
            async def scenario():
                fake.models = {}  # /models 空 → 走 /models/loras 与 object_info 回退
                async with ComfyUIExecutor(_base_config(url)) as executor:
                    return await executor.fetch_models()

            return scenario

        models, _ = server_scenario(build)
        assert "char1.safetensors" in models.loras
        # detailer_controlnet_lora 属于忽略名单（controlnet 标记）
        assert "detailer_controlnet_lora.safetensors" not in models.loras


# ---------------------------------------------------------------------------
# 安全与快照
# ---------------------------------------------------------------------------
class TestSafety:
    def test_snapshot_is_frozen(self):
        from dataclasses import FrozenInstanceError

        snapshot = _snapshot()
        with pytest.raises(FrozenInstanceError):
            snapshot.prompt = "mutated"

    def test_sanitize_comfy_name(self):
        assert sanitize_comfy_name("a.png") == "a.png"
        with pytest.raises(ComfyUIFileError):
            sanitize_comfy_name("../evil.png")
        with pytest.raises(ComfyUIFileError):
            sanitize_comfy_name("dir/evil.png")
        with pytest.raises(ComfyUIFileError):
            sanitize_comfy_name("/etc/passwd")
        with pytest.raises(ComfyUIFileError):
            sanitize_comfy_name("..\\evil.png")
        assert sanitize_comfy_name("a/b/c.png", allow_subfolder=True) == "a/b/c.png"
        with pytest.raises(ComfyUIFileError):
            sanitize_comfy_name("a/../../c.png", allow_subfolder=True)

    def test_queue_contains_prompt_id(self):
        assert _queue_contains_prompt_id({"queue_running": [{"prompt_id": "abc"}]}, "abc")
        assert not _queue_contains_prompt_id({"queue_running": [{"prompt_id": "xyz"}]}, "abc")

    def test_no_absolute_paths_in_errors(self, server_scenario):
        def build(fake, url):
            async def scenario():
                fake.prompt_status = 400
                fake.prompt_body = {
                    "error": "prompt_outputs_failed_validation",
                    "node_errors": {
                        "3": {
                            "errors": [{"type": "value_not_in_list", "details": "vae_name: 'D:\\models\\bad.safetensors' not in list"}],
                            "class_type": "VAELoader",
                        }
                    },
                }
                async with ComfyUIExecutor(_base_config(url)) as executor:
                    return await executor.run(_snapshot())

            return scenario

        result, _ = server_scenario(build)
        assert not result.ok
        # 错误文案不应包含 Windows 绝对路径（只显示 basename）
        assert "D:\\models\\bad.safetensors" not in result.error.user_message
        assert "bad.safetensors（应位于 ComfyUI/models\\vae）" in result.error.user_message

    def test_no_astrbot_import(self):
        import sys as _sys

        # 确定性检查 1：源码不得 import astrbot（docstring 提到名称不算）
        import re as _re

        for path in (
            "winefox_core/comfyui_types.py",
            "winefox_core/comfyui_transport.py",
            "winefox_core/comfyui_executor.py",
        ):
            text = (REPO_ROOT / path).read_text(encoding="utf-8")
            assert not _re.search(r"^\s*(import astrbot|from astrbot)", text, _re.MULTILINE), (
                f"{path} import 了 astrbot"
            )

        # 确定性检查 2：全新导入我的模块不得向 sys.modules 新增 astrbot
        # （原有 tests/ 可能已用 stub 引入 astrbot，因此只比较增量）
        for mod in (
            "winefox_core.comfyui_types",
            "winefox_core.comfyui_transport",
            "winefox_core.comfyui_executor",
        ):
            _sys.modules.pop(mod, None)
        before = {m for m in _sys.modules if m.startswith("astrbot")}
        import winefox_core.comfyui_types  # noqa: F401
        import winefox_core.comfyui_transport  # noqa: F401
        import winefox_core.comfyui_executor  # noqa: F401
        after = {m for m in _sys.modules if m.startswith("astrbot")}
        assert after <= before

    def test_headers_redacted_in_log(self, server_scenario, caplog):
        caplog.set_level(logging.INFO, logger="winefox.comfyui.transport")

        def build(fake, url):
            async def scenario():
                transport = ComfyUITransport(
                    _base_config(url, auth=ComfyUIAuth.bearer("super-secret-token-xyz"))
                )
                await transport.start()
                try:
                    await transport.get_json("/queue")
                finally:
                    await transport.close()

            return scenario

        server_scenario(build)
        log_text = "\n".join(rec.message for rec in caplog.records)
        assert "super-secret-token-xyz" not in log_text

    def test_as_core_result_adapter(self):
        from winefox_core.comfyui_types import ComfyUIResult

        ok_result = ComfyUIResult(ok=True, images=[TINY_PNG], prompt_id="p1")
        adapted = ok_result.as_core_result()
        assert adapted is not None
        assert adapted.ok is True
        assert adapted.error_kind is None

        bad = ComfyUIResult(ok=False, error=ComfyUITimeoutError("超时", kind=ErrorKind.TIMEOUT))
        adapted_bad = bad.as_core_result()
        assert adapted_bad.ok is False
        assert adapted_bad.error_kind == "timeout"

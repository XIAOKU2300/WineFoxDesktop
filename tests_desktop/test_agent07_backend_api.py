"""Agent 07 Backend API 测试：aiohttp 本地后端 / 会话安全 / 上传 / 路由 / 错误映射。

业务核心全部通过 fake 实现（Protocol 见 desktop_backend/protocols.py）。
运行：cd 源码根目录 && pytest tests_desktop/test_agent07_backend_api.py -q
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import logging
import os
import signal
import socket
import sys
import threading
import time

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from desktop_backend import PROTOCOL_VERSION, VERSION  # noqa: E402
from desktop_backend.app import create_app  # noqa: E402
from desktop_backend.errors import AppError  # noqa: E402
from desktop_backend.models import HistoryEntry, ImageData, JobRecord  # noqa: E402
from desktop_backend.security import SESSION_COOKIE_NAME  # noqa: E402
from desktop_backend.server import WineFoxBackendServer  # noqa: E402

VALID_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGM8ISfHAANMcBZeDgA0dgEMydTl/QAAAABJRU5ErkJggg=="
)


# =====================================================================
# Fakes（Protocol 实现）
# =====================================================================

class FakeEngine:
    """CoreEngine fake：记录调用、返回固定数据、可注入异常。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.raise_error: Exception | None = None
        self._jobs: dict[str, JobRecord] = {}
        self._history: dict[str, HistoryEntry] = {}
        self._images: dict[str, ImageData] = {
            "img-1": ImageData(data=VALID_PNG, content_type="image/png", filename="a.png"),
        }
        self._history["hist-1"] = HistoryEntry(
            id="hist-1", prompt="cat", created_at=1000.0, image_id="img-1", favorite=False
        )
        self._history["hist-2"] = HistoryEntry(
            id="hist-2", prompt="dog", created_at=2000.0, image_id=None, favorite=True
        )

    def _maybe_raise(self, name: str, *args) -> None:
        self.calls.append((name, args))
        if self.raise_error is not None:
            raise self.raise_error

    async def probe_comfyui(self, payload=None):
        self._maybe_raise("probe_comfyui", payload)
        return {"ok": True, "server": "127.0.0.1:8188", "latency_ms": 12}

    async def get_catalog(self):
        self._maybe_raise("get_catalog")
        return {"loras": [{"name": "lora_a"}], "checkpoints": [{"name": "ckpt_b"}], "workflows": []}

    async def submit_job(self, payload):
        self._maybe_raise("submit_job", payload)
        job = JobRecord(id=f"job-{len(self._jobs) + 1}", status="queued", created_at=time.time(), prompt=payload.get("prompt", ""))
        self._jobs[job.id] = job
        return job

    async def get_job(self, job_id):
        self._maybe_raise("get_job", job_id)
        return self._jobs.get(job_id)

    async def cancel_job(self, job_id):
        self._maybe_raise("cancel_job", job_id)
        job = self._jobs.get(job_id)
        if job is None or job.status in ("done", "cancelled", "failed"):
            return False
        job.status = "cancelled"
        job.updated_at = time.time()
        return True

    async def list_history(self, limit=50, offset=0, favorite_only=False):
        self._maybe_raise("list_history", limit, offset, favorite_only)
        items = sorted(self._history.values(), key=lambda h: h.created_at, reverse=True)
        if favorite_only:
            items = [h for h in items if h.favorite]
        return items[offset : offset + limit]

    async def get_history_image(self, history_id):
        self._maybe_raise("get_history_image", history_id)
        entry = self._history.get(history_id)
        if entry is None or entry.image_id is None:
            return None
        return self._images.get(entry.image_id)

    async def set_favorite(self, history_id, favorite):
        self._maybe_raise("set_favorite", history_id, favorite)
        entry = self._history.get(history_id)
        if entry is None:
            return False
        entry.favorite = favorite
        return True

    async def delete_history(self, history_id):
        self._maybe_raise("delete_history", history_id)
        return self._history.pop(history_id, None) is not None

    async def diagnostics(self):
        self._maybe_raise("diagnostics")
        return {"version": VERSION, "ok": True, "errors": []}


class FakeConfig:
    """ConfigStore fake：已配置视图 + 密钥存储。"""

    def __init__(self) -> None:
        self.settings = {
            "server_address": "127.0.0.1:8188",
            "timeout": 60,
            "api_key": "sk-live-123456",  # 敏感键，GET 时必须被打码
            "proxy": "http://user:pass@127.0.0.1:7890",
        }
        self.secrets: dict[str, str] = {}
        self.calls: list[tuple] = []

    async def get_settings(self):
        self.calls.append(("get_settings",))
        return dict(self.settings)

    async def patch_settings(self, patch):
        self.calls.append(("patch_settings", patch))
        for k, v in patch.items():
            if k == "timeout":
                if not isinstance(v, int) or v <= 0:
                    raise AppError(422, "validation_error", "timeout 需要正整数")
            self.settings[k] = v
        return dict(self.settings)

    async def put_secret(self, name, value):
        self.calls.append(("put_secret", name, value))
        self.secrets[name] = value

    async def delete_secret(self, name):
        self.calls.append(("delete_secret", name))
        return self.secrets.pop(name, None) is not None


class FakeUploadStore:
    """UploadStore fake：内存存储，只按 ID 存取。"""

    def __init__(self) -> None:
        self.items: dict[str, ImageData] = {}
        self.calls: list[tuple] = []

    async def save(self, upload_id, data, *, filename, content_type, kind):
        self.calls.append(("save", upload_id, filename, content_type, kind, len(data)))
        self.items[upload_id] = ImageData(data=data, content_type=content_type, filename=filename)

    async def get(self, upload_id):
        return self.items.get(upload_id)

    async def delete(self, upload_id):
        return self.items.pop(upload_id, None) is not None


# =====================================================================
# 测试基建
# =====================================================================

# 服务生命周期必须绑定在同一个持续运行的事件循环上：
# - asyncio.run 每次都会关闭 loop（Windows Proactor 下监听 socket 失效）
# - 阻塞式 raw socket 测试要求 loop 在后台线程持续运行
_LOOP: asyncio.AbstractEventLoop | None = None
_LOOP_THREAD: threading.Thread | None = None


@pytest.fixture(scope="session", autouse=True)
def _shared_loop():
    global _LOOP, _LOOP_THREAD
    _LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(_LOOP)
    _LOOP_THREAD = threading.Thread(target=_LOOP.run_forever, name="winefox-test-loop", daemon=True)
    _LOOP_THREAD.start()
    yield _LOOP
    _LOOP.call_soon_threadsafe(_LOOP.stop)
    _LOOP_THREAD.join(timeout=10)
    _LOOP.run_until_complete(_LOOP.shutdown_asyncgens())
    _LOOP.close()
    asyncio.set_event_loop(None)
    _LOOP = None
    _LOOP_THREAD = None


def _run(coro):
    assert _LOOP is not None
    return asyncio.run_coroutine_threadsafe(coro, _LOOP).result(timeout=30)


def make_server(tmp_path, *, max_upload_bytes=1024 * 1024, **kw):
    """构造带 fake 依赖 + 临时静态目录的服务。"""
    static_dir = tmp_path / "desktop_ui"
    static_dir.mkdir(exist_ok=True)
    (static_dir / "index.html").write_text("<html>winefox ui</html>", encoding="utf-8")
    (static_dir / "app.js").write_text("console.log('ui')", encoding="utf-8")
    engine = kw.pop("engine", FakeEngine())
    config = kw.pop("config", FakeConfig())
    uploads = kw.pop("uploads", FakeUploadStore())
    server = WineFoxBackendServer(
        engine=engine,
        config=config,
        uploads=uploads,
        static_dir=static_dir,
        max_upload_bytes=max_upload_bytes,
        **kw,
    )
    return server, engine, config, uploads


@contextlib.contextmanager
def started(tmp_path, **kw):
    server, engine, config, uploads = make_server(tmp_path, **kw)
    _run(server.start(ready_stream=io.StringIO()))
    try:
        yield server, engine, config, uploads
    finally:
        _run(server.stop())


async def _request(server, method, path, *, cookies=None, headers=None, raw=False, **kw):
    import aiohttp

    async with asyncio.timeout(10):
        async with aiohttp.ClientSession() as session:
            url = f"http://127.0.0.1:{server.port}{path}"
            async with session.request(method, url, cookies=cookies, headers=headers, **kw) as resp:
                if raw:
                    body = await resp.read()
                    return resp.status, resp.headers, body
                body = await resp.text()
                try:
                    js = json.loads(body) if body else None
                except json.JSONDecodeError:
                    js = None
                return resp.status, resp.headers, js


def get(server, path, **kw):
    return _run(_request(server, "GET", path, **kw))


def post(server, path, **kw):
    return _run(_request(server, "POST", path, **kw))


def _cookie_from_headers(headers) -> str:
    set_cookie = headers.getall("Set-Cookie")
    assert set_cookie
    return set_cookie[0].split(";", 1)[0].split("=", 1)[1]


def bootstrap(server) -> str:
    """执行 bootstrap，返回会话 Cookie 值。"""
    status, headers, js = get(server, server.bootstrap_path)
    assert status == 200, f"bootstrap 失败: {status} {js}"
    set_cookie = headers.getall("Set-Cookie")
    assert set_cookie, "bootstrap 响应缺少 Set-Cookie"
    cookie = set_cookie[0]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    return _cookie_from_headers(headers)


def raw_request(port: int, raw: bytes) -> tuple[int, bytes]:
    """原始 TCP 请求（绕过 aiohttp 客户端），用于 Host 头攻击测试。"""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(raw)
        s.settimeout(5)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    status = int(data.split(b" ", 2)[1])
    return status, data


# =====================================================================
# 1. 绑定 / ready JSON
# =====================================================================

def test_loopback_bind(tmp_path):
    """仅绑定 127.0.0.1，端口系统分配。"""
    with started(tmp_path) as (server, *_):
        assert server.host == "127.0.0.1"
        assert 0 < server.port < 65536
        # 确认真实监听在 loopback
        status, _ = raw_request(server.port, b"GET /api/v1/health HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % server.port)
        assert status == 200


def test_ready_json_stdout(tmp_path):
    """stdout 只输出一次 ready JSON，字段与顺序固定。"""
    server, *_ = make_server(tmp_path)
    out = io.StringIO()
    _run(server.start(ready_stream=out))
    try:
        lines = [l for l in out.getvalue().strip().splitlines() if l.strip()]
        assert len(lines) == 1, f"stdout 应只有一行 ready JSON: {lines}"
        payload = json.loads(lines[0])
        assert list(payload.keys()) == ["type", "protocol", "version", "host", "port", "bootstrapPath"]
        assert payload["type"] == "ready"
        assert payload["protocol"] == 1
        assert payload["host"] == "127.0.0.1"
        assert isinstance(payload["port"], int) and payload["port"] > 0
        assert payload["bootstrapPath"].startswith("/bootstrap/")
        assert len(payload["bootstrapPath"].split("/")[-1]) == 64  # 256 bit hex
        # ready_payload 与 stdout 一致
        assert server.ready_payload == payload
    finally:
        _run(server.stop())


def test_version_and_protocol(tmp_path):
    assert PROTOCOL_VERSION == 1
    assert VERSION and VERSION.split(".")[0].isdigit()
    with started(tmp_path) as (server, *_):
        status, _, js = get(server, "/api/v1/health")
        assert status == 200 and js["ok"] is True
        assert js["data"]["version"] == VERSION
        assert js["data"]["protocol"] == 1
        assert js["data"]["status"] == "ok"


# =====================================================================
# 2. bootstrap / 会话
# =====================================================================

def test_bootstrap_once(tmp_path):
    with started(tmp_path) as (server, *_):
        # 第一次成功 + Cookie 属性
        session = bootstrap(server)
        assert len(session) == 64  # 256 bit
        # 重复使用同一 token -> 404
        status, _, js = get(server, server.bootstrap_path)
        assert status == 404 and js["error"]["code"] == "bootstrap_not_found"
        # 错误 token -> 404
        status, _, js = get(server, "/bootstrap/" + "0" * 64)
        assert status == 404
        # bootstrap 成功后 API 可访问
        status, _, js = get(server, "/api/v1/settings", cookies={SESSION_COOKIE_NAME: session})
        assert status == 200


def test_no_cookie_401(tmp_path):
    with started(tmp_path) as (server, *_):
        for path in ("/api/v1/settings", "/api/v1/history", "/api/v1/catalog", "/api/v1/diagnostics"):
            status, _, js = get(server, path)
            assert status == 401, path
            assert js["ok"] is False and js["error"]["code"] == "unauthorized"
        # bootstrap 前无会话
        assert server.security.session_token is None


def test_query_token_invalid(tmp_path):
    """query token 一律不作为凭据。"""
    with started(tmp_path) as (server, *_):
        token = server.bootstrap_path.split("/")[-1]
        status, _, js = get(server, f"/api/v1/settings?token={token}")
        assert status == 401, "query token 不应通过认证"
        assert js["error"]["code"] == "unauthorized"
        # 有 Cookie 时 query token 被忽略（不构成拒绝，也不参与认证）
        session = bootstrap(server)
        status, _, js = get(server, f"/api/v1/settings?token=garbage", cookies={SESSION_COOKIE_NAME: session})
        assert status == 200


def test_bad_cookie_rejected(tmp_path):
    with started(tmp_path) as (server, *_):
        bootstrap(server)
        status, _, _ = get(server, "/api/v1/settings", cookies={SESSION_COOKIE_NAME: "0" * 64})
        assert status == 401


# =====================================================================
# 3. Host / Origin / CSRF
# =====================================================================

def test_bad_host_rejected(tmp_path):
    with started(tmp_path) as (server, *_):
        for host in (b"evil.com", b"127.0.0.1", b"127.0.0.1:9999", b"localhost:9999"):
            status, _ = raw_request(
                server.port, b"GET /api/v1/health HTTP/1.1\r\nHost: " + host + b"\r\n\r\n"
            )
            assert status == 403, f"Host {host!r} 应被拒绝"
        # 精确匹配合法 Host 放行
        status, body = raw_request(
            server.port, b"GET /api/v1/health HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % server.port
        )
        assert status == 200


def test_bad_origin_rejected(tmp_path):
    with started(tmp_path) as (server, *_):
        session = bootstrap(server)
        cookies = {SESSION_COOKIE_NAME: session}
        status, _, js = get(server, "/api/v1/history", cookies=cookies, headers={"Origin": "http://evil.com"})
        assert status == 403 and js["error"]["code"] == "bad_origin"
        status, _, js = get(server, "/api/v1/history", cookies=cookies, headers={"Origin": "http://127.0.0.1:9999"})
        assert status == 403
        # 合法 Origin 的 GET 放行
        status, _, js = get(
            server, "/api/v1/history", cookies=cookies,
            headers={"Origin": f"http://127.0.0.1:{server.port}"},
        )
        assert status == 200


def test_csrf_write_operations(tmp_path):
    with started(tmp_path) as (server, *_):
        session = bootstrap(server)
        cookies = {SESSION_COOKIE_NAME: session}
        body = json.dumps({"prompt": "cat"})
        # 无 Origin 也无自定义头 -> 403
        status, _, js = post(server, "/api/v1/jobs", cookies=cookies, data=body, headers={"Content-Type": "application/json"})
        assert status == 403 and js["error"]["code"] == "csrf_blocked"
        # 合法 Origin -> 放行
        status, _, js = post(
            server, "/api/v1/jobs", cookies=cookies, data=body,
            headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{server.port}"},
        )
        assert status == 202
        # X-WineFox-Client 头 -> 放行（非浏览器客户端）
        status, _, js = post(
            server, "/api/v1/jobs", cookies=cookies, data=body,
            headers={"Content-Type": "application/json", "X-WineFox-Client": "1"},
        )
        assert status == 202
        # 恶意 Origin + 自定义头同时出现，恶意 Origin 不构成放行条件
        status, _, js = post(
            server, "/api/v1/jobs", cookies=cookies, data=body,
            headers={"Content-Type": "application/json", "X-WineFox-Client": "1", "Origin": "http://evil.com"},
        )
        assert status == 403 and js["error"]["code"] == "bad_origin"
        # PATCH / DELETE 同样受 CSRF 保护
        status, _, js = get(server, "/api/v1/settings", cookies=cookies)
        assert status == 200
        status, _, js = _run(
            _request(server, "PATCH", "/api/v1/settings", cookies=cookies,
                     data=json.dumps({"timeout": 30}), headers={"Content-Type": "application/json"})
        )
        assert status == 403


# =====================================================================
# 4. 上传
# =====================================================================

def _upload(server, *, file_bytes=VALID_PNG, filename="a.png", ctype="image/png", session=None, headers=None, fields=None):
    import aiohttp

    hdrs = {"X-WineFox-Client": "1"}
    if headers:
        hdrs.update(headers)

    async def go():
        async with aiohttp.ClientSession() as s:
            form = aiohttp.FormData()
            for k, v in (fields or {}).items():
                form.add_field(k, v)
            form.add_field("file", file_bytes, filename=filename, content_type=ctype)
            async with s.post(
                f"http://127.0.0.1:{server.port}/api/v1/uploads",
                data=form,
                cookies={SESSION_COOKIE_NAME: session} if session else None,
                headers=hdrs,
            ) as resp:
                body = await resp.text()
                try:
                    js = json.loads(body)
                except json.JSONDecodeError:
                    js = None
                return resp.status, resp.headers, js

    return _run(go())


def test_upload_ok(tmp_path):
    with started(tmp_path) as (server, _e, _c, uploads):
        session = bootstrap(server)
        status, headers, js = _upload(server, session=session)
        assert status == 201
        assert js["ok"] is True
        data = js["data"]
        assert data["uploadId"] and len(data["uploadId"]) == 32  # 128 bit hex
        assert data["bytes"] == len(VALID_PNG)
        assert data["kind"] == "image"
        assert data["contentType"] == "image/png"
        # 绝不返回绝对路径
        assert "path" not in data and "filepath" not in data and "url" not in data
        assert uploads.items[data["uploadId"]].data == VALID_PNG


def test_upload_too_large(tmp_path):
    with started(tmp_path, max_upload_bytes=1024) as (server, *_):
        session = bootstrap(server)
        big = b"x" * (2048 + len(VALID_PNG))
        status, _, js = _upload(server, file_bytes=big, session=session)
        assert status == 413
        assert js["error"]["code"] == "payload_too_large"


def test_upload_corrupt(tmp_path):
    with started(tmp_path) as (server, *_):
        session = bootstrap(server)
        # 完全不是图片
        status, _, js = _upload(server, file_bytes=b"not an image at all", session=session)
        assert status == 400 and js["error"]["code"] == "upload_invalid"
        # 魔数正确但内容损坏（截断 PNG）
        status, _, js = _upload(server, file_bytes=VALID_PNG[:24], session=session)
        assert status == 400 and js["error"]["code"] == "upload_invalid"


def test_upload_missing_file_and_wrong_media_type(tmp_path):
    with started(tmp_path) as (server, *_):
        session = bootstrap(server)
        # 非 multipart
        status, _, js = post(
            server, "/api/v1/uploads", cookies={SESSION_COOKIE_NAME: session},
            data=b"plain", headers={"Content-Type": "application/octet-stream", "X-WineFox-Client": "1"},
        )
        assert status == 415 and js["error"]["code"] == "unsupported_media_type"
        # 缺少 file 字段（手工构造 multipart body）
        boundary = "----WineFoxBoundary7f8a"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="kind"\r\n\r\n'
            f"image\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")
        status, _, js = post(
            server, "/api/v1/uploads", cookies={SESSION_COOKIE_NAME: session}, data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "X-WineFox-Client": "1"},
        )
        assert status == 400 and js["error"]["code"] == "upload_no_file"


def test_upload_kind_validation(tmp_path):
    with started(tmp_path) as (server, *_):
        session = bootstrap(server)
        status, _, js = _upload(server, session=session, fields={"kind": "banana"})
        assert status == 422 and js["error"]["code"] == "validation_error"


# =====================================================================
# 5. 设置 / 密钥
# =====================================================================

def test_settings_masking(tmp_path):
    with started(tmp_path) as (server, *_):
        session = bootstrap(server)
        cookies = {SESSION_COOKIE_NAME: session}
        status, _, js = get(server, "/api/v1/settings", cookies=cookies)
        assert status == 200
        data = js["data"]
        assert data["server_address"] == "127.0.0.1:8188"  # 普通键原样
        assert data["api_key"] == "********"  # 敏感键打码
        assert data["timeout"] == 60
        assert "pass" not in str(data["api_key"])
        # URL 内嵌凭据只打码 userinfo
        assert data["proxy"] == "http://********@127.0.0.1:7890"


def test_settings_patch(tmp_path):
    with started(tmp_path) as (server, *_):
        session = bootstrap(server)
        cookies = {SESSION_COOKIE_NAME: session}
        # 正常 PATCH
        status, _, js = _run(
            _request(server, "PATCH", "/api/v1/settings", cookies=cookies,
                     data=json.dumps({"timeout": 90}), headers={"Content-Type": "application/json",
                                                                 "X-WineFox-Client": "1"})
        )
        assert status == 200 and js["data"]["timeout"] == 90
        # 敏感键不允许走 settings
        status, _, js = _run(
            _request(server, "PATCH", "/api/v1/settings", cookies=cookies,
                     data=json.dumps({"api_key": "sk-new"}), headers={"Content-Type": "application/json",
                                                                       "X-WineFox-Client": "1"})
        )
        assert status == 422 and js["error"]["code"] == "validation_error"
        assert js["error"]["details"]["keys"] == ["api_key"]
        # 非对象 body
        status, _, js = _run(
            _request(server, "PATCH", "/api/v1/settings", cookies=cookies,
                     data=json.dumps([1, 2]), headers={"Content-Type": "application/json",
                                                       "X-WineFox-Client": "1"})
        )
        assert status == 422


def test_secrets_put_delete(tmp_path):
    with started(tmp_path) as (server, _e, config, _u):
        session = bootstrap(server)
        cookies = {SESSION_COOKIE_NAME: session}
        headers = {"Content-Type": "application/json", "X-WineFox-Client": "1"}
        status, _, js = _run(
            _request(server, "PUT", "/api/v1/secrets/civitai_api_key", cookies=cookies,
                     data=json.dumps({"value": "secret-value"}), headers=headers)
        )
        assert status == 200 and config.secrets["civitai_api_key"] == "secret-value"
        # 非法名称
        status, _, js = _run(
            _request(server, "PUT", "/api/v1/secrets/bad name!", cookies=cookies,
                     data=json.dumps({"value": "x"}), headers=headers)
        )
        assert status == 422
        # 缺 value
        status, _, js = _run(
            _request(server, "PUT", "/api/v1/secrets/a", cookies=cookies,
                     data=json.dumps({"other": 1}), headers=headers)
        )
        assert status == 422
        # DELETE
        status, _, js = _run(_request(server, "DELETE", "/api/v1/secrets/civitai_api_key", cookies=cookies, headers=headers))
        assert status == 200 and js["data"]["deleted"] is True
        # DELETE 不存在 -> 404
        status, _, js = _run(_request(server, "DELETE", "/api/v1/secrets/nope", cookies=cookies, headers=headers))
        assert status == 404 and js["error"]["code"] == "secret_not_found"


# =====================================================================
# 6. 错误映射
# =====================================================================

def test_error_mapping(tmp_path):
    with started(tmp_path) as (server, engine, *_):
        session = bootstrap(server)
        cookies = {SESSION_COOKIE_NAME: session}
        hdr = {"X-WineFox-Client": "1"}
        # 未知 API 路由 -> 404 envelope
        status, _, js = get(server, "/api/v1/nope", cookies=cookies)
        assert status == 404 and js["ok"] is False and js["error"]["code"] == "not_found"
        # 方法不允许 -> 405
        status, _, js = get(server, "/api/v1/secrets/x", cookies=cookies)
        assert status == 405 and js["error"]["code"] == "method_not_allowed"
        # 坏 JSON -> 400
        status, _, js = post(server, "/api/v1/jobs", cookies=cookies, data="{not json",
                             headers={**hdr, "Content-Type": "application/json"})
        assert status == 400 and js["error"]["code"] == "invalid_json"
        # 校验失败 -> 422
        status, _, js = post(server, "/api/v1/jobs", cookies=cookies, data=json.dumps({"prompt": "  "}),
                             headers={**hdr, "Content-Type": "application/json"})
        assert status == 422 and js["error"]["code"] == "validation_error"
        # 引擎抛 AppError -> 原样映射
        engine.raise_error = AppError(404, "history_not_found", "历史记录不存在")
        status, _, js = get(server, "/api/v1/history", cookies=cookies)
        assert status == 404 and js["error"]["code"] == "history_not_found"
        # 引擎抛未知异常 -> 500 且不泄露内部细节
        engine.raise_error = ValueError("secret internal detail")
        status, _, js = get(server, "/api/v1/history", cookies=cookies)
        assert status == 500 and js["error"]["code"] == "internal_error"
        assert "secret internal detail" not in str(js) and "Traceback" not in str(js)
        # 未注入引擎 -> 503
        with started(tmp_path, engine=None) as (server2, *_):
            s2 = bootstrap(server2)
            status, _, js = get(server2, "/api/v1/catalog", cookies={SESSION_COOKIE_NAME: s2})
            assert status == 503 and js["error"]["code"] == "core_engine_not_configured"


# =====================================================================
# 7. fake 引擎路由
# =====================================================================

def test_fake_engine_routing(tmp_path):
    with started(tmp_path) as (server, engine, config, uploads):
        session = bootstrap(server)
        cookies = {SESSION_COOKIE_NAME: session}
        hdr = {"Content-Type": "application/json", "X-WineFox-Client": "1"}

        # probe
        status, _, js = post(server, "/api/v1/comfyui/probe", cookies=cookies, data="{}", headers=hdr)
        assert status == 200 and js["data"]["ok"] is True
        assert ("probe_comfyui", ({},)) in engine.calls

        # catalog
        status, _, js = get(server, "/api/v1/catalog", cookies=cookies)
        assert status == 200 and js["data"]["loras"][0]["name"] == "lora_a"

        # jobs
        status, _, js = post(server, "/api/v1/jobs", cookies=cookies,
                             data=json.dumps({"prompt": "cat", "upload_ids": ["u1"]}), headers=hdr)
        assert status == 202 and js["data"]["status"] == "queued"
        job_id = js["data"]["id"]
        status, _, js = get(server, f"/api/v1/jobs/{job_id}", cookies=cookies)
        assert status == 200 and js["data"]["id"] == job_id
        status, _, js = get(server, "/api/v1/jobs/missing", cookies=cookies)
        assert status == 404 and js["error"]["code"] == "job_not_found"
        status, _, js = post(server, f"/api/v1/jobs/{job_id}/cancel", cookies=cookies, data="{}", headers=hdr)
        assert status == 200 and js["data"]["cancelled"] is True
        # 校验 upload_ids
        status, _, js = post(server, "/api/v1/jobs", cookies=cookies,
                             data=json.dumps({"prompt": "cat", "upload_ids": "not-a-list"}), headers=hdr)
        assert status == 422

        # history
        status, _, js = get(server, "/api/v1/history", cookies=cookies)
        assert status == 200 and [h["id"] for h in js["data"]] == ["hist-2", "hist-1"]
        status, _, js = get(server, "/api/v1/history?favorite=1", cookies=cookies)
        assert status == 200 and [h["id"] for h in js["data"]] == ["hist-2"]
        # 历史图按 ID 返回二进制
        status, headers, body = _run(
            _request(server, "GET", "/api/v1/history/hist-1/image", cookies=cookies, raw=True)
        )
        assert status == 200 and body == VALID_PNG and headers["Content-Type"].startswith("image/png")
        status, _, js = get(server, "/api/v1/history/hist-missing/image", cookies=cookies)
        assert status == 404
        # 收藏
        status, _, js = post(server, "/api/v1/history/hist-1/favorite", cookies=cookies, data="{}", headers=hdr)
        assert status == 200 and js["data"]["favorite"] is True
        status, _, js = _run(_request(server, "DELETE", "/api/v1/history/hist-1/favorite", cookies=cookies, headers=hdr))
        assert status == 200 and js["data"]["favorite"] is False
        status, _, js = post(server, "/api/v1/history/missing/favorite", cookies=cookies, data="{}", headers=hdr)
        assert status == 404
        # 删除历史走 CoreEngine
        status, _, js = _run(_request(server, "DELETE", "/api/v1/history/hist-1", cookies=cookies, headers=hdr))
        assert status == 200 and js["data"]["deleted"] is True
        assert ("delete_history", ("hist-1",)) in engine.calls
        status, _, js = _run(_request(server, "DELETE", "/api/v1/history/hist-1", cookies=cookies, headers=hdr))
        assert status == 404

        # diagnostics
        status, _, js = get(server, "/api/v1/diagnostics", cookies=cookies)
        assert status == 200 and js["data"]["ok"] is True

        # uploads 走 UploadStore
        status, _, js = _upload(server, session=session)
        assert status == 201
        assert uploads.calls and uploads.calls[0][0] == "save"


# =====================================================================
# 8. 静态目录 / 路径逃逸
# =====================================================================

def test_static_serving_and_path_escape(tmp_path):
    with started(tmp_path) as (server, *_):
        # 正常静态文件
        status, headers, body = _run(_request(server, "GET", "/index.html", raw=True))
        assert status == 200 and b"<html>winefox ui</html>" in body
        status, _, body = _run(_request(server, "GET", "/", raw=True))
        assert status == 200
        # 静态文件同样带安全头
        assert headers.get("X-Content-Type-Options") == "nosniff"
        assert "frame-ancestors 'none'" in headers.get("Content-Security-Policy", "")

        # 路径逃逸（外部放一个 secret.txt）
        outside = tmp_path / "secret.txt"
        outside.write_text("top-secret", encoding="utf-8")
        attacks = [
            "/../secret.txt",
            "/%2e%2e/secret.txt",
            "/..%2fsecret.txt",
            "/%2e%2e%2fsecret.txt",
            "/sub/../secret.txt",
            "/..\\secret.txt",
            "/%2e%2e%5csecret.txt",
            "/....//secret.txt",
        ]
        for path in attacks:
            status, _, body = _run(_request(server, "GET", path, raw=True))
            assert status == 404, f"{path} 应返回 404，实际 {status}"
            assert b"top-secret" not in (body or b"")
        # 不存在的静态文件
        status, _, _ = _run(_request(server, "GET", "/missing.js", raw=True))
        assert status == 404


def test_default_static_dir_param(tmp_path):
    """静态目录参数注入：默认 desktop_ui，可注入自定义目录。"""
    from desktop_backend import DEFAULT_STATIC_DIR
    assert DEFAULT_STATIC_DIR == "desktop_ui"
    # 注入自定义目录（make_server 已注入 tmp_path/desktop_ui）
    with started(tmp_path) as (server, *_):
        status, _, _ = _run(_request(server, "GET", "/app.js"))
        assert status == 200


# =====================================================================
# 9. 安全头 / 访问日志 / 停止
# =====================================================================

def test_security_headers(tmp_path):
    with started(tmp_path) as (server, *_):
        # bootstrap 响应同样带安全头（先捕获，再消费）
        status, boot_headers, _ = get(server, server.bootstrap_path)
        assert status == 200
        assert boot_headers.get("X-Frame-Options") == "DENY"
        assert boot_headers.get("Cache-Control") == "no-store"
        assert boot_headers.get("X-Content-Type-Options") == "nosniff"

        session = _cookie_from_headers(boot_headers)
        status, headers, js = get(server, "/api/v1/settings", cookies={SESSION_COOKIE_NAME: session})
        assert status == 200
        assert headers.get("X-Content-Type-Options") == "nosniff"
        assert headers.get("X-Frame-Options") == "DENY"
        assert headers.get("Referrer-Policy") == "no-referrer"
        assert headers.get("Cache-Control") == "no-store"
        csp = headers.get("Content-Security-Policy", "")
        assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
        # 错误响应同样带安全头
        status, headers, js = get(server, "/api/v1/nope", cookies={SESSION_COOKIE_NAME: session})
        assert status == 404
        assert headers.get("X-Content-Type-Options") == "nosniff"
        assert headers.get("Cache-Control") == "no-store"
        assert headers.get("X-Frame-Options") == "DENY"


def test_no_cors_headers(tmp_path):
    with started(tmp_path) as (server, *_):
        session = bootstrap(server)
        status, headers, _ = post(
            server, "/api/v1/jobs", cookies={SESSION_COOKIE_NAME: session},
            data=json.dumps({"prompt": "x"}), headers={"Content-Type": "application/json",
                                                        "X-WineFox-Client": "1"},
        )
        assert status == 202
        assert "Access-Control-Allow-Origin" not in headers
        assert "Access-Control-Allow-Credentials" not in headers


def test_bootstrap_not_in_access_log(tmp_path):
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = lambda r: records.append(r)
    logger = logging.getLogger("winefox.access")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        with started(tmp_path) as (server, *_):
            session = bootstrap(server)  # bootstrap 请求
            get(server, "/api/v1/history", cookies={SESSION_COOKIE_NAME: session})
            get(server, "/api/v1/health")
            get(server, "/index.html")
    finally:
        logger.removeHandler(handler)
    logged = " | ".join(r.getMessage() for r in records)
    assert "/bootstrap" not in logged, "bootstrap 不应进访问日志"
    assert "/api/v1/history" in logged
    assert "/index.html" in logged


def test_stop_releases_port(tmp_path):
    server, *_ = make_server(tmp_path)
    _run(server.start(ready_stream=io.StringIO()))
    port = server.port
    _run(server.stop())
    # stop 后可重复调用
    _run(server.stop())
    # 端口已释放
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
    # 停止后连接被拒绝
    with pytest.raises(OSError):
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass


def test_graceful_stop_via_cli(tmp_path):
    """CLI 启动后 SIGTERM 优雅退出，stdout 只有 ready JSON。"""
    import subprocess
    # 启动子进程，读一行 stdout，发信号优雅停止
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        [sys.executable, "-m", "desktop_backend", "--static-dir", str(tmp_path / "desktop_ui"), "--log-level", "WARNING"],
        cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH": REPO_ROOT}, **kwargs,
    )
    try:
        line = proc.stdout.readline().strip()
        assert line, "子进程未输出 ready JSON"
        payload = json.loads(line)
        assert payload["type"] == "ready" and payload["protocol"] == 1
        # 发信号优雅停止（Windows 用 CTRL_BREAK）
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
        proc.wait(timeout=15)
        assert proc.returncode == 0, f"退出码 {proc.returncode}: {proc.stderr.read()}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


# =====================================================================
# 10. 杂项：并发 bootstrap 只成功一次
# =====================================================================

def test_bootstrap_concurrent_once(tmp_path):
    with started(tmp_path) as (server, *_):
        async def go():
            import aiohttp
            async with aiohttp.ClientSession() as s:
                url = f"http://127.0.0.1:{server.port}{server.bootstrap_path}"
                results = await asyncio.gather(
                    *[s.get(url) for _ in range(8)],
                    return_exceptions=False,
                )
                statuses = [r.status for r in results]
                for r in results:
                    await r.release()
                return statuses

        statuses = _run(go())
        assert statuses.count(200) == 1, f"并发 bootstrap 应只有一次成功: {statuses}"
        assert all(s == 404 for s in statuses if s != 200)

"""环境中心 setup API 的 HTTP 层测试。

仿 test_agent07_backend_api.py 的 make_server 模式：真实 aiohttp 服务 + 注入
SetupManager，走完整 bootstrap / Session / Origin / CSRF / CSP 安全中间件。
运行：cd 源码根目录 && pytest tests_desktop/test_setup_handlers.py -q
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import sys
import threading
import time

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from pathlib import Path  # noqa: E402

from desktop_backend.security import SESSION_COOKIE_NAME  # noqa: E402
from desktop_backend.server import WineFoxBackendServer  # noqa: E402
from desktop_runtime.setup_manager import SetupManager  # noqa: E402

from test_setup_manager import build_manifest, closed_port, make_comfyui_root  # noqa: E402


# =====================================================================
# 测试基建（与 test_agent07_backend_api.py 同构）
# =====================================================================

_LOOP: asyncio.AbstractEventLoop | None = None
_LOOP_THREAD: threading.Thread | None = None


@pytest.fixture(scope="session", autouse=True)
def _shared_loop():
    global _LOOP, _LOOP_THREAD
    _LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(_LOOP)
    _LOOP_THREAD = threading.Thread(target=_LOOP.run_forever, name="winefox-setup-test-loop", daemon=True)
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


async def _request(server, method, path, *, cookies=None, headers=None, raw=False, **kw):
    import aiohttp

    async with asyncio.timeout(15):
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
    status, headers, js = get(server, server.bootstrap_path)
    assert status == 200, f"bootstrap 失败: {status} {js}"
    set_cookie = headers.getall("Set-Cookie")
    assert set_cookie, "bootstrap 响应缺少 Set-Cookie"
    cookie = set_cookie[0]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    return _cookie_from_headers(headers)


class SetupFakeEngine:
    """reprobe 需要的引擎：probe_lora_bridge + refresh_after_env_setup。"""

    def __init__(self) -> None:
        self.refreshed = False

    async def probe_lora_bridge(self):
        return {"ok": True, "installed": True}

    async def refresh_after_env_setup(self):
        self.refreshed = True
        return {"ok": True}


class FakeConfig:
    def __init__(self) -> None:
        self.secrets: dict[str, str] = {}

    async def put_secret(self, name, value):
        self.secrets[name] = value

    async def get_secret(self, name):
        return self.secrets.get(name)


class FakeUploadStore:
    pass


@contextlib.contextmanager
def _env(tmp_path, *, engine=None, setup=None):
    """构造真实服务：注入 SetupManager（默认）+ 临时 ComfyUI 根。"""
    if setup is None:
        root = make_comfyui_root(Path(tmp_path))
        install = Path(tmp_path) / "WineFox"
        install.mkdir(exist_ok=True)
        setup = SetupManager(
            manifest=build_manifest(base_url=f"http://127.0.0.1:{closed_port()}"),
            install_dir=install,
            work_dir=Path(tmp_path) / "work",
        )
    else:
        root = make_comfyui_root(Path(tmp_path))
    static_dir = Path(tmp_path) / "desktop_ui"
    static_dir.mkdir(exist_ok=True)
    (static_dir / "index.html").write_text("<html>winefox ui</html>", encoding="utf-8")
    server = WineFoxBackendServer(
        engine=engine if engine is not None else SetupFakeEngine(),
        config=FakeConfig(),
        uploads=FakeUploadStore(),
        setup=setup,
        static_dir=static_dir,
    )
    _run(server.start(ready_stream=io.StringIO()))
    try:
        yield server, setup, root
    finally:
        _run(server.stop())
        _run(setup.close())


def _collect_strings(obj) -> list[str]:
    out: list[str] = []
    if isinstance(obj, dict):
        for value in obj.values():
            out.extend(_collect_strings(value))
    elif isinstance(obj, list):
        for item in obj:
            out.extend(_collect_strings(item))
    elif isinstance(obj, str):
        out.append(obj)
    return out


def _assert_no_absolute_path(payload) -> None:
    for value in _collect_strings(payload):
        assert not value.startswith(("C:", "D:", "\\\\", "/")), f"泄露绝对路径: {value!r}"


# =====================================================================
# 未注入 setup -> 503
# =====================================================================

def test_setup_endpoints_503_without_manager(tmp_path):
    # 显式注入 None：所有 setup 路由返回 503
    static_dir = Path(tmp_path) / "desktop_ui"
    static_dir.mkdir(exist_ok=True)
    (static_dir / "index.html").write_text("<html>winefox ui</html>", encoding="utf-8")
    server = WineFoxBackendServer(
        engine=SetupFakeEngine(),
        config=FakeConfig(),
        uploads=FakeUploadStore(),
        setup=None,
        static_dir=static_dir,
    )
    _run(server.start(ready_stream=io.StringIO()))
    try:
        session = bootstrap(server)
        cookies = {SESSION_COOKIE_NAME: session}
        status, _, js = get(server, "/api/v1/setup/summary", cookies=cookies)
        assert status == 503 and js["error"]["code"] == "setup_not_configured"
        status, _, js = get(server, "/api/v1/setup/capabilities", cookies=cookies)
        assert status == 503
    finally:
        _run(server.stop())


# =====================================================================
# summary / capabilities / scan / plan
# =====================================================================

def test_summary_no_absolute_paths(tmp_path):
    with _env(tmp_path) as (server, manager, _root):
        session = bootstrap(server)
        cookies = {SESSION_COOKIE_NAME: session}
        status, _, js = get(server, "/api/v1/setup/summary", cookies=cookies)
        assert status == 200 and js["ok"] is True
        data = js["data"]
        assert data["state_code"] == "repair_needed"
        assert data["root"]["name"] == "ComfyUI"
        assert "\\" not in data["root"]["display"]
        _assert_no_absolute_path(data)


def test_capabilities_lists_tiers_and_manual(tmp_path):
    with _env(tmp_path) as (server, manager, _root):
        session = bootstrap(server)
        cookies = {SESSION_COOKIE_NAME: session}
        status, _, js = get(server, "/api/v1/setup/capabilities", cookies=cookies)
        assert status == 200 and js["ok"] is True
        caps = js["data"]
        assert "lite" in caps["profiles"]
        assert caps["bridge"]["version"] == "1.4.2"
        assert [m["name"] for m in caps["manual_files"]] == ["manual-a.safetensors"]
        assert "127.0.0.1" in caps["allowed_hosts"]
        _assert_no_absolute_path(caps)


def test_scan_and_plan(tmp_path):
    with _env(tmp_path) as (server, manager, root):
        session = bootstrap(server)
        cookies = {SESSION_COOKIE_NAME: session}
        hdr = {"Content-Type": "application/json", "X-WineFox-Client": "1"}

        status, _, js = post(server, "/api/v1/setup/scans", cookies=cookies, data="{}", headers=hdr)
        assert status == 200 and js["data"]["state_code"] == "repair_needed"

        status, _, js = post(
            server, "/api/v1/setup/scans", cookies=cookies,
            data=json.dumps({"root": str(root)}), headers=hdr,
        )
        assert status == 200 and js["data"]["root"]["name"] == "ComfyUI"

        status, _, js = post(
            server, "/api/v1/setup/plans", cookies=cookies,
            data=json.dumps({"profile": "lite"}), headers=hdr,
        )
        assert status == 200
        plan = js["data"]
        assert plan["manual_required"] is True
        assert [s["kind"] for s in plan["steps"]] == ["nodes", "models", "bridge"]
        _assert_no_absolute_path(plan)

        # 缺参数 / 未知 profile / 未知步骤 -> 422 稳定码（非 500）
        status, _, js = post(server, "/api/v1/setup/plans", cookies=cookies, data="{}", headers=hdr)
        assert status == 422 and js["error"]["code"] == "SETUP_CONFIG_INVALID"
        status, _, js = post(
            server, "/api/v1/setup/plans", cookies=cookies,
            data=json.dumps({"profile": "nope"}), headers=hdr,
        )
        assert status == 422 and js["error"]["code"] == "validation_error"
        status, _, js = post(
            server, "/api/v1/setup/plans", cookies=cookies,
            data=json.dumps({"custom": [{"step": "bogus"}]}), headers=hdr,
        )
        assert status == 422 and js["error"]["code"] == "validation_error"


# =====================================================================
# jobs 生命周期 + SSE
# =====================================================================

def test_setup_jobs_lifecycle(tmp_path):
    with _env(tmp_path) as (server, manager, root):
        session = bootstrap(server)
        cookies = {SESSION_COOKIE_NAME: session}
        hdr = {"Content-Type": "application/json", "X-WineFox-Client": "1"}

        # 只规划 models 一步：模型 URL 指向已关闭端口，下载失败且与网络无关
        status, _, js = post(
            server, "/api/v1/setup/plans", cookies=cookies,
            data=json.dumps({"custom": [{"step": "models", "group": "core"}], "only_missing": False}),
            headers=hdr,
        )
        assert status == 200
        plan = js["data"]

        status, _, js = post(
            server, "/api/v1/setup/jobs", cookies=cookies,
            data=json.dumps({"root": str(root), "plan": plan}), headers=hdr,
        )
        assert status == 202 and js["data"]["status"] == "pending"
        job_id = js["data"]["id"]

        # 轮询到终态
        for _ in range(100):
            status, _, js = get(server, f"/api/v1/setup/jobs/{job_id}", cookies=cookies)
            assert status == 200
            if js["data"]["status"] in ("success", "failed", "cancelled", "rolled_back"):
                break
            time.sleep(0.05)
        assert js["data"]["status"] == "failed"
        assert js["data"]["error_code"] == "SETUP_NETWORK_UNREACHABLE"
        assert js["data"]["root"] is None  # 绝不回显绝对路径

        # jobs 列表
        status, _, js = get(server, "/api/v1/setup/jobs", cookies=cookies)
        assert status == 200 and js["data"]["items"][0]["id"] == job_id

        # SSE 事件流（已结束的任务：subscribe 播种历史事件，读到 job_finished 即收尾）
        status, headers, body = _run(
            _request(server, "GET", f"/api/v1/setup/jobs/{job_id}/events", cookies=cookies, raw=True)
        )
        assert status == 200
        assert headers["Content-Type"] == "text/event-stream"
        text = body.decode("utf-8", "replace")
        assert "job_started" in text and "job_finished" in text
        assert "SETUP_NETWORK_UNREACHABLE" in text

        # retry -> 新 job；rollback 原始 job
        status, _, js = post(
            server, f"/api/v1/setup/jobs/{job_id}/retry", cookies=cookies, data="{}", headers=hdr
        )
        assert status == 202 and js["data"]["id"] != job_id
        retried_id = js["data"]["id"]
        for _ in range(100):
            status, _, js = get(server, f"/api/v1/setup/jobs/{retried_id}", cookies=cookies)
            if js["data"]["status"] in ("success", "failed", "cancelled", "rolled_back"):
                break
            time.sleep(0.05)

        status, _, js = post(
            server, f"/api/v1/setup/jobs/{job_id}/rollback", cookies=cookies, data="{}", headers=hdr
        )
        assert status == 200 and js["data"]["rolled_back"] is True

        # 终态 job 不能取消 -> 409
        status, _, js = post(
            server, f"/api/v1/setup/jobs/{job_id}/cancel", cookies=cookies, data="{}", headers=hdr
        )
        assert status == 409 and js["error"]["code"] == "setup_job_busy"

        # 未知 job -> 404
        status, _, js = get(server, "/api/v1/setup/jobs/nope", cookies=cookies)
        assert status == 404 and js["error"]["code"] == "setup_job_not_found"
        status, _, js = get(server, "/api/v1/setup/jobs/nope/events", cookies=cookies)
        assert status == 404 and js["error"]["code"] == "setup_job_not_found"


# =====================================================================
# reprobe / remote-kit
# =====================================================================

def test_reprobe_calls_engine(tmp_path):
    engine = SetupFakeEngine()
    with _env(tmp_path, engine=engine) as (server, manager, _root):
        session = bootstrap(server)
        cookies = {SESSION_COOKIE_NAME: session}
        hdr = {"Content-Type": "application/json", "X-WineFox-Client": "1"}
        status, _, js = post(server, "/api/v1/setup/reprobe", cookies=cookies, data="{}", headers=hdr)
        assert status == 200
        assert js["data"]["bridge"]["installed"] is True
        assert engine.refreshed is True


def test_remote_kit_no_credentials(tmp_path):
    with _env(tmp_path) as (server, manager, _root):
        session = bootstrap(server)
        cookies = {SESSION_COOKIE_NAME: session}
        status, _, js = get(server, "/api/v1/setup/remote-kit", cookies=cookies)
        assert status == 200
        kit = js["data"]
        assert kit["configured"] is False
        assert "token" not in json.dumps(kit).lower()
        assert kit["bridge_version"] == "1.4.2"
        _assert_no_absolute_path(kit)


# =====================================================================
# 安全中间件在 setup 路由上同样生效
# =====================================================================

def test_setup_security_gate(tmp_path):
    with _env(tmp_path) as (server, manager, _root):
        # 无会话 -> 401
        status, _, js = get(server, "/api/v1/setup/summary")
        assert status == 401 and js["error"]["code"] == "unauthorized"
        session = bootstrap(server)
        cookies = {SESSION_COOKIE_NAME: session}
        # 恶意 Origin -> 403
        status, _, js = get(
            server, "/api/v1/setup/summary", cookies=cookies,
            headers={"Origin": "http://evil.com"},
        )
        assert status == 403 and js["error"]["code"] == "bad_origin"
        # POST 无 Origin 也无 X-WineFox-Client -> CSRF 拦截
        status, _, js = post(
            server, "/api/v1/setup/scans", cookies=cookies,
            data="{}", headers={"Content-Type": "application/json"},
        )
        assert status == 403 and js["error"]["code"] == "csrf_blocked"
        # 合法 Origin 的 POST -> 放行
        status, _, js = post(
            server, "/api/v1/setup/scans", cookies=cookies, data="{}",
            headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{server.port}"},
        )
        assert status == 200

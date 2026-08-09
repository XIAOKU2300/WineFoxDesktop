"""会话安全：一次性 bootstrap、进程级 Cookie、Host/Origin/CSRF 校验。

设计：
- 服务启动时生成 256 位（32 字节）一次性 bootstrap token；``GET /bootstrap/{token}``
  成功后即作废，并下发进程级会话 Cookie（HttpOnly + SameSite=Strict，整个进程
  生命周期内值不变）。
- 所有 ``/api/v1/*``（除 health）必须携带会话 Cookie；不接受 query token、固定密码。
- Host 必须精确等于 ``127.0.0.1:{port}`` / ``localhost:{port}``；Origin（若存在）同理。
- 写操作（POST/PUT/PATCH/DELETE）必须满足：合法 Origin，或自定义头
  ``X-WineFox-Client: 1``（非浏览器客户端；浏览器无法跨域自带头部，且无 CORS）。
"""

from __future__ import annotations

import hmac
import logging
import secrets
import threading

from aiohttp import web

from .errors import AppError

logger = logging.getLogger("winefox.security")

SESSION_COOKIE_NAME = "winefox_session"
CSRF_HEADER = "X-WineFox-Client"
CSRF_HEADER_VALUE = "1"
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

API_PREFIX = "/api/v1"
BOOTSTRAP_PREFIX = "/bootstrap/"


class SecurityManager:
    """进程级安全状态。线程安全（bootstrap 竞态用锁保护）。"""

    def __init__(self, extra_allowed_hosts: tuple[str, ...] = ()) -> None:
        self._lock = threading.Lock()
        self._bootstrap_token: str | None = None
        self._bootstrap_used = False
        self._session_token: str | None = None
        self._port: int | None = None
        self._extra_allowed_hosts = tuple(extra_allowed_hosts or ())

    # ---------- 状态 ----------

    def arm(self, port: int, bootstrap_token: str | None = None) -> str:
        """绑定端口并生成一次性 bootstrap token（256 位）。"""
        with self._lock:
            self._port = int(port)
            self._bootstrap_token = bootstrap_token or secrets.token_hex(32)
            self._bootstrap_used = False
            self._session_token = None
            return self._bootstrap_token

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("SecurityManager 未 arm")
        return self._port

    @property
    def bootstrap_token(self) -> str:
        if self._bootstrap_token is None:
            raise RuntimeError("SecurityManager 未 arm")
        return self._bootstrap_token

    @property
    def bootstrap_used(self) -> bool:
        return self._bootstrap_used

    @property
    def session_token(self) -> str | None:
        return self._session_token

    def is_bootstrapped(self) -> bool:
        return self._session_token is not None

    @property
    def allowed_hosts(self) -> set[str]:
        p = self.port
        hosts = {f"127.0.0.1:{p}", f"localhost:{p}"}
        hosts.update(self._extra_allowed_hosts)
        return hosts

    @property
    def allowed_origins(self) -> set[str]:
        p = self.port
        return {f"http://127.0.0.1:{p}", f"http://localhost:{p}"}

    # ---------- bootstrap ----------

    def consume_bootstrap(self, token: str) -> str | None:
        """消费 bootstrap token。成功返回会话 token（首次生成），失败返回 None。
        有效 token 只可成功一次；错误 token 不消耗。"""
        with self._lock:
            if self._bootstrap_token is None or self._bootstrap_used:
                return None
            if not hmac.compare_digest(str(token), self._bootstrap_token):
                return None
            self._bootstrap_used = True
            if self._session_token is None:
                self._session_token = secrets.token_hex(32)  # 进程级：进程生命周期内不变
            return self._session_token

    # ---------- 请求校验 ----------

    def check_host(self, request: web.Request) -> None:
        host = request.host  # aiohttp 已解析 Host 头
        if host not in self.allowed_hosts:
            logger.warning("拒绝非本地 Host: %r", host)
            raise AppError(403, "bad_host", "Host 不被允许")

    def check_origin(self, request: web.Request, *, required: bool) -> None:
        origin = request.headers.get("Origin")
        if origin is None:
            if required:
                raise AppError(403, "csrf_blocked", "写操作缺少 Origin 或 X-WineFox-Client")
            return
        if origin not in self.allowed_origins:
            logger.warning("拒绝非本地 Origin: %r", origin)
            raise AppError(403, "bad_origin", "Origin 不被允许")

    def check_csrf(self, request: web.Request) -> None:
        """写操作 CSRF：合法 Origin 或自定义头二选一。"""
        origin = request.headers.get("Origin")
        if origin in self.allowed_origins:
            return
        if request.headers.get(CSRF_HEADER) == CSRF_HEADER_VALUE:
            return
        raise AppError(403, "csrf_blocked", "写操作缺少 Origin 或 X-WineFox-Client")

    def check_session(self, request: web.Request) -> None:
        """会话 Cookie 校验。不接受 query token。"""
        session = self._session_token
        if session is None:
            raise AppError(401, "unauthorized", "尚未 bootstrap，请先获取会话")
        cookie = request.cookies.get(SESSION_COOKIE_NAME, "")
        if not cookie or not hmac.compare_digest(cookie, session):
            raise AppError(401, "unauthorized", "缺少或无效的会话 Cookie")

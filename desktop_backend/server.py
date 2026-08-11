"""服务生命周期：绑定 127.0.0.1:0、ready JSON、优雅 stop。

stdout 只输出一次 ready JSON（其余日志一律走 stderr）：

    {"type":"ready","protocol":1,"version":"8.5.10",
     "host":"127.0.0.1","port":51234,"bootstrapPath":"/bootstrap/<token>"}
"""

from __future__ import annotations

import json
import logging
import socket
import sys
from typing import Any, Optional, TextIO

from aiohttp import web

from . import DEFAULT_STATIC_DIR, PROTOCOL_VERSION, VERSION
from .app import create_app
from .handlers import CTX_KEY
from .security import SecurityManager

logger = logging.getLogger("winefox.server")

class WineFoxBackendServer:
    """本地后端服务。仅绑定 127.0.0.1，端口 0（系统分配）。"""

    HOST = "127.0.0.1"

    def __init__(
        self,
        *,
        engine: Any = None,
        config: Any = None,
        uploads: Any = None,
        setup: Any = None,
        static_dir: Optional[str] = DEFAULT_STATIC_DIR,
        max_upload_bytes: int = 20 * 1024 * 1024,
        csp: Optional[str] = None,
        extra_allowed_hosts: tuple[str, ...] = (),
        bootstrap_token: Optional[str] = None,
        app: Optional[web.Application] = None,
    ) -> None:
        self._app = app or create_app(
            engine=engine,
            config=config,
            uploads=uploads,
            setup=setup,
            static_dir=static_dir,
            max_upload_bytes=max_upload_bytes,
            csp=csp,
            extra_allowed_hosts=extra_allowed_hosts,
        )
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._port: Optional[int] = None
        self._started = False
        self._bootstrap_token: Optional[str] = bootstrap_token
        self.ready_payload: Optional[dict] = None

    # ---------------- 生命周期 ----------------

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("服务尚未启动")
        return self._port

    @property
    def host(self) -> str:
        return self.HOST

    @property
    def started(self) -> bool:
        return self._started

    @property
    def bootstrap_path(self) -> str:
        if self._bootstrap_token is None:
            raise RuntimeError("服务尚未启动")
        return f"/bootstrap/{self._bootstrap_token}"

    @property
    def base_url(self) -> str:
        return f"http://{self.HOST}:{self.port}"

    @property
    def security(self) -> SecurityManager:
        return self._app[CTX_KEY]["security"]

    async def start(self, *, ready_stream: Optional[TextIO] = sys.stdout) -> dict:
        """启动服务。成功后向 ready_stream（默认 stdout）输出 ready JSON。"""
        if self._started:
            return self.ready_payload or {}

        # 先探测空闲端口（127.0.0.1:0 -> 系统分配），再绑定 host:port
        self._port = self._probe_free_port()
        security = self.security
        self._bootstrap_token = security.arm(self._port, bootstrap_token=self._bootstrap_token)

        last_exc: Optional[Exception] = None
        for _ in range(5):  # 端口竞态重试
            self._runner = web.AppRunner(self._app, access_log=None)
            try:
                await self._runner.setup()
                self._site = web.TCPSite(self._runner, host=self.HOST, port=self._port)
                await self._site.start()
                self._started = True
                break
            except OSError as exc:  # 端口被占，换一个
                last_exc = exc
                self._port = self._probe_free_port()
                self._bootstrap_token = security.arm(self._port, bootstrap_token=self._bootstrap_token)
                await self._runner.cleanup()
                self._runner = None
        else:
            raise RuntimeError("无法绑定本地端口") from last_exc

        payload = {
            "type": "ready",
            "protocol": PROTOCOL_VERSION,
            "version": VERSION,
            "host": self.HOST,
            "port": self._port,
            "bootstrapPath": self.bootstrap_path,
        }
        self.ready_payload = payload
        if ready_stream is not None:
            print(json.dumps(payload, ensure_ascii=False), flush=True, file=ready_stream)
        logger.info("后端已就绪 %s (protocol=%s, version=%s)", self.base_url, PROTOCOL_VERSION, VERSION)
        return payload

    def _probe_free_port(self) -> int:
        """向系统申请一个空闲的 loopback 端口。"""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((self.HOST, 0))
            return int(s.getsockname()[1])

    async def stop(self) -> None:
        """优雅停止：关闭监听、清理 runner、释放端口。可重复调用。"""
        if self._site is not None:
            try:
                await self._site.stop()
            except Exception:
                logger.exception("停止 site 失败")
            self._site = None
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                logger.exception("清理 runner 失败")
            self._runner = None
        self._started = False
        logger.info("后端已停止")

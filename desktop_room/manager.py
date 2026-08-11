"""房间生命周期管理：启动/停止房间网关、Tunnel 配置、room_status 上报。

由 sidecar 进程持有；房间网关只监听 127.0.0.1:<随机端口>，公网访问只能走 Tunnel。
每次状态变化都通过 emit({"type":"room_status",...}) 推给 Rust（host_token 单独字段）。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from aiohttp import web

from . import protocol
from .gateway import create_room_app
from .security import hash_password, new_host_token, new_session_token, normalize_lookup
from .storage import RoomStorage, new_public_id, utcnow_iso
from .tunnel import (
    TunnelValidationError,
    validate_tunnel_url_syntax,
    verify_local_identity,
    verify_tunnel,
)

log = logging.getLogger(__name__)

# 关闭房间时的优雅停机等待（秒）。
_ROOM_STOP_GRACE_SECONDS = 3.0

_ROOM_STOPPED = "stopped"


def _detect_lan_url(port: int) -> str | None:
    """探测本机局域网 IP 并返回 http://<LAN-IP>:<port>/（绑定 0.0.0.0 时供同网段设备访问）。"""
    import socket

    try:
        _, _, ips = socket.gethostbyname_ex(socket.gethostname())
    except Exception:  # pragma: no cover - 探测失败不影响主流程
        return None
    for ip in ips:
        if ip and not ip.startswith("127.") and "." in ip:
            return f"http://{ip}:{port}/"
    return None


class RoomStartError(Exception):
    """房间启动失败。error_code 为稳定机器码。"""

    def __init__(self, message: str, error_code: str = "ROOM_START_FAILED") -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code


class RoomManager:
    def __init__(self, *, emit: Callable[[dict[str, Any]], None], rooms_root: Path,
                 generation_backend: Any = None,
                 generation_poll_interval: float = 0.4) -> None:
        self._emit = emit
        self._rooms_root = Path(rooms_root)
        self._generation_backend = generation_backend
        self._generation_poll_interval = float(generation_poll_interval)
        self._lock = threading.Lock()
        self._storage: RoomStorage | None = None
        self._gateway: web.TCPSite | None = None
        self._runner: web.AppRunner | None = None
        self._app: web.Application | None = None
        self._room_id: int | None = None
        self._data_dir: Path | None = None
        self._loop: Any = None
        self._owner_token: str | None = None
        self._status: dict[str, Any] = self._empty_status()

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    @staticmethod
    def _empty_status() -> dict[str, Any]:
        return {
            "state": _ROOM_STOPPED,
            "publicId": None,
            "name": None,
            "ownerName": None,
            "port": None,
            "bindHost": None,
            "localUrl": None,
            "lanUrl": None,
            "tunnelUrl": None,
            "tunnelConnected": False,
            "verifyWarning": None,
            "members": 0,
            "maxMembers": 0,
            "error": None,
            "memberLimitReached": False,
            "generationPaused": False,
            "membersJoinPaused": False,
            "failedLoginAttempts": 0,
        }

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def _publish(self, *, host_token: str | None = None) -> None:
        with self._lock:
            payload: dict[str, Any] = {"type": "room_status"}
            payload.update(dict(self._status))
            if host_token:
                payload["host_token"] = host_token
        try:
            self._emit(payload)
        except Exception:  # pragma: no cover - 上报失败不应影响主流程
            log.warning("emit room_status failed", exc_info=True)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self, settings: dict[str, Any]) -> dict[str, Any]:
        """启动房间：建库 → 建房 → 启动网关 → 上报 online。返回 room_status。"""
        if self.is_running():
            return self.get_status()
        if not isinstance(settings, dict):
            raise RoomStartError("房间设置无效", "INVALID_ROOM_SETTINGS")
        name = str(settings.get("name") or "").strip()
        owner_name = str(settings.get("ownerName") or "").strip()
        password = str(settings.get("password") or "")
        if not name or not owner_name or not password:
            raise RoomStartError("房间名称、房主名称和密码为必填项", "MISSING_FIELDS")
        if len(name) > 64:
            raise RoomStartError("房间名称过长", "INVALID_ROOM_NAME")
        if len(owner_name) > 24:
            raise RoomStartError("房主名称过长", "INVALID_OWNER_NAME")

        port = settings.get("port")
        if port is not None:
            if isinstance(port, bool) or not isinstance(port, int) or not (1024 <= port <= 65535):
                raise RoomStartError("端口需在 1024-65535 之间", "INVALID_PORT")
        bind_host = str(settings.get("bindHost") or "0.0.0.0")
        if bind_host not in ("0.0.0.0", "127.0.0.1"):
            raise RoomStartError("绑定地址无效", "INVALID_BIND_HOST")

        public_id = new_public_id()
        algorithm, params_json, salt_b64, hash_b64 = hash_password(password)
        host_token = new_host_token()

        self._loop = asyncio_get_running_loop()
        rooms_root = self._rooms_root
        rooms_root.mkdir(parents=True, exist_ok=True)

        storage = RoomStorage(rooms_root / f"{public_id}.db")
        room_id = storage.create_room(
            public_id=public_id,
            name=name,
            owner_name=owner_name,
            password_algorithm=algorithm,
            password_parameters_json=params_json,
            password_salt=salt_b64,
            password_hash=hash_b64,
            settings=settings,
        )
        data_dir = rooms_root / public_id
        data_dir.mkdir(parents=True, exist_ok=True)

        app = create_room_app(
            storage=storage, room_id=room_id, host_token=host_token,
            data_dir=data_dir,
            generation_backend=self._generation_backend,
            generation_poll_interval=self._generation_poll_interval,
        )
        app["notify_login_failure"] = self._on_login_failure
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host=bind_host, port=(port or 0))
        try:
            await site.start()
        except OSError as exc:
            await runner.cleanup()
            storage.close()
            raise RoomStartError(
                f"端口被占用或无法绑定：{port if port else '随机端口'}",
                "PORT_BIND_FAILED",
            ) from exc
        try:
            port = int(site._server.sockets[0].getsockname()[1])
        except (AttributeError, IndexError, TypeError):  # pragma: no cover - 防御
            raise RoomStartError("无法获取房间端口", "PORT_BIND_FAILED")

        # 房主以 role=owner 自动入房（首名成员）。
        owner_token, owner_hash = new_session_token()
        storage.add_member(
            room_id=room_id,
            display_name=owner_name,
            normalized_name=normalize_lookup(owner_name),
            role=protocol.ROLE_OWNER,
            session_hash=owner_hash,
        )
        storage.set_room_state(room_id, protocol.ROOM_STATE_ONLINE)

        with self._lock:
            self._storage = storage
            self._runner = runner
            self._gateway = site
            self._app = app
            self._room_id = room_id
            self._data_dir = data_dir
            self._status = {
                "state": protocol.ROOM_STATE_ONLINE,
                "publicId": public_id,
                "name": name,
                "ownerName": owner_name,
                "port": port,
                "bindHost": bind_host,
                "localUrl": f"http://127.0.0.1:{port}/",
                "lanUrl": _detect_lan_url(port) if bind_host == "0.0.0.0" else None,
                "tunnelUrl": None,
                "tunnelConnected": False,
                "verifyWarning": None,
                "members": 1,
                "maxMembers": int(settings.get("maxMembers", 8)),
                "error": None,
                "memberLimitReached": False,
                "generationPaused": False,
                "membersJoinPaused": False,
                "failedLoginAttempts": 0,
            }
            self._owner_token = owner_token  # 房主会话令牌（后续注入房主登录）
        self._publish(host_token=host_token)
        return self.get_status()

    def is_running(self) -> bool:
        with self._lock:
            return self._status["state"] == protocol.ROOM_STATE_ONLINE

    def fail(self, message: str, error_code: str = "ROOM_START_FAILED") -> None:
        """标记房间启动失败并上报（供 sidecar 捕获 start() 异常后调用）。"""
        with self._lock:
            self._status["state"] = protocol.ROOM_STATE_FAILED
            self._status["error"] = message
            self._status["errorCode"] = error_code
        self._publish()

    def _on_login_failure(self, count: int) -> None:
        with self._lock:
            self._status["failedLoginAttempts"] = int(count)
        self._publish()

    async def stop(self) -> None:
        """优雅关闭：先标记 closing + 广播 room.closing，再停网关、关库，最后上报 stopped。"""
        with self._lock:
            if self._status["state"] in (_ROOM_STOPPED, protocol.ROOM_STATE_CLOSED):
                return
            self._status["state"] = protocol.ROOM_STATE_CLOSING
        self._publish()
        app = self._app
        if app is not None:
            hub = app.get("chat_hub")
            if hub is not None:
                try:
                    await hub.broadcast_room_closing()
                    await hub.close_all()
                except Exception:  # pragma: no cover
                    log.warning("room.closing broadcast failed", exc_info=True)
            bridge = app.get("generation_bridge")
            if bridge is not None:
                try:
                    await bridge.stop_all()
                except Exception:  # pragma: no cover
                    log.warning("room generation bridge stop failed", exc_info=True)
        runner, storage, room_id = self._runner, self._storage, self._room_id
        self._runner = None
        self._gateway = None
        self._storage = None
        self._room_id = None
        self._data_dir = None
        self._owner_token = None
        try:
            if runner is not None:
                await runner.cleanup()
        except Exception:  # pragma: no cover
            log.warning("room gateway cleanup failed", exc_info=True)
        if room_id is not None and storage is not None:
            try:
                storage.set_room_state(room_id, protocol.ROOM_STATE_CLOSED)
            except Exception:  # pragma: no cover
                pass
            storage.close()
        with self._lock:
            self._status = self._empty_status()
        self._publish()

    async def shutdown(self) -> None:
        """进程退出时的兜底关闭（不抛异常）。"""
        try:
            await self.stop()
        except Exception:  # pragma: no cover
            log.warning("room shutdown failed", exc_info=True)

    # ------------------------------------------------------------------
    # Tunnel
    # ------------------------------------------------------------------
    async def set_tunnel(self, url: str) -> dict[str, Any]:
        public_id = self._status["publicId"]
        if not public_id:
            raise TunnelValidationError("当前没有运行中的房间")
        validated = validate_tunnel_url_syntax(url)
        parsed = urlparse(validated)
        scheme = (parsed.scheme or "").lower()
        hostname = (parsed.hostname or "").lower()
        port = self._status.get("port")
        # 网关中间件只放行 allow_tunnel_hosts 中的 Host；Host 保留型隧道
        # （FRP/nginx/Caddy）与直连不改写 Host 头，若先验证后放行本机探测必 403。
        # 因此先临时放行，任何失败分支都回滚，保证 allowlist 与状态原子一致。
        with self._lock:
            prev_allowed = list(self._app.get("allow_tunnel_hosts", ())) if self._app is not None else []
            if self._app is not None and hostname and hostname not in prev_allowed:
                self._app["allow_tunnel_hosts"] = prev_allowed + [hostname]
        try:
            if not isinstance(port, int) or not await verify_local_identity(hostname, port, public_id):
                raise TunnelValidationError("该地址的 Host 未指向本房间，请检查输入")
            verify_warning = None
            if scheme == "https":
                if not await verify_tunnel(validated, public_id):
                    raise TunnelValidationError("Tunnel 不可达或未指向本房间，请检查穿透配置")
            else:
                if not await verify_tunnel(validated, public_id):
                    verify_warning = (
                        "本机无法直接访问该公网地址，请确认路由器/防火墙已把端口转发到本机"
                        "（NAT hairpin 下自测失败属正常）"
                    )
        except Exception:
            with self._lock:
                if self._app is not None:
                    self._app["allow_tunnel_hosts"] = prev_allowed
            raise
        with self._lock:
            self._status["tunnelUrl"] = validated
            self._status["tunnelConnected"] = True
            self._status["verifyWarning"] = verify_warning
        self._publish()
        return self.get_status()

    def clear_tunnel(self) -> dict[str, Any]:
        with self._lock:
            self._status["tunnelUrl"] = None
            self._status["tunnelConnected"] = False
            self._status["verifyWarning"] = None
            if self._app is not None:
                self._app["allow_tunnel_hosts"] = []
        self._publish()
        return self.get_status()


def asyncio_get_running_loop() -> Any:
    import asyncio

    return asyncio.get_running_loop()

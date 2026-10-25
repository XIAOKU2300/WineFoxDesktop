"""Real sidecar process entry preserving the Agent 09 JSON-lines protocol."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import threading
import webbrowser
from typing import Any

from desktop_backend import PROTOCOL_VERSION, VERSION

from .runtime import DesktopRuntime


_STDOUT_LOCK = threading.Lock()


def emit(value: dict[str, Any]) -> None:
    with _STDOUT_LOCK:
        line = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            data = line.encode("utf-8")
        except UnicodeEncodeError:
            # 负载含孤立代理字符（Windows IME/剪贴板可能产生）时，UTF-8 无法编码。
            # 退回全转义序列（Rust 侧 serde_json 可正常解析 \uXXXX），
            # 保证 sidecar 永不因编码错误崩溃。
            line = json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n"
            data = line.encode("utf-8")
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="winefox-sidecar")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    return parser


def _start_stdin_reader(
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue[dict[str, Any]],
    stop_event: asyncio.Event,
) -> threading.Thread:
    def read() -> None:
        try:
            for raw in sys.stdin:
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if isinstance(message, dict):
                    loop.call_soon_threadsafe(queue.put_nowait, message)
        except (OSError, ValueError):
            pass
        finally:
            # stdin EOF / 管道破裂 = 父进程已退出：置 stop_event 让主循环
            # 走优雅关停。这是 Rust 壳层 Job Object 之外的第二道孤儿保险
            # （见 desktop_shell/src-tauri/src/job_object.rs 的契约注释）。
            loop.call_soon_threadsafe(stop_event.set)
    thread = threading.Thread(target=read, name="winefox-stdin", daemon=True)
    thread.start()
    return thread


async def _health_selftest(port: int) -> None:
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://127.0.0.1:{port}/api/health") as response:
            data = await response.json()
            if response.status != 200 or not data.get("ok"):
                raise RuntimeError("sidecar health selftest failed")


async def _room_start(runtime: DesktopRuntime, command: dict[str, Any]) -> None:
    """处理 room_start：启动房间网关；失败返回带稳定错误码的 ack。"""
    manager = runtime.room_manager
    if manager is None:
        emit({"type": "ack", "cmd": "room_start", "ok": False, "error": "房间管理器未就绪",
              "errorCode": "ROOM_MANAGER_UNAVAILABLE"})
        return
    settings = command.get("settings") if isinstance(command.get("settings"), dict) else {}
    try:
        status = await manager.start(settings)
    except Exception as exc:
        error_code = getattr(exc, "error_code", "ROOM_START_FAILED")
        message = getattr(exc, "message", None) or str(exc) or "房间启动失败"
        manager.fail(message, error_code)
        emit({"type": "ack", "cmd": "room_start", "ok": False, "error": message, "errorCode": error_code})
        return
    emit({"type": "ack", "cmd": "room_start", "ok": True, "room": status})


async def _room_tunnel(runtime: DesktopRuntime, command: dict[str, Any]) -> None:
    manager = runtime.room_manager
    if manager is None:
        emit({"type": "ack", "cmd": "room_tunnel", "ok": False, "error": "房间管理器未就绪"})
        return
    url = str(command.get("url") or "")
    try:
        status = await manager.set_tunnel(url)
    except Exception as exc:
        message = getattr(exc, "message", None) or str(exc) or "Tunnel 配置失败"
        emit({"type": "ack", "cmd": "room_tunnel", "ok": False, "error": message})
        return
    emit({"type": "ack", "cmd": "room_tunnel", "ok": True, "room": status})


async def _report_status(runtime: DesktopRuntime) -> None:
    last = None
    while True:
        status = (runtime.engine.busy(), runtime.engine.task_count())
        if status != last:
            emit({"type": "status", "busy": status[0], "tasks": status[1]})
            last = status
        await asyncio.sleep(0.2)


async def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    runtime = DesktopRuntime()
    payload = await runtime.start(ready_stream=None)
    emit({**payload, "impl": "winefox-desktop-runtime", "implVersion": VERSION, "pid": os.getpid()})

    from desktop_room.manager import RoomManager
    from desktop_room.generation_bridge import DesktopRoomBackend

    runtime.room_manager = RoomManager(
        emit=emit,
        rooms_root=runtime.paths.subdir("data") / "rooms",
        generation_backend=DesktopRoomBackend(runtime.engine),
    )

    if args.selftest:
        try:
            await _health_selftest(int(payload["port"]))
            emit({"type": "exit", "code": 0, "reason": "selftest-ok"})
            return 0
        finally:
            await runtime.stop()

    if args.open_browser:
        webbrowser.open(f"http://127.0.0.1:{payload['port']}{payload['bootstrapPath']}")

    commands: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    _start_stdin_reader(loop, commands, stop_event)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    exit_reason = "signal"
    shutdown_pending = False
    status_task = asyncio.create_task(_report_status(runtime), name="winefox-status")
    try:
        while not stop_event.is_set():
            try:
                command = await asyncio.wait_for(commands.get(), timeout=0.5)
            except asyncio.TimeoutError:
                if shutdown_pending and not runtime.engine.busy():
                    exit_reason = "shutdown-wait"
                    break
                continue
            cmd = str(command.get("cmd") or "")
            if cmd == "ping":
                emit({"type": "ack", "cmd": "ping", "ok": True, "pong": True})
            elif cmd == "status":
                emit({"type": "status", "busy": runtime.engine.busy(), "tasks": runtime.engine.task_count()})
            elif cmd == "room_start":
                await _room_start(runtime, command)
            elif cmd == "room_stop":
                if runtime.room_manager is not None:
                    await runtime.room_manager.stop()
                emit({"type": "ack", "cmd": "room_stop", "ok": True})
            elif cmd == "room_tunnel":
                await _room_tunnel(runtime, command)
            elif cmd == "room_clear_tunnel":
                if runtime.room_manager is not None:
                    runtime.room_manager.clear_tunnel()
                emit({"type": "ack", "cmd": "room_clear_tunnel", "ok": True})
            elif cmd == "shutdown-cancel":
                shutdown_pending = False
                emit({"type": "ack", "cmd": cmd, "ok": True, "pending": False})
            elif cmd == "shutdown":
                mode = str(command.get("mode") or "wait")
                if mode not in {"wait", "now"}:
                    emit({"type": "ack", "cmd": cmd, "ok": False, "error": "invalid shutdown mode"})
                    continue
                emit({"type": "ack", "cmd": cmd, "ok": True, "mode": mode, "busy": runtime.engine.busy()})
                if mode == "wait" and runtime.engine.busy():
                    shutdown_pending = True
                    continue
                exit_reason = f"shutdown-{mode}"
                break
            else:
                emit({"type": "ack", "cmd": cmd, "ok": False, "error": "unknown command"})
    finally:
        status_task.cancel()
        await asyncio.gather(status_task, return_exceptions=True)
        if runtime.room_manager is not None:
            await runtime.room_manager.shutdown()
        await runtime.stop()
    emit({"type": "exit", "code": 0, "reason": exit_reason})
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print(VERSION)
        return 0
    return asyncio.run(run(args))

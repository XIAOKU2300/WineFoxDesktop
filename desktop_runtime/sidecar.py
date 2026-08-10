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
        sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="winefox-sidecar")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    return parser


def _start_stdin_reader(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[dict[str, Any]]) -> threading.Thread:
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
            return
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


async def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    runtime = DesktopRuntime()
    payload = await runtime.start(ready_stream=None)
    emit({**payload, "impl": "winefox-desktop-runtime", "implVersion": VERSION, "pid": os.getpid()})

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
    _start_stdin_reader(loop, commands)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    exit_reason = "signal"
    try:
        while not stop_event.is_set():
            try:
                command = await asyncio.wait_for(commands.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            cmd = str(command.get("cmd") or "")
            if cmd == "ping":
                emit({"type": "ack", "cmd": "ping", "ok": True, "pong": True})
            elif cmd == "status":
                emit({"type": "status", "busy": runtime.engine.busy(), "tasks": runtime.engine.task_count()})
            elif cmd == "shutdown-cancel":
                emit({"type": "ack", "cmd": cmd, "ok": True, "pending": False})
            elif cmd == "shutdown":
                mode = str(command.get("mode") or "wait")
                emit({"type": "ack", "cmd": cmd, "ok": True, "mode": mode, "busy": runtime.engine.busy()})
                if mode == "wait":
                    while runtime.engine.busy() and not stop_event.is_set():
                        await asyncio.sleep(0.5)
                exit_reason = f"shutdown-{mode}"
                break
            else:
                emit({"type": "ack", "cmd": cmd, "ok": False, "error": "unknown command"})
    finally:
        await runtime.stop()
    emit({"type": "exit", "code": 0, "reason": exit_reason})
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print(VERSION)
        return 0
    return asyncio.run(run(args))

"""CLI 入口：``python -m desktop_backend``。

stdout 只输出一次 ready JSON；所有日志走 stderr。仅绑定 127.0.0.1。

示例::

    python -m desktop_backend --static-dir ../desktop_ui --engine myapp.engine:create_engine
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import logging
import os
import signal
import sys
from typing import Optional

from . import DEFAULT_MAX_UPLOAD_BYTES, DEFAULT_STATIC_DIR
from .app import CSP_DEFAULT
from .null_engine import NullConfig, NullEngine, NullUploadStore
from .server import WineFoxBackendServer


def _load_engine(spec: Optional[str]):
    """``module:factory`` 或 ``module``（模块本身即引擎实例）。"""
    if not spec:
        return NullEngine()
    module_name, _, attr = spec.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise SystemExit(f"无法导入引擎模块 {module_name!r}: {exc}") from exc
    if attr:
        factory = getattr(module, attr, None)
        if factory is None:
            raise SystemExit(f"模块 {module_name!r} 中没有 {attr!r}")
        if not callable(factory):
            raise SystemExit(f"{module_name}:{attr} 不可调用")
        return factory()
    return module


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="desktop_backend", description="WineFox Desktop 本地后端")
    p.add_argument("--static-dir", default=DEFAULT_STATIC_DIR, help=f"UI 静态目录（默认 {DEFAULT_STATIC_DIR}）")
    p.add_argument("--max-upload-mb", type=int, default=DEFAULT_MAX_UPLOAD_BYTES // (1024 * 1024), help="上传上限 MB（默认 20）")
    p.add_argument("--engine", default=None, help="引擎注入 module:factory（如 myapp.engine:create_engine）")
    p.add_argument("--log-level", default="INFO", help="日志级别（默认 INFO）")
    return p


def _install_stop_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    """安装优雅停止信号处理。

    Unix 用 add_signal_handler；Windows 本构建不支持 add_signal_handler，
    改用 SetConsoleCtrlHandler（处理 CTRL_C/CTRL_BREAK，不直接终止进程）。
    """
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        _PHANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        @_PHANDLER_ROUTINE
        def _console_handler(ctrl_type: int) -> bool:
            loop.call_soon_threadsafe(stop_event.set)
            return True  # 已处理，阻止默认终止

        # 保存引用防止被 GC（HandlerRoutine 必须存活到进程结束）
        _install_stop_handlers._handler_ref = _console_handler  # type: ignore[attr-defined]
        try:
            ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_handler, True)
        except Exception:
            pass
        return

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass


async def _run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    engine = await _maybe_await(_load_engine(args.engine))
    server = WineFoxBackendServer(
        engine=engine,
        config=NullConfig(),
        uploads=NullUploadStore(),
        static_dir=args.static_dir,
        max_upload_bytes=args.max_upload_mb * 1024 * 1024,
        csp=CSP_DEFAULT,
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    _install_stop_handlers(loop, stop_event)

    await server.start()  # 向 stdout 输出 ready JSON
    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        await server.stop()
    return 0


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())

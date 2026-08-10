"""WineFox desktop sidecar entry point (fake implementation).

Usage:
  python -m winefox_sidecar [--port 0] [--allow-simulate] [--selftest]
                            [--no-ready] [--version-override X] [--protocol-override N]
                            [--crash-after SEC] [--bad-startup] [--crash-on-shutdown]

Exit codes:
  0  clean exit (shutdown now / wait-done / selftest)
  2  bad startup (flag --bad-startup, simulates boot failure)
  3  runtime crash (unhandled exception, incl. --crash-after)
  4  protocol/argument error
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import threading
import time
import traceback
from pathlib import Path

from . import protocol
from ._version import APP_VERSION
from .control import StdinControl
from .httpd import WineFoxHttpServer
from .shutdown import ShutdownCoordinator
from .tasks import TaskManager


def _install_crash_hook() -> None:
    """Any unhandled exception in any thread must terminate the sidecar with
    code 3 (crash), printing a sanitized traceback to stderr first."""
    def _hook(args) -> None:
        try:
            tb = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
            print(protocol.sanitize_line(tb), file=sys.stderr, flush=True)
        except Exception:
            pass
        os._exit(3)
    threading.excepthook = _hook


def _bootstrap_path() -> str:
    return "/bootstrap/" + secrets.token_urlsafe(16)


def _make_crash(seconds: float) -> None:
    def _boom() -> None:
        time.sleep(seconds)
        raise RuntimeError("simulated crash (--crash-after): unexpected sidecar failure")
    threading.Thread(target=_boom, daemon=True).start()


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    fake_flags = {
        "--allow-simulate", "--no-ready", "--noise", "--bad-startup",
        "--version-override", "--protocol-override", "--crash-after",
        "--crash-on-shutdown",
    }
    if not any(flag in argv for flag in fake_flags):
        repo_root = Path(__file__).resolve().parents[3]
        if (repo_root / "desktop_runtime").is_dir() and str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        from desktop_runtime.sidecar import main as real_main

        return real_main(argv)

    parser = argparse.ArgumentParser(prog="winefox-sidecar", description="WineFox desktop sidecar")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="0 = ephemeral port")
    parser.add_argument("--allow-simulate", action="store_true", help="enable POST /api/tasks/simulate")
    parser.add_argument("--selftest", action="store_true", help="boot, verify, emit ready, exit 0")
    parser.add_argument("--no-ready", action="store_true", help="TEST: never emit the ready handshake")
    parser.add_argument("--noise", action="store_true", help="TEST: print a non-JSON stdout line before ready")
    parser.add_argument("--bad-startup", action="store_true", help="TEST: exit 2 before ready")
    parser.add_argument("--version-override", default=None, help="TEST: handshake version mismatch")
    parser.add_argument("--protocol-override", type=int, default=None, help="TEST: handshake protocol mismatch")
    parser.add_argument("--crash-after", type=float, default=0.0, help="TEST: crash after N seconds")
    parser.add_argument("--crash-on-shutdown", action="store_true", help="TEST: crash instead of clean exit")
    parser.add_argument("--version", action="store_true", help="print app version and exit")
    args = parser.parse_args(argv)

    protocol.configure_stderr_sanitized()
    _install_crash_hook()

    if args.version:
        sys.stdout.write(APP_VERSION + "\n")
        sys.stdout.flush()
        return 0
    if args.bad_startup:
        # stderr must be sanitized even on boot failure paths.
        print("fatal: simulated boot failure (api_key=deadbeefcafe0000 leaked token test)",
              file=sys.stderr)
        return 2

    if args.host != "127.0.0.1":
        print("refusing to bind non-loopback address", file=sys.stderr)
        return 4

    tasks = TaskManager(allow_simulate=args.allow_simulate)
    state = {"exiting": False, "exit_code": 0, "reason": "unknown"}

    def do_exit(code: int, reason: str, **extra) -> None:
        if state["exiting"]:
            return
        state["exiting"] = True
        state["exit_code"] = code
        state["reason"] = reason
        if args.crash_on_shutdown:
            raise RuntimeError("simulated crash on shutdown path")
        protocol.emit_exit(code, reason, **extra)
        threading.Timer(0.2, lambda: os._exit(code)).start()

    shutdown = ShutdownCoordinator(tasks, do_exit)

    server: WineFoxHttpServer = WineFoxHttpServer(
        (args.host, args.port),
        bootstrap_token="",
        tasks=tasks,
        shutdown_cb=shutdown,
    )
    # __init__ already bound the socket; the port is real now.
    host, port = server.server_address[0], server.server_address[1]
    token = _bootstrap_path()
    server.bootstrap_token = token[len("/bootstrap/") :]

    def request_shutdown(mode: str) -> None:
        shutdown.request(mode)

    server.request_shutdown = request_shutdown

    StdinControl(tasks, shutdown).start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=shutdown.run_monitor, daemon=True).start()

    if args.crash_after > 0:
        _make_crash(args.crash_after)

    if args.selftest:
        # Verify the HTTP surface is really serving, then handshake + exit.
        import urllib.request
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5) as resp:
                body = resp.read().decode("utf-8")
            if resp.status != 200 or not json.loads(body).get("ok"):
                print("selftest failed: health check", file=sys.stderr)
                return 3
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{token}", timeout=5) as resp:
                page = resp.read().decode("utf-8")
            if resp.status != 200 or "WineFox" not in page:
                print("selftest failed: bootstrap page", file=sys.stderr)
                return 3
        except Exception as exc:  # pragma: no cover
            print(f"selftest failed: {exc}", file=sys.stderr)
            return 3

    if not args.no_ready:
        if args.noise:
            # Non-JSON stdout line: the shell must skip it with a warning.
            sys.stdout.write("service booting (not json yet)...\n")
            sys.stdout.flush()
        protocol.emit_ready(
            port,
            token,
            version=args.version_override,
            protocol_version=args.protocol_override,
        )
        protocol.emit_log("info", "sidecar ready (fake)")

    if args.selftest:
        do_exit(0, "selftest-ok")
        time.sleep(0.5)

    try:
        while not state["exiting"]:
            time.sleep(0.2)
    except KeyboardInterrupt:
        do_exit(0, "keyboard-interrupt")
    return state["exit_code"]


if __name__ == "__main__":
    sys.exit(main())

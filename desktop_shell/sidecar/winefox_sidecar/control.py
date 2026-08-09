"""stdin control channel (JSON lines from the Tauri shell).

Commands:
  {"cmd":"ping"}                          -> {"type":"pong",...}
  {"cmd":"status"}                        -> {"type":"status",...}
  {"cmd":"shutdown","mode":"now"|"wait"}  -> ack; exit per mode
  {"cmd":"shutdown-cancel"}               -> abort pending wait shutdown
"""

from __future__ import annotations

import json
import os
import sys
import threading
from typing import Optional

from . import protocol


class StdinControl(threading.Thread):
    def __init__(self, tasks, shutdown, crash_cb=None) -> None:
        super().__init__(daemon=True, name="stdin-control")
        self._tasks = tasks
        self._shutdown = shutdown
        self._crash_cb = crash_cb

    def run(self) -> None:
        try:
            for raw in sys.stdin:
                line = raw.strip()
                if not line:
                    continue
                self._dispatch(line)
        except (EOFError, OSError):
            return

    def _dispatch(self, line: str) -> None:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            protocol.emit_log("warn", f"ignored non-JSON control line: {protocol.sanitize_line(line[:200])}")
            return
        if not isinstance(msg, dict):
            return
        cmd = msg.get("cmd")
        if cmd == "ping":
            protocol.emit_ack("ping", True, pong=True)
        elif cmd == "status":
            info = self._tasks.summary()
            protocol.emit_status(info["busy"], info["tasks"], info["task"],
                                 shutdown=self._shutdown.query())
        elif cmd == "shutdown":
            mode = str(msg.get("mode") or "wait")
            if mode in ("now", "wait"):
                self._shutdown.request(mode)
            else:
                protocol.emit_ack("shutdown", False, error=f"unknown mode {mode!r}")
        elif cmd == "shutdown-cancel":
            self._shutdown.cancel()
        elif cmd == "crash" and self._crash_cb is not None:
            # Test hook: simulate an unhandled exception (crash path).
            threading.Timer(0.1, self._crash_cb).start()
        else:
            protocol.emit_ack(cmd, False, error="unknown command")

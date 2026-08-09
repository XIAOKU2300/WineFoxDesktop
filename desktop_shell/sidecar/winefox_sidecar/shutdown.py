"""Shutdown state machine.

Modes (mirrors the shell's exit flow):
  now    - cancel local tasks, exit promptly (never calls ComfyUI /interrupt)
  wait   - default: wait for running tasks to finish, then exit
  cancel - abort a pending wait shutdown and return to the app

When the sidecar decides to exit it emits {"type":"exit",...} and exits with
the given code.  The shell watches for the exit event / process termination.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from . import protocol
from .tasks import TaskManager


class ShutdownCoordinator:
    def __init__(self, tasks: TaskManager, exit_cb, grace_seconds: float = 3.0) -> None:
        self._tasks = tasks
        self._exit_cb = exit_cb
        self._grace = grace_seconds
        self._lock = threading.Lock()
        self._pending: Optional[str] = None  # "now" | "wait"
        self._requested_at = 0.0

    # -- queries ------------------------------------------------------------

    def query(self) -> dict:
        with self._lock:
            return {"pending": self._pending is not None, "mode": self._pending}

    def pending(self) -> Optional[str]:
        with self._lock:
            return self._pending

    # -- requests -----------------------------------------------------------

    def request(self, mode: str) -> None:
        if mode not in ("now", "wait"):
            return
        with self._lock:
            already = self._pending
            self._pending = mode
            self._requested_at = time.monotonic()
        busy = self._tasks.busy()
        if mode == "now":
            interrupted = self._tasks.cancel_all()
            protocol.emit_ack("shutdown", True, mode="now", busy=False, tasks=self._tasks.count(),
                              interrupted_tasks=interrupted)
            self._exit_cb(0, "shutdown-now", interrupted_tasks=interrupted)
        else:
            protocol.emit_ack("shutdown", True, mode="wait", busy=busy, tasks=self._tasks.count(),
                              pending=True)
            if not busy:
                self._exit_cb(0, "shutdown-wait-idle")

    def cancel(self) -> None:
        with self._lock:
            self._pending = None
        protocol.emit_ack("shutdown-cancel", True, pending=False)

    # -- monitor thread (started after ready) --------------------------------

    def run_monitor(self) -> None:
        while True:
            mode = self.pending()
            if mode == "wait":
                idle = self._tasks.wait_idle(timeout=2.0)
                if idle:
                    protocol.emit_log("info", "all tasks finished, exiting")
                    self._exit_cb(0, "shutdown-wait-done")
                    return
                # Still busy: re-check after a pause.
                time.sleep(0.25)
            else:
                time.sleep(0.4)

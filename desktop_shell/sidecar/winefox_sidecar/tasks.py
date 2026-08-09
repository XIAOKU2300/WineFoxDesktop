"""Fake task manager.

The real sidecar will track ComfyUI generation jobs here.  The fake one
simulates tasks so the shell's exit flow (wait / stop-waiting / return)
can be exercised end to end.

IMPORTANT: shutdown of a task must NEVER call ComfyUI's global /interrupt
endpoint.  Tasks are cancelled locally from the sidecar's own queue; the
backend generation is left running (it finishes and is discarded).
"""

from __future__ import annotations

import itertools
import threading
import time
from typing import Dict, Optional

from . import protocol


class Task:
    def __init__(self, task_id: int, name: str, seconds: float) -> None:
        self.id = task_id
        self.name = name
        self.seconds = seconds
        self.started_at = time.monotonic()
        self._cancel = threading.Event()

    @property
    def remaining(self) -> float:
        return max(0.0, self.seconds - (time.monotonic() - self.started_at))

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self) -> None:
        self._cancel.set()

    def as_dict(self) -> Dict:
        return {
            "id": self.id,
            "name": self.name,
            "seconds": round(self.remaining, 1),
            "cancelled": self.cancelled,
        }


class TaskManager:
    """Holds the (simulated) running jobs and broadcasts status events."""

    def __init__(self, allow_simulate: bool = False) -> None:
        self._allow_simulate = allow_simulate
        self._lock = threading.Lock()
        self._tasks: Dict[int, Task] = {}
        self._ids = itertools.count(1)
        self._dirty = True
        self._notify: Optional[threading.Event] = None

    # -- status -------------------------------------------------------------

    def busy(self) -> bool:
        with self._lock:
            return bool(self._tasks)

    def count(self) -> int:
        with self._lock:
            return len(self._tasks)

    def summary(self) -> Dict:
        with self._lock:
            tasks = list(self._tasks.values())
            if not tasks:
                return {"busy": False, "tasks": 0, "task": None}
            t = min(tasks, key=lambda x: x.remaining)
            return {"busy": True, "tasks": len(tasks), "task": t.as_dict()}

    def set_notify(self, event: Optional[threading.Event]) -> None:
        with self._lock:
            self._notify = event

    def _wake(self) -> None:
        with self._lock:
            ev = self._notify
        if ev is not None:
            ev.set()

    # -- operations ---------------------------------------------------------

    def start(self, name: str, seconds: float) -> Task:
        if not self._allow_simulate:
            raise RuntimeError("task simulation is disabled (start with --allow-simulate)")
        with self._lock:
            task = Task(next(self._ids), name, max(0.2, seconds))
            self._tasks[task.id] = task
            busy = True
        protocol.emit_status(True, len(self._tasks), task.as_dict())
        threading.Thread(target=self._runner, args=(task,), daemon=True).start()
        return task

    def _runner(self, task: Task) -> None:
        deadline = task.started_at + task.seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if task._cancel.wait(min(0.2, remaining)):
                break
            self._wake()
        self._finish(task)

    def _finish(self, task: Task) -> None:
        with self._lock:
            self._tasks.pop(task.id, None)
            tasks = list(self._tasks.values())
        protocol.emit_status(bool(tasks), len(tasks), tasks[0].as_dict() if tasks else None)

    def cancel_all(self) -> int:
        """Local cancellation only - no ComfyUI global interrupt."""
        with self._lock:
            tasks = list(self._tasks.values())
        for t in tasks:
            t.cancel()
        return len(tasks)

    def wait_idle(self, timeout: float, poll: float = 0.15) -> bool:
        """Block until no tasks remain (or timeout).  Returns True if idle."""
        deadline = time.monotonic() + timeout
        while True:
            if not self.busy():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll)

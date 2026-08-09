"""Loopback-only HTTP server: bootstrap page + health/status/shutdown API.

Security rules:
  * binds 127.0.0.1 only (never 0.0.0.0)
  * the UI is served only under /bootstrap/<one-time-token> that the shell
    received in the ready handshake
  * every non-UI response is JSON with Content-Type: application/json
  * path traversal is rejected (resolved path must stay inside the UI dir)
"""

from __future__ import annotations

import json
import os
import posixpath
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional

from ._version import APP_VERSION
from . import protocol

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "desktop_ui")


def _resolve_ui_dir() -> str:
    """Locate the desktop_ui directory in dev and frozen (PyInstaller) runs."""
    candidates = [
        UI_DIR,  # dev tree / editable install
    ]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "desktop_ui"))  # onefile
        candidates.append(os.path.join(meipass, "winefox_sidecar", "desktop_ui"))
    candidates.append(os.path.join(os.path.dirname(sys.executable), "_internal", "desktop_ui"))  # onedir
    for cand in candidates:
        if os.path.isdir(cand) and os.path.isfile(os.path.join(cand, "index.html")):
            return cand
    return UI_DIR


UI_DIR = _resolve_ui_dir()

_TEXT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
}


class _Handler(BaseHTTPRequestHandler):
    server: "WineFoxHttpServer"  # type: ignore[name-defined]

    # -- helpers ------------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # keep stderr clean
        pass

    def _send_json(self, code: int, obj: Dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Optional[Dict]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > 1 << 20:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return None

    # -- routing ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = posixpath.normpath(self.path.split("?", 1)[0])
        if path == "/api/health":
            return self._send_json(200, {"ok": True, "version": APP_VERSION, "impl": "winefox-sidecar-fake"})
        if path == "/api/status":
            return self._send_json(200, self.server.status())
        if path == "/":
            # No token yet: give the shell a machine-readable hint only.
            return self._send_json(200, {"service": "winefox-sidecar", "hint": "bootstrap requires one-time token"})
        if path.startswith("/bootstrap/"):
            token = path[len("/bootstrap/") :].strip("/")
            if token == self.server.bootstrap_token:
                return self._serve_ui(path)
            return self._send_json(403, {"error": "invalid bootstrap token"})
        self._send_json(404, {"error": "not found", "path": path})

    def do_POST(self) -> None:  # noqa: N802
        path = posixpath.normpath(self.path.split("?", 1)[0])
        if path == "/api/tasks/simulate":
            body = self._read_json_body() or {}
            try:
                task = self.server.tasks.start(
                    str(body.get("name") or "模拟生成任务"),
                    float(body.get("seconds") or 8.0),
                )
            except RuntimeError as exc:
                return self._send_json(400, {"error": str(exc)})
            return self._send_json(200, {"task": task.as_dict()})
        if path == "/api/shutdown":
            body = self._read_json_body() or {}
            mode = str(body.get("mode") or "wait")
            if mode not in ("now", "wait"):
                return self._send_json(400, {"error": "mode must be 'now' or 'wait'"})
            self.server.request_shutdown(mode)
            return self._send_json(200, self.server.status())
        self._send_json(404, {"error": "not found", "path": path})

    # -- static UI ----------------------------------------------------------

    def _serve_ui(self, url_path: str) -> None:
        rel = posixpath.normpath(url_path).lstrip("/")
        rel = rel[len("bootstrap/"):] if rel.startswith("bootstrap/") else rel
        # The token segment is not a file: strip it.
        parts = rel.split("/", 1)
        rel = parts[1] if len(parts) == 2 and parts[0] else ""
        if not rel:
            rel = "index.html"
        candidate = os.path.normpath(os.path.join(UI_DIR, *rel.split("/")))
        if os.path.commonpath([candidate, os.path.normpath(UI_DIR)]) != os.path.normpath(UI_DIR):
            return self._send_json(403, {"error": "forbidden"})
        if not os.path.isfile(candidate):
            return self._send_json(404, {"error": "not found"})
        ext = os.path.splitext(candidate)[1].lower()
        ctype = _TEXT_TYPES.get(ext, "application/octet-stream")
        try:
            with open(candidate, "rb") as fh:
                body = fh.read()
        except OSError:
            return self._send_json(500, {"error": "read failed"})
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


class WineFoxHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, bootstrap_token: str, tasks, shutdown_cb) -> None:
        super().__init__(addr, _Handler)
        self.bootstrap_token = bootstrap_token
        self.tasks = tasks
        self._shutdown_cb = shutdown_cb

    def status(self) -> Dict:
        info = self.tasks.summary()
        info["version"] = APP_VERSION
        info["shutdown"] = self._shutdown_cb.query() if self._shutdown_cb else {"pending": False}
        return info

    def request_shutdown(self, mode: str) -> None:
        if self._shutdown_cb:
            self._shutdown_cb.request(mode)

"""JSON-lines protocol helpers: stdout events + stderr sanitization.

The shell treats every line on stdout as a JSON object.  Anything that is
not JSON is ignored with a warning (forwarded to the sanitized log), so the
sidecar must never print free text to stdout.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from typing import Any, Dict, Optional

from ._version import APP_VERSION, IMPL_VERSION, PROTOCOL

_STDOUT_LOCK = threading.Lock()


def emit(event: Dict[str, Any]) -> None:
    """Write one JSON line to stdout (atomic, UTF-8, flushed)."""
    if sys.stdout is None:  # windowed exe without redirection
        return
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    with _STDOUT_LOCK:
        try:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            # Shell died (kill-on-parent-exit may already have taken us down).
            pass


def emit_ready(port: int, bootstrap_path: str, *,
                version: Optional[str] = None, protocol_version: Optional[int] = None) -> None:
    emit(
        {
            "type": "ready",
            "protocol": PROTOCOL if protocol_version is None else protocol_version,
            "version": APP_VERSION if version is None else version,
            "host": "127.0.0.1",
            "port": port,
            "bootstrapPath": bootstrap_path,
            "impl": "winefox-sidecar-fake",
            "implVersion": IMPL_VERSION,
            "pid": os.getpid(),
        }
    )


def emit_status(busy: bool, tasks: int, task: Optional[Dict[str, Any]], shutdown: Optional[Dict[str, Any]] = None) -> None:
    ev: Dict[str, Any] = {"type": "status", "busy": busy, "tasks": tasks}
    if task is not None:
        ev["task"] = task
    if shutdown is not None:
        ev["shutdown"] = shutdown
    emit(ev)


def emit_exit(code: int, reason: str, **extra: Any) -> None:
    ev: Dict[str, Any] = {"type": "exit", "code": code, "reason": reason}
    ev.update(extra)
    emit(ev)


def emit_log(level: str, message: str) -> None:
    emit({"type": "log", "level": level, "message": message})


def emit_ack(cmd: str, ok: bool = True, **extra: Any) -> None:
    ev: Dict[str, Any] = {"type": "ack", "cmd": cmd, "ok": ok}
    ev.update(extra)
    emit(ev)


# ---------------------------------------------------------------------------
# stderr sanitization (mirrors the redaction rules in src-tauri sanitize.rs)
# ---------------------------------------------------------------------------

_REDACT = "[REDACTED]"

# Generic secret-looking tokens: sk-..., long hex/base64 runs, JWT-ish blobs.
_TOKEN_RE = re.compile(
    r"(?i)\b(?:sk|pk|ak|token|secret|password|passwd|api[_-]?key|authorization|bearer|signature|sig)"
    r"['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9_\-./+=]{6,}",
)
_LONG_HEX_RE = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_LONG_B64_RE = re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b")
_AUTH_HDR_RE = re.compile(r"(?i)(authorization\s*:\s*)[^\r\n]+")
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)


def sanitize_line(line: str) -> str:
    """Redact secrets from one stderr line before it is logged anywhere."""
    if not line:
        return line
    out = _AUTH_HDR_RE.sub(lambda m: m.group(1) + _REDACT, line)
    out = _TOKEN_RE.sub(_REDACT, out)
    out = _PRIVATE_KEY_RE.sub(_REDACT, out)
    out = _LONG_HEX_RE.sub(_REDACT, out)
    out = _LONG_B64_RE.sub(_REDACT, out)
    return out


class SanitizingWriter:
    """File-like object that redacts every line before writing to a real stream.

    Used for stderr so the shell's captured stream never receives raw secrets.
    """

    def __init__(self, stream) -> None:  # stream: io.TextIOBase
        self._stream = stream
        self._buf = ""

    def write(self, text: str) -> int:
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._stream.write(sanitize_line(line) + "\n")
            self._stream.flush()
        return len(text)

    def flush(self) -> None:
        if self._buf:
            self._stream.write(sanitize_line(self._buf))
            self._stream.flush()
            self._buf = ""


def configure_stderr_sanitized() -> None:
    """Replace sys.stderr with a sanitizing writer (idempotent).
    Windowed exes may have stderr=None; fall back to a null sink."""
    if isinstance(sys.stderr, SanitizingWriter):
        return
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    sys.stderr = SanitizingWriter(sys.stderr)

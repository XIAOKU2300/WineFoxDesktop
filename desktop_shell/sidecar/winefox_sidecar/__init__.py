"""WineFox desktop sidecar (fake implementation for Agent 09).

Replaces the future real desktop backend.  It implements the exact
wire protocol the Tauri shell (src-tauri) consumes:

stdout  : JSON lines only (handshake, status events, acks, exit event)
stderr  : human-readable, sanitized logs (never secrets)
stdin   : JSON lines (control commands from the shell)
HTTP    : loopback-only web UI + health/status/shutdown endpoints

See README.md in this directory for the full protocol specification.
"""

__all__ = ["APP_VERSION", "PROTOCOL", "IMPL_VERSION"]

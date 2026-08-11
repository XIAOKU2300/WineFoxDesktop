"""Stable error codes for the ComfyUI environment center.

Codes are frozen (UI, docs/desktop_setup_migration.md section 6 and the
backend contract all depend on them).  New failures must reuse or extend this
module, never hard-code new strings.
"""

from __future__ import annotations

from typing import Any


class SetupErrorCode:
    ROOT_INVALID = "SETUP_ROOT_INVALID"
    ROOT_AMBIGUOUS = "SETUP_ROOT_AMBIGUOUS"
    ROOT_READ_ONLY = "SETUP_ROOT_READ_ONLY"
    PERMISSION_DENIED = "SETUP_PERMISSION_DENIED"
    REMOTE_MANUAL_REQUIRED = "SETUP_REMOTE_MANUAL_REQUIRED"
    DISK_SPACE_LOW = "SETUP_DISK_SPACE_LOW"
    NETWORK_UNREACHABLE = "SETUP_NETWORK_UNREACHABLE"
    SOURCE_UNTRUSTED = "SETUP_SOURCE_UNTRUSTED"
    HASH_MISMATCH = "SETUP_HASH_MISMATCH"
    ARCHIVE_UNSAFE = "SETUP_ARCHIVE_UNSAFE"
    DOWNLOAD_FAILED = "SETUP_DOWNLOAD_FAILED"
    NODE_CONFLICT = "SETUP_NODE_CONFLICT"
    PYTHON_NOT_FOUND = "SETUP_PYTHON_NOT_FOUND"
    PYTHON_UNSUPPORTED = "SETUP_PYTHON_UNSUPPORTED"
    PIP_FAILED = "SETUP_PIP_FAILED"
    BRIDGE_AUTH_FAILED = "SETUP_BRIDGE_AUTH_FAILED"
    BRIDGE_TOO_OLD = "SETUP_BRIDGE_TOO_OLD"
    RESTART_REQUIRED = "SETUP_RESTART_REQUIRED"
    RESTART_TIMEOUT = "SETUP_RESTART_TIMEOUT"
    JOB_BUSY = "SETUP_JOB_BUSY"
    JOB_NOT_FOUND = "SETUP_JOB_NOT_FOUND"
    CANCELLED = "SETUP_CANCELLED"
    ROLLBACK_FAILED = "SETUP_ROLLBACK_FAILED"
    CONFIG_INVALID = "SETUP_CONFIG_INVALID"
    UNKNOWN = "SETUP_UNKNOWN"
    INTERNAL = "SETUP_INTERNAL"


class SetupError(Exception):
    """A user-facing environment-center failure with a stable code."""

    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            result["details"] = self.details
        return result


def ok(payload: Any = None) -> dict[str, Any]:
    """Standard envelope for setup endpoints (matches desktop_backend.errors)."""
    return {"ok": True, "data": payload}


def fail(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message, **({"details": details} if details is not None else {})}}

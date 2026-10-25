"""Run desktop regressions without touching installed data or OS credentials."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
test_root = ROOT.parents[1] / ".winefox-review-20261015"
test_root.mkdir(parents=True, exist_ok=True)
os.environ["TEMP"] = os.environ["TMP"] = str(test_root)
tempfile.tempdir = str(test_root)
os.environ["WINEFOX_DATA_ROOT"] = str(test_root / "default-data")
for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(name, None)
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

import keyring
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError
import pytest


class ReviewMemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        self.values = {}

    def get_password(self, service, username):
        return self.values.get((service, username))

    def set_password(self, service, username, password):
        self.values[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.values:
            raise PasswordDeleteError("not configured")
        del self.values[(service, username)]


if __name__ == "__main__":
    keyring.set_keyring(ReviewMemoryKeyring())
    raise SystemExit(pytest.main([*sys.argv[1:], "-p", "no:cacheprovider"]))

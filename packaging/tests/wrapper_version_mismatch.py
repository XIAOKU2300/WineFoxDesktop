"""Test wrapper: force a version mismatch handshake (shell must reject)."""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "desktop_shell", "sidecar")
)
sys.argv = [sys.argv[0], "--version-override", "9.9.9"]

from winefox_sidecar.__main__ import main  # noqa: E402

sys.exit(main())

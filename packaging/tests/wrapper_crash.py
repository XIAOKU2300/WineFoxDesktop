"""Test wrapper: crash N seconds after ready (crash -> auto-restart -> error)."""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "desktop_shell", "sidecar")
)
sys.argv = [sys.argv[0], "--crash-after", "2"]

from winefox_sidecar.__main__ import main  # noqa: E402

sys.exit(main())

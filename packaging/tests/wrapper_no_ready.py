"""Test wrapper: force the ready handshake timeout path in the shell.

The shell appends its own args; this wrapper replaces them with --no-ready
so the shell's ready-timeout logic is exercised end to end.
"""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "desktop_shell", "sidecar")
)
sys.argv = [sys.argv[0], "--no-ready"]

from winefox_sidecar.__main__ import main  # noqa: E402

sys.exit(main())

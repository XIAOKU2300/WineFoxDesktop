"""PyInstaller entry point for the WineFox sidecar.

Lives OUTSIDE the winefox_sidecar package so the packaged exe can use
regular absolute imports (a package __main__ cannot be a PyInstaller entry).
Dev runs keep using `python -m winefox_sidecar`.
"""

import sys

# The Rust parent reads UTF-8 pipes. Frozen Python otherwise uses the Windows
# code page (for example cp936), which breaks the stderr reader on Chinese logs.
for stream in (sys.stdin, sys.stdout, sys.stderr):
    if stream is not None and hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

from winefox_sidecar.__main__ import main

if __name__ == "__main__":
    sys.exit(main())

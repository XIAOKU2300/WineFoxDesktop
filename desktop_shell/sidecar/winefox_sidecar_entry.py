"""PyInstaller entry point for the WineFox sidecar.

Lives OUTSIDE the winefox_sidecar package so the packaged exe can use
regular absolute imports (a package __main__ cannot be a PyInstaller entry).
Dev runs keep using `python -m winefox_sidecar`.
"""

import sys

from winefox_sidecar.__main__ import main

if __name__ == "__main__":
    sys.exit(main())

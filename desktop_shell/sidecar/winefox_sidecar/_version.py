"""Version stamp for the WineFox desktop sidecar.

APP_VERSION must match the Tauri shell version (desktop_shell/VERSION.txt)
and packaging/version.json.  packaging/build.ps1 rewrites this file from
packaging/version.json so the packaged binary always matches the shell.

The sidecar handshake carries APP_VERSION; the Tauri shell refuses to
navigate if it does not equal the shell version.
"""

APP_VERSION = "8.5.9"
PROTOCOL = 1

# Implementation version of the fake sidecar itself (not the app version).
IMPL_VERSION = "0.1.0"

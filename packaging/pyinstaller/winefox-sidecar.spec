# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: WineFox desktop sidecar (onedir, windowed, x64).

Base package policy (Agent 09):
  * MUST NOT include TensorFlow / Keras / torch / opennsfw2
  * MUST NOT include Playwright (and its Chromium)
  * MUST NOT include ComfyUI code, models or CUDA runtime
  * MUST collect: workflow/*.json, templates/, assets/, desktop_ui/,
    lora_source_aliases.json, sensitive_words.json, zayu_messages_template.json
  * Heavy features (image audit, GPT vision, browser rendering) belong to a
    future full build; they are excluded here.

Data paths:
  * sidecar package: ../../desktop_shell/sidecar/winefox_sidecar
  * plugin source:   env WINE_FOX_PLUGIN_SRC or ../../source/<plugin dir>
"""

import json
import os
import sys

from PyInstaller.utils.hooks import collect_data_files  # noqa: F401  (kept for future use)

# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------
SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
ROOT = os.path.normpath(os.path.join(SPEC_DIR, "..", ".."))
SIDECAR_DIR = os.path.normpath(os.path.join(ROOT, "desktop_shell", "sidecar"))
SIDECAR_PKG = os.path.join(SIDECAR_DIR, "winefox_sidecar")

PLUGIN_SRC = os.environ.get("WINE_FOX_PLUGIN_SRC") or os.path.normpath(
    os.path.join(ROOT, "source", "astrbot_plugin_comfyui_pro_UltraPlusMax-master")
)

with open(os.path.join(ROOT, "packaging", "version.json"), encoding="utf-8") as fh:
    VERSIONS = json.load(fh)
APP_VERSION = VERSIONS["app"]

# ---------------------------------------------------------------------------
# collect plugin data (skip missing dirs with a warning)
# ---------------------------------------------------------------------------
datas = []
plugin_datas = [
    ("workflow", "plugin_data/workflow"),
    ("migrations", "migrations"),
    ("templates", "plugin_data/templates"),
    ("assets", "plugin_data/assets"),
    ("desktop_ui", "plugin_data/desktop_ui"),
    ("lora_source_aliases.json", "plugin_data"),
    ("sensitive_words.json", "plugin_data"),
    ("zayu_messages_template.json", "plugin_data"),
    ("metadata.yaml", "plugin_data"),
]
for src_rel, dst in plugin_datas:
    src = os.path.join(PLUGIN_SRC, src_rel)
    if os.path.exists(src):
        if os.path.isdir(src):
            datas.append((src, dst))
        else:
            datas.append((src, dst))
    else:
        print(f"[spec] WARN: plugin data {src_rel} not found, skipping", file=sys.stderr)

metadata_root = os.path.join(PLUGIN_SRC, "metadata.yaml")
if os.path.isfile(metadata_root):
    datas.append((metadata_root, "."))

# sidecar package data (desktop_ui served by the sidecar itself).
# collect_data_files cannot import the uninstalled package, so walk it.
desktop_ui = os.path.join(SIDECAR_PKG, "desktop_ui")
if os.path.isdir(desktop_ui):
    for dirpath, _dirnames, filenames in os.walk(desktop_ui):
        rel = os.path.relpath(dirpath, SIDECAR_PKG)
        for fn in filenames:
            datas.append((os.path.join(dirpath, fn), rel))
datas.append((os.path.join(SIDECAR_DIR, "README.md"), "sidecar"))

# room_ui: 房间网关访客界面（随 desktop_room 包一起进入 _internal）。
room_ui = os.path.join(ROOT, "desktop_room", "room_ui")
if os.path.isdir(room_ui):
    for dirpath, _dirnames, filenames in os.walk(room_ui):
        rel = os.path.join("desktop_room", "room_ui", os.path.relpath(dirpath, room_ui))
        for fn in filenames:
            datas.append((os.path.join(dirpath, fn), rel))
else:
    print("[spec] WARN: desktop_room/room_ui not found, guest UI will be unavailable", file=sys.stderr)

# setup manifest: 环境中心安装清单。desktop_runtime/setup_manifest.py 在 frozen
# 下从 _MEIPASS/plugin_data/resources 读取，仓库根 resources/ 是唯一来源。
repo_resources = os.path.join(ROOT, "resources")
if os.path.isdir(repo_resources):
    datas.append((repo_resources, os.path.join("plugin_data", "resources")))
else:
    print("[spec] WARN: resources/ not found, setup manifest unavailable", file=sys.stderr)

# LoraBridge 随包分发（环境中心安装桥接器时从 _MEIPASS/plugin_data 复制）。
bridge_src = os.path.join(ROOT, "comfyui_custom_nodes", "ComfyUI-AstrBot-LoraBridge")
if os.path.isdir(bridge_src):
    # The bridge's ignored config.json contains the local installation token.
    # Ship source/documentation only; setup writes a fresh configuration later.
    bridge_dst = os.path.join("plugin_data", "comfyui_custom_nodes", "ComfyUI-AstrBot-LoraBridge")
    for dirpath, dirnames, filenames in os.walk(bridge_src):
        dirnames[:] = [name for name in dirnames if not name.startswith(".") and name != "__pycache__"]
        for filename in filenames:
            if filename.endswith(".py") or filename in {"README.md", "LICENSE", "requirements.txt", "config.example.json"}:
                relative_dir = os.path.relpath(dirpath, bridge_src)
                datas.append((os.path.join(dirpath, filename), os.path.join(bridge_dst, relative_dir)))
else:
    print("[spec] WARN: ComfyUI-AstrBot-LoraBridge not found, bridge install will be unavailable", file=sys.stderr)

# ---------------------------------------------------------------------------
# exclusions: heavy / out-of-scope dependencies
# ---------------------------------------------------------------------------
excludes = [
    "tensorflow", "tensorflow_core", "keras", "tf_keras",
    "torch", "torchvision", "torchaudio",
    "opennsfw2", "onnxruntime",
    "playwright", "selenium",
    "matplotlib", "scipy", "notebook", "IPython",
    "PyQt5", "PySide2", "PySide6", "tkinter",
    "numba", "pandas",
]

# ---------------------------------------------------------------------------
# version resource (stamped by build.ps1 from packaging/version.json)
# ---------------------------------------------------------------------------
version_file = os.path.join(SPEC_DIR, "generated", "sidecar_version_file.txt")
if os.path.exists(version_file):
    vs_info = version_file
else:
    vs_info = None

a = Analysis(
    [os.path.join(SIDECAR_DIR, "winefox_sidecar_entry.py")],
    pathex=[ROOT, SIDECAR_DIR],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "winefox_sidecar",
        "desktop_runtime",
        "desktop_runtime.sidecar",
        "desktop_backend",
        "winefox_core",
        "desktop_room",
        "desktop_room.manager",
        "argon2",
        "keyring.backends.Windows",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="winefox-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # The JSON-lines control protocol requires real stdio pipes. The Tauri
    # parent launches this console binary with CREATE_NO_WINDOW.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=vs_info,
    icon=os.path.join(ROOT, "desktop_shell", "src-tauri", "icons", "icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="winefox-sidecar",
)

print(f"[spec] WineFox sidecar v{APP_VERSION} onedir -> dist/winefox-sidecar/")

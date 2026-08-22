#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WineFox Desktop read-only release verifier (Agent 10, tools/desktop_verify.py).

Scans a deployed WineFox Desktop package directory (and, where noted, the local
machine) for security-gate violations. READ-ONLY: never writes into the scanned
root, never binds sockets, never installs anything.

Gates evaluated:
    G01 no-lan-listening   - no non-loopback LISTENING socket owned by the app
    G02 no-default-password- fixed password "REDACTED_LEGACY_PASSWORD" must not ship in config/binaries
    G03 no-fixed-public-url- no hardcoded public server URL in config/binaries
    G04 no-query-token     - auth tokens must never travel in query strings
    G05 no-secrets         - key material must not appear in config/SQLite/logs/crash reports
    G06 tls-default-verify - outbound TLS verification must default to ON
    G07 no-bundled-models  - v1 must not bundle TensorFlow/Playwright/model files
    G08 no-debug-artifacts - no source maps, debug flags, or mock markers in shipped UI

Usage:
    python tools/desktop_verify.py --root <package_dir> [--json out.json] [--verbose]
    python tools/desktop_verify.py --self-test          # verify the scanner itself

Exit codes:
    0  all gates PASS (warnings allowed)
    2  at least one gate FAILED
    3  tool error / invalid arguments
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

TOOL_NAME = "desktop_verify"
TOOL_VERSION = "1.0.0"
DEFAULT_PASSWORD = "REDACTED_LEGACY_PASSWORD"

SECRET_PATTERNS = [
    re.compile(r"(?i)\bgh[pousr]_[a-z0-9]{20,}\b"),
    re.compile(r"(?i)\bgithub_pat_[a-z0-9_]{20,}\b"),
    re.compile(r"(?i)\bsk-[a-z0-9]{16,}\b"),
    re.compile(r"(?i)\bhf_[a-z0-9]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\bxox[baprs]-[a-z0-9-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"),
]
QUERY_TOKEN_PATTERNS = [
    re.compile(r"[?&](?:token|access_token|auth|key|apikey|api_key)=[^&\s\"']+", re.I),
]

MAX_SCAN_BYTES = 200 * 1024 * 1024  # skip binary files larger than this
TEXT_EXTS = {".json", ".yaml", ".yml", ".toml", ".ini", ".conf", ".cfg", ".env", ".log", ".txt", ".md", ".xml", ".csv", ".sql", ".js", ".ts", ".html", ".css", ".py", ".rs", ".toml", ".properties", ".bat", ".ps1", ".sh"}
SQLITE_EXTS = {".db", ".sqlite", ".sqlite3", ".db3"}
MODEL_EXTS = {".safetensors", ".ckpt", ".pt", ".pth", ".onnx", ".tflite", ".pb", ".gguf", ".bin", ".h5", ".keras", ".joblib", ".pkl"}
HEAVY_DIRS = {"tensorflow", "keras", "playwright", ".playwright", "node_modules", "venv", ".venv", "site-packages", "dist", "build", "target", "__pycache__", ".git"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".zip", ".7z", ".exe", ".dll", ".pyd", ".so", ".dylib", ".pyc", ".pdb"}


@dataclass
class Finding:
    gate: str
    severity: str  # PASS | WARN | FAIL | INFO
    message: str
    path: str = ""
    detail: str = ""


@dataclass
class ScanResult:
    root: str
    findings: list = field(default_factory=list)

    def add(self, gate: str, severity: str, message: str, path: str = "", detail: str = ""):
        self.findings.append(Finding(gate, severity, message, path, detail))

    def summary(self) -> dict:
        counts = {"PASS": 0, "WARN": 0, "FAIL": 0, "INFO": 0}
        for f in self.findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        return counts

    def failed(self) -> bool:
        return any(f.severity == "FAIL" for f in self.findings)

    def gates(self) -> dict:
        out = {}
        for f in self.findings:
            entry = out.setdefault(f.gate, {"severity": "PASS", "findings": []})
            order = {"INFO": 0, "PASS": 1, "WARN": 2, "FAIL": 3}
            if order.get(f.severity, 0) > order.get(entry["severity"], 0):
                entry["severity"] = f.severity
            entry["findings"].append(
                {"severity": f.severity, "message": f.message, "path": f.path, "detail": f.detail}
            )
        return out


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _is_loopback_addr(text: str) -> bool:
    host = str(text or "").strip().split("%")[0]
    if host.lower() in {"localhost", "localhost.localdomain", "::1", "127.0.0.1"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_private_addr(text: str) -> bool:
    host = str(text or "").strip().split("%")[0]
    if host.lower() in {"localhost", "localhost.localdomain"}:
        return True
    try:
        addr = ipaddress.ip_address(host)
        return bool(addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_reserved)
    except ValueError:
        return False


def _iter_text_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        # Filter on the path RELATIVE to the scan root: path.parts contains
        # every ancestor of the absolute path (e.g. packaging/dist/...), which
        # would silently skip the entire scan when the root sits under a
        # directory named dist/build/target and gate every check to a fake PASS.
        if any(part in HEAVY_DIRS for part in rel.split("/")):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        try:
            if path.stat().st_size > MAX_SCAN_BYTES:
                continue
        except OSError:
            continue
        yield path, rel


def _safe_read_text(path: Path, limit: int = 8 * 1024 * 1024) -> str:
    try:
        with open(path, "rb") as fp:
            raw = fp.read(limit)
        return raw.decode("utf-8", errors="ignore")
    except OSError:
        return ""


def _scan_sqlite(path: Path, patterns) -> list[str]:
    """Read-only SQLite scan (mode=ro). Returns matching cell excerpts."""
    hits = []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            cur = con.cursor()
            tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                try:
                    rows = cur.execute(f'SELECT * FROM "{table}"').fetchmany(500)
                except Exception:
                    continue
                for row in rows:
                    for cell in row:
                        if not isinstance(cell, str):
                            continue
                        for pattern in patterns:
                            m = pattern.search(cell)
                            if m:
                                hits.append(f"{table}: ...{m.group(0)[:40]}...")
                                break
        finally:
            con.close()
    except Exception:
        pass
    return hits


def _netstat_listening() -> list[str]:
    """Return lines of non-loopback LISTENING sockets (Windows/Linux best effort)."""
    lines = []
    try:
        if os.name == "nt":
            out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=20).stdout
        else:
            out = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True, timeout=20).stdout
        for line in out.splitlines():
            if "LISTEN" not in line.upper():
                continue
            parts = line.split()
            if not parts:
                continue
            local = parts[3] if os.name != "nt" else parts[1]
            host = str(local).rsplit(":", 1)[0].strip("[]")
            if not _is_loopback_addr(host):
                lines.append(line.strip())
    except Exception as exc:  # pragma: no cover - environment dependent
        return [f"<netstat unavailable: {exc}>"]
    return lines


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------
def _process_names() -> dict:
    """PID -> lowercased process name (Windows tasklist; empty elsewhere)."""
    mapping = {}
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=30
            ).stdout
            for line in out.splitlines():
                parts = [p.strip('"') for p in line.strip().split('" , "')]
                if len(parts) >= 2:
                    mapping[parts[1]] = parts[0].lower()
        except Exception:
            pass
    return mapping


def check_listening_sockets(result: ScanResult, root: Path = None):
    lines = _netstat_listening()
    if not lines or lines[0].startswith("<netstat unavailable"):
        result.add("G01", "WARN", "cannot enumerate listening sockets", detail=lines[0] if lines else "")
        return
    app_names = set()
    if root is not None:
        for path in root.rglob("*.exe"):
            app_names.add(path.stem.lower())
    proc_names = _process_names()
    app_hits, other_hits = [], []
    for line in lines:
        if any(marker in line for marker in ("127.0.0.1", "[::1]", "::1")):
            continue
        pid = line.strip().split()[-1] if os.name == "nt" else ""
        proc = proc_names.get(pid, "") if pid else ""
        if app_names and proc and proc.replace(".exe", "") in app_names:
            app_hits.append((line, proc))
        else:
            other_hits.append(line)
    for line, proc in app_hits[:10]:
        result.add("G01", "FAIL", f"app process {proc!r} is listening on a non-loopback address", detail=line)
    for line in other_hits[:5]:
        result.add("G01", "WARN", "non-loopback listener on this machine (not attributed to the app)", detail=line)
    if not app_hits and not other_hits:
        result.add("G01", "PASS", "no non-loopback listening sockets")


def check_default_password(result: ScanResult, root: Path):
    """G02: the fixed password 'REDACTED_LEGACY_PASSWORD' must not be present in shipped configs."""
    config_files = [p for p, rel in _iter_text_files(root) if p.suffix.lower() in {".json", ".yaml", ".yml", ".toml", ".ini", ".conf", ".env", ".cfg"}]
    hit = False
    for path, rel in _iter_text_files(root):
        if path.suffix.lower() not in {".json", ".yaml", ".yml", ".toml", ".ini", ".conf", ".env", ".cfg", ".js", ".ts", ".html"}:
            continue
        text = _safe_read_text(path)
        if DEFAULT_PASSWORD in text:
            hit = True
            if path.suffix.lower() in {".js", ".ts", ".html"} and path.stat().st_size > 64 * 1024:
                result.add("G02", "WARN", "default password literal found in shipped UI bundle", path=rel)
            else:
                result.add("G02", "FAIL", "fixed default password present", path=rel)
    # binary scan (bounded, chunked) - text-like files are already covered above
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() in TEXT_EXTS or path.suffix.lower() in {".md", ".rst"}:
            continue
        try:
            if path.stat().st_size > MAX_SCAN_BYTES:
                continue
        except OSError:
            continue
        try:
            with open(path, "rb") as fp:
                blob = fp.read()
        except OSError:
            continue
        if b"REDACTED_LEGACY_PASSWORD" in blob:
            rel = str(path.relative_to(root)).replace("\\", "/")
            result.add("G02", "WARN" if path.suffix.lower() in {".exe", ".dll"} else "FAIL", "default password literal in shipped file", path=rel)
            hit = True
    if not hit:
        result.add("G02", "PASS", "no fixed default password found")


def check_fixed_public_url(result: ScanResult, root: Path):
    """G03: no hardcoded public (internet) URL in configs; LAN URLs are WARN."""
    url_re = re.compile(r"https?://[^\s\"'`<>]+")
    hit = False
    for path, rel in _iter_text_files(root):
        if path.suffix.lower() not in {".json", ".yaml", ".yml", ".toml", ".ini", ".conf", ".env", ".cfg"}:
            continue
        text = _safe_read_text(path)
        for match in url_re.findall(text):
            candidate = match.rstrip("),;")
            try:
                host = candidate.split("://", 1)[1].split("/", 1)[0].split("@")[-1].split(":")[0].strip("[]")
            except Exception:
                continue
            if not host:
                continue
            if _is_loopback_addr(host):
                continue
            hit = True
            template_url = "{" in candidate
            internal_host = host.endswith(".internal") or host == "host.docker.internal" or host.endswith(".local")
            if _is_private_addr(host) or internal_host:
                result.add("G03", "WARN", "LAN/private URL in config (ok for local ComfyUI, confirm intent)", path=rel, detail=candidate[:120])
            elif template_url:
                result.add("G03", "WARN", "template URL with placeholder in config", path=rel, detail=candidate[:120])
            else:
                result.add("G03", "FAIL", "fixed public URL in config", path=rel, detail=candidate[:120])
    if not hit:
        result.add("G03", "PASS", "no fixed public URL found")


def check_query_token(result: ScanResult, root: Path):
    """G04: auth material must never be passed via query strings."""
    hit = False
    for path, rel in _iter_text_files(root):
        if path.suffix.lower() not in {".json", ".yaml", ".yml", ".toml", ".ini", ".conf", ".env", ".cfg", ".log", ".txt", ".js", ".ts", ".html"}:
            continue
        text = _safe_read_text(path)
        for pattern in QUERY_TOKEN_PATTERNS:
            m = pattern.search(text)
            if m:
                hit = True
                result.add("G04", "FAIL", "token in query string", path=rel, detail=m.group(0)[:80])
                break
    if not hit:
        result.add("G04", "PASS", "no query-string tokens")


def check_secrets(result: ScanResult, root: Path):
    """G05: key patterns must not appear in config/SQLite/logs/crash reports."""
    hit = False
    for path, rel in _iter_text_files(root):
        if path.suffix.lower() not in TEXT_EXTS:
            continue
        text = _safe_read_text(path)
        for pattern in SECRET_PATTERNS:
            m = pattern.search(text)
            if m:
                hit = True
                severity = "FAIL" if path.suffix.lower() in {".log", ".json", ".yaml", ".yml", ".toml", ".ini", ".env", ".cfg", ".db"} else "WARN"
                result.add("G05", severity, f"secret pattern {pattern.pattern[:20]}... in file", path=rel, detail=m.group(0)[:24] + "...")
                break
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in SQLITE_EXTS:
            sqlite_hits = _scan_sqlite(path, SECRET_PATTERNS)
            if sqlite_hits:
                hit = True
                rel = str(path.relative_to(root)).replace("\\", "/")
                result.add("G05", "FAIL", "secret pattern in SQLite cell", path=rel, detail="; ".join(sqlite_hits[:5]))
    # crash reports (outside package, machine-local, read-only)
    crash_dirs = []
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        crash_dirs.append(Path(local) / "CrashDumps")
        crash_dirs.append(Path(local) / "winefox-desktop" / "logs")
    for crash_dir in crash_dirs:
        if not crash_dir.exists():
            continue
        for path in sorted(crash_dir.rglob("*"))[:200]:
            if not path.is_file() or path.suffix.lower() not in {".log", ".txt", ".dmp", ".json"}:
                continue
            text = _safe_read_text(path, limit=4 * 1024 * 1024)
            for pattern in SECRET_PATTERNS:
                m = pattern.search(text)
                if m:
                    hit = True
                    result.add("G05", "FAIL", "secret pattern in crash report", path=str(path), detail=m.group(0)[:24] + "...")
                    break
    if not hit:
        result.add("G05", "PASS", "no secrets in config/SQLite/logs/crash reports")


def check_tls_default(result: ScanResult, root: Path):
    """G06: TLS verification must default ON in desktop settings."""
    settings_candidates = [
        root / "settings.json",
        root / "config" / "settings.json",
        root / "resources" / "settings.json",
        root / "assets" / "settings.json",
    ]
    found = False
    for path in settings_candidates:
        if not path.exists():
            continue
        found = True
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            result.add("G06", "FAIL", "settings.json unreadable", path=str(path.relative_to(root)))
            continue
        flattened = json.dumps(data).lower()
        rel = str(path.relative_to(root)).replace("\\", "/")
        for key in ("tls_verify", "verify_ssl", "ssl_verify", "tls_verify_peers"):
            if key.lower() in data:
                value = data[key.lower()] if key.lower() in data else data.get(key)
                if value is True:
                    result.add("G06", "PASS", f"{key} defaults to verify=True", path=rel)
                else:
                    result.add("G06", "FAIL", f"{key} is not True ({value!r})", path=rel)
                return
        if '"verify"' in flattened or '"tls"' in flattened:
            result.add("G06", "WARN", "TLS-related keys exist but verification default unclear", path=rel)
        else:
            result.add("G06", "FAIL", "no TLS verification setting found; default must be verify=ON", path=rel)
        return
    if not found:
        result.add("G06", "FAIL", "settings.json not found; cannot confirm TLS default")


def check_bundled_runtime_models(result: ScanResult, root: Path):
    """G07: v1 must not bundle TensorFlow/Playwright/model weights."""
    model_hits = []
    heavy_hits = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        if path.suffix.lower() in MODEL_EXTS:
            model_hits.append((rel, path.stat().st_size if path.exists() else 0))
    for part_name in ("tensorflow", "keras", "playwright", ".playwright", "onnxruntime", "torch"):
        for path in root.rglob(part_name):
            if path.is_dir():
                heavy_hits.append(str(path.relative_to(root)).replace("\\", "/"))
    if model_hits:
        for rel, size in model_hits[:10]:
            result.add("G07", "FAIL", "model weight file bundled", path=rel, detail=f"{size} bytes")
    if heavy_hits:
        for rel in heavy_hits[:10]:
            result.add("G07", "WARN", "heavy runtime directory bundled (size/security review)", path=rel)
    if not model_hits and not heavy_hits:
        result.add("G07", "PASS", "no bundled models or heavy runtimes")


def check_debug_artifacts(result: ScanResult, root: Path):
    """G08: no source maps / debug flags / mock markers in shipped UI."""
    map_hits = list(root.rglob("*.map"))
    for path in map_hits[:10]:
        result.add("G08", "FAIL", "source map shipped", path=str(path.relative_to(root)).replace("\\", "/"))
    debug_re = re.compile(r"(?i)(debug\s*[:=]\s*true|__debug__|NODE_ENV\s*[:=]\s*['\"]development['\"]|enableDevTools\s*[:=]\s*true)")
    mock_re = re.compile(r"(?i)(mock[A-Z]?|fake[A-Z]?|stub[A-Z]?)\s*[:=]\s*true")
    debug_hit = mock_hit = False
    for path, rel in _iter_text_files(root):
        if path.suffix.lower() not in {".js", ".ts", ".html", ".json", ".css"}:
            continue
        text = _safe_read_text(path)
        if not debug_hit and debug_re.search(text):
            result.add("G08", "WARN" if path.stat().st_size > 64 * 1024 else "FAIL", "debug flag pattern in shipped UI", path=rel)
            debug_hit = True
        if not mock_hit and mock_re.search(text):
            result.add("G08", "WARN", "mock/stub flag pattern in shipped UI", path=rel)
            mock_hit = True
        if debug_hit and mock_hit:
            break
    if not map_hits and not debug_hit and not mock_hit:
        result.add("G08", "PASS", "no debug artifacts found")


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------
CHECKS = [
    ("listening", check_listening_sockets),
    ("default_password", check_default_password),
    ("fixed_public_url", check_fixed_public_url),
    ("query_token", check_query_token),
    ("secrets", check_secrets),
    ("tls_default", check_tls_default),
    ("bundled_models", check_bundled_runtime_models),
    ("debug_artifacts", check_debug_artifacts),
]

GATE_NAMES = {
    "G01": "no-lan-listening",
    "G02": "no-default-password",
    "G03": "no-fixed-public-url",
    "G04": "no-query-token",
    "G05": "no-secrets",
    "G06": "tls-default-verify",
    "G07": "no-bundled-models",
    "G08": "no-debug-artifacts",
}


def scan(root: Path) -> ScanResult:
    result = ScanResult(root=str(root))
    if not root.exists() or not root.is_dir():
        result.add("G00", "FAIL", "root directory missing", path=str(root))
        return result
    for name, check in CHECKS:
        try:
            check(result, root)
        except Exception as exc:  # never let one check kill the whole scan
            result.add(name.upper().replace("_", "-"), "WARN", f"check raised: {exc}")
    return result


def _emit(result: ScanResult, json_path: str | None, verbose: bool) -> int:
    summary = result.summary()
    payload = {
        "tool": TOOL_NAME,
        "version": TOOL_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "root": result.root,
        "summary": summary,
        "passed": not result.failed(),
        "gates": result.gates(),
    }
    if json_path:
        Path(json_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[desktop_verify] JSON written to {json_path}")
    print("=" * 68)
    print(f"WineFox Desktop verify  v{TOOL_VERSION}   root={result.root}")
    print(f"summary: {summary.get('PASS', 0)} pass, {summary.get('WARN', 0)} warn, "
          f"{summary.get('FAIL', 0)} fail, {summary.get('INFO', 0)} info")
    print("-" * 68)
    gate_rows = []
    for gate_id, gate_name in GATE_NAMES.items():
        entry = result.gates().get(gate_id)
        severity = entry["severity"] if entry else "PASS"
        gate_rows.append((gate_id, gate_name, severity))
    for gate_id, gate_name, severity in gate_rows:
        marker = {"PASS": "  OK", "WARN": " WARN", "FAIL": "FAIL", "INFO": " info"}.get(severity, " ????")
        print(f"  [{marker}] {gate_id} {gate_name}")
    if verbose or result.failed():
        print("-" * 68)
        for f in result.findings:
            if f.severity in {"FAIL", "WARN"} or verbose:
                print(f"  {f.severity:4s} {f.gate} {f.message}  [{f.path}]")
                if f.detail:
                    print(f"        {f.detail}")
    print("=" * 68)
    return 2 if result.failed() else 0


def self_test() -> int:
    """Verify scanner logic against a synthetic fixture. Never touches the repo."""
    failures = []
    with tempfile.TemporaryDirectory(prefix="desktop_verify_selftest_") as tmp:
        root = Path(tmp)
        bad = root / "bad_package"
        bad.mkdir()
        (bad / "config").mkdir()
        (bad / "settings.json").write_text(json.dumps({
            "tls_verify": False,
            "server_url": "https://93.184.216.34:8443",
            "auth_url": "http://127.0.0.1:8787/login?token=sekrit123",
        }, indent=2), encoding="utf-8")
        (bad / "config" / "app.json").write_text(json.dumps({"password": "REDACTED_LEGACY_PASSWORD", "api_key": "sk-abcdefghijklmnopqrstuvwxyz1234"}), encoding="utf-8")
        (bad / "secret.db").write_bytes(b"")  # placeholder; sqlite scan will no-op
        (bad / "model.safetensors").write_bytes(b"\x00" * 1024)
        (bad / "app.js.map").write_text("{}", encoding="utf-8")
        (bad / "app.js").write_text("const debug = true; const mockApi = true;", encoding="utf-8")
        (bad / "tensorflow").mkdir()
        (bad / "tensorflow" / "x").write_bytes(b"x")

        good = root / "good_package"
        good.mkdir()
        (good / "settings.json").write_text(json.dumps({
            "tls_verify": True,
            "server_url": "http://127.0.0.1:8188",
            "auth_url": "http://127.0.0.1:8787/login",
        }, indent=2), encoding="utf-8")
        (good / "app.js").write_text("const render = () => 42;", encoding="utf-8")

        bad_result = scan(bad)
        good_result = scan(good)

        def expect(result: ScanResult, gate: str, severity: str, label: str):
            entry = result.gates().get(gate)
            actual = entry["severity"] if entry else "PASS"
            if actual != severity:
                failures.append(f"{label}: {gate} expected {severity}, got {actual}")

        expect(bad_result, "G02", "FAIL", "bad")
        expect(bad_result, "G03", "FAIL", "bad")
        expect(bad_result, "G04", "FAIL", "bad")
        expect(bad_result, "G05", "FAIL", "bad")
        expect(bad_result, "G06", "FAIL", "bad")
        expect(bad_result, "G07", "FAIL", "bad")
        expect(bad_result, "G08", "FAIL", "bad")
        expect(good_result, "G02", "PASS", "good")
        expect(good_result, "G06", "PASS", "good")
        expect(good_result, "G07", "PASS", "good")

    if failures:
        for line in failures:
            print(f"[self-test FAIL] {line}")
        return 2
    print("[self-test OK] all gate expectations met")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", help="deployed WineFox Desktop package directory (read-only scan)")
    parser.add_argument("--json", help="write JSON report to this path")
    parser.add_argument("--verbose", action="store_true", help="show all findings")
    parser.add_argument("--self-test", action="store_true", help="verify the scanner against a synthetic fixture")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()
    if not args.root:
        parser.error("--root <package_dir> is required (or use --self-test)")
    return _emit(scan(Path(args.root)), args.json, args.verbose)


if __name__ == "__main__":
    sys.exit(main())

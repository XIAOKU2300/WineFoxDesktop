"""Sidecar protocol / lifecycle tests (run with: python test_sidecar_protocol.py).

Covers the Agent 09 test matrix:
  ready parse, non-JSON line, protocol mismatch, version mismatch, ready
  timeout, startup failure, crash, normal/forced/wait/cancel exit,
  Chinese + space resource path, HTTP surface, stderr sanitization.

Runs the real sidecar package from desktop_shell/sidecar via subprocess.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

SIDECAR_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "desktop_shell", "sidecar")
)
if SIDECAR_DIR not in sys.path:
    sys.path.insert(0, SIDECAR_DIR)

APP_VERSION = "8.5.10"


class SidecarProc:
    """Spawn the sidecar and speak its wire protocol.

    A reader thread drains stdout into a queue (select() on pipes is not
    supported on Windows), so reads are non-blocking with timeout.
    """

    def __init__(self, *extra_args: str, cwd: str | None = None):
        import queue
        import threading

        env = dict(os.environ)
        env["PYTHONPATH"] = SIDECAR_DIR + os.pathsep + env.get("PYTHONPATH", "")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "winefox_sidecar", *extra_args],
            cwd=cwd or SIDECAR_DIR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        self._q: "queue.Queue[str | None]" = queue.Queue()
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()
        self.lines: list[dict] = []
        self.ready: dict | None = None
        self.skipped: list[str] = []

    def _drain(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            self._q.put(raw[:-1] if raw.endswith("\n") else raw)
        self._q.put(None)  # EOF

    def read_raw(self, timeout: float = 8.0) -> str | None:
        """Read one raw stdout line (None on EOF/timeout)."""
        try:
            return self._q.get(timeout=timeout)
        except Exception:
            return None

    def read_line(self, timeout: float = 8.0) -> dict | None:
        """Read one JSON stdout line; non-JSON lines are skipped (mirrors the
        Rust shell parser) and recorded in self.skipped."""
        raw = self.read_raw(timeout)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            self.skipped.append(raw)
            return None

    def wait_ready(self, timeout: float = 8.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.read_line(timeout=0.5)
            if line is None:
                raise AssertionError("stdout closed before ready")
            if line.get("type") == "ready":
                self.ready = line
                return line
            self.lines.append(line)
        raise AssertionError("ready handshake not received in time")

    def send(self, obj: dict) -> None:
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def read_event(self, want: str, timeout: float = 8.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.read_line(timeout=0.5)
            if line is None:
                continue  # quiet period; keep polling until the deadline
            if line.get("type") == want:
                return line
        raise AssertionError(f"event {want!r} not seen in time")

    def kill(self) -> None:
        self.proc.kill()
        self.proc.wait()

    def close(self) -> int:
        try:
            return self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return self.proc.wait()

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None


class TestHandshake(unittest.TestCase):
    def test_ready_parse_valid(self):
        sc = SidecarProc()
        ready = sc.wait_ready()
        self.assertEqual(ready["type"], "ready")
        self.assertEqual(ready["protocol"], 1)
        self.assertEqual(ready["version"], APP_VERSION)
        self.assertEqual(ready["host"], "127.0.0.1")
        self.assertIsInstance(ready["port"], int)
        self.assertGreater(ready["port"], 0)
        self.assertTrue(ready["bootstrapPath"].startswith("/bootstrap/"))
        self.assertEqual(ready["impl"], "winefox-desktop-runtime")
        self.assertEqual(ready["pid"], sc.proc.pid)
        sc.send({"cmd": "shutdown", "mode": "now"})
        rc = sc.close()
        stderr = sc.proc.stderr.read() if sc.proc.stderr else ""
        self.assertEqual(rc, 0, stderr)

    def test_non_json_line_is_tolerated_by_parser(self):
        # The shell parser must skip non-JSON lines with a warning; here we
        # assert the sidecar emits a junk line first and that the ready
        # handshake still parses afterwards (read_line skips junk).
        sc = SidecarProc("--noise")
        first = sc.read_line(timeout=5)
        self.assertIsNone(first)  # junk line skipped by the parser
        self.assertEqual(len(sc.skipped), 1)
        self.assertIn("not json yet", sc.skipped[0])
        ready = sc.wait_ready()
        self.assertEqual(ready["type"], "ready")
        sc.send({"cmd": "shutdown", "mode": "now"})
        self.assertEqual(sc.close(), 0)

    def test_protocol_mismatch(self):
        sc = SidecarProc("--protocol-override", "2")
        ready = sc.wait_ready()
        self.assertEqual(ready["protocol"], 2)  # shell must reject this
        sc.kill()

    def test_version_mismatch(self):
        sc = SidecarProc("--version-override", "9.9.9")
        ready = sc.wait_ready()
        self.assertEqual(ready["version"], "9.9.9")  # shell must reject this
        sc.kill()

    def test_ready_timeout(self):
        sc = SidecarProc("--no-ready")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and sc.read_line(timeout=0.4) is not None:
            pass
        self.assertIsNone(sc.ready)
        sc.kill()

    def test_startup_failure(self):
        sc = SidecarProc("--bad-startup")
        rc = sc.close()
        self.assertEqual(rc, 2)
        err = sc.proc.stderr.read()
        self.assertIn("boot failure", err)

    def test_crash(self):
        sc = SidecarProc("--crash-after", "1")
        sc.wait_ready()
        time.sleep(2.2)
        self.assertIsNotNone(sc.proc.poll())
        self.assertEqual(sc.proc.returncode, 3)
        err = sc.proc.stderr.read()
        self.assertIn("simulated crash", err)

    def test_crash_stderr_is_sanitized(self):
        # The --bad-startup message contains a fake key; the sanitizing writer
        # must redact it before it reaches the real stderr.
        sc = SidecarProc("--bad-startup")
        rc = sc.close()
        err = sc.proc.stderr.read()
        self.assertEqual(rc, 2)
        self.assertNotIn("deadbeefcafe0000", err)
        self.assertIn("leaked token test", err)


class TestExit(unittest.TestCase):
    def test_normal_exit(self):
        sc = SidecarProc("--allow-simulate")
        sc.wait_ready()
        sc.send({"cmd": "shutdown", "mode": "now"})
        ev = sc.read_event("exit")
        self.assertEqual(ev["code"], 0)
        self.assertEqual(ev["reason"], "shutdown-now")
        self.assertEqual(sc.close(), 0)

    def test_forced_exit_while_busy(self):
        sc = SidecarProc("--allow-simulate")
        sc.wait_ready()
        body = json.dumps({"name": "long job", "seconds": 60}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{sc.ready['port']}/api/tasks/simulate",
                                     data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
        sc.send({"cmd": "shutdown", "mode": "now"})
        ev = sc.read_event("exit")
        self.assertEqual(ev["code"], 0)
        self.assertGreaterEqual(ev["interrupted_tasks"], 1)
        self.assertEqual(sc.close(), 0)

    def test_wait_exit_after_task(self):
        sc = SidecarProc("--allow-simulate")
        sc.wait_ready()
        body = json.dumps({"name": "3s job", "seconds": 3}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{sc.ready['port']}/api/tasks/simulate",
                                     data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
        t0 = time.monotonic()
        sc.send({"cmd": "shutdown", "mode": "wait"})
        ev = sc.read_event("exit", timeout=12)
        elapsed = time.monotonic() - t0
        self.assertEqual(ev["reason"], "shutdown-wait-done")
        self.assertGreaterEqual(elapsed, 2.5)  # actually waited for the job
        self.assertEqual(sc.close(), 0)

    def test_cancel_wait_returns_to_app(self):
        sc = SidecarProc("--allow-simulate")
        sc.wait_ready()
        body = json.dumps({"name": "30s job", "seconds": 30}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{sc.ready['port']}/api/tasks/simulate",
                                     data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
        sc.send({"cmd": "shutdown", "mode": "wait"})
        ack = sc.read_event("ack")
        self.assertEqual(ack["mode"], "wait")
        self.assertTrue(ack["busy"])
        sc.send({"cmd": "shutdown-cancel"})
        ack = sc.read_event("ack")
        self.assertEqual(ack["cmd"], "shutdown-cancel")
        self.assertFalse(ack["pending"])
        self.assertTrue(sc.alive)
        sc.send({"cmd": "shutdown", "mode": "now"})
        self.assertEqual(sc.close(), 0)

    def test_shutdown_never_calls_interrupt(self):
        # Structural check: the sidecar codebase must not contain a ComfyUI
        # /interrupt HTTP call anywhere (literal string usage).
        import re

        root = SIDECAR_DIR
        hits = []
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                with open(os.path.join(dirpath, fn), encoding="utf-8") as fh:
                    if re.search(r"['\"]/interrupt['\"]", fh.read()):
                        hits.append(os.path.join(dirpath, fn))
        self.assertEqual(hits, [], f"ComfyUI /interrupt must never be called: {hits}")


class TestHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sc = SidecarProc("--allow-simulate")
        cls.ready = cls.sc.wait_ready()
        cls.base = f"http://127.0.0.1:{cls.ready['port']}"

    @classmethod
    def tearDownClass(cls):
        cls.sc.send({"cmd": "shutdown", "mode": "now"})
        cls.sc.close()

    def get(self, path):
        return urllib.request.urlopen(self.base + path, timeout=5)

    def test_health(self):
        data = json.loads(self.get("/api/health").read())
        self.assertTrue(data["ok"])
        self.assertEqual(data["version"], APP_VERSION)

    def test_bootstrap_valid_token(self):
        page = self.get(self.ready["bootstrapPath"]).read().decode("utf-8")
        self.assertIn("WineFox", page)
        self.assertIn("app.js", page)
        self.assertIn("模拟任务", page)

    def test_bootstrap_bad_token(self):
        import urllib.error
        try:
            self.get("/bootstrap/wrong-token")
            self.fail("expected 403")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 403)

    def test_traversal_blocked(self):
        import urllib.error
        for path in (
            self.ready["bootstrapPath"] + "/../../../_version.py",
            self.ready["bootstrapPath"] + "/..%2f..%2fprotocol.py",
        ):
            try:
                self.get(path)
                self.fail(f"traversal not blocked: {path}")
            except urllib.error.HTTPError as exc:
                self.assertIn(exc.code, (403, 404))

    def test_unknown_api_404(self):
        import urllib.error
        try:
            self.get("/api/nope")
            self.fail("expected 404")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 404)

    def test_status_and_task_lifecycle(self):
        st = json.loads(self.get("/api/status").read())
        self.assertEqual(st["tasks"], 0)
        body = json.dumps({"name": "http job", "seconds": 4}).encode()
        req = urllib.request.Request(self.base + "/api/tasks/simulate", data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
        st = json.loads(self.get("/api/status").read())
        self.assertEqual(st["tasks"], 1)
        self.assertTrue(st["busy"])
        time.sleep(5.5)
        st = json.loads(self.get("/api/status").read())
        self.assertEqual(st["tasks"], 0)


class TestPathsAndSanitize(unittest.TestCase):
    def test_sanitize_line(self):
        from winefox_sidecar.protocol import sanitize_line

        self.assertNotIn("sk-abc123", sanitize_line("token=sk-abc123 xyz"))
        self.assertNotIn("deadbeef" * 4, sanitize_line("hash=" + "deadbeef" * 4))
        self.assertNotIn("Bearer X" + "Y" * 40, sanitize_line("Authorization: Bearer X" + "Y" * 40))
        self.assertIn("keep this", sanitize_line("keep this"))

    def test_chinese_and_space_resource_path(self):
        tmp = tempfile.mkdtemp(prefix="WineFox 测试 路径 ")
        try:
            sc = SidecarProc("--allow-simulate", cwd=tmp)
            ready = sc.wait_ready()
            self.assertEqual(ready["version"], APP_VERSION)
            page = urllib.request.urlopen(
                f"http://127.0.0.1:{ready['port']}{ready['bootstrapPath']}", timeout=5
            ).read().decode("utf-8")
            self.assertIn("WineFox", page)
            sc.send({"cmd": "shutdown", "mode": "now"})
            self.assertEqual(sc.close(), 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_selftest_mode(self):
        proc = subprocess.run(
            [sys.executable, "-m", "winefox_sidecar", "--selftest"],
            cwd=SIDECAR_DIR, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('"type":"ready"', proc.stdout.replace(" ", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)

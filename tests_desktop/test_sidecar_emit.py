"""sidecar emit() 输出健壮性：含孤立代理字符时绝不崩溃、输出仍为合法 JSON。"""

from __future__ import annotations

import io
import json
import sys
import unittest

from desktop_runtime.sidecar import emit


class SidecarEmitTests(unittest.TestCase):
    def _emit_and_read(self, payload: dict) -> str:
        buf = io.BytesIO()

        class _FakeStdout:
            buffer = buf

        original = sys.stdout
        sys.stdout = _FakeStdout()  # type: ignore[assignment]
        try:
            emit(payload)
        finally:
            sys.stdout = original
        return buf.getvalue().decode("utf-8")

    def test_normal_payload_roundtrips(self) -> None:
        line = self._emit_and_read({"type": "ack", "cmd": "ping", "ok": True})
        self.assertEqual(json.loads(line), {"type": "ack", "cmd": "ping", "ok": True})

    def test_lone_surrogate_never_crashes(self) -> None:
        line = self._emit_and_read({"type": "room_status", "state": "failed", "error": "名称\udcac"})
        # 输出必须是合法 JSON（Rust 侧 serde_json 可解析）
        parsed = json.loads(line)
        self.assertEqual(parsed["type"], "room_status")
        self.assertEqual(parsed["state"], "failed")
        # 全转义输出时 \udcac 以转义序列呈现；json.loads 可无损还原
        self.assertIn("\\udcac", line)
        self.assertEqual(parsed["error"], "名称\udcac")


if __name__ == "__main__":
    unittest.main()

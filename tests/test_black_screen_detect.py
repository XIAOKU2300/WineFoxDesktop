# -*- coding: utf-8 -*-
"""Functional tests for ComfyUI._is_blank_or_black_image (VAE black-screen guard).

astrbot is not installed in the test environment, so we stub astrbot.api.logger
before importing comfyui_api, then drive the detector against synthetic images.
"""
import io
import logging
import math
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _ensure_astrbot_stub() -> None:
    if "astrbot" in sys.modules and "astrbot.api" in sys.modules:
        return
    pkg = types.ModuleType("astrbot")
    pkg.__path__ = []  # type: ignore[attr-defined]
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("astrbot.stub")  # type: ignore[attr-defined]
    pkg.api = api  # type: ignore[attr-defined]
    sys.modules["astrbot"] = pkg
    sys.modules["astrbot.api"] = api


_ensure_astrbot_stub()
sys.path.insert(0, str(ROOT))
from comfyui_api import ComfyUI  # noqa: E402

try:
    from PIL import Image as PILImage  # noqa: E402
except Exception:  # pragma: no cover - Pillow is a stated dependency
    PILImage = None


def _png_bytes(mode: str, size, pixel_fn) -> bytes:
    img = PILImage.new(mode, size)
    img.putdata([pixel_fn(x, y) for y in range(size[1]) for x in range(size[0])])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class BlackScreenDetectTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api = ComfyUI({"server_address": "127.0.0.1:8188"}, data_dir=Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    @unittest.skipUnless(PILImage is not None, "Pillow required")
    def test_pure_black_is_flagged(self):
        data = _png_bytes("RGB", (64, 64), lambda x, y: (0, 0, 0))
        bad, reason = self.api._is_blank_or_black_image(data)
        self.assertTrue(bad, msg=f"pure black should be flagged, reason={reason}")
        self.assertIn("solid_fill", reason)

    @unittest.skipUnless(PILImage is not None, "Pillow required")
    def test_pure_white_is_flagged(self):
        data = _png_bytes("RGB", (64, 64), lambda x, y: (255, 255, 255))
        bad, reason = self.api._is_blank_or_black_image(data)
        self.assertTrue(bad)
        self.assertIn("solid_fill", reason)

    @unittest.skipUnless(PILImage is not None, "Pillow required")
    def test_pure_gray_is_flagged(self):
        data = _png_bytes("RGB", (64, 64), lambda x, y: (128, 128, 128))
        bad, reason = self.api._is_blank_or_black_image(data)
        self.assertTrue(bad)
        self.assertIn("solid_fill", reason)

    @unittest.skipUnless(PILImage is not None, "Pillow required")
    def test_normal_image_is_not_flagged(self):
        data = _png_bytes("RGB", (128, 128), lambda x, y: ((x * 2) % 256, (y * 3) % 256, ((x + y)) % 256))
        bad, reason = self.api._is_blank_or_black_image(data)
        self.assertFalse(bad, msg=f"normal varied image must not be flagged, reason={reason}")

    @unittest.skipUnless(PILImage is not None, "Pillow required")
    def test_dark_but_textured_image_is_not_flagged(self):
        def pix(x, y):
            v = 8 + (6 * math.sin(x / 3.0) + 6 * math.cos(y / 4.0) + (x % 7))
            v = int(max(0, min(40, v)))
            return (v, v, v)
        data = _png_bytes("RGB", (96, 96), pix)
        bad, reason = self.api._is_blank_or_black_image(data)
        self.assertFalse(bad, msg=f"dark textured image must not be flagged, reason={reason}")

    def test_empty_bytes_flagged(self):
        bad, reason = self.api._is_blank_or_black_image(b"")
        self.assertTrue(bad)
        self.assertEqual(reason, "empty_bytes")

    def test_corrupt_bytes_flagged(self):
        bad, reason = self.api._is_blank_or_black_image(b"not a real image payload at all")
        self.assertTrue(bad)
        self.assertIn("decode_failed", reason)

    def test_none_bytes_flagged(self):
        bad, reason = self.api._is_blank_or_black_image(None)
        self.assertTrue(bad)


if __name__ == "__main__":
    unittest.main()
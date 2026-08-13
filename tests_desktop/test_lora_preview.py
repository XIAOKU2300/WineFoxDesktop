# -*- coding: utf-8 -*-
"""LoraPreviewService 安全校验测试：魔数 / 像素 / 大小 / 未登记 token。

预览图只允许经同源 API + 一次性 token 获取；任何校验失败都必须返回 None，
绝不把第三方 URL 直接暴露给前端。

运行：``python -m unittest tests_desktop.test_lora_preview -v``
"""

from __future__ import annotations

import asyncio
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop_runtime.lora_preview import (  # noqa: E402
    LoraPreviewService,
    _infer_mime,
    _magic_matches,
    _parse_image_dimensions,
)


def _png_bytes(width: int, height: int) -> bytes:
    header = b"\x89PNG\r\n\x1a\n"
    ihdr = b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    return header + ihdr + b"\x00" * 16


def _jpeg_bytes(width: int, height: int) -> bytes:
    jfif = b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    data = bytearray(b"\xff\xd8\xff\xe0\x00\x10" + jfif)
    data += b"\xff\xc0\x00\x11\x08" + struct.pack(">HH", height, width) + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    data += b"\xff\xd9"
    return bytes(data)


def _gif_bytes(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00" * 4


def _webp_bytes(width: int, height: int) -> bytes:
    wbits = (width - 1) | ((height - 1) << 14)
    payload = b"\x2f" + struct.pack("<I", wbits) + b"\x00"
    return (
        b"RIFF" + struct.pack("<I", 12 + len(payload)) + b"WEBP"
        + b"VP8L" + struct.pack("<I", len(payload)) + payload
    )


class _FakeResp:
    def __init__(self, status: int, content_type: str, chunks: list[bytes]) -> None:
        self.status = status
        self.headers = {"Content-Type": content_type}
        self._chunks = chunks
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def iter_chunked(self, size: int):
        for chunk in self._chunks:
            yield chunk


class _FakeSession:
    def __init__(self, responses: list[tuple[int, str, list[bytes]]]) -> None:
        self._responses = list(responses)
        self._index = 0
        self.timeout = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def get(self, url: str, headers: dict | None = None):
        resp = self._responses[min(self._index, len(self._responses) - 1)]
        self._index += 1
        return _FakeResp(*resp)


class PreviewPureFunctions(unittest.TestCase):
    def test_magic_png(self):
        self.assertTrue(_magic_matches(_png_bytes(1, 1), "image/png"))
        self.assertTrue(_magic_matches(_png_bytes(8, 8), "image/png"))
        self.assertFalse(_magic_matches(b"not an image", "image/png"))

    def test_magic_jpeg(self):
        self.assertTrue(_magic_matches(_jpeg_bytes(4, 5), "image/jpeg"))
        self.assertTrue(_magic_matches(_jpeg_bytes(4, 5), "image/jpg"))
        self.assertFalse(_magic_matches(_jpeg_bytes(4, 5), "image/png"))

    def test_magic_webp_and_gif(self):
        self.assertTrue(_magic_matches(_webp_bytes(3, 4), "image/webp"))
        self.assertTrue(_magic_matches(_gif_bytes(2, 2), "image/gif"))

    def test_dimensions_png(self):
        self.assertEqual(_parse_image_dimensions(_png_bytes(640, 480)), (640, 480))

    def test_dimensions_jpeg(self):
        self.assertEqual(_parse_image_dimensions(_jpeg_bytes(320, 240)), (320, 240))

    def test_dimensions_gif(self):
        self.assertEqual(_parse_image_dimensions(_gif_bytes(100, 50)), (100, 50))

    def test_dimensions_webp(self):
        self.assertEqual(_parse_image_dimensions(_webp_bytes(200, 100)), (200, 100))

    def test_dimensions_garbage(self):
        self.assertIsNone(_parse_image_dimensions(b"") )
        self.assertIsNone(_parse_image_dimensions(b"hello world this is not an image"))
        self.assertIsNone(_parse_image_dimensions(_png_bytes(1, 1)[:10]))

    def test_infer_mime(self):
        self.assertEqual(_infer_mime(_png_bytes(1, 1)), "image/png")
        self.assertEqual(_infer_mime(_jpeg_bytes(1, 1)), "image/jpeg")
        self.assertEqual(_infer_mime(_webp_bytes(1, 1)), "image/webp")
        self.assertEqual(_infer_mime(_gif_bytes(1, 1)), "image/gif")
        self.assertEqual(_infer_mime(b"\x00\x01\x02\x03"), "")


class PreviewServiceFetch(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="lora_preview_")
        self.tmp = Path(self._tmpdir.name)
        self.service = LoraPreviewService(data_root=self.tmp)
        self.service.register("tok", "https://example.com/preview.png", source="civitai")
        self.addCleanup(self._tmpdir.cleanup)

    def test_register_rejects_non_https(self):
        for bad in ("http://example.com/a.png", "ftp://x/y", "", "/local/path.png", "file:///c:/a.png"):
            self.service.register("tok1", bad, source="civitai")
        self.assertNotIn("tok1", self.service._registry)

    def test_fetch_unregistered_token_returns_none(self):
        result = asyncio.run(self.service.fetch("no_such_token"))
        self.assertIsNone(result)

    async def test_fetch_success_and_disk_cache(self):
        data = _png_bytes(64, 64)
        with mock.patch("aiohttp.ClientSession", lambda timeout: _FakeSession([(200, "image/png", [data])])):
            result = await self.service.fetch("tok")
        self.assertIsNotNone(result)
        content_type, body = result
        self.assertEqual(content_type, "image/png")
        self.assertEqual(body, data)
        cache_dir = self.tmp / "civitai_preview_cache"
        self.assertTrue(any(cache_dir.glob("*.png")))
        # 第二次命中内存缓存，不再产生网络请求
        with mock.patch("aiohttp.ClientSession", side_effect=AssertionError("不应再次下载")):
            again = await self.service.fetch("tok")
        self.assertEqual(again, (content_type, body))

    async def test_fetch_non_image_content_type_rejected(self):
        with mock.patch("aiohttp.ClientSession", lambda timeout: _FakeSession([(200, "text/html", [b"<html>"])])):
            result = await self.service.fetch("tok")
        self.assertIsNone(result)

    async def test_fetch_bad_magic_rejected(self):
        with mock.patch("aiohttp.ClientSession", lambda timeout: _FakeSession([(200, "image/png", [b"<html>not an image</html>"])])):
            result = await self.service.fetch("tok")
        self.assertIsNone(result)

    async def test_fetch_dimension_out_of_range_rejected(self):
        data = _png_bytes(9999, 9999)
        with mock.patch("aiohttp.ClientSession", lambda timeout: _FakeSession([(200, "image/png", [data])])):
            result = await self.service.fetch("tok")
        self.assertIsNone(result)

    async def test_fetch_oversize_rejected(self):
        tiny = LoraPreviewService(data_root=self.tmp, max_bytes=64 * 1024)
        data = _png_bytes(8, 8) + b"\x00" * (200 * 1024)
        with mock.patch("aiohttp.ClientSession", lambda timeout: _FakeSession([(200, "image/png", [data])])):
            result = await tiny.fetch("tok")
        self.assertIsNone(result)

    async def test_fetch_http_error_rejected(self):
        with mock.patch("aiohttp.ClientSession", lambda timeout: _FakeSession([(404, "image/png", [b""])])):
            result = await self.service.fetch("tok")
        self.assertIsNone(result)

    async def test_fetch_missing_content_type_falls_back_to_magic(self):
        data = _png_bytes(16, 16)
        with mock.patch("aiohttp.ClientSession", lambda timeout: _FakeSession([(200, "", [data])])):
            result = await self.service.fetch("tok")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "image/png")

    async def test_register_evicts_oldest_over_cap(self):
        for index in range(600):
            self.service.register(f"tok{index}", f"https://example.com/{index}.png", source="civitai")
        self.assertLessEqual(len(self.service._registry), 512)
        # 最早登记的应被逐出
        self.assertNotIn("tok0", self.service._registry)


if __name__ == "__main__":
    unittest.main()

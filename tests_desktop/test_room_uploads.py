"""Phase E — 图片上传与附件（desktop_room/uploads）。

覆盖：
- 上传成功后以 type=image 消息落库，附件带授权 URL 与缩略图 URL
- 魔数校验（伪造/文本/不支持格式 → 400）
- 大小限制（>20MB → 413）、尺寸限制（>8192×8192 → 400）
- 未认证 401、房主关闭上传 403、限速 429（10 次/分钟/成员）
- EXIF 清理与方向纠正
- 附件下载：会话授权、缺失 404、缩略图
"""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

import aiohttp
from PIL import Image

from desktop_room.manager import RoomManager
from desktop_room.protocol import MSG_IMAGE

TEST_SETTINGS = {
    "name": "上传测试房",
    "ownerName": "房主A",
    "password": "正确密码",
    "maxMembers": 4,
    "allowChat": True,
    "allowUpload": True,
    "allowGeneration": True,
    "allowImg2img": True,
    "allowPromptEnhancement": True,
    "perMemberQueueLimit": 1,
    "roomQueueLimit": 10,
    "messageRetentionDays": 7,
}


def _extract_session_token(resp: aiohttp.ClientResponse) -> str:
    for header in resp.headers.getall("Set-Cookie", []):
        part = header.split(";", 1)[0].strip()
        if part.startswith("wf_room_session="):
            value = part.split("=", 1)[1]
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            return value
    raise AssertionError("响应未设置 wf_room_session Cookie")


class _Authed:
    """显式携带会话 Cookie 的请求包装器（http 下浏览器重发 Secure Cookie 的等价物）。"""

    def __init__(self, session: aiohttp.ClientSession, token: str | None = None) -> None:
        self._session = session
        self._token = token

    def with_token(self, token: str) -> "_Authed":
        return _Authed(self._session, token)

    def _headers(self, headers: dict | None) -> dict:
        headers = dict(headers or {})
        if self._token:
            headers.setdefault("Cookie", f"wf_room_session={self._token}")
        return headers

    def post(self, url: str, **kw):
        return self._session.post(url, headers=self._headers(kw.pop("headers", None)), **kw)

    def get(self, url: str, **kw):
        return self._session.get(url, headers=self._headers(kw.pop("headers", None)), **kw)

    def ws_connect(self, url: str, **kw):
        return self._session.ws_connect(url, headers=self._headers(kw.pop("headers", None)), **kw)


def _png_bytes(width: int = 16, height: int = 16, color: tuple = (255, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, "PNG")
    return buf.getvalue()


class RoomUploadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.emitted: list[dict] = []
        self.manager = RoomManager(emit=self.emitted.append, rooms_root=Path(self.tmp.name))
        self.status = await self.manager.start(dict(TEST_SETTINGS))
        self.port = int(self.status["port"])
        self.base = f"http://127.0.0.1:{self.port}"
        self.host_token = self.emitted[-1]["host_token"]
        self._sessions: list[aiohttp.ClientSession] = []
        self.session = aiohttp.ClientSession()
        self._sessions.append(self.session)
        self.guest = _Authed(self.session)
        # 登录一位普通访客
        resp = await self.session.post(
            f"{self.base}/room/v1/join", json={"name": "上传者", "password": "正确密码"})
        self.assertEqual(resp.status, 200)
        self.guest = self.guest.with_token(_extract_session_token(resp))

    async def asyncTearDown(self) -> None:
        for s in self._sessions:
            await s.close()
        await self.manager.shutdown()
        self.tmp.cleanup()

    def _make_authed(self) -> _Authed:
        session = aiohttp.ClientSession()
        self._sessions.append(session)
        return _Authed(session)

    def _upload_form(self, files: list[tuple[str, bytes]], client_message_id: str | None = None):
        form = aiohttp.FormData()
        if client_message_id:
            form.add_field("clientMessageId", client_message_id)
        for name, data in files:
            form.add_field("files", data, filename=name, content_type="application/octet-stream")
        return form

    async def _upload(self, files: list[tuple[str, bytes]], authed: _Authed | None = None,
                      client_message_id: str | None = None):
        target = authed or self.guest
        form = self._upload_form(files, client_message_id)
        return await target.post(f"{self.base}/room/v1/uploads", data=form)

    # ------------------------------------------------------------------
    # 成功路径
    # ------------------------------------------------------------------
    async def test_upload_creates_image_message_with_attachment(self) -> None:
        async with await self._upload([("a.png", _png_bytes())]) as resp:
            self.assertEqual(resp.status, 201)
            msg = (await resp.json())["message"]
            self.assertEqual(msg["type"], MSG_IMAGE)
            self.assertEqual(len(msg["attachments"]), 1)
            att = msg["attachments"][0]
            self.assertEqual(att["kind"], "image")
            self.assertTrue(att["url"].startswith("/room/v1/attachments/"))
            self.assertTrue(att["thumbUrl"].endswith("/thumb"))
            self.assertEqual(att["width"], 16)
            self.assertEqual(att["height"], 16)
            attachment_id = int(att["url"].rsplit("/", 1)[1])
        # 文件确实落盘（数据库只存相对路径，绝对根为房间数据目录）
        row = self.manager._storage.get_attachment(attachment_id)
        self.assertIsNotNone(row)
        self.assertTrue((Path(self.manager._data_dir) / row["relative_path"]).is_file())
        self.assertTrue((Path(self.manager._data_dir) / row["thumbnail_relpath"]).is_file())
        # 绝不泄露绝对路径
        self.assertNotIn(":", att["url"])
        self.assertNotIn("\\", att["url"])

    async def test_upload_broadcasts_message_created(self) -> None:
        async with self.guest.ws_connect(f"{self.base}/room/v1/ws") as ws:
            await ws.receive_json(timeout=5)  # snapshot
            async with await self._upload([("a.png", _png_bytes())]) as resp:
                self.assertEqual(resp.status, 201)
            event = await ws.receive_json(timeout=5)
            self.assertEqual(event["event"], "message.created")
            self.assertEqual(event["data"]["type"], MSG_IMAGE)
            self.assertEqual(len(event["data"]["attachments"]), 1)

    async def test_upload_multiple_files_single_message(self) -> None:
        async with await self._upload([("a.png", _png_bytes()), ("b.png", _png_bytes(8, 8))]) as resp:
            self.assertEqual(resp.status, 201)
            msg = (await resp.json())["message"]
            self.assertEqual(len(msg["attachments"]), 2)
            self.assertEqual(msg["type"], MSG_IMAGE)
        async with self.guest.get(f"{self.base}/room/v1/messages") as resp:
            messages = (await resp.json())["messages"]
            self.assertEqual(len(messages), 1)

    async def test_upload_with_client_message_id_idempotent(self) -> None:
        files = [("a.png", _png_bytes())]
        async with await self._upload(files, client_message_id="up-1") as resp:
            self.assertEqual(resp.status, 201)
            first_id = (await resp.json())["message"]["id"]
        async with await self._upload(files, client_message_id="up-1") as resp:
            self.assertEqual(resp.status, 201)
            second_id = (await resp.json())["message"]["id"]
        self.assertEqual(first_id, second_id)

    # ------------------------------------------------------------------
    # 校验失败
    # ------------------------------------------------------------------
    async def test_upload_rejects_bad_magic(self) -> None:
        async with await self._upload([("fake.png", b"this is not an image at all")]) as resp:
            self.assertEqual(resp.status, 400)
            self.assertEqual((await resp.json())["errorCode"], "UNSUPPORTED_IMAGE")

    async def test_upload_rejects_oversize(self) -> None:
        big = b"\x00" * (20 * 1024 * 1024 + 1)
        async with await self._upload([("big.png", big)]) as resp:
            self.assertEqual(resp.status, 413)
            self.assertEqual((await resp.json())["errorCode"], "FILE_TOO_LARGE")

    async def test_upload_rejects_huge_dimensions(self) -> None:
        huge = _png_bytes(width=8193, height=1)
        async with await self._upload([("huge.png", huge)]) as resp:
            self.assertEqual(resp.status, 400)
            self.assertEqual((await resp.json())["errorCode"], "IMAGE_TOO_LARGE")

    async def test_upload_requires_auth(self) -> None:
        anon = aiohttp.ClientSession()
        self._sessions.append(anon)
        async with anon.post(f"{self.base}/room/v1/uploads", data=self._upload_form([("a.png", _png_bytes())])) as resp:
            self.assertEqual(resp.status, 401)

    async def test_upload_disabled_by_owner(self) -> None:
        tmp2 = tempfile.TemporaryDirectory()
        settings = dict(TEST_SETTINGS, allowUpload=False)
        manager2 = RoomManager(emit=self.emitted.append, rooms_root=Path(tmp2.name))
        status2 = await manager2.start(settings)
        base2 = f"http://127.0.0.1:{int(status2['port'])}"
        session2 = aiohttp.ClientSession()
        self._sessions.append(session2)
        async with session2.post(f"{base2}/room/v1/join", json={"name": "上传者", "password": "正确密码"}) as r:
            token = _extract_session_token(r)
        guest2 = _Authed(session2, token)
        try:
            async with guest2.post(f"{base2}/room/v1/uploads", data=self._upload_form([("a.png", _png_bytes())])) as resp:
                self.assertEqual(resp.status, 403)
                self.assertEqual((await resp.json())["errorCode"], "UPLOAD_DISABLED")
        finally:
            await session2.close()
            await manager2.shutdown()
            tmp2.cleanup()

    async def test_upload_rate_limited_after_ten(self) -> None:
        for _ in range(10):
            async with await self._upload([("a.png", _png_bytes())]) as resp:
                self.assertEqual(resp.status, 201)
        async with await self._upload([("a.png", _png_bytes())]) as resp:
            self.assertEqual(resp.status, 429)
            self.assertEqual((await resp.json())["errorCode"], "UPLOAD_RATE_LIMITED")

    # ------------------------------------------------------------------
    # EXIF
    # ------------------------------------------------------------------
    async def test_upload_strips_exif_and_fixes_orientation(self) -> None:
        # 200×100，EXIF Orientation=6（需旋转 90° 显示）→ 清洗后应 100×200 且无 EXIF
        img = Image.new("RGB", (200, 100), (10, 200, 30))
        exif = img.getexif()
        exif[0x0112] = 6
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=90, exif=exif)
        async with await self._upload([("photo.jpg", buf.getvalue())]) as resp:
            self.assertEqual(resp.status, 201)
            att = (await resp.json())["message"]["attachments"][0]
            self.assertEqual(att["mimeType"], "image/jpeg")
        # 下载主图验证
        async with self.guest.get(f"{self.base}{att['url']}") as resp:
            self.assertEqual(resp.status, 200)
            body = await resp.read()
        with Image.open(io.BytesIO(body)) as downloaded:
            self.assertEqual(downloaded.size, (100, 200))
            self.assertIsNone(downloaded.info.get("exif"))

    # ------------------------------------------------------------------
    # 附件下载
    # ------------------------------------------------------------------
    async def test_get_attachment_returns_file(self) -> None:
        async with await self._upload([("a.png", _png_bytes(32, 32))]) as resp:
            att = (await resp.json())["message"]["attachments"][0]
        async with self.guest.get(f"{self.base}{att['url']}") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "image/png")
            body = await resp.read()
        with Image.open(io.BytesIO(body)) as downloaded:
            self.assertEqual(downloaded.size, (32, 32))

    async def test_get_thumb_returns_small_png(self) -> None:
        async with await self._upload([("a.png", _png_bytes(2000, 2000))]) as resp:
            att = (await resp.json())["message"]["attachments"][0]
        async with self.guest.get(f"{self.base}{att['thumbUrl']}") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "image/png")
            body = await resp.read()
        with Image.open(io.BytesIO(body)) as thumb:
            self.assertLessEqual(max(thumb.size), 512)

    async def test_get_attachment_requires_auth(self) -> None:
        async with await self._upload([("a.png", _png_bytes())]) as resp:
            att = (await resp.json())["message"]["attachments"][0]
        anon = aiohttp.ClientSession()
        self._sessions.append(anon)
        async with anon.get(f"{self.base}{att['url']}") as resp:
            self.assertEqual(resp.status, 401)

    async def test_get_missing_attachment_404(self) -> None:
        async with self.guest.get(f"{self.base}/room/v1/attachments/999999") as resp:
            self.assertEqual(resp.status, 404)


if __name__ == "__main__":
    unittest.main()

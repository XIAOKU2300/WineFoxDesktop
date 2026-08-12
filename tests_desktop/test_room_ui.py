"""Phase G — 访客界面（desktop_room/room_ui）：静态页服务 + 目录扩展 + 本地 http 会话令牌。

覆盖：
- GET / 返回访客界面 index.html（no-store、nosniff），引用 app.js/styles.css
- GET /assets/app.js、/assets/styles.css 正常下发（含关键接线标记）
- 房主后端提供 catalog 时，/room/v1/catalog 下发白名单 families/workflows/loras，
  且不泄露后端内部字段（valid/error/description/model_families）
- 后端无 catalog 方法时，目录不出现 families（不报错）
- 本地回环 http join 响应体携带 sessionToken（无 TLS 时浏览器不收 Secure Cookie），
  Set-Cookie 仍必须带 Secure（契约 test_join_success_sets_secure_cookie 不受影响）
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import aiohttp

from desktop_room.manager import RoomManager

from tests_desktop.test_room_generation import BASE_SETTINGS, FakeEngine, _extract_session_token


class CatalogEngine(FakeEngine):
    """提供 get_catalog 语义的房主引擎替身（含会被白名单过滤的敏感字段）。"""

    async def catalog(self) -> dict:
        return {
            "families": [
                {"id": "anima", "label": "Anima", "description": "内部描述-不应下发"},
                {"id": "illustrious", "label": "Illustrious", "description": "内部描述-不应下发"},
            ],
            "model_families": ["anima", "illustrious"],
            "workflows": [{
                "id": "wf-anima-2k",
                "name": "anima_2k.json",
                "label": "anima 2k",
                "mode": "txt2img",
                "family": "anima",
                "valid": True,
                "error": None,
            }],
            "loras": [{
                "id": "style_art.safetensors",
                "name": "style_art.safetensors",
                "label": "艺术风格",
                "family": "anima",
                "compat": "ready",
                "strength": 0.8,
                "character": "",
                "aliases": ["艺术"],
                "trigger_words": ["artstyle"],
                "tags": ["style"],
                "profiled": False,
                "extra_secret": "绝不外泄",
            }],
        }


class RoomUiTests(unittest.IsolatedAsyncioTestCase):
    def _new_manager(self, engine: FakeEngine, root: tempfile.TemporaryDirectory) -> RoomManager:
        return RoomManager(
            emit=self.emitted.append,
            rooms_root=Path(root.name),
            generation_backend=engine,
            generation_poll_interval=0.02,
        )

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.emitted: list[dict] = []
        self.manager = self._new_manager(FakeEngine(), self.tmp)
        self.status = await self.manager.start(dict(BASE_SETTINGS))
        self.base = f"http://127.0.0.1:{int(self.status['port'])}"
        self.session = aiohttp.ClientSession()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.manager.shutdown()
        self.tmp.cleanup()

    # ------------------------------------------------------------------
    # 访客界面静态页
    # ------------------------------------------------------------------
    async def test_index_served_with_ui_markers(self) -> None:
        async with self.session.get(f"{self.base}/") as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers.get("Content-Type", ""))
            self.assertEqual(resp.headers.get("Cache-Control"), "no-store")
            self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
            html = await resp.text()
        self.assertIn('id="screen-boot"', html)
        self.assertIn('id="screen-join"', html)
        self.assertIn('id="screen-workspace"', html)
        self.assertIn('src="/assets/app.js"', html)
        self.assertIn('href="/assets/styles.css"', html)

    async def test_assets_served(self) -> None:
        async with self.session.get(f"{self.base}/assets/app.js") as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("javascript", resp.headers.get("Content-Type", ""))
            app_js = await resp.text()
        async with self.session.get(f"{self.base}/assets/styles.css") as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("css", resp.headers.get("Content-Type", ""))
            css = await resp.text()

        # 接线标记：会话 Cookie、WS 地址、幂等 ID、界面装配函数。
        for marker in ("wf_room_session", "/room/v1/ws", "clientRequestId", "showScreen", "renderMessages"):
            self.assertIn(marker, app_js)
        for marker in ("--primary: #9B5067", ".workspace", "prefers-color-scheme"):
            self.assertIn(marker, css)
        # 回归守卫：hidden 属性必须最高优先级（否则 .screen-closed/.lightbox 的
        # display:flex 会盖住 [hidden]{display:none}，导致关闭遮罩永远盖住页面）。
        self.assertIn("[hidden] { display: none !important; }", css)

    # ------------------------------------------------------------------
    # 本地 http join：响应体携带 sessionToken，Cookie 仍带 Secure
    # ------------------------------------------------------------------
    async def test_join_over_http_returns_session_token(self) -> None:
        async with self.session.post(f"{self.base}/room/v1/join",
                                     json={"name": "访客甲", "password": "正确密码"}) as resp:
            self.assertEqual(resp.status, 200)
            body = await resp.json()
            token = body["sessionToken"]
            self.assertIsInstance(token, str)
            self.assertTrue(len(token) >= 32)
            # 令牌只出现在响应体，绝不进入 query 字符串。
            self.assertNotIn("token", str(resp.url))
        # 响应体令牌与 Cookie 中会话一致，且 Cookie 必须带 Secure。
        cookie_token = _extract_session_token(resp)
        self.assertEqual(cookie_token, token)
        secure_parts = [h for h in resp.headers.getall("Set-Cookie", []) if "Secure" in h]
        self.assertTrue(secure_parts)

    # ------------------------------------------------------------------
    # 目录扩展（后端有 catalog）
    # ------------------------------------------------------------------
    async def test_catalog_includes_whitelisted_guest_fields(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        manager = self._new_manager(CatalogEngine(), tmp)
        status = await manager.start(dict(BASE_SETTINGS))
        base = f"http://127.0.0.1:{int(status['port'])}"
        session = aiohttp.ClientSession()
        try:
            async with session.get(f"{base}/room/v1/catalog") as resp:
                self.assertEqual(resp.status, 200)
                data = await resp.json()
            self.assertIn("families", data)
            self.assertIn("workflows", data)
            self.assertIn("loras", data)
            family = data["families"][0]
            self.assertEqual(family, {"id": "anima", "label": "Anima"})
            wf = data["workflows"][0]
            self.assertEqual(wf["id"], "wf-anima-2k")
            self.assertNotIn("valid", wf)
            self.assertNotIn("error", wf)
            lora = data["loras"][0]
            self.assertEqual(lora["id"], "style_art.safetensors")
            self.assertEqual(lora["label"], "艺术风格")
            self.assertNotIn("profiled", lora)
            self.assertNotIn("extra_secret", lora)
            # 内部字段绝不外泄。
            self.assertNotIn("model_families", data)
            self.assertNotIn("description", data["families"][0])
            # 能力与设置照常下发。
            self.assertIn("capabilities", data)
            self.assertIn("settings", data)
            self.assertTrue(any(cap == "txt2img" for cap in data["capabilities"]))
        finally:
            await session.close()
            await manager.shutdown()
            tmp.cleanup()

    async def test_catalog_without_backend_catalog(self) -> None:
        # FakeEngine 无 catalog 方法：目录不报错，也不出现 families。
        async with self.session.get(f"{self.base}/room/v1/catalog") as resp:
            self.assertEqual(resp.status, 200)
            data = await resp.json()
        self.assertNotIn("families", data)
        self.assertIn("capabilities", data)
        self.assertIn("settings", data)


if __name__ == "__main__":
    unittest.main()

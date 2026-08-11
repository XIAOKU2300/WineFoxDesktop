"""Phase D — 聊天室 WebSocket 与消息层（desktop_room）。

覆盖：
- 加入流程（密码、保留名、重名、房间满、限流、明确错误码）
- 会话 Cookie（Secure/HttpOnly/SameSite=Lax）、房主令牌路径
- 消息发送/幂等/删除权限/聊天开关/分页
- WebSocket：未认证 401、快照、member.joined / message.created 广播、room.closing

说明：aiohttp 客户端在 http 下不会重发 Secure Cookie（unsafe 只影响存储），
因此测试把登录后拿到的会话令牌通过显式 Cookie 头携带，等价于浏览器走 HTTPS
时重发 Secure Cookie 的行为；服务端 Cookie 仍按规范设置 Secure。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import aiohttp

from desktop_room.manager import RoomManager
from desktop_room.protocol import MSG_TEXT

TEST_SETTINGS = {
    "name": "聊天测试房",
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
    """从登录响应的 Set-Cookie 中取出 wf_room_session 令牌原文。"""
    for header in resp.headers.getall("Set-Cookie", []):
        part = header.split(";", 1)[0].strip()
        if part.startswith("wf_room_session="):
            value = part.split("=", 1)[1]
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            return value
    raise AssertionError("响应未设置 wf_room_session Cookie")


class _Authed:
    """携带会话 Cookie 的请求包装器（显式 Cookie 头，模拟浏览器重发 Secure Cookie）。"""

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

    # 返回 aiohttp 的 _RequestContextManager / _WsConnectContextManager，
    # 既支持 await，也支持 async with（与原生 session.get/post 一致）。
    def post(self, url: str, **kw):
        return self._session.post(url, headers=self._headers(kw.pop("headers", None)), **kw)

    def get(self, url: str, **kw):
        return self._session.get(url, headers=self._headers(kw.pop("headers", None)), **kw)

    def ws_connect(self, url: str, **kw):
        return self._session.ws_connect(url, headers=self._headers(kw.pop("headers", None)), **kw)


class RoomChatTests(unittest.IsolatedAsyncioTestCase):
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
        self.guest = _Authed(self.session)  # 未登录，登录成功后被替换为带令牌的实例

    async def asyncTearDown(self) -> None:
        for s in self._sessions:
            await s.close()
        await self.manager.shutdown()
        self.tmp.cleanup()

    def _make_authed(self) -> _Authed:
        session = aiohttp.ClientSession()
        self._sessions.append(session)
        return _Authed(session)

    async def _join(self, name: str, password: str = "正确密码", client: _Authed | None = None):
        """登录一位成员；缺省使用 self.guest。成功后把会话令牌注入对应客户端。"""
        target = client or self.guest
        resp = await target.post(f"{self.base}/room/v1/join",
                                 json={"name": name, "password": password})
        if resp.status == 200:
            token = _extract_session_token(resp)
            if client is None:
                self.guest = self.guest.with_token(token)
            else:
                client._token = token
        return resp

    # ------------------------------------------------------------------
    # 加入流程
    # ------------------------------------------------------------------
    async def test_join_success_sets_secure_cookie(self) -> None:
        async with await self._join("访客一号") as resp:
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertTrue(data["joined"])
            self.assertEqual(data["member"]["role"], "guest")
            self.assertEqual(data["member"]["displayName"], "访客一号")
        set_cookie = resp.headers.getall("Set-Cookie") if "Set-Cookie" in resp.headers else []
        combined = " ".join(set_cookie)
        self.assertIn("wf_room_session=", combined)
        self.assertIn("HttpOnly", combined)
        self.assertIn("SameSite=Lax", combined)
        self.assertIn("Secure", combined)

    async def test_join_wrong_password_returns_401(self) -> None:
        async with await self._join("访客A", password="错密码") as resp:
            self.assertEqual(resp.status, 401)
            data = await resp.json()
            self.assertEqual(data["errorCode"], "WRONG_PASSWORD")

    async def test_join_reserved_name_rejected(self) -> None:
        for name in ("系统", "管理员", "WineFox", "房主"):
            async with await self._join(name) as resp:
                self.assertEqual(resp.status, 400)
                self.assertEqual((await resp.json())["errorCode"], "INVALID_NAME")

    async def test_join_duplicate_name_returns_explicit_error(self) -> None:
        async with await self._join("小明") as resp:
            self.assertEqual(resp.status, 200)
        async with await self._join("小 明") as resp:  # 去空白后同键
            self.assertEqual(resp.status, 409)
            data = await resp.json()
            self.assertEqual(data["errorCode"], "NAME_TAKEN")
            self.assertIn("已被使用", data["error"])

    async def test_join_room_full(self) -> None:
        # 房主已占 1 席，maxMembers=4 → 还能进 3 位
        for i in range(3):
            async with await self._join(f"访客{i}") as resp:
                self.assertEqual(resp.status, 200)
        async with await self._join("满员者") as resp:
            self.assertEqual(resp.status, 409)
            self.assertEqual((await resp.json())["errorCode"], "ROOM_FULL")

    async def test_join_missing_password(self) -> None:
        async with await self._join("某人", password="") as resp:
            self.assertEqual(resp.status, 400)
            self.assertEqual((await resp.json())["errorCode"], "PASSWORD_REQUIRED")

    async def test_login_rate_limit_blocks_after_five_failures(self) -> None:
        for i in range(5):
            async with await self._join(f"攻击者{i}", password="错") as resp:
                self.assertEqual(resp.status, 401)
        async with await self._join("攻击者X", password="错") as resp:
            self.assertEqual(resp.status, 429)
            self.assertEqual((await resp.json())["errorCode"], "RATE_LIMITED_IP")

    async def test_failed_login_attempts_reported_to_host(self) -> None:
        for _ in range(2):
            async with await self._join("失败者", password="错") as resp:
                self.assertEqual(resp.status, 401)
        # manager 通过 notify_login_failure 上报 room_status 携带失败次数
        room_statuses = [e for e in self.emitted if e.get("type") == "room_status"]
        self.assertTrue(room_statuses)
        latest = room_statuses[-1]
        self.assertGreaterEqual(int(latest.get("failedLoginAttempts", 0)), 2)

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------
    async def test_session_requires_cookie(self) -> None:
        async with self.session.get(f"{self.base}/room/v1/session") as resp:
            self.assertEqual(resp.status, 401)

    async def test_session_with_cookie_returns_member(self) -> None:
        async with await self._join("会话者") as resp:
            self.assertEqual(resp.status, 200)
        async with self.guest.get(f"{self.base}/room/v1/session") as resp:
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertEqual(data["member"]["displayName"], "会话者")
            self.assertEqual(data["room"]["id"], self.status["publicId"])

    async def test_host_token_path_returns_owner(self) -> None:
        async with self.session.get(
            f"{self.base}/room/v1/session",
            headers={"X-WineFox-Room-Host": self.host_token},
        ) as resp:
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertEqual(data["member"]["role"], "owner")

    # ------------------------------------------------------------------
    # 消息
    # ------------------------------------------------------------------
    async def test_send_message_and_persist(self) -> None:
        async with await self._join("发信者") as resp:
            self.assertEqual(resp.status, 200)
        async with self.guest.post(
            f"{self.base}/room/v1/messages",
            json={"text": "大家好", "clientMessageId": "c-1"},
        ) as resp:
            self.assertEqual(resp.status, 201)
            data = await resp.json()
            self.assertEqual(data["message"]["text"], "大家好")
            self.assertEqual(data["message"]["type"], MSG_TEXT)
            self.assertEqual(data["message"]["clientMessageId"], "c-1")
        async with self.guest.get(f"{self.base}/room/v1/messages") as resp:
            messages = (await resp.json())["messages"]
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["text"], "大家好")

    async def test_message_idempotent_by_client_message_id(self) -> None:
        async with await self._join("幂等者") as resp:
            self.assertEqual(resp.status, 200)
        payload = {"text": "只发一次", "clientMessageId": "dup-1"}
        async with self.guest.post(f"{self.base}/room/v1/messages", json=payload) as resp:
            first_id = (await resp.json())["message"]["id"]
        async with self.guest.post(f"{self.base}/room/v1/messages", json=payload) as resp:
            second_id = (await resp.json())["message"]["id"]
        self.assertEqual(first_id, second_id)
        async with self.guest.get(f"{self.base}/room/v1/messages") as resp:
            self.assertEqual(len((await resp.json())["messages"]), 1)

    async def test_empty_message_rejected(self) -> None:
        async with await self._join("空信者") as resp:
            self.assertEqual(resp.status, 200)
        async with self.guest.post(f"{self.base}/room/v1/messages", json={"text": "   "}) as resp:
            self.assertEqual(resp.status, 400)
            self.assertEqual((await resp.json())["errorCode"], "EMPTY_TEXT")

    async def test_message_with_lone_surrogate_rejected(self) -> None:
        async with await self._join("代理信者") as resp:
            self.assertEqual(resp.status, 200)
        async with self.guest.post(
            f"{self.base}/room/v1/messages",
            json={"text": "你好\udcac"},
        ) as resp:
            self.assertEqual(resp.status, 400)
            self.assertEqual((await resp.json())["errorCode"], "INVALID_CHARACTERS")
        # 未被写入数据库
        async with self.guest.get(f"{self.base}/room/v1/messages") as resp:
            self.assertEqual(len((await resp.json())["messages"]), 0)

    async def test_delete_own_message_allowed_sender_only(self) -> None:
        async with await self._join("删信者") as resp:
            self.assertEqual(resp.status, 200)
        async with self.guest.post(f"{self.base}/room/v1/messages", json={"text": "待删"}) as resp:
            message_id = (await resp.json())["message"]["id"]
        async with self.guest.post(
            f"{self.base}/room/v1/messages",
            json={"action": "delete", "messageId": message_id},
        ) as resp:
            self.assertEqual(resp.status, 200)
        # 已被删 → 分页不可见
        async with self.guest.get(f"{self.base}/room/v1/messages") as resp:
            self.assertEqual(len((await resp.json())["messages"]), 0)

    async def test_cannot_delete_others_message(self) -> None:
        async with await self._join("作者A") as resp:
            self.assertEqual(resp.status, 200)
        async with self.guest.post(f"{self.base}/room/v1/messages", json={"text": "他人消息"}) as resp:
            message_id = (await resp.json())["message"]["id"]
        # 用独立会话登录作者B，尝试删除作者A的消息 → 403
        guest_b = self._make_authed()
        async with await self._join("作者B", client=guest_b) as resp2:
            self.assertEqual(resp2.status, 200)
        async with guest_b.post(
            f"{self.base}/room/v1/messages",
            json={"action": "delete", "messageId": message_id},
        ) as resp3:
            self.assertEqual(resp3.status, 403)
            self.assertEqual((await resp3.json())["errorCode"], "FORBIDDEN")

    async def test_messages_require_auth(self) -> None:
        async with self.session.post(f"{self.base}/room/v1/messages", json={"text": "未登录"}) as resp:
            self.assertEqual(resp.status, 401)

    async def test_owner_can_delete_any_message(self) -> None:
        async with await self._join("被删作者") as resp:
            self.assertEqual(resp.status, 200)
        async with self.guest.post(f"{self.base}/room/v1/messages", json={"text": "房主删我"}) as resp:
            message_id = (await resp.json())["message"]["id"]
        async with self.session.post(
            f"{self.base}/room/v1/messages",
            json={"action": "delete", "messageId": message_id},
            headers={"X-WineFox-Room-Host": self.host_token},
        ) as resp:
            self.assertEqual(resp.status, 200)

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------
    async def test_ws_requires_auth(self) -> None:
        # 未认证 → 握手被拒（401）
        with self.assertRaises(aiohttp.WSServerHandshakeError):
            async with self.session.ws_connect(f"{self.base}/room/v1/ws"):
                pass

    async def test_ws_snapshot_on_connect(self) -> None:
        async with await self._join("看快照") as resp:
            self.assertEqual(resp.status, 200)
        async with self.guest.ws_connect(f"{self.base}/room/v1/ws") as ws:
            msg = await ws.receive_json(timeout=5)
            self.assertEqual(msg["event"], "room.snapshot")
            snapshot = msg["data"]
            self.assertEqual(snapshot["room"]["name"], "聊天测试房")
            self.assertEqual(snapshot["room"]["viewerMemberId"], (await self._self_member_id()))

    async def test_member_joined_broadcast_to_connected_ws(self) -> None:
        # 先登录一位并连 WS
        async with await self._join("先到者") as resp:
            self.assertEqual(resp.status, 200)
        async with self.guest.ws_connect(f"{self.base}/room/v1/ws") as ws:
            first = await ws.receive_json(timeout=5)
            self.assertEqual(first["event"], "room.snapshot")
            # 第二位通过独立会话登录 → 触发 member.joined 广播
            guest2 = self._make_authed()
            async with await self._join("后到者", client=guest2) as resp2:
                self.assertEqual(resp2.status, 200)
            event = await ws.receive_json(timeout=5)
            self.assertEqual(event["event"], "member.joined")
            self.assertEqual(event["data"]["displayName"], "后到者")

    async def test_message_created_broadcast(self) -> None:
        async with await self._join("广播者") as resp:
            self.assertEqual(resp.status, 200)
        async with self.guest.ws_connect(f"{self.base}/room/v1/ws") as ws:
            await ws.receive_json(timeout=5)  # snapshot
            async with self.guest.post(f"{self.base}/room/v1/messages", json={"text": "实时消息"}) as resp:
                self.assertEqual(resp.status, 201)
            event = await ws.receive_json(timeout=5)
            self.assertEqual(event["event"], "message.created")
            self.assertEqual(event["data"]["text"], "实时消息")

    async def test_room_closing_broadcast_on_stop(self) -> None:
        async with await self._join("闭房者") as resp:
            self.assertEqual(resp.status, 200)
        async with self.guest.ws_connect(f"{self.base}/room/v1/ws") as ws:
            await ws.receive_json(timeout=5)  # snapshot
            await self.manager.stop()
            msg = await ws.receive(timeout=5)
            self.assertIsNotNone(msg)

    async def _self_member_id(self) -> int:
        async with self.guest.get(f"{self.base}/room/v1/session") as resp:
            return int((await resp.json())["member"]["id"])


if __name__ == "__main__":
    unittest.main()

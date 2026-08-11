"""Phase C — 房间安全与存储（desktop_room 包）。

覆盖：
- Argon2id 哈希往返、参数、随机盐、常量时间比较
- 成员名称校验（1–24 字符、NFC、控制字符、保留名）
- 会话令牌（256 位、只存哈希）
- 登录限流（每 IP 5 次/分、每房间 30 次/分、指数退避、全局并发）
- 5 张表的 SQLite 存储（幂等迁移、消息/任务幂等、附件关联消息）
- Tunnel URL 语法校验
- 房间管理器生命周期（127.0.0.1 绑定、host-origin 中间件、优雅关闭）
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import aiohttp

from desktop_room import protocol
from desktop_room.manager import RoomManager, RoomStartError
from desktop_room.rate_limit import LoginRateLimiter
from desktop_room.security import (
    NameErrorDetail,
    RESERVED_NAMES,
    constant_time_equal,
    contains_lone_surrogate,
    hash_password,
    hash_session_token,
    normalize_lookup,
    new_host_token,
    new_session_token,
    validate_name,
    verify_password,
)
from desktop_room.storage import RoomStorage, new_public_id, utcnow_iso
from desktop_room.tunnel import TunnelValidationError, validate_tunnel_url_syntax


ROOT = Path(__file__).parents[1]

TEST_SETTINGS = {
    "name": "测试房间",
    "ownerName": "房主A",
    "password": "s3cret-密码",
    "maxMembers": 8,
    "allowChat": True,
    "allowUpload": True,
    "allowGeneration": True,
    "allowImg2img": True,
    "allowPromptEnhancement": True,
    "allowedModelFamilies": ["sdxl"],
    "allowedWorkflowIds": ["wf-1"],
    "allowedLoras": ["lora-1"],
    "perMemberQueueLimit": 1,
    "roomQueueLimit": 10,
    "messageRetentionDays": 7,
}


class Argon2SecurityTests(unittest.TestCase):
    def test_hash_password_returns_spec_params(self) -> None:
        algorithm, params_json, salt_b64, hash_b64 = hash_password("正确的密码")
        self.assertEqual(algorithm, "argon2id")
        params = json.loads(params_json)
        self.assertEqual(params["time_cost"], 3)
        self.assertEqual(params["memory_cost"], 65536)
        self.assertEqual(params["parallelism"], 1)
        self.assertEqual(params["hash_len"], 32)
        # salt 16 字节、hash 32 字节（base64url：24 / 44 字符，无填充）
        self.assertEqual(len(salt_b64), 24)
        self.assertEqual(len(hash_b64), 44)

    def test_verify_roundtrip_and_rejects_wrong(self) -> None:
        algorithm, params_json, salt_b64, hash_b64 = hash_password("正确")
        self.assertTrue(verify_password("正确", salt_b64, hash_b64, params_json))
        self.assertFalse(verify_password("错误", salt_b64, hash_b64, params_json))

    def test_random_salt_makes_hashes_differ(self) -> None:
        _, _, salt1, hash1 = hash_password("同一密码")
        _, _, salt2, hash2 = hash_password("同一密码")
        self.assertNotEqual(salt1, salt2)
        self.assertNotEqual(hash1, hash2)

    def test_no_plaintext_leaks(self) -> None:
        _, params, salt, h = hash_password("绝密密码")
        joined = params + salt + h
        self.assertNotIn("绝密密码", joined)


class NameValidationTests(unittest.TestCase):
    def test_normalizes_and_strips(self) -> None:
        self.assertEqual(validate_name("  张三  "), "张三")
        # NFC 规范化：组合字符归一
        self.assertEqual(validate_name("\u4e8c\u5143\u65e5"), "\u4e8c\u5143\u65e5")

    def test_rejects_empty_and_too_long(self) -> None:
        with self.assertRaises(NameErrorDetail):
            validate_name("   ")
        with self.assertRaises(NameErrorDetail):
            validate_name("名" * 25)

    def test_rejects_control_chars(self) -> None:
        for bad in ("a\nb", "a\tb", "a\x00b", "\u200b"):
            with self.assertRaises(NameErrorDetail):
                validate_name(bad)

    def test_rejects_reserved_names_case_insensitive(self) -> None:
        self.assertIn("winefox", RESERVED_NAMES)
        for name in ("系统", "管理员", "WineFox", "WINEFOX", "房主"):
            with self.assertRaises(NameErrorDetail):
                validate_name(name)

    def test_lookup_key_normalizes_case(self) -> None:
        self.assertEqual(normalize_lookup("  张三  "), "张三")
        self.assertEqual(normalize_lookup("WineFox"), "winefox")


class SessionTokenTests(unittest.TestCase):
    def test_token_is_256bit_and_only_hash_stored(self) -> None:
        token, token_hash = new_session_token()
        # 32 字节随机 → 64 位十六进制
        self.assertEqual(len(token), 64)
        self.assertEqual(len(token_hash), 64)  # sha256 hex
        self.assertNotEqual(token, token_hash)
        self.assertNotIn(token, token_hash)

    def test_hash_matches_stored(self) -> None:
        token, token_hash = new_session_token()
        self.assertEqual(hash_session_token(token), token_hash)

    def test_host_token_is_256bit_and_random(self) -> None:
        a, b = new_host_token(), new_host_token()
        self.assertEqual(len(a), 64)
        self.assertNotEqual(a, b)

    def test_constant_time_compare(self) -> None:
        self.assertTrue(constant_time_equal("abc", "abc"))
        self.assertFalse(constant_time_equal("abc", "abd"))


class LoneSurrogateTests(unittest.TestCase):
    """孤立代理字符检测（Windows IME/剪贴板可能产生，无法 UTF-8 编码）。"""

    def test_accepts_plain_text(self) -> None:
        for text in ("abc", "中文房间", "😀 合法代理对", "1234567"):
            self.assertFalse(contains_lone_surrogate(text))

    def test_detects_lone_low_surrogate(self) -> None:
        # 用户实际遇到的崩溃字符：\udcac（孤立低代理）
        self.assertTrue(contains_lone_surrogate("1234567\udcac"))
        self.assertTrue(contains_lone_surrogate("a\udc00b"))

    def test_detects_lone_high_surrogate(self) -> None:
        self.assertTrue(contains_lone_surrogate("\ud83d"))
        self.assertTrue(contains_lone_surrogate("名称\udbff"))

    def test_valid_surrogate_pair_not_flagged(self) -> None:
        # 高代理 + 低代理 = 合法 Unicode 字符（emoji），不视为孤立
        self.assertFalse(contains_lone_surrogate("😀"))
        self.assertFalse(contains_lone_surrogate("a😀b"))


class RateLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.limiter = LoginRateLimiter()

    def test_per_ip_five_failures_then_blocked(self) -> None:
        ip = "10.0.0.1"
        for _ in range(5):
            self.assertTrue(self.limiter.ip_allows(ip))
            self.limiter.record_failure(ip, "room1")
        self.assertFalse(self.limiter.ip_allows(ip))

    def test_per_room_thirty_failures(self) -> None:
        ip = "10.0.0.2"
        for _ in range(30):
            self.assertTrue(self.limiter.room_allows("room1", ip))
            self.limiter.record_failure(ip, "room1")
        self.assertFalse(self.limiter.room_allows("room1", ip))
        # 同一 IP 对另一房间不受房间级限制影响（仍受 IP 级限制）
        self.assertFalse(self.limiter.ip_allows(ip))

    def test_backoff_grows_and_caps(self) -> None:
        ip = "10.0.0.3"
        previous = 0.0
        for i in range(8):
            self.limiter.record_failure(ip, "r")
            current = self.limiter.backoff_seconds(ip)
            if i >= 1:
                self.assertGreaterEqual(current, previous)
            previous = current
        self.assertLessEqual(previous, LoginRateLimiter.BACKOFF_MAX_SECONDS)

    def test_success_resets(self) -> None:
        ip = "10.0.0.4"
        for _ in range(5):
            self.limiter.record_failure(ip, "r")
        self.limiter.record_success(ip, "r")
        self.assertTrue(self.limiter.ip_allows(ip))

    def test_room_failed_count_and_reset(self) -> None:
        self.limiter.record_failure("10.0.0.5", "roomX")
        self.limiter.record_failure("10.0.0.5", "roomX")
        self.assertEqual(self.limiter.room_failed_count("roomX"), 2)
        self.limiter.reset_room("roomX")
        self.assertEqual(self.limiter.room_failed_count("roomX"), 0)

    def test_global_concurrency_semaphore(self) -> None:
        sem = self.limiter.concurrent_guard()
        self.assertEqual(sem._value, LoginRateLimiter.GLOBAL_CONCURRENCY)


class TunnelSyntaxTests(unittest.TestCase):
    def test_accepts_valid_https(self) -> None:
        self.assertEqual(
            validate_tunnel_url_syntax("https://abc.trycloudflare.com/"),
            "https://abc.trycloudflare.com",
        )
        self.assertEqual(
            validate_tunnel_url_syntax("https://host:8443"),
            "https://host:8443",
        )

    def test_rejects_non_https_schemes(self) -> None:
        for bad in (
            "ftp://abc", "wss://abc", "ws://abc",
            "file:///C:/x", "javascript:alert(1)", "data:text/plain;base64,eA==",
            "about:blank", "gopher://abc",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(TunnelValidationError):
                    validate_tunnel_url_syntax(bad)

    def test_accepts_http_scheme(self) -> None:
        self.assertEqual(
            validate_tunnel_url_syntax("http://host:8443"),
            "http://host:8443",
        )
        self.assertEqual(
            validate_tunnel_url_syntax("http://192.168.1.5:51423/"),
            "http://192.168.1.5:51423",
        )

    def test_rejects_missing_host_and_userinfo(self) -> None:
        with self.assertRaises(TunnelValidationError):
            validate_tunnel_url_syntax("https:///path")
        with self.assertRaises(TunnelValidationError):
            validate_tunnel_url_syntax("https://user:pass@host")
        with self.assertRaises(TunnelValidationError):
            validate_tunnel_url_syntax("")

    def test_rejects_fragment_and_overlong(self) -> None:
        with self.assertRaises(TunnelValidationError):
            validate_tunnel_url_syntax("https://host#frag")
        with self.assertRaises(TunnelValidationError):
            validate_tunnel_url_syntax("https://" + "h" * 2100)


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "room.db"
        self.store = RoomStorage(self.db)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def _create_room(self, public_id: str | None = None) -> int:
        algorithm, params, salt, h = hash_password("pw")
        return self.store.create_room(
            public_id=public_id or new_public_id(),
            name="房间",
            owner_name="房主",
            password_algorithm=algorithm,
            password_parameters_json=params,
            password_salt=salt,
            password_hash=h,
            settings=TEST_SETTINGS,
        )

    def test_five_tables_exist(self) -> None:
        tables = {row[0] for row in self.store.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'room_%'"
        )}
        self.assertTrue({"rooms", "room_members", "room_messages", "room_attachments", "room_jobs"} <= tables)

    def test_migration_is_idempotent(self) -> None:
        self.store.close()
        self.store = RoomStorage(self.db)
        self.store.close()
        self.store = RoomStorage(self.db)
        tables = {row[0] for row in self.store.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'room_%'"
        )}
        self.assertIn("rooms", tables)

    def test_room_roundtrip_and_settings(self) -> None:
        rid = self._create_room()
        row = self.store.get_room(rid)
        self.assertEqual(row["name"], "房间")
        self.assertEqual(row["state"], protocol.ROOM_STATE_CREATED)
        settings = self.store.room_settings_map(rid)
        self.assertEqual(settings["publicId"], row["public_id"])
        self.assertEqual(settings["maxMembers"], 8)
        self.assertIn("sdxl", settings["allowedModelFamilies"])

    def test_member_lifecycle_and_uniqueness(self) -> None:
        rid = self._create_room()
        _, h1 = new_session_token()
        mid = self.store.add_member(room_id=rid, display_name="张三", normalized_name="张三",
                                    role=protocol.ROLE_GUEST, session_hash=h1)
        self.assertTrue(self.store.name_taken(rid, "张三"))
        self.assertFalse(self.store.name_taken(rid, "李四"))
        self.assertEqual(self.store.active_member_count(rid), 1)
        self.assertEqual(self.store.member_by_session(h1)["id"], mid)
        self.assertEqual(self.store.get_member(mid)["display_name"], "张三")

    def test_message_idempotency_by_client_message_id(self) -> None:
        rid = self._create_room()
        id1 = self.store.add_message(room_id=rid, sender_id=1, msg_type=protocol.MSG_TEXT,
                                    text="你好", client_message_id="c-1")
        id2 = self.store.add_message(room_id=rid, sender_id=1, msg_type=protocol.MSG_TEXT,
                                     text="你好", client_message_id="c-1")
        self.assertEqual(id1, id2)
        self.assertEqual(len(self.store.list_messages(room_id=rid)), 1)

    def test_message_pagination_and_delete(self) -> None:
        rid = self._create_room()
        ids = [self.store.add_message(room_id=rid, sender_id=1, msg_type=protocol.MSG_TEXT, text=f"m{i}")
               for i in range(5)]
        recent = self.store.list_messages(room_id=rid, before=None, limit=3)
        # 倒序返回
        self.assertEqual([r["id"] for r in recent], ids[::-1][:3])
        older = self.store.list_messages(room_id=rid, before=recent[-1]["id"], limit=10)
        self.assertEqual([r["id"] for r in older], ids[:2][::-1])
        self.store.mark_message_deleted(ids[2])
        self.assertIsNone(self.store.get_message(ids[2]))

    def test_attachments_link_to_message(self) -> None:
        rid = self._create_room()
        msg_id = self.store.add_message(room_id=rid, sender_id=1, msg_type=protocol.MSG_GENERATION_RESULT,
                                        text=None)
        a1 = self.store.add_attachment(room_id=rid, uploader_id=1, message_id=None, kind="generation_output",
                                       relative_path="out/abc.png", thumbnail_relpath="out/abc_thumb.png",
                                       mime_type="image/png", size=100, width=512, height=512, sha256="h")
        a2 = self.store.add_attachment(room_id=rid, uploader_id=1, message_id=None, kind="generation_output",
                                       relative_path="out/def.png", thumbnail_relpath=None,
                                       mime_type="image/png", size=200, width=512, height=512, sha256="h2")
        self.store.link_attachments_to_message([a1, a2], msg_id)
        atts = self.store.attachments_for_message(msg_id)
        self.assertEqual({a["id"] for a in atts}, {a1, a2})

    def test_job_idempotency_and_state(self) -> None:
        rid = self._create_room()
        cid = protocol.make_client_request_id(new_public_id(), 1, "uuid-1")
        job_id, is_new = self.store.create_job(room_id=rid, member_id=1, client_request_id=cid,
                                               request_snapshot={"prompt": "cat"})
        self.assertTrue(is_new)
        job_id2, is_new2 = self.store.create_job(room_id=rid, member_id=1, client_request_id=cid,
                                                 request_snapshot={"prompt": "cat"})
        self.assertEqual(job_id, job_id2)
        self.assertFalse(is_new2)
        self.store.set_job_state(job_id, protocol.JOB_STATE_RUNNING, core_job_id="core-1")
        self.assertEqual(self.store.get_job(job_id)["core_job_id"], "core-1")
        self.assertEqual(self.store.room_running_jobs(rid), 1)
        self.assertEqual(self.store.member_running_jobs(rid, 1), 1)
        self.store.set_job_state(job_id, protocol.JOB_STATE_COMPLETED)
        self.assertEqual(self.store.room_running_jobs(rid), 0)

    def test_room_state_transition(self) -> None:
        rid = self._create_room()
        self.store.set_room_state(rid, protocol.ROOM_STATE_ONLINE)
        self.assertEqual(self.store.get_room(rid)["state"], protocol.ROOM_STATE_ONLINE)
        self.store.set_room_state(rid, protocol.ROOM_STATE_CLOSED)
        self.assertIsNotNone(self.store.get_room(rid)["closed_at"])

    def test_public_id_is_unique(self) -> None:
        rid1 = self._create_room()
        rid2 = self._create_room()
        self.assertNotEqual(self.store.get_room(rid1)["public_id"], self.store.get_room(rid2)["public_id"])


class ManagerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.emitted: list[dict] = []
        self.manager = RoomManager(emit=self.emitted.append, rooms_root=Path(self.tmp.name))

    async def asyncTearDown(self) -> None:
        await self.manager.shutdown()
        self.tmp.cleanup()

    async def test_start_publishes_online_status_with_loopback_port(self) -> None:
        status = await self.manager.start(dict(TEST_SETTINGS))
        self.assertEqual(status["state"], "online")
        self.assertEqual(len(status["publicId"]), 16)
        self.assertEqual(status["members"], 1)
        self.assertEqual(status["maxMembers"], 8)
        port = status["port"]
        self.assertGreater(port, 0)
        self.assertEqual(status["localUrl"], f"http://127.0.0.1:{port}/")
        # 上报的 host_token 为 256 位
        last = self.emitted[-1]
        self.assertEqual(last["type"], "room_status")
        self.assertEqual(len(last.get("host_token", "")), 64)

    async def test_gateway_serves_info_and_catalog_on_loopback(self) -> None:
        status = await self.manager.start(dict(TEST_SETTINGS))
        port = status["port"]
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/room/v1/info") as resp:
                self.assertEqual(resp.status, 200)
                data = await resp.json()
                self.assertEqual(data["room"]["id"], status["publicId"])
                self.assertEqual(data["room"]["name"], "测试房间")
            async with session.get(f"http://127.0.0.1:{port}/room/v1/catalog") as resp:
                self.assertEqual(resp.status, 200)
                catalog = await resp.json()
                self.assertIn("txt2img", catalog["capabilities"])
                self.assertEqual(catalog["settings"]["maxMembers"], 8)

    async def test_host_origin_middleware_rejects_bad_host(self) -> None:
        status = await self.manager.start(dict(TEST_SETTINGS))
        port = status["port"]
        async with aiohttp.ClientSession() as session:
            # Host 头为外来主机名 → 403
            async with session.get(
                f"http://127.0.0.1:{port}/room/v1/info",
                headers={"Host": "evil.example.com"},
            ) as resp:
                self.assertEqual(resp.status, 403)
            # 面板代理注入正确 host_token → 放行
            host_token = self.emitted[-1]["host_token"]
            async with session.get(
                f"http://127.0.0.1:{port}/room/v1/info",
                headers={"Host": "evil.example.com", "X-WineFox-Room-Host": host_token},
            ) as resp:
                self.assertEqual(resp.status, 200)

    async def test_configured_tunnel_host_is_allowed_others_denied(self) -> None:
        status = await self.manager.start(dict(TEST_SETTINGS))
        port = status["port"]
        # 模拟 Tunnel 配置后 Host 放行（verify_tunnel 需要真实隧道，测试直接注入 allowlist）
        self.manager._app["allow_tunnel_hosts"] = ["abc.trycloudflare.com"]
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"http://127.0.0.1:{port}/room/v1/info",
                headers={"Host": "abc.trycloudflare.com"},
            ) as resp:
                self.assertEqual(resp.status, 200)
            async with session.get(
                f"http://127.0.0.1:{port}/room/v1/info",
                headers={"Host": "other.example.com"},
            ) as resp:
                self.assertEqual(resp.status, 403)

    async def test_lan_host_allowed_others_denied(self) -> None:
        status = await self.manager.start(dict(TEST_SETTINGS))
        port = status["port"]
        # 模拟管理器探测到的本机局域网 IP 放行（真实探测依赖网卡，测试直接注入）
        self.manager._app["lan_hosts"] = ["192.168.9.9"]
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"http://127.0.0.1:{port}/room/v1/info",
                headers={"Host": "192.168.9.9"},
            ) as resp:
                self.assertEqual(resp.status, 200)
            async with session.get(
                f"http://127.0.0.1:{port}/room/v1/info",
                headers={"Host": "192.168.9.10"},
            ) as resp:
                self.assertEqual(resp.status, 403)

    async def test_stop_returns_to_stopped_and_closes_gateway(self) -> None:
        status = await self.manager.start(dict(TEST_SETTINGS))
        port = status["port"]
        await self.manager.stop()
        self.assertEqual(self.manager.get_status()["state"], "stopped")
        async with aiohttp.ClientSession() as session:
            with self.assertRaises(aiohttp.ClientConnectorError):
                await session.get(f"http://127.0.0.1:{port}/room/v1/info")

    async def test_start_requires_fields(self) -> None:
        with self.assertRaises(RoomStartError):
            await self.manager.start({"name": "", "ownerName": "", "password": ""})

    async def test_start_rejects_lone_surrogates(self) -> None:
        for field in ("name", "ownerName", "password"):
            settings = dict(TEST_SETTINGS)
            settings[field] = settings[field] + "\udcac"
            with self.subTest(field=field):
                with self.assertRaises(RoomStartError) as ctx:
                    await self.manager.start(settings)
                self.assertEqual(ctx.exception.error_code, "INVALID_CHARACTERS")
        # 失败后状态仍为 stopped，不会残留半启动状态
        self.assertEqual(self.manager.get_status()["state"], "stopped")

    async def test_start_twice_is_idempotent(self) -> None:
        await self.manager.start(dict(TEST_SETTINGS))
        status = await self.manager.start(dict(TEST_SETTINGS))
        self.assertEqual(status["state"], "online")

    async def test_tunnel_clear_without_start_is_noop(self) -> None:
        status = self.manager.clear_tunnel()
        self.assertEqual(status["state"], "stopped")

    async def test_set_tunnel_requires_running_room(self) -> None:
        with self.assertRaises(TunnelValidationError):
            await self.manager.set_tunnel("https://abc.trycloudflare.com")

    async def test_start_with_custom_port(self) -> None:
        import socket as _socket
        with _socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]
        settings = dict(TEST_SETTINGS, port=free_port, bindHost="127.0.0.1")
        status = await self.manager.start(settings)
        self.assertEqual(status["port"], free_port)
        self.assertEqual(status["bindHost"], "127.0.0.1")
        self.assertEqual(status["localUrl"], f"http://127.0.0.1:{free_port}/")
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{free_port}/room/v1/info") as resp:
                self.assertEqual(resp.status, 200)

    async def test_start_rejects_invalid_port_and_bind(self) -> None:
        for port in (80, 65536):
            with self.subTest(port=port):
                with self.assertRaises(RoomStartError) as ctx:
                    await self.manager.start(dict(TEST_SETTINGS, port=port))
                self.assertEqual(ctx.exception.error_code, "INVALID_PORT")
        with self.assertRaises(RoomStartError) as ctx:
            await self.manager.start(dict(TEST_SETTINGS, port="abc"))
        self.assertEqual(ctx.exception.error_code, "INVALID_PORT")
        with self.assertRaises(RoomStartError) as ctx:
            await self.manager.start(dict(TEST_SETTINGS, bindHost="8.8.8.8"))
        self.assertEqual(ctx.exception.error_code, "INVALID_BIND_HOST")

    async def test_start_port_busy_raises_port_bind_failed(self) -> None:
        status = await self.manager.start(dict(TEST_SETTINGS))
        busy_port = status["port"]
        other = RoomManager(emit=self.emitted.append, rooms_root=Path(self.tmp.name))
        try:
            with self.assertRaises(RoomStartError) as ctx:
                await other.start(dict(TEST_SETTINGS, port=busy_port))
            self.assertEqual(ctx.exception.error_code, "PORT_BIND_FAILED")
        finally:
            await other.shutdown()

    async def test_set_tunnel_http_direct_connect(self) -> None:
        status = await self.manager.start(dict(TEST_SETTINGS))
        port = status["port"]
        result = await self.manager.set_tunnel(f"http://127.0.0.1:{port}")
        self.assertEqual(result["tunnelUrl"], f"http://127.0.0.1:{port}")
        self.assertTrue(result["tunnelConnected"])
        self.assertIsNone(result["verifyWarning"])
        self.assertIn("127.0.0.1", self.manager._app["allow_tunnel_hosts"])

    async def test_set_tunnel_http_unreachable_records_warning(self) -> None:
        await self.manager.start(dict(TEST_SETTINGS))
        result = await self.manager.set_tunnel("http://127.0.0.1:1")
        self.assertTrue(result["tunnelConnected"])
        self.assertIsNotNone(result["verifyWarning"])

    async def test_set_tunnel_https_unreachable_records_warning(self) -> None:
        from unittest.mock import AsyncMock, patch
        await self.manager.start(dict(TEST_SETTINGS))
        with patch("desktop_room.manager.verify_tunnel", new=AsyncMock(return_value=False)):
            result = await self.manager.set_tunnel("https://abc.trycloudflare.com")
        # 身份校验（回环 Host）通过即配置成功；公网探测失败仅记警告，不硬阻断
        self.assertTrue(result["tunnelConnected"])
        self.assertIsNotNone(result["verifyWarning"])
        self.assertIn("abc.trycloudflare.com", self.manager._app["allow_tunnel_hosts"])


class ModelSafetyTests(unittest.TestCase):
    """序列化模型绝不泄露密码/哈希/本地绝对路径。"""

    def test_package_exports_protocol_version(self) -> None:
        import desktop_room
        self.assertEqual(desktop_room.ROOM_PROTOCOL_VERSION, 1)

    def test_serialization_never_leaks_secrets(self) -> None:
        models = ROOT.joinpath("desktop_room/models.py").read_text(encoding="utf-8")
        for leak in ("password_hash", "password_salt", "session_hash", "password_parameters_json"):
            self.assertNotIn(leak, models)
        gateway = ROOT.joinpath("desktop_room/gateway.py").read_text(encoding="utf-8")
        self.assertNotIn("0.0.0.0", gateway)
        self.assertNotIn("password", gateway)


if __name__ == "__main__":
    unittest.main()

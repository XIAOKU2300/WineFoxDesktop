"""Phase F — 共享生图：访客通过房主 WineFox/ComfyUI 生成（desktop_room/generation_bridge）。

覆盖：
- 提交/查询/列表/幂等（clientRequestId）
- 队列限制（perMemberQueueLimit / roomQueueLimit）
- 任务状态广播 job.queued / job.progress / job.completed / job.failed
- 完成后结果消息（type=generation_result）+ 附件落库 + 授权 URL
- img2img 引用附件（refAttachmentId → 引擎 /uploads 引用图）
- 提示词增强（enhance）与图片分析（analyze）路由及开关
- 房主路由：暂停生图/入房、公告、踢人、改密码、仅房主可用
"""

from __future__ import annotations

import asyncio
import io
import tempfile
import time
import unittest
from pathlib import Path

import aiohttp
from PIL import Image

from desktop_room.manager import RoomManager
from desktop_room.protocol import (
    JOB_STATE_COMPLETED,
    JOB_STATE_QUEUED,
    MSG_GENERATION_RESULT,
    MSG_SYSTEM,
)

BASE_SETTINGS = {
    "name": "生图测试房",
    "ownerName": "房主A",
    "password": "正确密码",
    "maxMembers": 8,
    "allowChat": True,
    "allowUpload": True,
    "allowGeneration": True,
    "allowImg2img": True,
    "allowPromptEnhancement": True,
    "perMemberQueueLimit": 1,
    "roomQueueLimit": 10,
    "messageRetentionDays": 7,
}


class FakeJob:
    def __init__(self, job_id: str, payload: dict) -> None:
        self.id = job_id
        self.status = "queued"
        self.payload = payload
        self.queue_position = 1
        self.result = None
        self.error = None


class FakeEngine:
    """房主桌面引擎的替身：可即时完成，也可由测试手动推进状态。"""

    def __init__(self, *, auto_complete: bool = True) -> None:
        self.jobs: dict[str, FakeJob] = {}
        self.uploads: dict[str, bytes] = {}
        self.counter = 0
        self.auto_complete = auto_complete

    def _next_id(self) -> str:
        self.counter += 1
        return str(self.counter)

    async def submit(self, payload: dict) -> FakeJob:
        job = FakeJob(self._next_id(), dict(payload))
        self.jobs[job.id] = job
        if self.auto_complete:
            job.status = "completed"
            job.result = {"history_id": f"hist-{job.id}"}
        else:
            job.status = "queued"
        return job

    async def get(self, job_id: str) -> FakeJob | None:
        return self.jobs.get(str(job_id))

    async def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(str(job_id))
        if job is None or job.status in ("completed", "failed", "cancelled"):
            return False
        job.status = "cancelled"
        job.error = "cancelled"
        return True

    def set_status(self, job_id: str, status: str, *, result: dict | None = None,
                   error: str | None = None) -> None:
        job = self.jobs[str(job_id)]
        job.status = status
        if result is not None:
            job.result = result
        if error is not None:
            job.error = error

    async def history_image(self, history_id: str) -> tuple[bytes, str, str] | None:
        return (_png_bytes(64, 64), "image/png", f"{history_id}.png")

    async def save_upload(self, upload_id: str, data: bytes, *, filename: str, content_type: str) -> None:
        self.uploads[upload_id] = data

    async def enhance_prompt(self, prompt: str) -> str:
        return f"[enhanced] {prompt}"

    async def analyze_image(self, source_bytes: bytes, *, prompt: str) -> tuple[list[str], dict]:
        return (["tag1", "tag2"], {"status": "ok"})


def _png_bytes(width: int = 16, height: int = 16, color: tuple = (10, 200, 60)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, "PNG")
    return buf.getvalue()


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
    """显式携带会话 Cookie（及可选房主面板令牌）的请求包装器。"""

    def __init__(self, session: aiohttp.ClientSession, token: str | None = None,
                 host_token: str | None = None) -> None:
        self._session = session
        self._token = token
        self._host_token = host_token

    def with_token(self, token: str) -> "_Authed":
        return _Authed(self._session, token, self._host_token)

    def _headers(self, headers: dict | None) -> dict:
        headers = dict(headers or {})
        if self._token:
            headers.setdefault("Cookie", f"wf_room_session={self._token}")
        if self._host_token:
            headers.setdefault("X-WineFox-Room-Host", self._host_token)
        return headers

    def post(self, url: str, **kw):
        return self._session.post(url, headers=self._headers(kw.pop("headers", None)), **kw)

    def get(self, url: str, **kw):
        return self._session.get(url, headers=self._headers(kw.pop("headers", None)), **kw)

    def ws_connect(self, url: str, **kw):
        return self._session.ws_connect(url, headers=self._headers(kw.pop("headers", None)), **kw)


async def _join(session: aiohttp.ClientSession, base: str, name: str, password: str) -> str:
    async with session.post(f"{base}/room/v1/join",
                            json={"name": name, "password": password}) as resp:
        assert resp.status == 200, resp.status
        return _extract_session_token(resp)


class RoomGenerationTests(unittest.IsolatedAsyncioTestCase):
    def _new_manager(self, settings: dict, engine: FakeEngine, root: tempfile.TemporaryDirectory) -> RoomManager:
        return RoomManager(
            emit=self.emitted.append,
            rooms_root=Path(root.name),
            generation_backend=engine,
            generation_poll_interval=0.02,
        )

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.emitted: list[dict] = []
        self.engine = FakeEngine()
        self.manager = self._new_manager(dict(BASE_SETTINGS), self.engine, self.tmp)
        self.status = await self.manager.start(dict(BASE_SETTINGS))
        self.port = int(self.status["port"])
        self.base = f"http://127.0.0.1:{self.port}"
        self.public_id = str(self.status["publicId"])
        self.host_token = self.emitted[-1]["host_token"]
        self._sessions: list[aiohttp.ClientSession] = []
        self.session = aiohttp.ClientSession()
        self._sessions.append(self.session)
        async with self.session.post(f"{self.base}/room/v1/join",
                                     json={"name": "生成者", "password": "正确密码"}) as resp:
            self.assertEqual(resp.status, 200)
            token = _extract_session_token(resp)
            self.member_id = int((await resp.json())["member"]["id"])
        self.guest = _Authed(self.session, token, host_token=self.host_token)
        self.guest_member = _Authed(self.session, token)  # 无房主令牌（普通访客）

    async def asyncTearDown(self) -> None:
        for s in self._sessions:
            await s.close()
        await self.manager.shutdown()
        self.tmp.cleanup()

    def _client_id(self, member_id: int | None = None, uuid_hex: str = "a" * 32) -> str:
        return f"room:{self.public_id}:member:{member_id or self.member_id}:{uuid_hex}"

    def _request(self, prompt: str = "一只猫", **kw) -> dict:
        req = {"prompt": prompt, "width": 512, "height": 512, "steps": 20, "seed": -1}
        req.update(kw)
        return {"clientRequestId": self._client_id(), "request": req}

    async def _submit(self, body: dict, authed: _Authed | None = None):
        return await (authed or self.guest_member).post(
            f"{self.base}/room/v1/generations", json=body)

    async def _upload(self, data: bytes, filename: str = "ref.png") -> int:
        form = aiohttp.FormData()
        form.add_field("files", data, filename=filename, content_type="application/octet-stream")
        async with await self.guest_member.post(f"{self.base}/room/v1/uploads", data=form) as resp:
            self.assertEqual(resp.status, 201)
            return int((await resp.json())["message"]["attachments"][0]["id"])

    async def _wait_job_state(self, manager: RoomManager, job_id: int, state: str,
                              timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = manager._storage.get_job(job_id)
            if row is not None and row["state"] == state:
                return True
            await asyncio.sleep(0.02)
        return False

    # ------------------------------------------------------------------
    # 成功路径
    # ------------------------------------------------------------------
    async def test_submit_completes_and_creates_result_message(self) -> None:
        async with await self._submit(self._request("一只猫")) as resp:
            self.assertEqual(resp.status, 201)
            job = (await resp.json())["job"]
            self.assertEqual(job["state"], JOB_STATE_QUEUED)
            self.assertEqual(job["memberId"], self.member_id)
            self.assertEqual(job["request"]["prompt"], "一只猫")
            job_id = job["id"]
        self.assertTrue(await self._wait_job_state(self.manager, job_id, JOB_STATE_COMPLETED))
        async with self.guest_member.get(f"{self.base}/room/v1/messages") as resp:
            messages = (await resp.json())["messages"]
        results = [m for m in messages if m["type"] == MSG_GENERATION_RESULT]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["generationJobId"], job_id)
        self.assertEqual(len(results[0]["attachments"]), 1)
        att = results[0]["attachments"][0]
        self.assertEqual(att["kind"], "generated")
        self.assertTrue(att["url"].startswith("/room/v1/attachments/"))
        self.assertNotIn(":", att["url"])
        self.assertNotIn("\\", att["url"])
        row = self.manager._storage.get_attachment(att["id"])
        self.assertTrue((Path(self.manager._data_dir) / row["relative_path"]).is_file())
        async with self.guest_member.get(f"{self.base}{att['url']}") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "image/png")

    async def test_submit_idempotent(self) -> None:
        body = self._request("幂等测试")
        async with await self._submit(body) as resp:
            first = (await resp.json())["job"]
        async with await self._submit(body) as resp:
            data = await resp.json()
        self.assertEqual(first["id"], data["job"]["id"])
        self.assertFalse(data["created"])
        async with self.guest_member.get(f"{self.base}/room/v1/messages") as resp:
            messages = (await resp.json())["messages"]
        self.assertEqual(len([m for m in messages if m["type"] == MSG_GENERATION_RESULT]), 1)

    async def test_submit_broadcasts_events_and_result(self) -> None:
        async with self.guest_member.ws_connect(f"{self.base}/room/v1/ws") as ws:
            await ws.receive_json(timeout=5)  # snapshot
            async with await self._submit(self._request()) as resp:
                self.assertEqual(resp.status, 201)
            events: list[str] = []
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                event = await asyncio.wait_for(ws.receive_json(), timeout=5)
                events.append(event["event"])
                if event["event"] == "message.created" and event["data"]["type"] == MSG_GENERATION_RESULT:
                    break
            self.assertIn("job.queued", events)
            self.assertIn("job.completed", events)
            self.assertEqual(events.count("message.created"), 1)

    # ------------------------------------------------------------------
    # 校验与权限
    # ------------------------------------------------------------------
    async def test_submit_requires_auth(self) -> None:
        anon = aiohttp.ClientSession()
        self._sessions.append(anon)
        async with anon.post(f"{self.base}/room/v1/generations", json=self._request()) as resp:
            self.assertEqual(resp.status, 401)

    async def test_submit_requires_generation_enabled(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        manager = self._new_manager(dict(BASE_SETTINGS, allowGeneration=False), FakeEngine(), tmp)
        status = await manager.start(dict(BASE_SETTINGS, allowGeneration=False))
        base = f"http://127.0.0.1:{int(status['port'])}"
        session = aiohttp.ClientSession()
        self._sessions.append(session)
        token = await _join(session, base, "生成者", "正确密码")
        guest = _Authed(session, token)
        try:
            body = {"clientRequestId": f"room:{status['publicId']}:member:1:{'b'*32}",
                    "request": {"prompt": "x"}}
            async with guest.post(f"{base}/room/v1/generations", json=body) as resp:
                self.assertEqual(resp.status, 403)
                self.assertEqual((await resp.json())["errorCode"], "GENERATION_DISABLED")
        finally:
            await session.close()
            await manager.shutdown()
            tmp.cleanup()

    async def test_submit_rejects_bad_client_request_id(self) -> None:
        bad = {"clientRequestId": f"room:wrongroom:member:{self.member_id}:{'c'*32}",
               "request": {"prompt": "x"}}
        async with await self._submit(bad) as resp:
            self.assertEqual(resp.status, 400)
            self.assertEqual((await resp.json())["errorCode"], "BAD_CLIENT_REQUEST_ID")

    async def test_submit_rejects_empty_prompt(self) -> None:
        async with await self._submit(self._request("")) as resp:
            self.assertEqual(resp.status, 400)
            self.assertEqual((await resp.json())["errorCode"], "EMPTY_PROMPT")

    async def test_submit_rejects_ref_when_img2img_disabled(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        manager = self._new_manager(dict(BASE_SETTINGS, allowImg2img=False), FakeEngine(), tmp)
        status = await manager.start(dict(BASE_SETTINGS, allowImg2img=False))
        base = f"http://127.0.0.1:{int(status['port'])}"
        session = aiohttp.ClientSession()
        self._sessions.append(session)
        token = await _join(session, base, "生成者", "正确密码")
        guest = _Authed(session, token)
        try:
            async with guest.get(f"{base}/room/v1/session") as r:
                member_id = int((await r.json())["member"]["id"])
            body = {"clientRequestId": f"room:{status['publicId']}:member:{member_id}:{'d'*32}",
                    "request": {"prompt": "x", "refAttachmentId": 1}}
            async with guest.post(f"{base}/room/v1/generations", json=body) as resp:
                self.assertEqual(resp.status, 403)
                self.assertEqual((await resp.json())["errorCode"], "IMG2IMG_DISABLED")
        finally:
            await session.close()
            await manager.shutdown()
            tmp.cleanup()

    # ------------------------------------------------------------------
    # 队列限制
    # ------------------------------------------------------------------
    async def test_member_queue_limit(self) -> None:
        engine = FakeEngine(auto_complete=False)
        tmp = tempfile.TemporaryDirectory()
        manager = self._new_manager(dict(BASE_SETTINGS), engine, tmp)
        status = await manager.start(dict(BASE_SETTINGS))
        base = f"http://127.0.0.1:{int(status['port'])}"
        public_id = str(status["publicId"])
        session = aiohttp.ClientSession()
        self._sessions.append(session)
        token = await _join(session, base, "生成者", "正确密码")
        guest = _Authed(session, token)
        try:
            async with guest.get(f"{base}/room/v1/session") as r:
                member_id = int((await r.json())["member"]["id"])
            cid = f"room:{public_id}:member:{member_id}:{'e'*32}"
            async with guest.post(f"{base}/room/v1/generations",
                                  json={"clientRequestId": cid, "request": {"prompt": "一"}}) as resp:
                self.assertEqual(resp.status, 201)
            cid2 = f"room:{public_id}:member:{member_id}:{'f'*32}"
            async with guest.post(f"{base}/room/v1/generations",
                                  json={"clientRequestId": cid2, "request": {"prompt": "二"}}) as resp:
                self.assertEqual(resp.status, 429)
                self.assertEqual((await resp.json())["errorCode"], "QUEUE_LIMIT_MEMBER")
        finally:
            await session.close()
            await manager.shutdown()
            tmp.cleanup()

    async def test_room_queue_limit(self) -> None:
        engine = FakeEngine(auto_complete=False)
        tmp = tempfile.TemporaryDirectory()
        manager = self._new_manager(dict(BASE_SETTINGS, perMemberQueueLimit=2, roomQueueLimit=1), engine, tmp)
        status = await manager.start(dict(BASE_SETTINGS, perMemberQueueLimit=2, roomQueueLimit=1))
        base = f"http://127.0.0.1:{int(status['port'])}"
        public_id = str(status["publicId"])
        s1 = aiohttp.ClientSession(); self._sessions.append(s1)
        s2 = aiohttp.ClientSession(); self._sessions.append(s2)
        async with s1.post(f"{base}/room/v1/join", json={"name": "成员A", "password": "正确密码"}) as r:
            token_a = _extract_session_token(r)
            member_a = int((await r.json())["member"]["id"])
        token_b = await _join(s2, base, "成员B", "正确密码")
        g_a = _Authed(s1, token_a); g_b = _Authed(s2, token_b)
        try:
            async with g_a.post(f"{base}/room/v1/generations",
                                json={"clientRequestId": f"room:{public_id}:member:{member_a}:{'1'*32}",
                                      "request": {"prompt": "A"}}) as resp:
                self.assertEqual(resp.status, 201)
            async with g_b.get(f"{base}/room/v1/session") as r:
                member_b = int((await r.json())["member"]["id"])
            async with g_b.post(f"{base}/room/v1/generations",
                                json={"clientRequestId": f"room:{public_id}:member:{member_b}:{'2'*32}",
                                      "request": {"prompt": "B"}}) as resp:
                self.assertEqual(resp.status, 429)
                self.assertEqual((await resp.json())["errorCode"], "QUEUE_LIMIT_ROOM")
        finally:
            await s1.close(); await s2.close()
            await manager.shutdown(); tmp.cleanup()

    # ------------------------------------------------------------------
    # 状态推进 / 失败 / 取消
    # ------------------------------------------------------------------
    async def test_job_progress_and_completion_events(self) -> None:
        engine = FakeEngine(auto_complete=False)
        tmp = tempfile.TemporaryDirectory()
        manager = self._new_manager(dict(BASE_SETTINGS), engine, tmp)
        status = await manager.start(dict(BASE_SETTINGS))
        base = f"http://127.0.0.1:{int(status['port'])}"
        public_id = str(status["publicId"])
        session = aiohttp.ClientSession(); self._sessions.append(session)
        async with session.post(f"{base}/room/v1/join", json={"name": "生成者", "password": "正确密码"}) as r:
            token = _extract_session_token(r)
            member_id = int((await r.json())["member"]["id"])
        guest = _Authed(session, token)
        try:
            async with guest.ws_connect(f"{base}/room/v1/ws") as ws:
                await ws.receive_json(timeout=5)  # snapshot
                cid = f"room:{public_id}:member:{member_id}:{'3'*32}"
                async with guest.post(f"{base}/room/v1/generations",
                                      json={"clientRequestId": cid, "request": {"prompt": "推进"}}) as resp:
                    job_id = (await resp.json())["job"]["id"]
                seen: list[tuple[str, str]] = []
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    event = await asyncio.wait_for(ws.receive_json(), timeout=5)
                    if event["event"].startswith("job."):
                        seen.append((event["event"], event["data"]["job"]["state"]))
                        if event["event"] == "job.queued":
                            break
                core_id = next(iter(engine.jobs))
                engine.set_status(core_id, "running")
                while time.monotonic() < deadline and not (seen and seen[-1][0] == "job.progress"):
                    event = await asyncio.wait_for(ws.receive_json(), timeout=5)
                    if event["event"].startswith("job."):
                        seen.append((event["event"], event["data"]["job"]["state"]))
                self.assertEqual(seen[-1][0], "job.progress")
                self.assertEqual(seen[-1][1], "running")
                engine.set_status(core_id, "completed", result={"history_id": f"hist-{core_id}"})
                while time.monotonic() < deadline:
                    event = await asyncio.wait_for(ws.receive_json(), timeout=5)
                    if event["event"] == "job.completed":
                        break
                self.assertTrue(await self._wait_job_state(manager, job_id, JOB_STATE_COMPLETED))
        finally:
            await session.close()
            await manager.shutdown(); tmp.cleanup()

    async def test_job_failure_event(self) -> None:
        engine = FakeEngine(auto_complete=False)
        tmp = tempfile.TemporaryDirectory()
        manager = self._new_manager(dict(BASE_SETTINGS), engine, tmp)
        status = await manager.start(dict(BASE_SETTINGS))
        base = f"http://127.0.0.1:{int(status['port'])}"
        public_id = str(status["publicId"])
        session = aiohttp.ClientSession(); self._sessions.append(session)
        async with session.post(f"{base}/room/v1/join", json={"name": "生成者", "password": "正确密码"}) as r:
            token = _extract_session_token(r)
            member_id = int((await r.json())["member"]["id"])
        guest = _Authed(session, token)
        try:
            async with guest.ws_connect(f"{base}/room/v1/ws") as ws:
                await ws.receive_json(timeout=5)
                cid = f"room:{public_id}:member:{member_id}:{'4'*32}"
                async with guest.post(f"{base}/room/v1/generations",
                                      json={"clientRequestId": cid, "request": {"prompt": "失败"}}) as resp:
                    job_id = (await resp.json())["job"]["id"]
                core_id = next(iter(engine.jobs))
                engine.set_status(core_id, "failed", error="boom")
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    event = await asyncio.wait_for(ws.receive_json(), timeout=5)
                    if event["event"] == "job.failed":
                        break
                self.assertEqual(event["data"]["job"]["state"], "failed")
                # 安全要求：引擎原始错误（含本地路径/模型名等）绝不透传给访客。
                self.assertEqual(event["data"]["error"], "生成失败，请稍后再试")
                self.assertNotEqual(event["data"]["error"], "boom")
                self.assertTrue(await self._wait_job_state(manager, job_id, "failed"))
        finally:
            await session.close()
            await manager.shutdown(); tmp.cleanup()

    async def test_cancel_job(self) -> None:
        engine = FakeEngine(auto_complete=False)
        tmp = tempfile.TemporaryDirectory()
        manager = self._new_manager(dict(BASE_SETTINGS), engine, tmp)
        status = await manager.start(dict(BASE_SETTINGS))
        base = f"http://127.0.0.1:{int(status['port'])}"
        public_id = str(status["publicId"])
        session = aiohttp.ClientSession(); self._sessions.append(session)
        async with session.post(f"{base}/room/v1/join", json={"name": "生成者", "password": "正确密码"}) as r:
            token = _extract_session_token(r)
            member_id = int((await r.json())["member"]["id"])
        guest = _Authed(session, token)
        try:
            cid = f"room:{public_id}:member:{member_id}:{'5'*32}"
            async with guest.post(f"{base}/room/v1/generations",
                                  json={"clientRequestId": cid, "request": {"prompt": "取消"}}) as resp:
                job_id = (await resp.json())["job"]["id"]
            async with guest.post(f"{base}/room/v1/generations/{job_id}") as resp:
                self.assertEqual(resp.status, 200)
                body = await resp.json()
                self.assertTrue(body["cancelled"])
                self.assertEqual(body["job"]["state"], "cancelled")
            row = manager._storage.get_job(job_id)
            self.assertEqual(row["state"], "cancelled")
        finally:
            await session.close()
            await manager.shutdown(); tmp.cleanup()

    async def test_cancel_forbidden_for_other_member(self) -> None:
        engine = FakeEngine(auto_complete=False)
        tmp = tempfile.TemporaryDirectory()
        manager = self._new_manager(dict(BASE_SETTINGS), engine, tmp)
        status = await manager.start(dict(BASE_SETTINGS))
        base = f"http://127.0.0.1:{int(status['port'])}"
        public_id = str(status["publicId"])
        s1 = aiohttp.ClientSession(); self._sessions.append(s1)
        s2 = aiohttp.ClientSession(); self._sessions.append(s2)
        async with s1.post(f"{base}/room/v1/join", json={"name": "成员A", "password": "正确密码"}) as r:
            token_a = _extract_session_token(r); member_a = int((await r.json())["member"]["id"])
        token_b = await _join(s2, base, "成员B", "正确密码")
        g_a = _Authed(s1, token_a); g_b = _Authed(s2, token_b)
        try:
            async with g_a.post(f"{base}/room/v1/generations",
                                json={"clientRequestId": f"room:{public_id}:member:{member_a}:{'6'*32}",
                                      "request": {"prompt": "A"}}) as resp:
                job_id = (await resp.json())["job"]["id"]
            async with g_b.post(f"{base}/room/v1/generations/{job_id}") as resp:
                self.assertEqual(resp.status, 403)
                self.assertEqual((await resp.json())["errorCode"], "FORBIDDEN")
        finally:
            await s1.close(); await s2.close()
            await manager.shutdown(); tmp.cleanup()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    async def test_list_and_get(self) -> None:
        async with await self._submit(self._request("查询")) as resp:
            job_id = (await resp.json())["job"]["id"]
        async with self.guest_member.get(f"{self.base}/room/v1/generations") as resp:
            jobs = (await resp.json())["jobs"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], job_id)
        async with self.guest_member.get(f"{self.base}/room/v1/generations/") as resp:
            jobs2 = (await resp.json())["jobs"]
        self.assertEqual(len(jobs2), 1)
        async with self.guest_member.get(f"{self.base}/room/v1/generations/{job_id}") as resp:
            self.assertEqual((await resp.json())["job"]["id"], job_id)
        async with self.guest_member.get(f"{self.base}/room/v1/generations/999999") as resp:
            self.assertEqual(resp.status, 404)

    async def test_list_mine(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        manager = self._new_manager(dict(BASE_SETTINGS), FakeEngine(), tmp)
        status = await manager.start(dict(BASE_SETTINGS))
        base = f"http://127.0.0.1:{int(status['port'])}"
        public_id = str(status["publicId"])
        s1 = aiohttp.ClientSession(); self._sessions.append(s1)
        s2 = aiohttp.ClientSession(); self._sessions.append(s2)
        async with s1.post(f"{base}/room/v1/join", json={"name": "成员A", "password": "正确密码"}) as r:
            token_a = _extract_session_token(r); member_a = int((await r.json())["member"]["id"])
        token_b = await _join(s2, base, "成员B", "正确密码")
        g_a = _Authed(s1, token_a); g_b = _Authed(s2, token_b)
        try:
            async with g_a.post(f"{base}/room/v1/generations",
                                json={"clientRequestId": f"room:{public_id}:member:{member_a}:{'7'*32}",
                                      "request": {"prompt": "A"}}) as resp:
                self.assertEqual(resp.status, 201)
            async with g_b.get(f"{base}/room/v1/session") as r:
                member_b = int((await r.json())["member"]["id"])
            async with g_b.post(f"{base}/room/v1/generations",
                                json={"clientRequestId": f"room:{public_id}:member:{member_b}:{'8'*32}",
                                      "request": {"prompt": "B"}}) as resp:
                self.assertEqual(resp.status, 201)
            async with g_a.get(f"{base}/room/v1/generations?mine=1") as resp:
                jobs = (await resp.json())["jobs"]
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["memberId"], member_a)
            async with g_a.get(f"{base}/room/v1/generations") as resp:
                all_jobs = (await resp.json())["jobs"]
            self.assertEqual(len(all_jobs), 2)
        finally:
            await s1.close(); await s2.close()
            await manager.shutdown(); tmp.cleanup()

    # ------------------------------------------------------------------
    # enhance / analyze
    # ------------------------------------------------------------------
    async def test_enhance(self) -> None:
        async with self.guest_member.post(f"{self.base}/room/v1/generations/enhance",
                                          json={"prompt": "漂亮"}) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual((await resp.json())["enhanced"], "[enhanced] 漂亮")

    async def test_enhance_disabled(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        manager = self._new_manager(dict(BASE_SETTINGS, allowPromptEnhancement=False), FakeEngine(), tmp)
        status = await manager.start(dict(BASE_SETTINGS, allowPromptEnhancement=False))
        base = f"http://127.0.0.1:{int(status['port'])}"
        session = aiohttp.ClientSession(); self._sessions.append(session)
        token = await _join(session, base, "生成者", "正确密码")
        guest = _Authed(session, token)
        try:
            async with guest.post(f"{base}/room/v1/generations/enhance", json={"prompt": "x"}) as resp:
                self.assertEqual(resp.status, 403)
                self.assertEqual((await resp.json())["errorCode"], "ENHANCEMENT_DISABLED")
        finally:
            await session.close(); await manager.shutdown(); tmp.cleanup()

    async def test_analyze(self) -> None:
        att_id = await self._upload(_png_bytes(32, 32))
        async with self.guest_member.post(f"{self.base}/room/v1/generations/analyze",
                                          json={"attachmentId": att_id, "prompt": "看看"}) as resp:
            self.assertEqual(resp.status, 200)
            body = await resp.json()
            self.assertEqual(body["tags"], ["tag1", "tag2"])
            self.assertEqual(body["meta"]["status"], "ok")

    async def test_analyze_disabled(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        manager = self._new_manager(dict(BASE_SETTINGS, allowImg2img=False), FakeEngine(), tmp)
        status = await manager.start(dict(BASE_SETTINGS, allowImg2img=False))
        base = f"http://127.0.0.1:{int(status['port'])}"
        session = aiohttp.ClientSession(); self._sessions.append(session)
        token = await _join(session, base, "生成者", "正确密码")
        guest = _Authed(session, token)
        try:
            async with guest.post(f"{base}/room/v1/generations/analyze",
                                  json={"attachmentId": 1, "prompt": "x"}) as resp:
                self.assertEqual(resp.status, 403)
        finally:
            await session.close(); await manager.shutdown(); tmp.cleanup()

    async def test_img2img_ref_attachment(self) -> None:
        att_id = await self._upload(_png_bytes(48, 48))
        body = self._request("改图", refAttachmentId=att_id)
        async with await self._submit(body) as resp:
            self.assertEqual(resp.status, 201)
            job_id = (await resp.json())["job"]["id"]
        self.assertTrue(await self._wait_job_state(self.manager, job_id, JOB_STATE_COMPLETED))
        core_id = next(iter(self.engine.jobs))
        payload = self.engine.jobs[core_id].payload
        self.assertEqual(payload["mode"], "img2img")
        ref_url = payload["ref_image"]["url"]
        self.assertTrue(ref_url.startswith("/uploads/"))
        upload_id = ref_url.rsplit("/", 1)[1]
        self.assertIn(upload_id, self.engine.uploads)
        row = self.manager._storage.get_attachment(att_id)
        stored = (Path(self.manager._data_dir) / row["relative_path"]).read_bytes()
        self.assertEqual(self.engine.uploads[upload_id], stored)

    # ------------------------------------------------------------------
    # 房主管理
    # ------------------------------------------------------------------
    async def test_host_pause_blocks_generation(self) -> None:
        async with self.guest.post(f"{self.base}/room/v1/host/pause",
                                   json={"generationPaused": True}) as resp:
            self.assertEqual(resp.status, 200)
        async with await self._submit(self._request()) as resp:
            self.assertEqual(resp.status, 409)
            self.assertEqual((await resp.json())["errorCode"], "GENERATION_PAUSED")
        async with self.guest.post(f"{self.base}/room/v1/host/pause",
                                   json={"generationPaused": False}) as resp:
            self.assertEqual(resp.status, 200)
        async with await self._submit(self._request()) as resp:
            self.assertEqual(resp.status, 201)

    async def test_host_pause_blocks_join(self) -> None:
        async with self.guest.post(f"{self.base}/room/v1/host/pause",
                                   json={"membersJoinPaused": True}) as resp:
            self.assertEqual(resp.status, 200)
        anon = aiohttp.ClientSession()
        self._sessions.append(anon)
        async with anon.post(f"{self.base}/room/v1/join",
                             json={"name": "后来者", "password": "正确密码"}) as resp:
            self.assertEqual(resp.status, 409)
            self.assertEqual((await resp.json())["errorCode"], "JOIN_PAUSED")

    async def test_host_announce_and_kick(self) -> None:
        session_b = aiohttp.ClientSession(); self._sessions.append(session_b)
        token_b = await _join(session_b, self.base, "成员B", "正确密码")
        guest_b = _Authed(session_b, token_b)
        async with self.guest.post(f"{self.base}/room/v1/host/announce",
                                   json={"text": "欢迎光临"}) as resp:
            self.assertEqual(resp.status, 201)
        async with self.guest_member.get(f"{self.base}/room/v1/messages") as resp:
            messages = (await resp.json())["messages"]
        self.assertTrue(any(m["type"] == MSG_SYSTEM and m.get("text") == "欢迎光临" for m in messages))
        async with self.guest.get(f"{self.base}/room/v1/host/members") as resp:
            members = (await resp.json())["members"]
        target = next(m for m in members if m["displayName"] == "成员B")
        async with self.guest.post(f"{self.base}/room/v1/host/kick",
                                   json={"memberId": target["id"]}) as resp:
            self.assertEqual(resp.status, 200)
        async with guest_b.get(f"{self.base}/room/v1/session") as resp:
            self.assertEqual(resp.status, 401)
        async with self.guest.get(f"{self.base}/room/v1/host/members") as resp:
            names = [m["displayName"] for m in (await resp.json())["members"]]
        self.assertNotIn("成员B", names)

    async def test_host_password_change(self) -> None:
        async with self.guest.post(f"{self.base}/room/v1/host/password",
                                   json={"password": "新密码呀"}) as resp:
            self.assertEqual(resp.status, 200)
        anon = aiohttp.ClientSession(); self._sessions.append(anon)
        async with anon.post(f"{self.base}/room/v1/join",
                             json={"name": "新成员", "password": "正确密码"}) as resp:
            self.assertEqual(resp.status, 401)
        async with anon.post(f"{self.base}/room/v1/join",
                             json={"name": "新成员", "password": "新密码呀"}) as resp:
            self.assertEqual(resp.status, 200)

    async def test_host_routes_require_owner(self) -> None:
        session_b = aiohttp.ClientSession(); self._sessions.append(session_b)
        token_b = await _join(session_b, self.base, "成员B", "正确密码")
        guest_b = _Authed(session_b, token_b)
        async with guest_b.post(f"{self.base}/room/v1/host/kick", json={"memberId": 1}) as resp:
            self.assertEqual(resp.status, 403)
        async with guest_b.get(f"{self.base}/room/v1/host/status") as resp:
            self.assertEqual(resp.status, 403)


if __name__ == "__main__":
    unittest.main()

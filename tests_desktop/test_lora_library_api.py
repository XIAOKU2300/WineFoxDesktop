# -*- coding: utf-8 -*-
"""本地 LoRA 库 API 端点测试：路由校验 / 委托 / 参数边界。

轻量方式直接调用 handler（FakeRequest），不做真实 HTTP server，
避开安全中间件与静态目录装配。handler 校验分支与 engine 委托各覆盖一遍。

运行：``python -m unittest tests_desktop.test_lora_library_api -v``
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop_backend.errors import ValidationError  # noqa: E402
from desktop_backend.handlers import (  # noqa: E402
    CTX_KEY,
    handle_lora_library_delete,
    handle_lora_library_get,
    handle_lora_library_list,
    handle_lora_library_match_preview,
    handle_lora_library_quarantine,
    handle_lora_library_restore,
    handle_lora_library_update,
)

SAMPLE_ITEM = {
    "id": 7,
    "model_id": "456",
    "version_id": "123",
    "name": "Test Character LoRA",
    "family": "illustrious",
    "filename": "test_character_lora.safetensors",
    "triggers": ["test_character"],
    "aliases": ["Test Character"],
    "role": "",
    "state": "installed",
    "base_model": "Illustrious XL",
    "disabled": False,
}


class FakeEngine:
    def __init__(self):
        self.calls: list[tuple] = []

    def lora_library_list(self, family=None):
        self.calls.append(("list", family))
        return [dict(SAMPLE_ITEM)] if family in (None, "illustrious") else []

    def lora_library_get(self, asset_id):
        self.calls.append(("get", asset_id))
        if asset_id != 7:
            raise LookupError("no such asset")
        return dict(SAMPLE_ITEM)

    def lora_library_update(self, asset_id, *, aliases=None, role=None, triggers=None, disabled=None):
        self.calls.append(("update", asset_id, aliases, role, triggers, disabled))
        if asset_id != 7:
            raise LookupError("no such asset")
        item = dict(SAMPLE_ITEM)
        if aliases is not None:
            item["aliases"] = aliases
        if disabled is not None:
            item["disabled"] = disabled
        return item

    def lora_library_delete(self, asset_id):
        self.calls.append(("delete", asset_id))

    def lora_library_quarantine(self, asset_id, *, reason=""):
        self.calls.append(("quarantine", asset_id, reason))
        item = dict(SAMPLE_ITEM)
        item["state"] = "quarantined"
        return item

    def lora_library_restore(self, asset_id):
        self.calls.append(("restore", asset_id))
        item = dict(SAMPLE_ITEM)
        item["state"] = "installed"
        return item

    async def lora_library_match_preview(self, prompt, *, family="illustrious", limit=8):
        self.calls.append(("match_preview", prompt, family, limit))
        return {"prompt": prompt, "family": family, "matches": []}


class FakeRequest:
    def __init__(self, app_ctx, *, query=None, match=None, body=""):
        self.app = {CTX_KEY: app_ctx}
        self.query = dict(query or {})
        self.match_info = dict(match or {})
        self._body = body

    async def text(self):
        return self._body


def _body(resp) -> dict:
    data = json.loads(resp.body.decode("utf-8"))
    return data


class LoraLibraryApiBase(IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = FakeEngine()
        self.ctx = {"engine": self.engine}

    def _req(self, **kw):
        return FakeRequest(self.ctx, **kw)


class LibraryList(LoraLibraryApiBase):
    async def test_list_returns_items(self):
        resp = await handle_lora_library_list(self._req(query={"family": "illustrious"}))
        payload = _body(resp)["data"]
        self.assertEqual(payload["items"][0]["name"], SAMPLE_ITEM["name"])
        self.assertEqual(self.engine.calls[-1], ("list", "illustrious"))

    async def test_list_without_family_lists_all(self):
        await handle_lora_library_list(self._req(query={}))
        self.assertEqual(self.engine.calls[-1], ("list", None))

    async def test_list_bad_family_rejected(self):
        with self.assertRaises(ValidationError):
            await handle_lora_library_list(self._req(query={"family": "pokemon"}))


class LibraryGetUpdateDelete(LoraLibraryApiBase):
    async def test_get_ok(self):
        resp = await handle_lora_library_get(self._req(match={"id": "7"}))
        self.assertEqual(_body(resp)["data"]["id"], 7)

    async def test_get_bad_id_rejected(self):
        for bad in ("abc", "0", "-3", ""):
            with self.assertRaises(ValidationError):
                await handle_lora_library_get(self._req(match={"id": bad}))

    async def test_update_delegates_fields(self):
        resp = await handle_lora_library_update(
            self._req(match={"id": "7"}, body=json.dumps({"aliases": ["a", "b"], "disabled": True}))
        )
        item = _body(resp)["data"]
        self.assertEqual(item["aliases"], ["a", "b"])
        self.assertTrue(item["disabled"])
        call = self.engine.calls[-1]
        self.assertEqual(call[0], "update")
        self.assertEqual(call[2], ["a", "b"])
        self.assertEqual(call[5], True)

    async def test_update_invalid_types_rejected(self):
        for body in (
            {"aliases": "not-a-list"},
            {"triggers": [1, 2]},
            {"role": 42},
            {"disabled": "yes"},
            "not-a-dict",
        ):
            with self.assertRaises(ValidationError):
                await handle_lora_library_update(
                    self._req(match={"id": "7"}, body=json.dumps(body))
                )

    async def test_delete_ok(self):
        resp = await handle_lora_library_delete(self._req(match={"id": "7"}))
        self.assertTrue(_body(resp)["ok"])
        self.assertEqual(self.engine.calls[-1], ("delete", 7))


class LibraryQuarantineRestore(LoraLibraryApiBase):
    async def test_quarantine_with_reason(self):
        resp = await handle_lora_library_quarantine(
            self._req(match={"id": "7"}, body=json.dumps({"reason": "违规角色"}))
        )
        self.assertEqual(_body(resp)["data"]["state"], "quarantined")
        self.assertEqual(self.engine.calls[-1], ("quarantine", 7, "违规角色"))

    async def test_quarantine_empty_body_ok(self):
        resp = await handle_lora_library_quarantine(self._req(match={"id": "7"}, body=""))
        self.assertEqual(_body(resp)["data"]["state"], "quarantined")
        self.assertEqual(self.engine.calls[-1], ("quarantine", 7, ""))

    async def test_quarantine_long_reason_rejected(self):
        with self.assertRaises(ValidationError):
            await handle_lora_library_quarantine(
                self._req(match={"id": "7"}, body=json.dumps({"reason": "x" * 201}))
            )

    async def test_restore_ok(self):
        resp = await handle_lora_library_restore(self._req(match={"id": "7"}))
        self.assertEqual(_body(resp)["data"]["state"], "installed")
        self.assertEqual(self.engine.calls[-1], ("restore", 7))


class LibraryMatchPreview(LoraLibraryApiBase):
    async def test_match_preview_ok(self):
        resp = await handle_lora_library_match_preview(
            self._req(body=json.dumps({"prompt": "hoshino 与蓝天", "family": "illustrious"}))
        )
        payload = _body(resp)["data"]
        self.assertEqual(payload["family"], "illustrious")
        self.assertEqual(self.engine.calls[-1], ("match_preview", "hoshino 与蓝天", "illustrious", 8))

    async def test_match_preview_custom_limit_clamped(self):
        await handle_lora_library_match_preview(
            self._req(body=json.dumps({"prompt": "x", "limit": 999}))
        )
        self.assertEqual(self.engine.calls[-1][3], 20)
        await handle_lora_library_match_preview(
            self._req(body=json.dumps({"prompt": "x", "limit": -5}))
        )
        self.assertEqual(self.engine.calls[-1][3], 1)

    async def test_match_preview_empty_or_long_prompt_rejected(self):
        for prompt in ("", "   ", "x" * 2001):
            with self.assertRaises(ValidationError):
                await handle_lora_library_match_preview(
                    self._req(body=json.dumps({"prompt": prompt}))
                )

    async def test_match_preview_bad_family_rejected(self):
        with self.assertRaises(ValidationError):
            await handle_lora_library_match_preview(
                self._req(body=json.dumps({"prompt": "x", "family": "nope"}))
            )


class LibraryEngineMissing(LoraLibraryApiBase):
    def setUp(self):
        self.ctx = {"engine": None}

    async def test_all_handlers_503_without_engine(self):
        from desktop_backend.errors import AppError

        handlers = (
            handle_lora_library_list,
            handle_lora_library_get,
            handle_lora_library_update,
            handle_lora_library_delete,
            handle_lora_library_quarantine,
            handle_lora_library_restore,
            handle_lora_library_match_preview,
        )
        requests = (
            self._req(query={}),
            self._req(match={"id": "7"}),
            self._req(match={"id": "7"}, body="{}"),
            self._req(match={"id": "7"}),
            self._req(match={"id": "7"}, body="{}"),
            self._req(match={"id": "7"}),
            self._req(body='{"prompt": "x"}'),
        )
        for handler, request in zip(handlers, requests):
            with self.assertRaises(AppError) as ctx:
                await handler(request)
            self.assertEqual(ctx.exception.status, 503)


if __name__ == "__main__":
    unittest.main()

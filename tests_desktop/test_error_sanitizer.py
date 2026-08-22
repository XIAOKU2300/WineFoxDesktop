# -*- coding: utf-8 -*-
"""错误脱敏/翻译/死代理回退回归测试。

锁住三类真实事故：
1. 路径脱敏正则曾把 ``http://`` 里的 ``p:/`` 当盘符，URL 被截成 ``htt[LOCAL_PATH]``；
2. aiohttp 英文原文（Cannot connect to host ...）曾不经翻译直进界面；
3. 预览图/缩略图补拉曾绕过死代理探测，反复撞一个拒绝连接的端口。

运行：``python -m unittest tests_desktop.test_error_sanitizer -v``
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop_runtime import net_env
from desktop_runtime.lora_download import _safe_error, _translate_network_error
from desktop_runtime.setup_executor import _sanitize_error_text


class SanitizerUrlSafetyTests(unittest.TestCase):
    def test_proxy_url_not_mangled(self) -> None:
        message = "无法连接 Civitai 代理：http://192.168.0.1:7890。原始错误：连接被拒绝"
        cleaned = _safe_error(message)
        self.assertIn("http://192.168.0.1:7890", cleaned)
        self.assertNotIn("[LOCAL_PATH]", cleaned)

    def test_windows_path_still_redacted(self) -> None:
        cleaned = _safe_error("读取 C:\\Users\\admin\\x.model 失败")
        self.assertNotIn("C:", cleaned)
        self.assertIn("[LOCAL_PATH]", cleaned)

    def test_unc_path_redacted(self) -> None:
        cleaned = _safe_error("无法访问 \\\\server\\share\\model.safetensors")
        self.assertIn("[LOCAL_PATH]", cleaned)

    def test_executor_url_preserved(self) -> None:
        cleaned = _sanitize_error_text("下载 https://codeload.github.com/x.zip 失败")
        self.assertIn("codeload.github.com", cleaned)

    def test_executor_windows_path_filename_only(self) -> None:
        cleaned = _sanitize_error_text(r"复制 F:\AI\models\big.safetensors 失败")
        self.assertNotIn("F:", cleaned)
        self.assertIn("big.safetensors", cleaned)


class NetworkErrorTranslationTests(unittest.TestCase):
    def test_cannot_connect_translated(self) -> None:
        message = "Cannot connect to host 192.168.0.1:7890 ssl:default [远程计算机拒绝网络连接。]"
        cleaned = _safe_error(message)
        self.assertIn("无法连接 192.168.0.1:7890", cleaned)
        self.assertNotIn("Cannot connect", cleaned)
        self.assertNotIn("ssl:", cleaned)

    def test_dns_failure_translated(self) -> None:
        self.assertIn("域名无法解析", _translate_network_error("Name or service not known"))

    def test_connection_reset_translated(self) -> None:
        self.assertIn("连接被重置", _translate_network_error("Server disconnected: Connection reset by peer"))


class DeadProxyFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_probe_called_before_proxy_use(self) -> None:
        """预览图下载前必须先探测显式代理（死代理→回退系统代理）。"""
        from desktop_runtime.lora_preview import LoraPreviewService

        calls: list[str] = []

        async def fake_probe() -> None:
            calls.append("probe")

        service = LoraPreviewService(
            data_root=Path("."),
            proxy="",
            proxy_probe=fake_probe,
        )
        token = service.register("", "https://civitai.com/x/preview.jpeg")
        assert token
        # 走真实 fetch -> _download_and_validate：探测必须先于网络请求发生，
        # 请求本身连不上的地址即可（返回 None 不影响断言）。
        await service.fetch(str(token))
        self.assertEqual(calls[:1], ["probe"])

    async def test_library_backfill_probes_explicit_proxy(self) -> None:
        """缩略图补拉：ensure_probed 必须先于 effective_proxy 执行。"""
        import inspect

        from desktop_runtime import lora_library

        source = inspect.getsource(lora_library.LoraLibraryService._backfill_one_preview)
        ensure_pos = source.find("ensure_probed")
        effective_pos = source.find("effective_proxy")
        self.assertGreater(ensure_pos, 0, "补拉路径缺少 ensure_probed")
        self.assertGreater(effective_pos, 0)
        self.assertLess(ensure_pos, effective_pos, "必须先探测再取代理")


if __name__ == "__main__":
    unittest.main()

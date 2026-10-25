# -*- coding: utf-8 -*-
"""出网环境/凭证简化功能测试：系统代理自动回退、Cookie 归一化、
LoRA 链路诊断报告。

运行：``python -m unittest tests_desktop.test_net_env_and_credentials -v``
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest import mock  # noqa: F401  (NetEnvTests 使用 unittest.mock)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop_runtime import net_env  # noqa: E402
from desktop_runtime.config_adapter import normalize_civitai_cookie, normalize_secret_value  # noqa: E402
from desktop_runtime.lora_download import LoraDownloadService  # noqa: E402


class FakeConfig:
    def __init__(self, snapshot=None, secrets=None):
        self._snapshot = dict(snapshot or {})
        self._secrets = dict(secrets or {})

    def snapshot(self):
        return json.loads(json.dumps(self._snapshot))

    def get_secret(self, name: str) -> str:
        return self._secrets.get(name, "")


class NetEnvTests(unittest.TestCase):
    def tearDown(self):
        net_env.reset_cache()

    def test_explicit_proxy_wins_and_normalized(self):
        proxy, source = net_env.effective_proxy("127.0.0.1:7890")
        self.assertEqual(proxy, "http://127.0.0.1:7890")
        self.assertEqual(source, "explicit")

    def test_blank_explicit_falls_back_to_environment(self):
        # 隔离真实机器的注册表设置，只验证环境变量回退。
        with mock.patch.object(net_env, "_proxy_from_windows_registry", return_value=""):
            net_env.reset_cache()
            old = dict(net_env.os.environ)
            try:
                net_env.os.environ.pop("HTTPS_PROXY", None)
                self.assertEqual(net_env.effective_proxy(""), ("", ""))
                net_env.os.environ["HTTPS_PROXY"] = "http://10.0.0.8:8888"
                net_env.reset_cache()
                proxy, source = net_env.effective_proxy("")
                self.assertEqual(proxy, "http://10.0.0.8:8888")
                self.assertEqual(source, "system")
            finally:
                net_env.os.environ.clear()
                net_env.os.environ.update(old)
                net_env.reset_cache()

    def test_registry_proxy_server_formats(self):
        self.assertEqual(
            net_env._parse_registry_proxy_server("127.0.0.1:7890"),
            "http://127.0.0.1:7890",
        )
        self.assertEqual(
            net_env._parse_registry_proxy_server("http=127.0.0.1:7890;https=127.0.0.1:7891;ftp=10.0.0.1:80"),
            "http://127.0.0.1:7891",
        )
        self.assertEqual(net_env._parse_registry_proxy_server(""), "")
        self.assertEqual(net_env._parse_registry_proxy_server("ftp=1.2.3.4:21"), "")

    def test_socks_url_not_mangled(self):
        proxy, _ = net_env.effective_proxy("socks5://127.0.0.1:7891")
        self.assertEqual(proxy, "socks5://127.0.0.1:7891")

    def test_unreachable_explicit_falls_back_to_system(self):
        with mock.patch.object(net_env, "_proxy_from_windows_registry", return_value="http://127.0.0.1:7890"):
            net_env.reset_cache()
            try:
                # 未探测时显式代理优先
                proxy, source = net_env.effective_proxy("http://192.168.0.1:7890")
                self.assertEqual((proxy, source), ("http://192.168.0.1:7890", "explicit"))
                # 记录"近期探测不可达"后自动回退系统代理
                net_env._proxy_probe_cache["http://192.168.0.1:7890"] = (net_env.time.monotonic(), False)
                proxy, source = net_env.effective_proxy("http://192.168.0.1:7890")
                self.assertEqual((proxy, source), ("http://127.0.0.1:7890", "system-fallback"))
            finally:
                net_env.reset_cache()

    def test_unreachable_explicit_without_system_goes_direct(self):
        with mock.patch.object(net_env, "_proxy_from_windows_registry", return_value=""):
            net_env.reset_cache()
            try:
                net_env._proxy_probe_cache["http://192.168.0.1:7890"] = (net_env.time.monotonic(), False)
                proxy, source = net_env.effective_proxy("http://192.168.0.1:7890")
                self.assertEqual((proxy, source), ("", ""))
            finally:
                net_env.reset_cache()

    def test_stale_unreachable_verdict_reverts_to_explicit(self):
        net_env.reset_cache()
        try:
            net_env._proxy_probe_cache["http://192.168.0.1:7890"] = (
                net_env.time.monotonic() - net_env._PROXY_PROBE_HARD_STALE - 1, False
            )
            proxy, source = net_env.effective_proxy("http://192.168.0.1:7890")
            self.assertEqual((proxy, source), ("http://192.168.0.1:7890", "explicit"))
        finally:
            net_env.reset_cache()

    def test_ensure_probed_records_reachability(self):
        async def run():
            net_env.reset_cache()
            try:
                await net_env.ensure_probed("http://127.0.0.1:1")
                at, reachable = net_env._proxy_probe_cache["http://127.0.0.1:1"]
                self.assertFalse(reachable)
                # socks 协议直接判不可用
                await net_env.ensure_probed("socks5://127.0.0.1:1080")
                at2, reachable2 = net_env._proxy_probe_cache["socks5://127.0.0.1:1080"]
                self.assertFalse(reachable2)
            finally:
                net_env.reset_cache()

        asyncio.run(run())


class CookieNormalizationTests(unittest.TestCase):
    def test_plain_header(self):
        self.assertEqual(
            normalize_civitai_cookie("a=1; b=2"),
            "a=1; b=2",
        )

    def test_leading_cookie_prefix(self):
        self.assertEqual(
            normalize_civitai_cookie("Cookie: a=1; b=2"),
            "a=1; b=2",
        )

    def test_full_request_headers_block(self):
        raw = (
            "GET / HTTP/1.1\n"
            "Host: civitai.com\n"
            "User-Agent: Mozilla/5.0\n"
            "cookie: civitai_token=abc; theme=dark\n"
            "Accept: */*"
        )
        self.assertEqual(
            normalize_civitai_cookie(raw),
            "civitai_token=abc; theme=dark",
        )

    def test_multiline_value_joined(self):
        self.assertEqual(
            normalize_civitai_cookie("a=1;\n b=2"),
            "a=1; b=2",
        )

    def test_quotes_and_trailing_semicolon(self):
        self.assertEqual(
            normalize_civitai_cookie('"a=1; b=2";'),
            "a=1; b=2",
        )

    def test_token_normalization_only_whitespace(self):
        self.assertEqual(
            normalize_secret_value("civitai_api_token", "  abc\n123 \n"),
            "abc 123",
        )

    def test_unknown_secret_passthrough_trim(self):
        self.assertEqual(normalize_secret_value("other_key", " x "), "x")


class TestConnectionReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_shape_and_guidance(self):
        class FailingClient:
            api_base = "https://civitai.com/api/v1"

            def __init__(self, **kwargs):
                self.kwargs = kwargs

            async def _get_json(self, url):
                raise RuntimeError("连接 Civitai 超时")

        service = LoraDownloadService(
            db=None,
            config=FakeConfig(
                snapshot={"lora_download": {"proxy": "", "request_timeout": 20}},
                secrets={"civitai_api_token": "tok", "civitai_cookie": "civitai_token=abc; x=1"},
            ),
            data_root=Path("."),
            connection_factory=lambda: None,
            client_factory=FailingClient,
            red_client_factory=FailingClient,
            hf_client_factory=FailingClient,
        )
        net_env.reset_cache()
        old = dict(net_env.os.environ)
        try:
            with mock.patch.object(net_env, "_proxy_from_windows_registry", return_value=""):
                net_env.os.environ.pop("HTTPS_PROXY", None)
                net_env.os.environ.pop("https_proxy", None)
                net_env.os.environ.pop("ALL_PROXY", None)
                net_env.os.environ.pop("all_proxy", None)
                net_env.os.environ.pop("HTTP_PROXY", None)
                net_env.os.environ.pop("http_proxy", None)
                report = await service.test_connection()
        finally:
            net_env.os.environ.clear()
            net_env.os.environ.update(old)
            net_env.reset_cache()

        self.assertIn("proxy", report)
        self.assertIn("api", report)
        self.assertIn("token", report)
        self.assertIn("cookie", report)
        self.assertFalse(report["api"]["ok"])
        self.assertTrue(report["token"]["configured"])
        self.assertTrue(report["cookie"]["configured"])
        self.assertTrue(report["cookie"]["looks_valid"])
        self.assertTrue(any("代理" in item for item in report["guidance"]), report["guidance"])
        self.assertFalse(report["ok"])

    async def test_socks_proxy_gets_actionable_guidance(self):
        class OkClient:
            api_base = "https://civitai.com/api/v1"

            def __init__(self, **kwargs):
                self.kwargs = kwargs

            async def _get_json(self, url):
                return {"items": []}

        service = LoraDownloadService(
            db=None,
            config=FakeConfig(snapshot={"lora_download": {"proxy": "socks5://127.0.0.1:7891"}}),
            data_root=Path("."),
            connection_factory=lambda: None,
            client_factory=OkClient,
            red_client_factory=OkClient,
            hf_client_factory=OkClient,
        )
        with mock.patch.object(net_env, "_proxy_from_windows_registry", return_value=""):
            net_env.reset_cache()
            try:
                report = await service.test_connection()
            finally:
                net_env.reset_cache()
        # An unsupported explicit protocol cannot produce a false green result
        # by testing a different route from the one real downloads would use.
        self.assertEqual(report["proxy"]["explicit_value"], "socks5://127.0.0.1:7891")
        self.assertTrue(any("HTTP" in item for item in report["guidance"]), report["guidance"])
        self.assertEqual(report["proxy"]["value"], "")
        self.assertFalse(report["api"]["ok"])
        self.assertFalse(report["ok"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import web

import civitai_api
from civitai_api import CivitaiClient, HuggingFaceClient
from desktop_runtime import net_env
from desktop_runtime.lora_search import LoraSearchService
from winefox_core import lora_transfer


DATA = bytes(range(256)) * 256


class NetworkReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lora_network_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runners = []
        civitai_api._PROXY_CIRCUITS.clear()

    async def asyncTearDown(self):
        await civitai_api.close_shared_api_sessions()
        for runner in reversed(self.runners):
            await runner.cleanup()
        net_env.reset_cache()
        civitai_api._PROXY_CIRCUITS.clear()

    async def server(self, handler):
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.runners.append(runner)
        return f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    def client(self, base, cls=CivitaiClient, **kwargs):
        client = cls(network_max_retries=2, split=2, **kwargs)
        client.site_base = base
        return client

    def range_response(self, request, *, wrong=False):
        headers = {"ETag": '"fixture-v1"', "Accept-Ranges": "bytes"}
        if request.method == "HEAD":
            return web.Response(headers={**headers, "Content-Length": str(len(DATA))})
        value = request.headers.get("Range", "")
        if value:
            start, end = value.removeprefix("bytes=").split("-")
            start, end = int(start), int(end or len(DATA) - 1)
            headers["Content-Range"] = f"bytes {start + int(wrong)}-{end}/{len(DATA)}"
            return web.Response(status=206, body=DATA[start:end + 1], headers=headers)
        return web.Response(body=DATA, headers=headers)

    async def test_web_search_post_sends_custom_authorization(self):
        seen = []
        async def handle(request):
            seen.append(bool(request.headers.get("Authorization")))
            return web.json_response({"hits": []})
        base = await self.server(handle)
        await self.client(base)._post_json(base, {}, headers={"Authorization": "Bearer fixture"})
        self.assertEqual(seen, [True])

    async def test_page_get_sends_configured_credentials(self):
        async def handle(request):
            return web.Response(text="yes" if request.headers.get("Cookie") else "missing")
        base = await self.server(handle)
        result = await self.client(base, cookie="fixture=value")._get_text(base)
        self.assertEqual(result, "yes")

    async def test_page_get_obeys_request_timeout(self):
        async def handle(request):
            await asyncio.sleep(.4)
            return web.Response(text="late")
        base = await self.server(handle)
        client = self.client(base)
        client.network_max_retries = 1
        client.timeout = .03
        started = time.monotonic()
        with self.assertRaises(RuntimeError):
            await client._get_text(base)
        self.assertLess(time.monotonic() - started, .25)

    async def test_origin_timeout_does_not_poison_proxy_circuit(self):
        client = CivitaiClient(proxy="http://127.0.0.1:12345")
        self.assertFalse(client._is_proxy_network_error(TimeoutError()))

    async def test_rest_fallback_is_not_starved_by_slow_web_search(self):
        class Config:
            def snapshot(self): return {}
            def get_secret(self, name): return ""
        client = CivitaiClient()
        async def web_search(**kwargs):
            await asyncio.sleep(1)
            return []
        async def rest(**kwargs):
            return [{"name": "Fox", "type": "LORA", "version_id": "1", "base_model": "Illustrious", "page_url": "https://civitai.com/models/1"}]
        client.search_loras_via_web = web_search
        client._search_loras_rest_api = rest
        client._combined_search_timeout = lambda: 1
        client._search_hedge_delay = lambda: .01
        service = LoraSearchService(config=Config(), data_root=self.root, client_factory=lambda **kw: client)
        service._per_source_search_timeout = lambda: .12
        with mock.patch.object(net_env, "ensure_probed", new=mock.AsyncMock()):
            result = await service.search("Fox", sources=["civitai"])
        self.assertTrue(result["items"])

    async def test_huggingface_download_does_not_require_head_permission(self):
        async def handle(request):
            if request.method == "HEAD":
                return web.Response(status=403)
            return self.range_response(request)
        base = await self.server(handle)
        dest = self.root / "hf.safetensors"
        await self.client(base, HuggingFaceClient).download_file_multithread(base, dest)
        self.assertEqual(dest.read_bytes(), DATA)

    async def test_incorrect_content_range_is_rejected(self):
        async def handle(request): return self.range_response(request, wrong=True)
        base = await self.server(handle)
        with self.assertRaisesRegex(RuntimeError, "Range|范围|分片"):
            await self.client(base).download_file_multithread(base, self.root / "wrong.safetensors")

    async def test_stale_done_markers_cannot_fabricate_a_completed_file(self):
        async def handle(request): return self.range_response(request)
        base = await self.server(handle)
        dest = self.root / "stale.safetensors"
        stale = dest.parent / f".{dest.name}.parts"
        stale.mkdir()
        for index in range(2):
            (stale / f"{index}.done").write_text(str(len(DATA) // 2))
        await self.client(base).download_file_multithread(base, dest)
        self.assertEqual(dest.read_bytes(), DATA)

    async def test_transient_stream_disconnect_resumes_without_losing_bytes(self):
        calls = []
        broken = False
        async def handle(request):
            nonlocal broken
            calls.append(request.headers.get("Range", ""))
            if request.method != "HEAD" and not request.headers.get("Range") and not broken:
                broken = True
                response = web.StreamResponse(headers={"Content-Length": str(len(DATA)), "ETag": '"fixture-v1"'})
                await response.prepare(request)
                await response.write(DATA[:16384])
                request.transport.close()
                return response
            return self.range_response(request)
        base = await self.server(handle)
        dest = self.root / "resume.safetensors"
        await self.client(base).download_file_single(base, dest)
        self.assertEqual(dest.read_bytes(), DATA)
        self.assertTrue(any(value.startswith("bytes=16384-") for value in calls))

    async def test_forbidden_is_not_retried_as_a_different_download_mode(self):
        calls = []
        async def handle(request):
            calls.append(request.method)
            return web.Response(status=403)
        base = await self.server(handle)
        with self.assertRaises(RuntimeError):
            await self.client(base).download_file(base, self.root / "private.safetensors")
        self.assertEqual(len(calls), 1)

    async def test_cross_origin_download_does_not_forward_provider_credentials(self):
        seen = []
        async def cdn(request):
            seen.append(any(request.headers.get(key) for key in ("Authorization", "Cookie")))
            return self.range_response(request)
        target = await self.server(cdn)
        async def origin(request): return web.HTTPFound(target)
        base = await self.server(origin)
        await self.client(base, api_token="fixture", cookie="fixture=value").download_file(base, self.root / "redirect.safetensors")
        self.assertTrue(seen)
        self.assertFalse(any(seen))

    async def test_dead_environment_proxy_falls_back_to_working_windows_proxy(self):
        dead, healthy = "http://127.0.0.1:1", "http://127.0.0.1:2"
        with mock.patch.object(net_env, "_proxy_from_environment", return_value=dead), mock.patch.object(net_env, "_proxy_from_windows_registry", return_value=healthy), mock.patch.object(net_env, "probe_proxy_reachable", new=mock.AsyncMock(side_effect=lambda value: value == healthy)):
            net_env.reset_cache()
            await net_env.ensure_probed("")
            proxy, _ = net_env.effective_proxy("")
        self.assertEqual(proxy, healthy)

    async def test_range_ignored_after_probe_falls_back_to_a_clean_complete_stream(self):
        async def handle(request):
            if request.headers.get("Range") == "bytes=0-0":
                return self.range_response(request)
            return web.Response(body=DATA, headers={"ETag": '"fixture-v1"'})
        base = await self.server(handle)
        dest = self.root / "ignored.safetensors"
        await self.client(base).download_file(base, dest)
        self.assertEqual(dest.read_bytes(), DATA)

    async def test_rate_limit_retries_with_retry_after_and_finishes(self):
        attempts = 0
        async def handle(request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return web.Response(status=429, headers={"Retry-After": "0"})
            return self.range_response(request)
        base = await self.server(handle)
        dest = self.root / "limited.safetensors"
        await self.client(base).download_file(base, dest)
        self.assertEqual(dest.read_bytes(), DATA)
        self.assertEqual(attempts, 4)

    async def test_unavailable_source_has_bounded_retries(self):
        attempts = 0
        async def handle(request):
            nonlocal attempts
            attempts += 1
            return web.Response(status=503, headers={"Retry-After": "0"})
        base = await self.server(handle)
        with self.assertRaisesRegex(RuntimeError, "503"):
            await self.client(base).download_file(base, self.root / "unavailable.safetensors")
        self.assertEqual(attempts, 3)

    async def test_cancel_stops_all_workers_and_a_new_call_resumes(self):
        hold = asyncio.Event()
        started = asyncio.Event()
        blocked = True
        starts = []
        async def handle(request):
            value = request.headers.get("Range", "")
            starts.append(value)
            if value == "bytes=0-0" or not blocked:
                return self.range_response(request)
            start, end = map(int, value.removeprefix("bytes=").split("-"))
            response = web.StreamResponse(status=206, headers={"Content-Range": f"bytes {start}-{end}/{len(DATA)}", "Content-Length": str(end-start+1), "ETag": '"fixture-v1"'})
            await response.prepare(request)
            await response.write(DATA[start:start+1024])
            started.set()
            await hold.wait()
            return response
        base = await self.server(handle)
        dest = self.root / "cancel.safetensors"
        task = asyncio.create_task(self.client(base).download_file(base, dest))
        await started.wait()
        await asyncio.sleep(.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse([task for task in asyncio.all_tasks() if task.get_name().startswith("lora-transfer-")])
        blocked = False
        hold.set()
        starts.clear()
        await self.client(base).download_file(base, dest)
        self.assertEqual(dest.read_bytes(), DATA)
        self.assertTrue(any(value.startswith("bytes=1024-") for value in starts))

    async def test_redirect_validator_runs_before_the_destination_is_contacted(self):
        seen = []
        async def forbidden(request):
            seen.append(True)
            return self.range_response(request)
        target = await self.server(forbidden)
        async def handle(request): return web.HTTPFound(target)
        base = await self.server(handle)
        client = self.client(base, download_redirect_validator=lambda url: False)
        with self.assertRaisesRegex(RuntimeError, "安全校验"):
            await client.download_file(base, self.root / "unsafe.safetensors")
        self.assertEqual(seen, [])

    def test_unsupported_proxy_is_actionable_without_echoing_its_credentials(self):
        with self.assertRaisesRegex(ValueError, "HTTP/Mixed") as caught:
            CivitaiClient(proxy="socks5://fixture:private@localhost:1080")
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(net_env.proxy_display("http://fixture:private@localhost:7890?token=private"), "http://localhost:7890")

    def test_retry_after_accepts_http_dates_and_caps_waiting(self):
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime
        self.assertEqual(lora_transfer.retry_delay("120", 0), 20)
        future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=10))
        self.assertGreater(lora_transfer.retry_delay(future, 0), 8)

    def test_desktop_retry_can_ignore_a_stale_legacy_circuit(self):
        proxy = "http://localhost:12345"
        previous = CivitaiClient(proxy=proxy)
        for _ in range(3):
            previous._mark_proxy_failure(OSError("fixture failure"))
        with self.assertRaises(RuntimeError):
            previous._check_proxy_circuit()
        current = CivitaiClient(proxy=proxy, proxy_circuit_breaker=False)
        current._check_proxy_circuit()

    async def test_cancel_search_stops_its_source_requests(self):
        started, stopped = asyncio.Event(), asyncio.Event()
        class Config:
            def snapshot(self): return {}
            def get_secret(self, name): return ""
        class Client:
            async def search_loras(self, *args, **kwargs):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()
        service = LoraSearchService(config=Config(), data_root=self.root, client_factory=lambda **kwargs: Client())
        with mock.patch.object(net_env, "ensure_probed", new=mock.AsyncMock()):
            task = asyncio.create_task(service.search("fixture", sources=["civitai"]))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(stopped.is_set())

    async def test_http_proxy_resolves_the_source_instead_of_local_dns(self):
        async def proxy(request):
            if request.path.endswith("/api"):
                return web.json_response({"working": True})
            return self.range_response(request)
        base = await self.server(proxy)
        client = CivitaiClient(proxy=base, site_base="http://lora-source.invalid", split=2)
        self.assertTrue((await client._get_json("http://lora-source.invalid/api"))["working"])
        dest = self.root / "proxy.safetensors"
        await client.download_file("http://lora-source.invalid/model", dest)
        self.assertEqual(dest.read_bytes(), DATA)

    async def test_search_to_verified_install_with_real_http_client_and_local_fixture(self):
        import hashlib
        import struct
        from desktop_runtime.lora_download import LoraDownloadService
        from winefox_core.storage import Database, LoraAssetStore, LoraProfileStore
        header = json.dumps({"tensor": {"dtype": "F32", "shape": [512], "data_offsets": [0, 2048]}}).encode()
        payload = struct.pack("<Q", len(header)) + header + bytes(2048)
        digest = hashlib.sha256(payload).hexdigest()
        base = ""
        version = {"id": 20, "modelId": 10, "name": "fixture", "baseModel": "Illustrious", "trainedWords": ["fox_token"], "files": []}
        model = {"id": 10, "name": "Fox", "type": "LORA", "modelVersions": [version]}
        async def handle(request):
            if request.path == "/api/v1/models":
                return web.json_response({"items": [model], "metadata": {"totalItems": 1}})
            if request.path == "/api/v1/model-versions/20":
                return web.json_response(version)
            if request.path == "/api/v1/models/10":
                return web.json_response(model)
            if request.path == "/weight":
                headers = {"ETag": '"fixture-valid"'}
                value = request.headers.get("Range", "")
                if value:
                    first, last = map(int, value.removeprefix("bytes=").split("-"))
                    headers["Content-Range"] = f"bytes {first}-{last}/{len(payload)}"
                    return web.Response(status=206, body=payload[first:last+1], headers=headers)
                return web.Response(body=payload, headers=headers)
            return web.Response(status=404)
        base = await self.server(handle)
        version["files"] = [{"name": "fixture.safetensors", "primary": True, "type": "Model", "downloadUrl": base + "/weight", "hashes": {"SHA256": digest}}]
        install = self.root / "installed"
        class Config:
            def snapshot(self): return {"lora_download": {"mode": "local", "local_dir": str(install), "split": 2}}
            def get_secret(self, name): return ""
        def factory(**kwargs):
            client = CivitaiClient(site_base=base, **kwargs)
            client.search_loras_via_web = mock.AsyncMock(return_value=[])
            return client
        db = Database(":memory:")
        db.initialize()
        self.addCleanup(db.close)
        service = LoraDownloadService(db=db, config=Config(), data_root=self.root, connection_factory=lambda: None, client_factory=factory, search_service_factory=lambda **kwargs: LoraSearchService(client_factory=factory, **kwargs))
        with mock.patch.object(net_env, "ensure_probed", new=mock.AsyncMock()), mock.patch.object(net_env, "effective_proxy", return_value=("", "")), mock.patch("desktop_runtime.lora_download._require_trusted_download_url", side_effect=lambda url, source: url):
            results = await service.search("Fox", sources=["civitai"])
            self.assertTrue(results["items"])
            job = service.start_download(results["items"][0]["id"])
            await service._tasks[job["id"]]
        final = service.snapshot(job["id"])
        self.assertEqual(final["status"], "completed", final.get("error"))
        self.assertEqual((install / "fixture.safetensors").read_bytes(), payload)
        self.assertEqual(final["result"]["hash"], digest)
        self.assertEqual(len(LoraAssetStore(db).list()), 1)
        self.assertEqual(LoraProfileStore(db).get_family("illustrious")[0].profile["trigger_words"], ["fox_token"])

    async def test_search_request_recovers_from_one_connection_reset(self):
        calls = 0
        async def handle(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                request.transport.close()
                return web.Response()
            return web.json_response({"ok": True})
        base = await self.server(handle)
        class Config:
            def snapshot(self): return {}
            def get_secret(self, name): return ""
        service = LoraSearchService(config=Config(), data_root=self.root)
        kwargs = service._client_kwargs()
        kwargs.pop("split")
        kwargs["proxy"] = ""
        with mock.patch("civitai_api.random.uniform", return_value=0):
            result = await CivitaiClient(site_base=base, **kwargs)._get_json(base)
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(calls, 2)

    async def test_fast_source_results_are_returned_without_waiting_for_a_stalled_source(self):
        class Config:
            def snapshot(self): return {}
            def get_secret(self, name): return ""
        class Client:
            async def search_loras(self, *args, **kwargs):
                return [{"name": "Fox", "type": "LORA", "version_id": "1", "base_model": "Illustrious", "page_url": "https://civitai.com/models/1"}]
        class Slow:
            async def search_loras(self, *args, **kwargs):
                await asyncio.sleep(10)
                return []
        service = LoraSearchService(config=Config(), data_root=self.root, client_factory=lambda **kwargs: Client(), hf_client_factory=lambda **kwargs: Slow())
        service._per_source_search_timeout = lambda: 1
        started = time.monotonic()
        with mock.patch.object(net_env, "ensure_probed", new=mock.AsyncMock()), mock.patch("desktop_runtime.lora_search._SEARCH_RESULT_GRACE", .02):
            result = await service.search("Fox", sources=["civitai", "huggingface"])
        self.assertLess(time.monotonic()-started, .8)
        self.assertTrue(result["items"])

    async def test_canonical_rest_query_does_not_depend_on_slow_global_sorts(self):
        calls = []
        version = {"id": 2, "baseModel": "Illustrious", "trainedWords": ["fixture_token"]}
        async def handle(request):
            calls.append(dict(request.query))
            if "sort" in request.query:
                return web.Response(status=503, headers={"Retry-After": "0"})
            return web.json_response({"items": [{"id": 1, "name": "Fox", "type": "LORA", "modelVersions": [version]}]})
        base = await self.server(handle)
        client = self.client(base)
        client.api_base = base
        items = await client._search_loras_rest_api("Fox", limit=5, base_models=["Illustrious"])
        self.assertEqual(len(items), 1)
        self.assertEqual(len(calls), 1)

    async def test_weak_validator_cannot_mix_bytes_from_two_revisions(self):
        changed = DATA[::-1]
        broken = False
        async def handle(request):
            nonlocal broken
            headers = {"ETag": 'W/"same"', "Last-Modified": "Thu, 01 Jan 2026 00:00:00 GMT"}
            if request.headers.get("Range") == "bytes=0-0":
                return web.Response(status=206, body=DATA[:1], headers={**headers, "Content-Range": f"bytes 0-0/{len(DATA)}"})
            if not broken:
                broken = True
                result = web.StreamResponse(headers={**headers, "Content-Length": str(len(DATA))})
                await result.prepare(request)
                await result.write(DATA[:16384])
                request.transport.close()
                return result
            value = request.headers.get("Range")
            if value:
                first, last = map(int, value.removeprefix("bytes=").split("-"))
                return web.Response(status=206, body=changed[first:last+1], headers={**headers, "Content-Range": f"bytes {first}-{last}/{len(DATA)}"})
            return web.Response(body=changed, headers=headers)
        base = await self.server(handle)
        dest = self.root / "revision.safetensors"
        await self.client(base).download_file_single(base, dest)
        self.assertEqual(dest.read_bytes(), changed)


if __name__ == "__main__":
    unittest.main()

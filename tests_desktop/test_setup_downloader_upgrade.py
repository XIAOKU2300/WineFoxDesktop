# -*- coding: utf-8 -*-
"""环境中心下载器升级测试：多连接下载、断点续传、进度事件字段、
镜像测速与优选排序。

运行：``python -m unittest tests_desktop.test_setup_downloader_upgrade -v``
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import aiohttp
from aiohttp import web

from desktop_runtime.setup_errors import SetupErrorCode
from desktop_runtime.setup_executor import SetupExecutor, _MULTICONN_THRESHOLD
from desktop_runtime.setup_manager import SetupManager
from desktop_runtime.setup_manifest import ModelSpec, parse_manifest

from tests_desktop.test_setup_executor import make_safetensors, small_manifest  # noqa: E402


def big_manifest(base_url: str, *, total: int) -> SetupManifest:
    """生成一个大模型（超过多连接阈值）清单，服务端支持 Range。"""
    data = {
        "format_version": 1,
        "manifest_version": "test",
        "bridge": {
            "source": "comfyui_custom_nodes/ComfyUI-AstrBot-LoraBridge",
            "dest_folder": "ComfyUI-AstrBot-LoraBridge",
            "version": "1.4.2",
            "allow_local_requests_without_token": False,
            "allowed_hosts": ["127.0.0.1", "slow-mirror"],
            "config_defaults": {
                "token": "",
                "chunk_size_mb": 1,
                "connect_timeout": 30,
                "sock_read_timeout": 120,
                "fetch_timeout": 45,
                "max_concurrent_downloads": 1,
                "proxy_cooldown": 60,
                "proxy": "",
            },
        },
        "python": {"min_version": [3, 10], "probe_requires_pip": True, "candidate_priority": ["system_python"]},
        "directories": {"checkpoints": "models/checkpoints"},
        "model_groups": ["core"],
        "models": [
            {
                "id": "core.big",
                "name": "big.safetensors",
                "kind": "safetensors",
                "dest": "models/checkpoints/big.safetensors",
                "min_gb": 0.0,
                "sha256": "",
                "sources": [
                    {"host": "slow-mirror", "url": "http://slow-mirror/models/big.safetensors"},
                    {"host": "127.0.0.1", "url": base_url + "/models/big.safetensors"},
                ],
                "group": "core",
            }
        ],
        "node_groups": {},
        "nodes": [],
        "tiers": {"lite": [{"step": "models", "group": "core"}]},
        "manual_files": [],
    }
    manifest = parse_manifest(data)
    # 缩小阈值便于测试
    object.__setattr__(sys.modules["desktop_runtime.setup_executor"], "_MULTICONN_THRESHOLD", total)
    return manifest


class RangeServer:
    """支持 Range 分片的真实 HTTP 测试服务器（本地回环）。"""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.range_requests = 0
        self.full_requests = 0
        self.app = web.Application()
        self.app.router.add_get("/models/big.safetensors", self._serve)
        self.runner: web.AppRunner | None = None
        self.port = 0

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = self.runner.addresses[0][1]

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()

    async def _serve(self, request: web.Request) -> web.Response:
        rng = request.headers.get("Range", "")
        if not rng:
            self.full_requests += 1
            return web.Response(body=self.payload, content_type="application/octet-stream")
        self.range_requests += 1
        unit, _, spec = rng.partition("=")
        start_s, _, end_s = spec.partition("-")
        start = int(start_s or 0)
        end = int(end_s) if end_s else len(self.payload) - 1
        chunk = self.payload[start : end + 1]
        headers = {"Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{len(self.payload)}"}
        return web.Response(
            status=206,
            body=chunk,
            content_type="application/octet-stream",
            headers=headers,
        )


class DownloaderUpgradeTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiconn_download_and_progress_fields(self) -> None:
        payload = make_safetensors({"data": (0, 1024 * 1024)})  # 1MB 数据区，验证器认可
        total = len(payload)
        server = RangeServer(payload)
        await server.start()
        try:
            manifest = big_manifest(f"http://127.0.0.1:{server.port}", total=total)
            with tempfile.TemporaryDirectory() as tmp:
                work = Path(tmp)
                ex = SetupExecutor(manifest, work_dir=work / "w")
                job = ex.new_job(Path(tmp), "lite", _plan_steps(manifest), work_dir=work / "w")
                await ex.run_job(job)
                await ex.close()
                self.assertEqual(job.status, "success", job.error_message)
                target = Path(tmp) / "models/checkpoints/big.safetensors"
                self.assertTrue(target.is_file())
                self.assertEqual(target.stat().st_size, total)
                self.assertEqual(target.read_bytes(), payload)
                # 多连接证据：Range 请求数 > 1
                self.assertGreater(server.range_requests, 1)
                # 进度事件字段完整
                progress = [e for e in job.events if e["type"] == "item_progress"]
                self.assertTrue(progress)
                last = progress[-1]
                for key in ("done", "total", "speed_bps", "percent"):
                    self.assertIn(key, last)
                self.assertEqual(last["total"], total)
                self.assertEqual(last["percent"], 100.0)
                # 完成后 .prog 清理
                self.assertFalse(target.with_name(target.name + ".part.prog").exists())
        finally:
            await server.stop()

    async def test_multiconn_resume_from_prog(self) -> None:
        payload = make_safetensors({"data": (0, 512 * 1024)})
        total = len(payload)
        server = RangeServer(payload)
        await server.start()
        try:
            manifest = big_manifest(f"http://127.0.0.1:{server.port}", total=total)
            with tempfile.TemporaryDirectory() as tmp:
                work = Path(tmp)
                ex = SetupExecutor(manifest, work_dir=work / "w")
                target_root = Path(tmp)
                part = target_root / "models/checkpoints/big.safetensors.part"
                part.parent.mkdir(parents=True, exist_ok=True)
                prog = part.with_name(part.name + ".prog")
                prefix = 64 * 1024
                # 真实场景：前缀来自先前完成的单连接阶段，需要真实写入。
                part.write_bytes(payload[:prefix])
                # 预置：前缀 64KB 已完成 + 两片各下了一部分
                mid = prefix + (total - prefix) // 2
                done = (total - prefix) // 4
                prog.write_text(
                    json.dumps(
                        {
                            "total": total,
                            "prefix": prefix,
                            "slices": [[prefix, mid - 1], [mid, total - 1]],
                            "done": [done, done],
                        }
                    ),
                    encoding="utf-8",
                )
                job = ex.new_job(target_root, "lite", _plan_steps(manifest), work_dir=work / "w")
                await ex.run_job(job)
                await ex.close()
                self.assertEqual(job.status, "success", job.error_message)
                target = target_root / "models/checkpoints/big.safetensors"
                self.assertEqual(target.read_bytes(), payload)
        finally:
            await server.stop()

    async def test_mirror_probe_and_preferred_order(self) -> None:
        payload = b"x" * 4096
        server = RangeServer(payload)
        await server.start()
        try:
            manifest = big_manifest(f"http://127.0.0.1:{server.port}", total=1024)
            object.__setattr__(
                sys.modules["desktop_runtime.setup_manager"],
                "_MIRROR_SAMPLE_BYTES",
                2048,
            )
            with tempfile.TemporaryDirectory() as tmp:
                manager = SetupManager(manifest=manifest, work_dir=Path(tmp) / "w")
                report = await manager.probe_mirrors()
                hosts = {item["host"] for item in report["items"]}
                self.assertIn("127.0.0.1", hosts)
                ok_item = next(item for item in report["items"] if item["host"] == "127.0.0.1")
                self.assertTrue(ok_item["ok"], ok_item)
                self.assertGreaterEqual(ok_item["sample_bytes"], 2048)

                # 优选 127.0.0.1：来源排序应把它排到 slow-mirror 之前
                manager.set_preferred_host("127.0.0.1")
                self.assertTrue(Path(tmp, "setup_prefs.json").is_file())
                model = manifest.models["core.big"]
                ordered = manager._executor._ordered_sources(model)
                self.assertEqual(ordered[0].host, "127.0.0.1")

                # 空串恢复自动
                manager.set_preferred_host("")
                self.assertEqual(manager._executor._ordered_sources(model)[0].host, "slow-mirror")

                # 非法主机被拒绝
                with self.assertRaises(Exception):
                    manager.set_preferred_host("evil.example.com")
        finally:
            await server.stop()


def _plan_steps(manifest: SetupManifest) -> list:
    from desktop_runtime.setup_service import PlanItem, PlanStep

    model = manifest.models["core.big"]
    step = PlanStep(
        kind="models",
        group="core",
        label="下载模型组 core",
        items=[PlanItem(kind="model", spec=model)],
    )
    return [step]


if __name__ == "__main__":
    unittest.main()

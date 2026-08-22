# -*- coding: utf-8 -*-
"""从零装机（bootstrap）测试：manifest runtime 段、计划生成、安装目标校验、
ComfyUI 本体安装步骤。

运行：``python -m unittest tests_desktop.test_setup_bootstrap -v``
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aiohttp import web

from desktop_runtime.setup_errors import SetupError
from desktop_runtime.setup_executor import JobStatus, SetupExecutor
from desktop_runtime.setup_manager import SetupManager
from desktop_runtime.setup_manifest import load_setup_manifest
from desktop_runtime.setup_paths import validate_bootstrap_root
from desktop_runtime.setup_service import Planner, EnvironmentStatus


def comfyui_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ComfyUI-master/main.py", "print('comfyui')\n")
        zf.writestr("ComfyUI-master/requirements.txt", "torch\n")
        zf.writestr("ComfyUI-master/models/.keep", "")
        zf.writestr("ComfyUI-master/custom_nodes/.keep", "")
    return buf.getvalue()


class FakeComfyUIServer:
    def __init__(self) -> None:
        self.app = web.Application()
        self.app.router.add_get("/comfyui.zip", self._zip)
        self.runner = None
        self.port = 0

    async def _zip(self, request: web.Request) -> web.Response:
        return web.Response(body=comfyui_zip(), content_type="application/zip")

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = self.runner.addresses[0][1]

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()


def bootstrap_manifest(base_url: str):
    """真实清单 + 把 ComfyUI 源指向本地测试服务器。"""
    import dataclasses

    manifest = load_setup_manifest()
    runtime = manifest.runtime
    new_runtime = dataclasses.replace(
        runtime,
        comfyui_sources=(type(runtime.comfyui_sources[0])(host="127.0.0.1", url=f"{base_url}/comfyui.zip"),),
        torch_packages=(),  # 测试不装 torch
        pip_index_urls=(),
    )
    bridge = dataclasses.replace(manifest.bridge, allowed_hosts=(*manifest.bridge.allowed_hosts, "127.0.0.1"))
    return dataclasses.replace(manifest, runtime=new_runtime, bridge=bridge)


class BootstrapManifestTests(unittest.TestCase):
    def test_runtime_section_parsed(self) -> None:
        manifest = load_setup_manifest()
        runtime = manifest.runtime
        self.assertIsNotNone(runtime)
        self.assertEqual(runtime.python_embed.version, "3.11.9")
        self.assertTrue(runtime.comfyui_sources)
        self.assertIn("https://pypi.tuna.tsinghua.edu.cn/simple", runtime.pip_index_urls)
        self.assertIn("12.8", runtime.torch_index_by_cuda)
        self.assertIn("bootstrap", manifest.tiers)
        kinds = [s.step for s in manifest.tiers["bootstrap"]]
        self.assertEqual(
            kinds[:3], ["comfyui", "python_runtime", "requirements"]
        )

    def test_planner_bootstrap_steps(self) -> None:
        manifest = load_setup_manifest()
        planner = Planner(manifest)
        status = EnvironmentStatus()
        plan = planner.plan_for_profile(status, "bootstrap")
        labels = [s.label for s in plan.steps]
        self.assertTrue(any("ComfyUI 本体" in label for label in labels))
        self.assertTrue(any("Python 运行时" in label for label in labels))
        self.assertTrue(any("PyTorch" in label for label in labels))


class BootstrapRootValidationTests(unittest.TestCase):
    def test_empty_dir_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "ComfyUI"
            resolved = validate_bootstrap_root(target)
            self.assertEqual(resolved, target.resolve())

    def test_nonempty_invalid_root_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "ComfyUI"
            target.mkdir()
            (target / "userfile.txt").write_text("x", encoding="utf-8")
            with self.assertRaises(SetupError):
                validate_bootstrap_root(target)

    def test_drive_root_rejected(self) -> None:
        with self.assertRaises(SetupError):
            validate_bootstrap_root("C:\\")

    def test_valid_existing_root_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "ComfyUI"
            (target / "models").mkdir(parents=True)
            (target / "custom_nodes").mkdir()
            (target / "main.py").write_text("print(1)", encoding="utf-8")
            resolved = validate_bootstrap_root(target)
            self.assertEqual(resolved, target.resolve())


class BootstrpExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_comfyui_step_installs_zip(self) -> None:
        server = FakeComfyUIServer()
        await server.start()
        try:
            manifest = bootstrap_manifest(f"http://127.0.0.1:{server.port}")
            with tempfile.TemporaryDirectory() as tmp:
                work = Path(tmp)
                root = work / "ComfyUI"
                root.mkdir()
                ex = SetupExecutor(manifest, work_dir=work / "w")
                from desktop_runtime.setup_service import PlanStep

                steps = [PlanStep(kind="comfyui", group=None, label="获取 ComfyUI 本体")]
                job = ex.new_job(root, "bootstrap", steps, work_dir=work / "w")
                await ex.run_job(job)
                await ex.close()
                self.assertEqual(job.status, JobStatus.SUCCESS, job.error_message)
                self.assertTrue((root / "main.py").is_file())
                self.assertTrue((root / "requirements.txt").is_file())
                events = [e["type"] for e in job.events]
                self.assertIn("runtime_installed", events)
        finally:
            await server.stop()

    async def test_comfyui_step_skips_existing(self) -> None:
        manifest = load_setup_manifest()
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            root = work / "ComfyUI"
            (root / "models").mkdir(parents=True)
            (root / "main.py").write_text("print(1)", encoding="utf-8")
            ex = SetupExecutor(manifest, work_dir=work / "w")
            from desktop_runtime.setup_service import PlanStep

            job = ex.new_job(root, "bootstrap", [PlanStep(kind="comfyui", group=None, label="x")], work_dir=work / "w")
            await ex.run_job(job)
            await ex.close()
            self.assertEqual(job.status, JobStatus.SUCCESS)
            self.assertIn("runtime_skipped", [e["type"] for e in job.events])

    async def test_comfyui_step_rejects_nonempty(self) -> None:
        manifest = load_setup_manifest()
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            root = work / "ComfyUI"
            root.mkdir()
            (root / "other.txt").write_text("x", encoding="utf-8")
            ex = SetupExecutor(manifest, work_dir=work / "w")
            from desktop_runtime.setup_service import PlanStep

            job = ex.new_job(root, "bootstrap", [PlanStep(kind="comfyui", group=None, label="x")], work_dir=work / "w")
            await ex.run_job(job)
            await ex.close()
            self.assertEqual(job.status, JobStatus.FAILED)
            self.assertTrue(any("非空" in (e.get("message") or "") for e in job.events if e["type"] == "step_failed"))

    async def test_manager_bootstrap_plan_requires_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = SetupManager(work_dir=Path(tmp) / "w")
            with self.assertRaises(SetupError):
                await manager.plan(profile="bootstrap", root=None)

    async def test_manager_bootstrap_plan_with_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "ComfyUI"
            manager = SetupManager(work_dir=Path(tmp) / "w")
            plan = await manager.plan(profile="bootstrap", only_missing=False, root=target)
            kinds = [s["kind"] for s in plan["steps"]]
            self.assertEqual(kinds[:3], ["comfyui", "python_runtime", "requirements"])
            self.assertGreater(plan["estimated_bytes"], 0)

    def test_enable_embed_site(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            pth = target / "python311._pth"
            pth.write_text("python311.zip\n.\n#import site\n", encoding="utf-8")
            SetupExecutor._enable_embed_site(target, "3.11.9")
            content = pth.read_text(encoding="utf-8")
            self.assertIn("import site", content)
            self.assertNotIn("#import site", content)


if __name__ == "__main__":
    unittest.main()

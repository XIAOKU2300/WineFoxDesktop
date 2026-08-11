from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

import aiohttp
from aiohttp import web

from desktop_runtime.setup_errors import SetupErrorCode
from desktop_runtime.setup_executor import JobStatus, SetupExecutor, StepState
from desktop_runtime.setup_manifest import ModelSource, ModelSpec, SetupManifest, parse_manifest
from desktop_runtime.setup_service import PlanItem, PlanStep


def make_safetensors(tensors: dict[str, tuple[int, int]]) -> bytes:
    header: dict[str, object] = {"__metadata__": {}}
    for name, (start, end) in tensors.items():
        header[name] = {"dtype": "F32", "shape": [end - start], "data_offsets": [start, end]}
    header_bytes = json.dumps(header).encode("utf-8")
    data_len = max((end for _, (_, end) in tensors.items()), default=0)
    return struct.pack("<Q", len(header_bytes)) + header_bytes + b"\x00" * data_len


_BRIDGE_DEFAULTS = {
    "token": "",
    "chunk_size_mb": 1,
    "connect_timeout": 30,
    "sock_read_timeout": 120,
    "fetch_timeout": 45,
    "max_concurrent_downloads": 1,
    "proxy_cooldown": 60,
    "proxy": "",
}


def small_manifest(base_url: str, *, allow_host: bool = True, model_url: str | None = None) -> SetupManifest:
    data = {
        "format_version": 1,
        "manifest_version": "test",
        "bridge": {
            "source": "comfyui_custom_nodes/ComfyUI-AstrBot-LoraBridge",
            "dest_folder": "ComfyUI-AstrBot-LoraBridge",
            "version": "1.4.1",
            "allow_local_requests_without_token": False,
            "allowed_hosts": ["127.0.0.1"] if allow_host else ["huggingface.co"],
            "config_defaults": dict(_BRIDGE_DEFAULTS),
        },
        "python": {"min_version": [3, 10], "probe_requires_pip": True, "candidate_priority": ["paths:root/python/python.exe"]},
        "directories": {"checkpoints": "models/checkpoints"},
        "model_groups": ["core"],
        "models": [
            {
                "id": "core.tiny",
                "name": "tiny.safetensors",
                "kind": "safetensors",
                "dest": "models/checkpoints/tiny.safetensors",
                "min_gb": 0.0,
                "sha256": "",
                "sources": [{"host": "127.0.0.1", "url": model_url or (base_url + "/models/tiny.safetensors")}],
                "group": "core",
            }
        ],
        "node_groups": {"minimal": {"nodes": ["minimal.demo"], "pip_packages": []}},
        "nodes": [
            {
                "id": "minimal.demo",
                "name": "ComfyUI-Demo",
                "repo": "demo/repo",
                "ref": "a" * 40,
                "ref_kind": "commit",
                "install_requirements": False,
            }
        ],
        "tiers": {"lite": [{"step": "nodes", "group": "minimal"}, {"step": "models", "group": "core"}]},
        "manual_files": [],
    }
    return parse_manifest(data)


def demo_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ComfyUI-Demo/__init__.py", "NODE_CLASS_MAPPINGS = {}\n")
    return buf.getvalue()


class FakeServer:
    def __init__(self, model_bytes: bytes) -> None:
        self.model_bytes = model_bytes
        self.app = web.Application()
        self.app.router.add_get("/models/tiny.safetensors", self._model)
        self.app.router.add_get("/nodes/demo.zip", self._zip)
        self.app.router.add_get("/redirect", self._redirect)
        self.app.router.add_get("/slow", self._slow)

    async def _model(self, request: web.Request) -> web.Response:
        return web.Response(body=self.model_bytes, content_type="application/octet-stream")

    async def _zip(self, request: web.Request) -> web.Response:
        return web.Response(body=demo_zip(), content_type="application/octet-stream")

    async def _redirect(self, request: web.Request) -> web.Response:
        return web.Response(status=302, headers={"Location": "http://evil.example.com/x"})

    async def _slow(self, request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "application/octet-stream"})
        await resp.prepare(request)
        try:
            for _ in range(20):
                await resp.write(b"\x00" * 1024)
                await asyncio.sleep(0.05)
        except (ConnectionResetError, aiohttp.ClientConnectionResetError):
            pass  # client cancelled mid-stream; expected in the cancel test
        return resp


class InMemorySecrets:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def get_secret(self, name: str) -> str | None:
        return self._data.get(name)

    async def put_secret(self, name: str, value: str) -> None:
        self._data[name] = value


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.server = FakeServer(make_safetensors({"a": (0, 4)}))
        self.runner = web.AppRunner(self.server.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        self.port = self.site._server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "ComfyUI"
        (self.root / "custom_nodes").mkdir(parents=True)
        self.work = Path(self.tmp.name) / "work"
        self.session = aiohttp.ClientSession()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.site.stop()
        await self.runner.cleanup()
        self.tmp.cleanup()

    def _executor(self, manifest: SetupManifest | None = None, **kwargs) -> SetupExecutor:
        kwargs.setdefault("url_rewriter", lambda u: u)
        return SetupExecutor(
            manifest or small_manifest(self.base_url),
            work_dir=self.work,
            secret_store=InMemorySecrets(),
            session=self.session,
            **kwargs,
        )

    def _job(self, executor: SetupExecutor, step_kinds: list[str]) -> object:
        manifest = executor.manifest
        plan_steps: list[PlanStep] = []
        for kind in step_kinds:
            if kind == "models":
                plan_steps.append(
                    PlanStep(kind="models", group="core", label="models", items=[PlanItem(kind="model", spec=manifest.models["core.tiny"])])
                )
            elif kind == "nodes":
                plan_steps.append(
                    PlanStep(kind="nodes", group="minimal", label="nodes", items=[PlanItem(kind="node", spec=manifest.nodes["minimal.demo"])])
                )
        return executor.new_job(self.root, "lite", plan_steps, work_dir=self.work)

    async def test_model_download_verifies_and_renames(self) -> None:
        ex = self._executor()
        job = self._job(ex, ["models"])
        await ex.run_job(job)
        self.assertEqual(job.status, JobStatus.SUCCESS)
        target = self.root / "models" / "checkpoints" / "tiny.safetensors"
        self.assertTrue(target.is_file())
        self.assertFalse(target.with_name("tiny.safetensors.part").exists())
        self.assertEqual(target.read_bytes(), make_safetensors({"a": (0, 4)}))

    async def test_node_installed(self) -> None:
        # NodeSpec.archive_url always points at github.com; the test-only
        # url_rewriter maps it onto the local FakeServer.
        def rewriter(url: str) -> str:
            if url.startswith("https://github.com/"):
                return f"{self.base_url}/nodes/demo.zip"
            return url

        ex = self._executor(url_rewriter=rewriter)
        job = self._job(ex, ["nodes"])
        await ex.run_job(job)
        self.assertEqual(job.status, JobStatus.SUCCESS)
        self.assertTrue((self.root / "custom_nodes" / "ComfyUI-Demo" / "__init__.py").is_file())

    async def test_rollback_restores_overwritten_file(self) -> None:
        target = self.root / "models" / "checkpoints" / "tiny.safetensors"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"ORIGINAL")
        ex = self._executor()
        job = self._job(ex, ["models"])
        await ex.run_job(job)
        self.assertTrue(target.is_file())
        self.assertNotEqual(target.read_bytes(), b"ORIGINAL")
        self.assertGreater(len(job.rollback_entries), 0)
        await ex.rollback_job(job)
        self.assertEqual(job.status, JobStatus.ROLLED_BACK)
        self.assertEqual(target.read_bytes(), b"ORIGINAL")

    async def test_rollback_deletes_newly_created(self) -> None:
        ex = self._executor()
        job = self._job(ex, ["models"])
        await ex.run_job(job)
        target = self.root / "models" / "checkpoints" / "tiny.safetensors"
        self.assertTrue(target.exists())
        await ex.rollback_job(job)
        self.assertFalse(target.exists())

    async def test_untrusted_redirect_rejected(self) -> None:
        manifest = small_manifest(self.base_url, model_url=f"{self.base_url}/redirect")
        ex = self._executor(manifest)
        job = self._job(ex, ["models"])
        await ex.run_job(job)
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual(job.error_code, SetupErrorCode.SOURCE_UNTRUSTED)

    async def test_host_not_allowlisted_skipped(self) -> None:
        # parse_manifest enforces host-in-allowlist at parse time; build the
        # dataclass directly to exercise the executor's defense-in-depth skip.
        m = small_manifest(self.base_url)
        model = m.models["core.tiny"]
        bad = ModelSpec(
            id=model.id,
            name=model.name,
            kind=model.kind,
            dest=model.dest,
            min_gb=model.min_gb,
            sha256=model.sha256,
            sources=(ModelSource(host="huggingface.co", url=f"{self.base_url}/models/tiny.safetensors"),),
            group=model.group,
        )
        manifest = dataclasses.replace(m, models={**m.models, "core.tiny": bad})
        ex = self._executor(manifest)
        job = self._job(ex, ["models"])
        await ex.run_job(job)
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual(job.error_code, SetupErrorCode.DOWNLOAD_FAILED)

    async def test_cancel_stops_download(self) -> None:
        manifest = small_manifest(self.base_url, model_url=f"{self.base_url}/slow")
        ex = self._executor(manifest)
        job = self._job(ex, ["models"])
        task = asyncio.create_task(ex.run_job(job))
        await asyncio.sleep(0.15)
        await ex.cancel_job(job)
        await asyncio.wait_for(task, 5)
        self.assertIn(job.status, (JobStatus.CANCELLED, JobStatus.FAILED))
        target = self.root / "models" / "checkpoints" / "tiny.safetensors"
        self.assertFalse(target.is_file(), "cancelled download must not land the final file")


if __name__ == "__main__":
    unittest.main()

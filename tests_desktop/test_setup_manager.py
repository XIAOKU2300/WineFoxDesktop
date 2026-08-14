from __future__ import annotations

import asyncio
import json
import socket
import tempfile
import unittest
from pathlib import Path

from desktop_runtime.setup_executor import JobStatus
from desktop_runtime.setup_manifest import SetupManifest, parse_manifest
from desktop_runtime.setup_manager import SetupManager

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


def build_manifest(base_url: str = "http://127.0.0.1:1") -> SetupManifest:
    data = {
        "format_version": 1,
        "manifest_version": "test",
        "bridge": {
            "source": "comfyui_custom_nodes/ComfyUI-AstrBot-LoraBridge",
            "dest_folder": "ComfyUI-AstrBot-LoraBridge",
            "version": "1.4.2",
            "allow_local_requests_without_token": False,
            "allowed_hosts": ["127.0.0.1"],
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
                "sources": [{"host": "127.0.0.1", "url": base_url + "/tiny.safetensors"}],
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
        "tiers": {"lite": [{"step": "nodes", "group": "minimal"}, {"step": "models", "group": "core"}, {"step": "bridge"}]},
        "manual_files": [
            {"name": "manual-a.safetensors", "dest": "models/loras/manual-a.safetensors", "note": "手动放置"}
        ],
    }
    return parse_manifest(data)


def make_comfyui_root(base: Path) -> Path:
    root = base / "ComfyUI"
    (root / "models" / "checkpoints").mkdir(parents=True)
    (root / "models" / "loras").mkdir(parents=True)
    (root / "custom_nodes").mkdir(parents=True)
    (root / "main.py").write_text("pass\n", encoding="utf-8")
    return root


def closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _collect_strings(obj) -> list[str]:
    out: list[str] = []
    if isinstance(obj, dict):
        for value in obj.values():
            out.extend(_collect_strings(value))
    elif isinstance(obj, list):
        for item in obj:
            out.extend(_collect_strings(item))
    elif isinstance(obj, str):
        out.append(obj)
    return out


class SetupManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = make_comfyui_root(self.base)
        self.manifest = build_manifest(base_url=f"http://127.0.0.1:{closed_port()}")
        self.install_dir = self.base / "WineFox"
        self.install_dir.mkdir()
        self.work = self.base / "work"
        self.manager = SetupManager(
            manifest=self.manifest,
            install_dir=self.install_dir,
            work_dir=self.work,
        )

    async def asyncTearDown(self) -> None:
        await self.manager.close()
        self.tmp.cleanup()

    async def test_summary_redacts_absolute_paths(self) -> None:
        summary = await self.manager.summary()
        blob = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn(str(self.base), blob)
        self.assertNotIn(str(self.root), blob)
        self.assertEqual(summary["state_code"], "repair_needed")  # 节点缺失
        root = summary["root"]
        self.assertEqual(root["name"], "ComfyUI")
        self.assertNotIn("\\", root["display"])  # display 是 posix 风格的相对显示
        for value in _collect_strings(summary):
            # 绝对路径（盘符/UNC/根起始）绝不允许出现在 API 输出
            self.assertFalse(value.startswith(("C:", "D:", "\\\\", "/")), value)

    async def test_scan_finds_root_and_manual(self) -> None:
        data = await self.manager.scan(root=self.root)
        self.assertEqual(data["state_code"], "repair_needed")
        self.assertIsNotNone(data["root"])
        self.assertEqual(data["root"]["name"], "ComfyUI")
        manual = {m["name"]: m["present"] for m in data["manual"]}
        self.assertFalse(manual["manual-a.safetensors"])

    async def test_scan_marks_manual_present(self) -> None:
        (self.root / "models" / "loras" / "manual-a.safetensors").write_bytes(b"x" * 10)
        data = await self.manager.scan(root=self.root, force=True)
        manual = {m["name"]: m["present"] for m in data["manual"]}
        self.assertTrue(manual["manual-a.safetensors"])

    async def test_summary_includes_disk(self) -> None:
        data = await self.manager.scan(root=self.root)
        self.assertIn("disk", data)
        self.assertTrue(data["disk"]["available"])

    async def test_capabilities_from_manifest(self) -> None:
        manifest = self.manager.manifest
        self.assertIn("lite", manifest.tiers)
        self.assertEqual(manifest.bridge.version, "1.4.2")
        self.assertEqual(manifest.manual_files[0].name, "manual-a.safetensors")
        self.assertIn("core.tiny", [m.id for m in manifest.models.values()])

    async def test_plan_profile_marks_manual_required(self) -> None:
        plan = await self.manager.plan(profile="lite")
        self.assertTrue(plan["manual_required"])
        self.assertGreater(plan["estimated_bytes"], 0)
        kinds = [s["kind"] for s in plan["steps"]]
        self.assertEqual(kinds, ["nodes", "models", "bridge"])

    async def test_plan_repair(self) -> None:
        plan = await self.manager.plan(repair=True)
        self.assertIn("bridge", [s["kind"] for s in plan["steps"]])

    async def test_start_job_fails_on_unreachable_model_then_retry_rollback(self) -> None:
        # 只规划 models 一步：模型 URL 指向已关闭端口，下载必然失败且与网络环境无关
        plan = await self.manager.plan(custom=[("models", "core")], only_missing=False)
        result = await self.manager.start_job(root=self.root, plan_dict=plan)
        job_id = result["id"]
        job = self.manager.get_job(job_id)
        self.assertIsNotNone(job)
        # wait for the terminal state (download to a closed port fails fast)
        await self._wait_terminal(job)
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual(job.error_code, "SETUP_NETWORK_UNREACHABLE")
        # retry spawns a new job
        retried = await self.manager.retry_job(job_id)
        self.assertNotEqual(retried["id"], job_id)
        retried_job = self.manager.get_job(retried["id"])
        self.assertIsNotNone(retried_job)
        await self._wait_terminal(retried_job)  # 让重试任务也到达终态，避免悬挂任务
        # rollback of the original is a no-op (nothing landed) but succeeds
        ok = await self.manager.rollback_job(job_id)
        self.assertTrue(ok)

    async def _wait_terminal(self, job) -> None:
        for _ in range(50):
            if job.status in (JobStatus.SUCCESS, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.ROLLED_BACK):
                return
            await asyncio.sleep(0.05)

    async def test_rollback_restores_snapshot(self) -> None:
        # 直接对目标打快照，再覆盖，再回滚，验证恢复
        target = self.root / "models" / "checkpoints" / "existing.safetensors"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"ORIGINAL")
        job = self.manager._executor.new_job(self.root, "custom", [], work_dir=self.work)
        self.manager._executor._snapshot(job, target)
        target.write_bytes(b"NEW")
        self.manager._jobs[job.id] = job
        ok = await self.manager.rollback_job(job.id)
        self.assertTrue(ok)
        self.assertEqual(target.read_bytes(), b"ORIGINAL")

    async def test_reprobe_calls_engine_refresh(self) -> None:
        class FakeEngine:
            def __init__(self) -> None:
                self.refreshed = False

            async def probe_lora_bridge(self) -> dict:
                return {"ok": True, "installed": True}

            async def refresh_after_env_setup(self) -> dict:
                self.refreshed = True
                return {"ok": True}

        engine = FakeEngine()
        result = await self.manager.reprobe(engine)
        self.assertTrue(engine.refreshed)
        self.assertTrue(result["bridge"]["installed"])

    async def test_remote_kit_has_no_credentials(self) -> None:
        kit = await self.manager.remote_kit()
        self.assertFalse(kit["configured"])
        blob = json.dumps(kit, ensure_ascii=False).lower()
        self.assertNotIn("token", blob)
        self.assertEqual(kit["bridge_version"], "1.4.2")
        self.assertGreater(len(kit["manual_files"]), 0)

    async def test_start_job_rejects_root_in_install_dir(self) -> None:
        nested = self.install_dir / "ComfyUI"
        (nested / "models").mkdir(parents=True)
        (nested / "main.py").write_text("", encoding="utf-8")
        plan = await self.manager.plan(profile="lite")
        with self.assertRaises(Exception) as ctx:
            await self.manager.start_job(root=nested, plan_dict=plan)
        self.assertEqual(getattr(ctx.exception, "code", None), "SETUP_ROOT_READ_ONLY")


if __name__ == "__main__":
    unittest.main()

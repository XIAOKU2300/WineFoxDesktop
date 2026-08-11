"""Coordinator for the environment center: owns the manifest, scanner,
planner, executor and the live job table, and exposes the operations the
backend handlers need.

The manager is the single place that decides which root is active, caches
scans, estimates disk requirements and tracks jobs.  Every synchronous
filesystem call is wrapped in ``asyncio.to_thread`` so the aiohttp event
loop never blocks.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path
from typing import Any

from .setup_errors import SetupError, SetupErrorCode
from .setup_executor import JOB_CANCELLED, JOB_FAILED, SetupExecutor, SetupJob
from .setup_manifest import SetupManifest, load_setup_manifest
from .setup_paths import validate_comfyui_root
from .setup_service import EnvironmentStatus, Plan, PlanItem, PlanStep, Planner, Scanner

_GB = 1024 ** 3
_SCAN_TTL = 5.0
_TERMINAL_STATUSES = ("success", "failed", "cancelled", "rolled_back", "rolling_back")
_NODE_ZIP_ESTIMATE = 5 * 1024 * 1024


class SetupManager:
    """Coordination layer shared by the setup API handlers."""

    def __init__(
        self,
        *,
        manifest: SetupManifest | None = None,
        install_dir: Path | None = None,
        work_dir: Path | None = None,
        secret_store: Any = None,
        session: Any = None,
        url_rewriter: Any = None,
        scan_ttl: float = _SCAN_TTL,
    ) -> None:
        self.manifest = manifest or load_setup_manifest()
        self.install_dir = install_dir
        self.work_dir = Path(work_dir) if work_dir else None
        self.secret_store = secret_store
        self.scan_ttl = scan_ttl
        self._url_rewriter = url_rewriter
        self._owns_session = session is None
        self._session = session
        self._scanner = Scanner(self.manifest, install_dir=install_dir)
        self._planner = Planner(self.manifest)
        self._executor = SetupExecutor(
            self.manifest,
            install_dir=install_dir,
            work_dir=self.work_dir,
            secret_store=secret_store,
            session=session,
            url_rewriter=url_rewriter,
        )
        self._jobs: dict[str, SetupJob] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._job_lock = asyncio.Lock()
        self._scan_cache: EnvironmentStatus | None = None
        self._scan_cache_at = 0.0

    async def close(self) -> None:
        # 真正的会话由 executor 懒创建持有；manager 自持的 _session 仅为注入透传
        await self._executor.close()

    # ------------------------------------------------------------- scanning

    async def _scan(self, *, force: bool = False, root: Path | None = None) -> EnvironmentStatus:
        if root is not None:
            resolved = await asyncio.to_thread(validate_comfyui_root, root, install_dir=self.install_dir)
            status = await asyncio.to_thread(self._scanner.scan, injected_roots=[resolved])
            self._cache_scan(status)
            return status
        if self._scan_cache is not None and not force:
            if time.monotonic() - self._scan_cache_at < self.scan_ttl:
                return self._scan_cache
        status = await asyncio.to_thread(self._scanner.scan)
        self._cache_scan(status)
        return status

    def _cache_scan(self, status: EnvironmentStatus) -> None:
        self._scan_cache = status
        self._scan_cache_at = time.monotonic()

    async def scan(self, *, root: Path | None = None, force: bool = False) -> dict[str, Any]:
        status = await self._scan(force=force, root=root)
        data = status.to_dict()
        data["disk"] = await self._disk_usage(status.root)
        return data

    async def summary(self, *, force_scan: bool = False) -> dict[str, Any]:
        status = await self._scan(force=force_scan)
        data = status.to_dict()
        data["disk"] = await self._disk_usage(status.root)
        data["capabilities_version"] = self.manifest.manifest_version
        return data

    # ------------------------------------------------------------- plans

    def _plan_missing_manual(self, status: EnvironmentStatus) -> list[dict[str, str]]:
        return [
            {"name": mf.name, "dest": mf.dest, "note": mf.note}
            for mf in self.manifest.manual_files
            if not any(m.present for m in status.manual if m.name == mf.name)
        ]

    async def plan(
        self,
        *,
        profile: str | None = None,
        custom: list[tuple[str, str | None]] | None = None,
        repair: bool = False,
        retry_job_id: str | None = None,
        only_missing: bool = True,
    ) -> dict[str, Any]:
        status = await self._scan()
        if retry_job_id:
            base_job = self._jobs.get(retry_job_id)
            if base_job is None:
                raise SetupError(SetupErrorCode.JOB_NOT_FOUND, f"任务不存在: {retry_job_id}")
            failed = [s.index for s in base_job.steps if s.status == "failed"]
            base = Plan(root=status.root, profile=base_job.profile, steps=base_job.plan_steps)
            plan = self._planner.plan_for_retry(status, failed, base)
        elif repair:
            plan = self._planner.plan_for_repair(status)
        elif profile:
            plan = self._planner.plan_for_profile(status, profile, only_missing=only_missing)
        elif custom is not None:
            plan = self._planner.plan_for_custom(status, custom, only_missing=only_missing)
        else:
            raise SetupError(SetupErrorCode.CONFIG_INVALID, "缺少 plan 参数 (profile/custom/repair)")
        data = plan.to_dict()
        data["estimated_bytes"] = await self._estimate_plan_bytes(plan)
        data["manual_files"] = self._plan_missing_manual(status)
        data["manual_required"] = bool(data["manual_files"])
        return data

    async def _estimate_plan_bytes(self, plan: Plan) -> int:
        total = 0
        for step in plan.steps:
            if step.kind == "models":
                for item in step.items:
                    spec = item.spec  # type: ignore[union-attr]
                    total += int(max(getattr(spec, "min_gb", 0.0), 0.05) * _GB)
            elif step.kind == "nodes":
                total += _NODE_ZIP_ESTIMATE * len(step.items)
        return total

    # ------------------------------------------------------------- jobs

    async def start_job(self, *, root: Path | None, plan_dict: dict[str, Any]) -> dict[str, Any]:
        status = await self._scan()
        chosen = status.root if root is None else None
        if root is not None:
            chosen = await asyncio.to_thread(validate_comfyui_root, root, install_dir=self.install_dir)
        if chosen is None:
            raise SetupError(SetupErrorCode.ROOT_INVALID, "未指定 ComfyUI 根目录")
        plan = self._reconstruct_plan(plan_dict, root=chosen)
        estimated = await self._estimate_plan_bytes(plan)
        usage = await self._disk_usage(chosen)
        if usage.get("available") and usage["free_bytes"] < estimated:
            raise SetupError(
                SetupErrorCode.DISK_SPACE_LOW,
                f"磁盘空间不足，需要约 {estimated / _GB:.1f} GiB，剩余 {usage['free_bytes'] / _GB:.1f} GiB",
            )
        job = self._executor.new_job(chosen, plan.profile, plan.steps, work_dir=self.work_dir)
        async with self._job_lock:
            self._jobs[job.id] = job
        self._spawn(job)
        return {"id": job.id, "profile": plan.profile, "status": job.status}

    def _reconstruct_plan(self, plan_dict: dict[str, Any], *, root: Path) -> Plan:
        if not isinstance(plan_dict, dict):
            raise SetupError(SetupErrorCode.CONFIG_INVALID, "plan 必须是对象")
        steps_raw = plan_dict.get("steps")
        if not isinstance(steps_raw, list):
            raise SetupError(SetupErrorCode.CONFIG_INVALID, "plan 缺少 steps")
        steps: list[PlanStep] = []
        for raw in steps_raw:
            if not isinstance(raw, dict):
                raise SetupError(SetupErrorCode.CONFIG_INVALID, "plan step 必须是对象")
            kind = raw.get("kind")
            group = raw.get("group")
            label = str(raw.get("label") or kind)
            if kind == "bridge":
                steps.append(PlanStep(kind="bridge", group=None, label=label))
                continue
            if kind == "pip":
                steps.append(
                    PlanStep(kind="pip", group=group, label=label, pip_packages=tuple(str(p) for p in raw.get("packages", [])))
                )
                continue
            if kind == "models":
                items = [PlanItem(kind="model", spec=self.manifest.models[item["id"]]) for item in raw.get("items", [])]
                steps.append(PlanStep(kind="models", group=group, label=label, items=items))
                continue
            if kind == "nodes":
                items = [PlanItem(kind="node", spec=self.manifest.nodes[item["id"]]) for item in raw.get("items", [])]
                pip: tuple[str, ...] = ()
                if group and group in self.manifest.node_groups:
                    pip = self.manifest.node_groups[group].pip_packages
                steps.append(PlanStep(kind="nodes", group=group, label=label, items=items, pip_packages=pip))
                continue
            raise SetupError(SetupErrorCode.CONFIG_INVALID, f"未知 plan step: {kind!r}")
        if not steps:
            raise SetupError(SetupErrorCode.CONFIG_INVALID, "plan 不含任何步骤")
        return Plan(root=root, profile=str(plan_dict.get("profile") or "custom"), steps=steps)

    def _spawn(self, job: SetupJob) -> None:
        self._tasks[job.id] = asyncio.create_task(self._run_job_safely(job))

    async def _run_job_safely(self, job: SetupJob) -> None:
        try:
            await self._executor.run_job(job)
        except asyncio.CancelledError:
            job.status = JOB_CANCELLED
            job.emit("job_cancelled", id=job.id)
        except Exception as exc:  # noqa: BLE001 - defensive, executor handles SetupError internally
            job.status = JOB_FAILED
            job.error_code = SetupErrorCode.INTERNAL
            job.error_message = str(exc)
            job.emit("step_failed", step=-1, code=SetupErrorCode.INTERNAL, message=str(exc))
            job.emit("job_finished", status=JOB_FAILED)
        finally:
            self._tasks.pop(job.id, None)

    def get_job(self, job_id: str) -> SetupJob | None:
        return self._jobs.get(job_id)

    def jobs(self) -> list[SetupJob]:
        # newest first; bounded to avoid unbounded growth of the response
        return [self._jobs[k] for k in sorted(self._jobs, key=lambda k: self._jobs[k].id, reverse=True)][:50]

    def job_events(self, job_id: str) -> list[dict[str, Any]] | None:
        job = self._jobs.get(job_id)
        return job.events if job is not None else None

    async def cancel_job(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.status in _TERMINAL_STATUSES:
            return False
        await self._executor.cancel_job(job)
        return True

    async def retry_job(self, job_id: str) -> dict[str, Any]:
        job = self._jobs.get(job_id)
        if job is None:
            raise SetupError(SetupErrorCode.JOB_NOT_FOUND, f"任务不存在: {job_id}")
        if job.status not in _TERMINAL_STATUSES:
            raise SetupError(SetupErrorCode.JOB_BUSY, "任务仍在运行，无法重试")
        failed = [s.index for s in job.steps if s.status == "failed"]
        if not failed:
            raise SetupError(SetupErrorCode.JOB_NOT_FOUND, "没有失败步骤可重试")
        base = Plan(root=job.root, profile=job.profile, steps=job.plan_steps)
        plan = self._planner.plan_for_retry(EnvironmentStatus(root=job.root), failed, base)
        new_job = self._executor.new_job(job.root, plan.profile, plan.steps, work_dir=self.work_dir)
        async with self._job_lock:
            self._jobs[new_job.id] = new_job
        self._spawn(new_job)
        return {"id": new_job.id, "profile": plan.profile, "status": new_job.status}

    async def rollback_job(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None:
            raise SetupError(SetupErrorCode.JOB_NOT_FOUND, f"任务不存在: {job_id}")
        await self._executor.rollback_job(job)
        return job.status == "rolled_back"

    # ------------------------------------------------------------- reprobe / remote

    async def reprobe(self, engine: Any) -> dict[str, Any]:
        bridge = await engine.probe_lora_bridge()
        refresh = getattr(engine, "refresh_after_env_setup", None)
        if callable(refresh):
            await refresh()
        elif hasattr(engine, "probe_comfyui"):
            await engine.probe_comfyui({"force": True})
        self._scan_cache = None
        return {"bridge": bridge, "status": "ok"}

    async def remote_kit(self) -> dict[str, Any]:
        return {
            "configured": False,
            "mode": "local_only",
            "bridge_version": self.manifest.bridge.version,
            "manual_files": [
                {"name": mf.name, "dest": mf.dest, "note": mf.note} for mf in self.manifest.manual_files
            ],
            "note": "桌面环境直接在本机安装，无需远程部署；远程场景需手动放置以下文件",
        }

    # ------------------------------------------------------------- helpers

    async def _disk_usage(self, root: Path | None) -> dict[str, Any]:
        if root is None:
            return {"available": False}
        try:
            usage = await asyncio.to_thread(shutil.disk_usage, root)
        except OSError:
            return {"available": False}
        return {
            "available": True,
            "free_bytes": usage.free,
            "total_bytes": usage.total,
            "free_gb": round(usage.free / _GB, 2),
        }

    @property
    def installed_dir_name(self) -> str | None:
        return self.install_dir.name if self.install_dir is not None else None

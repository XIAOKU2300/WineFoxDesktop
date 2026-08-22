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
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any

import aiohttp

from .setup_errors import SetupError, SetupErrorCode
from .setup_executor import JOB_CANCELLED, JOB_FAILED, SetupExecutor, SetupJob
from .setup_manifest import SetupManifest, load_setup_manifest
from .setup_paths import test_root, validate_bootstrap_root, validate_comfyui_root

logger = logging.getLogger("winefox.runtime.setup")
from .setup_service import EnvironmentStatus, Plan, PlanItem, PlanStep, Planner, Scanner

_GB = 1024 ** 3
_SCAN_TTL = 5.0
_TERMINAL_STATUSES = ("success", "failed", "cancelled", "rolled_back", "rolling_back")
_NODE_ZIP_ESTIMATE = 5 * 1024 * 1024
#: 镜像测速参数：取样 512 KB、8 秒超时，兼顾准确性与等待时间。
_MIRROR_SAMPLE_BYTES = 512 * 1024
_MIRROR_PROBE_TIMEOUT = 8.0


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
        self._persisted_root: str | None = None
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
        self._prefs_path = (self.work_dir.parent / "setup_prefs.json") if self.work_dir else None
        self._preferred_host = self._load_preferred_host()
        self._sync_executor_preferred()

    def _load_preferred_host(self) -> str:
        if self._prefs_path is None or not self._prefs_path.is_file():
            return ""
        try:
            data = json.loads(self._prefs_path.read_text(encoding="utf-8"))
            host = str((data or {}).get("preferred_host") or "").strip().lower()
            return host if host in self.manifest.bridge.allowed_hosts else ""
        except (OSError, ValueError):
            return ""

    def _save_preferred_host(self, host: str) -> None:
        if self._prefs_path is None:
            return
        try:
            self._prefs_path.parent.mkdir(parents=True, exist_ok=True)
            self._prefs_path.write_text(
                json.dumps({"preferred_host": host}, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass

    @property
    def preferred_host(self) -> str:
        return self._preferred_host

    def set_preferred_host(self, host: str) -> str:
        host = str(host or "").strip().lower()
        if host and host not in self.manifest.bridge.allowed_hosts:
            raise SetupError(SetupErrorCode.SOURCE_UNTRUSTED, f"未知镜像主机: {host}")
        self._preferred_host = host
        self._save_preferred_host(host)
        self._sync_executor_preferred()
        return host

    def _sync_executor_preferred(self) -> None:
        hosts = [self._preferred_host] if self._preferred_host else []
        self._executor.preferred_hosts = hosts

    # ------------------------------------------------------------- mirror probe

    async def probe_mirrors(self) -> dict[str, Any]:
        """对清单里出现过的下载主机做实测速（取样下载 512KB）。"""
        sample_urls: dict[str, str] = {}
        for model in self.manifest.models.values():
            for source in model.sources:
                if source.host not in sample_urls:
                    sample_urls[source.host] = source.url
        if not sample_urls:
            return {"items": [], "preferred_host": self._preferred_host}

        async def probe(host: str, url: str) -> dict[str, Any]:
            started = time.monotonic()
            try:
                return await _probe_once(host, url)
            except Exception as exc:  # noqa: BLE001 - 单个镜像探测失败不影响其余
                return {"host": host, "ok": False, "error": str(exc)[:120]}

        async def _probe_once(host: str, url: str) -> dict[str, Any]:
            started = time.monotonic()
            try:
                timeout = aiohttp.ClientTimeout(total=_MIRROR_PROBE_TIMEOUT, connect=4)
                # 注意不能 async with 共享会话：那会在块结束时关闭执行器会话。
                session = self._executor._session_for()
                if True:
                    async with session.get(
                        url,
                        headers={"Range": f"bytes=0-{_MIRROR_SAMPLE_BYTES - 1}"},
                        allow_redirects=True,
                        ssl=True,
                        timeout=timeout,
                    ) as resp:
                        if resp.status >= 400:
                            return {"host": host, "ok": False, "error": f"HTTP {resp.status}"}
                        first_byte = time.monotonic()
                        received = 0
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            received += len(chunk)
                            if received >= _MIRROR_SAMPLE_BYTES:
                                break
                        elapsed = time.monotonic() - started
                        transfer = max(time.monotonic() - first_byte, 1e-3)
                        return {
                            "host": host,
                            "ok": received > 0,
                            "latency_ms": round((first_byte - started) * 1000, 1),
                            "sample_bytes": received,
                            "speed_bps": round(received / transfer, 1),
                            "elapsed_ms": round(elapsed * 1000, 1),
                        }
            except asyncio.CancelledError:
                raise
            except Exception:
                raise

        items = await asyncio.gather(
            *(probe(h, u) for h, u in sample_urls.items()), return_exceptions=True
        )
        normalized: list[dict[str, Any]] = []
        for host, item in zip(sample_urls.keys(), items):
            if isinstance(item, dict):
                normalized.append(item)
            else:
                normalized.append({"host": host, "ok": False, "error": "探测失败"})
        ranked = sorted(
            [item for item in normalized if item.get("ok")],
            key=lambda item: item.get("speed_bps") or 0,
            reverse=True,
        )
        return {
            "items": normalized,
            "preferred_host": self._preferred_host,
            "fastest_host": ranked[0]["host"] if ranked else "",
            "recommended": bool(ranked and not self._preferred_host),
        }

    def _executor_session(self):
        return self._executor._session_for()

    async def close(self) -> None:
        # 真正的会话由 executor 懒创建持有；manager 自持的 _session 仅为注入透传
        await self._executor.close()

    # ------------------------------------------------------------- scanning

    async def _scan(self, *, force: bool = False, root: Path | None = None) -> EnvironmentStatus:
        if root is not None:
            resolved = await asyncio.to_thread(validate_comfyui_root, root, install_dir=self.install_dir)
            self._remember_root(resolved)
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

    def _remember_root(self, resolved: Path) -> None:
        """记住用户选定/装机成功的 ComfyUI 根目录（LoRA auto 模式推导本地目录用）。"""
        try:
            text = str(resolved)
        except Exception:
            return
        if not text.strip() or text == self._persisted_root:
            return
        self._persisted_root = text
        config = getattr(self.secret_store, "patch_settings", None)
        if config is None:
            return
        try:
            asyncio.get_running_loop().create_task(config({"comfyui_root": text}))
        except RuntimeError:
            pass
        except Exception:  # noqa: BLE001 - 记住失败不影响扫描
            logger.debug("持久化 comfyui_root 失败（忽略）", exc_info=True)

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
        root: Path | None = None,
    ) -> dict[str, Any]:
        if profile == "bootstrap":
            if root is None:
                raise SetupError(SetupErrorCode.CONFIG_INVALID, "从零装机需要指定安装目录 (root)")
            target = await asyncio.to_thread(validate_bootstrap_root, root, install_dir=self.install_dir)
            self._remember_root(target)
            status = await asyncio.to_thread(
                self._scanner.scan, injected_roots=[target] if test_root(target) else []
            )
            status.root = target
        else:
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
        profile_name = str((plan_dict or {}).get("profile") or "")
        if profile_name == "bootstrap" and root is not None:
            status: EnvironmentStatus | None = None
            chosen = await asyncio.to_thread(validate_bootstrap_root, root, install_dir=self.install_dir)
            self._remember_root(chosen)
        else:
            status = await self._scan()
            chosen = status.root if root is None else None
            if root is not None:
                chosen = await asyncio.to_thread(validate_comfyui_root, root, install_dir=self.install_dir)
                self._remember_root(chosen)
        if chosen is None:
            raise SetupError(
                SetupErrorCode.ROOT_INVALID,
                "未检测到 ComfyUI 根目录：请先在「根目录」卡片选择已有 ComfyUI 目录，或使用「从零装机」",
            )
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
            if kind in {"comfyui", "python_runtime", "requirements"}:
                steps.append(PlanStep(kind=kind, group=None, label=label))
                continue
            if kind == "manual_resolve":
                # 执行器自取清单缺失文件，无需 items；此前漏登记导致
                # 含该步骤的计划（complete/complete_manual）无法开始安装
                steps.append(PlanStep(kind="manual_resolve", group=None, label=label))
                continue
            if kind == "pip":
                steps.append(
                    PlanStep(kind="pip", group=group, label=label, pip_packages=tuple(str(p) for p in raw.get("packages", [])))
                )
                continue
            if kind == "models":
                items = [self._plan_item(raw, item, "model", self.manifest.models) for item in raw.get("items", [])]
                steps.append(PlanStep(kind="models", group=group, label=label, items=items))
                continue
            if kind == "nodes":
                items = [self._plan_item(raw, item, "node", self.manifest.nodes) for item in raw.get("items", [])]
                pip: tuple[str, ...] = ()
                if group and group in self.manifest.node_groups:
                    pip = self.manifest.node_groups[group].pip_packages
                steps.append(PlanStep(kind="nodes", group=group, label=label, items=items, pip_packages=pip))
                continue
            raise SetupError(SetupErrorCode.CONFIG_INVALID, f"未知 plan step: {kind!r}")
        if not steps:
            raise SetupError(SetupErrorCode.CONFIG_INVALID, "plan 不含任何步骤")
        return Plan(root=root, profile=str(plan_dict.get("profile") or "custom"), steps=steps)

    def _plan_item(self, raw: dict, item: Any, kind: str, catalog: dict) -> PlanItem:
        """校验客户端提交的条目引用，非法输入返回 CONFIG_INVALID 而非 500。"""
        if not isinstance(item, dict):
            raise SetupError(SetupErrorCode.CONFIG_INVALID, f"plan step {raw.get('kind')!r} 的 items 条目必须是对象")
        spec = catalog.get(str(item.get("id") or ""))
        if spec is None:
            raise SetupError(SetupErrorCode.CONFIG_INVALID, f"清单中不存在 {kind}: {item.get('id')!r}")
        return PlanItem(kind=kind, spec=spec)

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

    async def nodes_overview(self) -> list[dict[str, Any]]:
        """列出 ComfyUI 根目录 custom_nodes 下全部已安装节点 + 清单缺失项。

        纯本地文件系统枚举，不依赖 ComfyUI 在线——修“自定义节点页在
        ComfyUI 离线或无缺失时查不到任何内容”的问题。
        """
        status = await self._scan()
        root = status.root
        if root is None:
            return []
        custom_nodes = root / "custom_nodes"

        def scan_dir() -> list[dict[str, Any]]:
            entries: list[dict[str, Any]] = []
            if not custom_nodes.is_dir():
                return entries
            for child in sorted(custom_nodes.iterdir(), key=lambda p: p.name.lower()):
                if not child.is_dir() or child.name.startswith("."):
                    continue
                if child.name.endswith(".disabled"):
                    name = child.name[: -len(".disabled")]
                    entries.append({"name": name, "installed": True, "disabled": True, "managed": name in self.manifest.nodes or child.name in [n.name for n in self.manifest.nodes.values()]})
                    continue
                if not (child / "__init__.py").is_file() and not any(child.glob("*.py")):
                    continue
                entries.append({"name": child.name, "installed": True, "disabled": False, "managed": child.name in [n.name for n in self.manifest.nodes.values()]})
            return entries

        entries = await asyncio.to_thread(scan_dir)
        known = {entry["name"] for entry in entries}
        for node in self.manifest.nodes.values():
            if node.name not in known:
                entries.append({"name": node.name, "installed": False, "disabled": False, "managed": True})
        return entries

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

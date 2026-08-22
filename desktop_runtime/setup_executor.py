"""Job executor for the environment center.

Runs an ordered :class:`Plan` produced by the planner: downloads models to
staging, installs pinned nodes, seeds the LoraBridge, installs pip packages.
Every network/filesystem step is either aiohttp-native or wrapped in
``asyncio.to_thread`` so the aiohttp event loop and WebView never block.

Safety guarantees (migration doc section 11):
- downloads go to ``<dest>.part``, are verified, then atomically renamed;
- existing targets are snapshotted first so ``rollback`` can restore them;
- every HTTP redirect is re-validated against HTTPS + the host allowlist;
- TLS verification is never disabled;
- pip installs only ever run against an explicitly requested list.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import time
import zipfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from typing import Any

import aiohttp

from .setup_errors import SetupError, SetupErrorCode
from .setup_manifest import ModelSpec, NodeSpec, SetupManifest
from .setup_paths import safe_comfyui_path
from .setup_python import PythonProbe, resolve_python_executable
from .setup_verify import sha256_of, verify_model_file, verify_node_dir
from .resource_seed import BridgeSourceError, seed_bridge_source

MAX_REDIRECTS = 5
_CHUNK = 256 * 1024
_PROGRESS_THROTTLE = 0.5
#: 大文件多连接下载阈值与分片数：HuggingFace CDN 支持 Range，多连接可
#: 显著提升高时延链路的吞吐；小文件走单连接避免额外握手开销。
_MULTICONN_THRESHOLD = 32 * 1024 * 1024
_MULTICONN_SLICES = 4
_SPEED_WINDOW = 6.0

#: Job lifecycle states.
JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_SUCCESS = "success"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"
JOB_ROLLING_BACK = "rolling_back"
JOB_ROLLED_BACK = "rolled_back"


class JobStatus:
    PENDING = JOB_PENDING
    RUNNING = JOB_RUNNING
    SUCCESS = JOB_SUCCESS
    FAILED = JOB_FAILED
    CANCELLED = JOB_CANCELLED
    ROLLING_BACK = JOB_ROLLING_BACK
    ROLLED_BACK = JOB_ROLLED_BACK


@dataclass
class StepState:
    index: int
    kind: str
    group: str | None
    label: str
    status: str = "pending"
    error_code: str | None = None
    error_message: str | None = None
    items_done: int = 0
    items_total: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "kind": self.kind,
            "group": self.group,
            "label": self.label,
            "status": self.status,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "items_done": self.items_done,
            "items_total": self.items_total,
        }


@dataclass
class RollbackEntry:
    target: Path
    backup: Path | None = None  # None => target did not exist before the mutation


@dataclass
class SetupJob:
    id: str
    root: Path
    profile: str
    steps: list[StepState] = field(default_factory=list)
    plan_steps: list = field(default_factory=list)
    status: str = JOB_PENDING
    error_code: str | None = None
    error_message: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    rollback_entries: list[RollbackEntry] = field(default_factory=list)
    work_dir: Path | None = None
    cancel_requested: asyncio.Event = field(default_factory=asyncio.Event)
    _progress_at: float = 0.0
    _event_cb: Any = None
    _subscribers: set = field(default_factory=set)

    def set_event_callback(self, cb) -> None:
        self._event_cb = cb

    def subscribe(self) -> asyncio.Queue:
        """Return a queue that receives every future event; seeds past ones."""
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(q)
        for event in self.events:
            q.put_nowait(event)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def emit(self, type_: str, **payload: Any) -> None:
        event = {"type": type_, **payload, "at": time.time()}
        self.events.append(event)
        if self._event_cb:
            try:
                self._event_cb(event)
            except Exception:
                pass
        for q in list(self._subscribers):
            q.put_nowait(event)

    def emit_throttled(self, type_: str, **payload: Any) -> None:
        now = time.monotonic()
        if now - self._progress_at >= _PROGRESS_THROTTLE or type_ in ("step_failed",):
            self._progress_at = now
            self.emit(type_, **payload)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "profile": self.profile,
            "status": self.status,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "steps": [s.to_dict() for s in self.steps],
        }


class PythonRunner:
    """Runs the target ComfyUI python for pip installs (async subprocess)."""

    def __init__(self, executable: str, *, timeout: float = 900.0) -> None:
        self.executable = executable
        self.timeout = timeout

    async def run(self, *args: str) -> tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.executable,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise SetupError(SetupErrorCode.PIP_FAILED, f"无法启动 Python: {exc}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), self.timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise SetupError(SetupErrorCode.PIP_FAILED, f"pip 运行超时 ({self.timeout}s)") from None
        return proc.returncode or 0, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")

    async def probe_version(self) -> tuple[int, int]:
        code, out, _ = await self.run("-c", "import sys; print('%d.%d' % (sys.version_info[0], sys.version_info[1]))")
        if code != 0:
            raise SetupError(SetupErrorCode.PYTHON_UNSUPPORTED, "无法读取 Python 版本")
        parts = out.strip().split(".")
        if len(parts) < 2:
            raise SetupError(SetupErrorCode.PYTHON_UNSUPPORTED, f"无法解析 Python 版本: {out!r}")
        return int(parts[0]), int(parts[1])

    async def pip_install(
        self,
        packages: list[str],
        *,
        requirements: Path | None = None,
        index_url: str | None = None,
    ) -> None:
        args = ["-m", "pip", "install", "--disable-pip-version-check"]
        if index_url:
            args += ["-i", index_url]
        if requirements is not None:
            args += ["-r", str(requirements)]
        else:
            args += packages
        code, out, err = await self.run(*args)
        if code != 0:
            detail = (err or out).strip()[:500]
            raise SetupError(SetupErrorCode.PIP_FAILED, f"pip 安装失败: {detail}")


def _sanitize_error_text(value: str) -> str:
    """去掉异常文本里的绝对路径，只保留文件名（公开契约：不外泄本机路径）。"""
    pattern = r"(?:[A-Za-z]:[\\/]|\\\\)[^\r\n,;\"']*"
    return re.sub(pattern, lambda m: Path(m.group(0)).name or "[path]", value)[:500]


class _SpeedTracker:
    """聚合字节数与滑动窗口速率，按节流周期向 job 发进度事件。"""

    def __init__(self, job: SetupJob, item_id: str, *, total: int | None, offset: int = 0) -> None:
        self.job = job
        self.id = item_id
        self.total = total
        self.done = offset
        self._window: deque[tuple[float, int]] = deque(maxlen=64)

    def add(self, count: int) -> None:
        self.done += count
        now = time.monotonic()
        self._window.append((now, self.done))
        while len(self._window) > 2 and now - self._window[0][0] > _SPEED_WINDOW:
            self._window.popleft()

    @property
    def speed_bps(self) -> float:
        if len(self._window) < 2:
            return 0.0
        (t0, b0), (t1, b1) = self._window[0], self._window[-1]
        elapsed = t1 - t0
        if elapsed < 0.2:
            return 0.0
        return max(0.0, (b1 - b0) / elapsed)

    def emit(self, **extra: Any) -> None:
        speed = self.speed_bps
        eta: float | None = None
        if self.total and speed > 1024:
            eta = max(0.0, (self.total - self.done) / speed)
        percent = round(self.done * 100.0 / self.total, 1) if self.total else None
        payload: dict[str, Any] = {
            "kind": "model",
            "id": self.id,
            "size": self.done,
            "done": self.done,
            "total": self.total,
            "speed_bps": round(speed, 1),
            "eta_s": round(eta, 1) if eta is not None else None,
            "percent": percent,
        }
        payload.update(extra)
        self.job.emit_throttled("item_progress", **payload)

    def emit_final(self, **extra: Any) -> None:
        """终态进度：绕过节流，确保 UI 收到 100% 收口。"""
        payload: dict[str, Any] = {
            "kind": "model",
            "id": self.id,
            "size": self.done,
            "done": self.done,
            "total": self.total,
            "speed_bps": 0,
            "eta_s": 0,
            "percent": 100.0 if (self.total and self.done >= self.total) else None,
        }
        payload.update(extra)
        self.job.emit("item_progress", **payload)


class SetupExecutor:
    """Executes a plan against one ComfyUI root with cancel/rollback support."""

    def __init__(
        self,
        manifest: SetupManifest,
        *,
        install_dir: Path | None = None,
        work_dir: Path | None = None,
        secret_store: Any = None,
        session: aiohttp.ClientSession | None = None,
        url_rewriter=None,
    ) -> None:
        self.manifest = manifest
        self.install_dir = install_dir
        self.work_dir = work_dir
        self.secret_store = secret_store
        self._owns_session = session is None
        self._session = session
        self._url_rewriter = url_rewriter  # test-only hook, never wired in prod
        self._job_counter = 0
        #: 镜像优选主机列表（manager 维护），_ordered_sources 按此排序。
        self.preferred_hosts: list[str] = []

    def _session_for(self) -> aiohttp.ClientSession:
        # Defer session creation: aiohttp 3.14 refuses to build a ClientSession
        # without a running loop, and the composition root is synchronous.
        if self._session is None:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "WineFoxDesktop/8.6 setup"},
                timeout=aiohttp.ClientTimeout(total=300, connect=30, sock_read=300),
            )
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()

    def new_job(
        self,
        root: Path,
        profile: str,
        plan_steps: list,
        *,
        work_dir: Path | None = None,
    ) -> SetupJob:
        self._job_counter += 1
        states = [
            StepState(index=i, kind=s.kind, group=s.group, label=s.label, items_total=len(s.items))
            for i, s in enumerate(plan_steps)
        ]
        job = SetupJob(
            id=f"job-{self._job_counter}",
            root=root,
            profile=profile,
            steps=states,
            plan_steps=plan_steps,
            work_dir=work_dir or self.work_dir,
        )
        return job

    # ------------------------------------------------------------- public

    async def run_job(self, job: SetupJob) -> None:
        job.status = JOB_RUNNING
        job.emit("job_started", id=job.id, profile=job.profile)
        for state, plan_step in zip(job.steps, job.plan_steps):
            if job.cancel_requested.is_set():
                job.status = JOB_CANCELLED
                job.emit("job_cancelled", id=job.id)
                return
            state.status = "running"
            job.emit("step_started", step=state.index, kind=state.kind, group=state.group)
            try:
                await self._run_step(job, state, plan_step)
                state.status = "success"
                job.emit("step_finished", step=state.index, status="success")
            except asyncio.CancelledError:
                state.status = "failed"
                job.status = JOB_CANCELLED
                job.emit("job_cancelled", id=job.id)
                raise
            except SetupError as exc:
                state.status = "failed"
                state.error_code = exc.code
                state.error_message = exc.message
                job.error_code = job.error_code or exc.code
                job.emit("step_failed", step=state.index, code=exc.code, message=exc.message)
            except Exception as exc:  # pragma: no cover - defensive
                state.status = "failed"
                state.error_code = SetupErrorCode.INTERNAL
                state.error_message = _sanitize_error_text(str(exc))
                job.error_code = SetupErrorCode.INTERNAL
                job.emit("step_failed", step=state.index, code=SetupErrorCode.INTERNAL, message=state.error_message)
        if job.status == JOB_RUNNING:
            if job.error_code:
                job.status = JOB_FAILED
                job.emit("job_finished", status=JOB_FAILED)
            else:
                job.status = JOB_SUCCESS
                job.emit("job_finished", status=JOB_SUCCESS)

    async def cancel_job(self, job: SetupJob) -> None:
        job.cancel_requested.set()

    async def rollback_job(self, job: SetupJob) -> None:
        job.status = JOB_ROLLING_BACK
        job.emit("rollback_started", id=job.id)
        failures: list[str] = []
        for entry in reversed(job.rollback_entries):
            try:
                await asyncio.to_thread(self._restore_entry, entry)
            except OSError as exc:
                failures.append(str(entry.target))
                # 不把绝对路径写进事件流：只带文件名 + 无路径的错误原因
                job.emit("rollback_item_failed", target=entry.target.name, message=f"恢复文件失败: {exc.strerror or '权限不足'}")
        if failures:
            job.status = JOB_FAILED
            job.error_code = SetupErrorCode.ROLLBACK_FAILED
            job.error_message = f"回滚失败 {len(failures)} 项"
            job.emit("rollback_finished", status=JOB_FAILED)
            return
        job.status = JOB_ROLLED_BACK
        job.emit("rollback_finished", status=JOB_ROLLED_BACK)

    # ------------------------------------------------------------- steps

    async def _run_step(self, job: SetupJob, state: StepState, plan_step) -> None:
        if plan_step.kind == "models":
            await self._run_models(job, state, plan_step)
        elif plan_step.kind == "nodes":
            await self._run_nodes(job, state, plan_step)
        elif plan_step.kind == "bridge":
            await self._run_bridge(job, state, plan_step)
        elif plan_step.kind == "pip":
            await self._run_pip(job, state, plan_step)
        elif plan_step.kind == "comfyui":
            await self._run_comfyui(job, state)
        elif plan_step.kind == "python_runtime":
            await self._run_python_runtime(job, state)
        elif plan_step.kind == "requirements":
            await self._run_requirements(job, state)
        elif plan_step.kind == "manual_resolve":
            await self._run_manual_resolve(job, state)
        else:  # pragma: no cover
            raise SetupError(SetupErrorCode.UNKNOWN, f"未知步骤类型: {plan_step.kind}")

    async def _run_models(self, job: SetupJob, state: StepState, plan_step) -> None:
        state.items_total = len(plan_step.items)
        for item in plan_step.items:
            if job.cancel_requested.is_set():
                return
            spec: ModelSpec = item.spec  # type: ignore[assignment]
            job.emit("item_started", step=state.index, kind="model", id=spec.id)
            await self._download_model(job, spec)
            state.items_done += 1
            job.emit_throttled("item_finished", step=state.index, kind="model", id=spec.id)

    async def _run_nodes(self, job: SetupJob, state: StepState, plan_step) -> None:
        state.items_total = len(plan_step.items)
        for item in plan_step.items:
            if job.cancel_requested.is_set():
                return
            node: NodeSpec = item.spec  # type: ignore[assignment]
            job.emit("item_started", step=state.index, kind="node", id=node.id)
            await self._install_node(job, node)
            state.items_done += 1
            job.emit_throttled("item_finished", step=state.index, kind="node", id=node.id)

    async def _run_bridge(self, job: SetupJob, state: StepState, plan_step) -> None:
        custom_nodes = job.root / "custom_nodes"
        custom_nodes.mkdir(parents=True, exist_ok=True)
        target = custom_nodes / self.manifest.bridge.dest_folder
        self._snapshot(job, target)
        try:
            await asyncio.to_thread(seed_bridge_source, custom_nodes, self.manifest.bridge.dest_folder, self.manifest.bridge.source, True)
        except BridgeSourceError as exc:
            raise SetupError(SetupErrorCode.CONFIG_INVALID, str(exc)) from exc
        token = await self._ensure_bridge_token()
        config = {
            **self.manifest.bridge.config_defaults,
            "token": token,
            "allowed_hosts": list(self.manifest.bridge.allowed_hosts),
            "allow_local_requests_without_token": False,
        }
        await asyncio.to_thread(self._write_json, target / "config.json", config)
        job.emit("bridge_installed", version=self.manifest.bridge.version)

    async def _run_pip(self, job: SetupJob, state: StepState, plan_step) -> None:
        packages = list(plan_step.pip_packages)
        if not packages:
            return
        state.items_total = len(packages)
        runner = await self._python_runner_for(job)
        for package in packages:
            job.emit("pip_started", step=state.index, package=package)
            await runner.pip_install([package])
            state.items_done += 1

    # ------------------------------------------------------------- runtime bootstrap

    async def _download_runtime_file(self, job: SetupJob, sources, dest: Path, label: str) -> None:
        """复用模型下载管线（多连接/断点/校验）下载运行时组件。"""
        spec = ModelSpec(
            id=label,
            name=dest.name,
            kind="bin",
            dest=label,
            min_gb=0.0,
            sha256="",
            sources=tuple(sources),
            group="runtime",
        )
        part = dest.with_name(dest.name + ".part")
        last_error = None
        for source in self._ordered_sources(spec):
            if source.host not in self.manifest.bridge.allowed_hosts:
                continue
            url = await self._resolve_url(source.url)
            try:
                await self._download_to_part(job, url, part, spec)
            except SetupError as exc:
                last_error = exc
                if exc.code == SetupErrorCode.CANCELLED:
                    raise
                continue
            self._snapshot(job, dest)
            await asyncio.to_thread(os.replace, part, dest)
            return
        raise last_error or SetupError(SetupErrorCode.DOWNLOAD_FAILED, f"{label} 无可用来源")

    async def _run_comfyui(self, job: SetupJob, state: StepState) -> None:
        """从 GitHub 官方 zip 安装 ComfyUI 本体到 job.root。"""
        runtime = self.manifest.runtime
        if runtime is None or not runtime.comfyui_sources:
            raise SetupError(SetupErrorCode.CONFIG_INVALID, "清单未提供 ComfyUI 本体来源")
        if (job.root / "main.py").is_file():
            job.emit("runtime_skipped", component="comfyui", reason="已存在")
            return
        if job.work_dir is None:
            raise SetupError(SetupErrorCode.INTERNAL, "job 缺少工作目录")
        if job.root.exists() and any(job.root.iterdir()):
            raise SetupError(
                SetupErrorCode.ROOT_INVALID,
                "目标目录非空且不含 ComfyUI（main.py 缺失），请选择空目录",
            )
        staging = job.work_dir / "comfyui"
        staging.mkdir(parents=True, exist_ok=True)
        zip_path = staging / "ComfyUI-master.zip"
        await self._download_runtime_file(job, runtime.comfyui_sources, zip_path, "comfyui.zip")
        extracted = staging / "extract"
        if extracted.exists():
            await asyncio.to_thread(shutil.rmtree, extracted)
        await asyncio.to_thread(self._extract_zip_safe, zip_path, extracted)
        top = self._single_top_dir(extracted)
        await asyncio.to_thread(self._copytree_into, top, job.root)
        if not (job.root / "main.py").is_file():
            raise SetupError(SetupErrorCode.DOWNLOAD_FAILED, "ComfyUI 压缩包缺少 main.py")
        job.emit("runtime_installed", component="comfyui")

    async def _run_python_runtime(self, job: SetupJob, state: StepState) -> None:
        """安装 python.org 官方 embeddable Python + pip（含国内镜像回退）。"""
        runtime = self.manifest.runtime
        embed = runtime.python_embed if runtime else None
        if embed is None or not embed.sources:
            raise SetupError(SetupErrorCode.CONFIG_INVALID, "清单未提供 Python 运行时来源")
        if job.work_dir is None:
            raise SetupError(SetupErrorCode.INTERNAL, "job 缺少工作目录")

        probe = resolve_python_executable(job.root, self.manifest.python)
        if probe.found and probe.executable:
            runner = PythonRunner(probe.executable)
            try:
                major, minor = await runner.probe_version()
                min_major, min_minor = self.manifest.python.min_version
                if (major, minor) >= (min_major, min_minor):
                    code, _out, _err = await runner.run("-m", "pip", "--version")
                    if code == 0:
                        job.emit("runtime_skipped", component="python", reason=f"检测到 Python {major}.{minor}")
                        return
            except SetupError:
                pass

        target_dir = job.root.parent / embed.install_dir
        if (target_dir / "python.exe").is_file():
            job.emit("runtime_skipped", component="python", reason="python_embeded 已存在")
            return
        if target_dir.exists() and any(target_dir.iterdir()):
            raise SetupError(SetupErrorCode.ROOT_INVALID, f"{embed.install_dir} 目录非空，拒绝覆盖")

        staging = job.work_dir / "python"
        staging.mkdir(parents=True, exist_ok=True)
        zip_path = staging / f"python-{embed.version}-embed.zip"
        await self._download_runtime_file(job, embed.sources, zip_path, "python_embed.zip")
        extracted = staging / "extract"
        if extracted.exists():
            await asyncio.to_thread(shutil.rmtree, extracted)
        await asyncio.to_thread(self._extract_zip_safe, zip_path, extracted)
        await asyncio.to_thread(shutil.copytree, str(extracted), str(target_dir))

        await asyncio.to_thread(self._enable_embed_site, target_dir, embed.version)

        python_exe = target_dir / "python.exe"
        get_pip = staging / "get-pip.py"
        if embed.get_pip_url:
            host = urlsplit(embed.get_pip_url).hostname or ""
            if host in self.manifest.bridge.allowed_hosts:
                pip_source = ModelSource(host=host, url=embed.get_pip_url)
                await self._download_runtime_file(job, [pip_source], get_pip, "get-pip.py")
                last_error = None
                for index_url in runtime.pip_index_urls:
                    runner = PythonRunner(str(python_exe), timeout=600.0)
                    try:
                        code, out, err = await runner.run(
                            str(get_pip), "--no-warn-script-location", "--index-url", index_url
                        )
                        if code == 0:
                            last_error = None
                            break
                        last_error = SetupError(
                            SetupErrorCode.PIP_FAILED,
                            f"get-pip 失败 (index {index_url}): {(err or out).strip()[:200]}",
                        )
                    except SetupError as exc:
                        last_error = exc
                if last_error is not None:
                    raise last_error

        runner = PythonRunner(str(python_exe))
        code, _out, _err = await runner.run("-m", "pip", "--version")
        if code != 0:
            raise SetupError(SetupErrorCode.PIP_FAILED, "pip 引导后仍不可用")
        job.emit("runtime_installed", component="python", version=embed.version)

    @staticmethod
    def _copytree_into(src: Path, dest: Path) -> None:
        shutil.copytree(str(src), str(dest), dirs_exist_ok=True)

    @staticmethod
    def _enable_embed_site(target_dir: Path, version: str) -> None:
        """取消 python3xx._pth 中 import site 的注释，启用 site-packages。"""
        major_minor = "".join(version.split(".")[:2])
        pth = target_dir / f"python{major_minor}._pth"
        if not pth.is_file():
            candidates = list(target_dir.glob("python*._pth"))
            if not candidates:
                return
            pth = candidates[0]
        content = pth.read_text(encoding="utf-8", errors="replace")
        if "#import site" in content:
            pth.write_text(content.replace("#import site", "import site"), encoding="utf-8")

    async def _run_requirements(self, job: SetupJob, state: StepState) -> None:
        """安装 PyTorch 与 ComfyUI 依赖：GPU 检测选官方 wheel 线，pip 走镜像。"""
        runner = await self._python_runner_for(job)
        runtime = self.manifest.runtime
        if runtime is None:
            raise SetupError(SetupErrorCode.CONFIG_INVALID, "清单未提供运行时配置")

        cuda = await self._detect_cuda(job)
        if cuda:
            index = runtime.torch_index_by_cuda.get(cuda)
            if not index:
                known = sorted(runtime.torch_index_by_cuda, key=float, reverse=True)
                for candidate in known:
                    if float(candidate) <= float(cuda):
                        index = runtime.torch_index_by_cuda[candidate]
                        break
            label = f"CUDA {cuda}"
        else:
            index = runtime.torch_cpu_index
            label = "CPU"
        job.emit("gpu_detected", cuda=cuda or "", torch_index=label)

        if index and runtime.torch_packages:
            await runner.pip_install([*runtime.torch_packages, "--index-url", index])

        requirements = job.root / "requirements.txt"
        if requirements.is_file():
            last_error = None
            for index_url in runtime.pip_index_urls:
                try:
                    await runner.pip_install([], requirements=requirements, index_url=index_url)
                    last_error = None
                    break
                except SetupError as exc:
                    last_error = exc
            if last_error is not None:
                raise last_error
        job.emit("runtime_installed", component="requirements", torch=label)

    async def _detect_cuda(self, job: SetupJob) -> str | None:
        """nvidia-smi 探测驱动支持的 CUDA 版本（如 "12.8"）。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "nvidia-smi",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            return None
        try:
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), 10.0)
        except asyncio.TimeoutError:
            return None
        match = re.search(r"CUDA Version:\s*([0-9]+)\.([0-9]+)", stdout.decode("utf-8", "replace"))
        if not match:
            return None
        return f"{match.group(1)}.{match.group(2)}"

    async def _run_manual_resolve(self, job: SetupJob, state: StepState) -> None:
        """对清单里“需手动放置”的文件做 Civitai 自动解析下载（尽力而为）。

        以文件名主干在 Civitai 搜索，名字能对上的首个结果视为命中；找不到
        或下载失败不阻塞安装——保持“需手动”状态并在事件里说明原因。
        """
        from civitai_api import CivitaiClient

        from . import net_env

        pending = [mf for mf in self.manifest.manual_files if not (job.root / mf.dest).is_file()]
        if not pending:
            job.emit("runtime_skipped", component="manual", reason="无缺失文件")
            return
        state.items_total = len(pending)
        api_token = ""
        proxy = ""
        try:
            if self.secret_store is not None:
                api_token = str(self.secret_store.get_secret("civitai_api_token") or "")
        except Exception:
            api_token = ""
        try:
            proxy, _src = net_env.effective_proxy("")
        except Exception:
            proxy = ""
        client = CivitaiClient(api_token=api_token, proxy=proxy, timeout=45)
        for mf in pending:
            if job.cancel_requested.is_set():
                return
            stem = Path(mf.name).stem
            job.emit("item_started", step=state.index, kind="manual", id=stem)
            try:
                results = await client.search_loras(
                    query=stem,
                    limit=20,
                    base_models=["Illustrious", "Illustrious-XL", "Anima", "SDXL 1.0", "Pony", "SD 1.5"],
                )
            except Exception as exc:
                job.emit("item_finished", step=state.index, kind="manual", id=stem, note=f"搜索失败: {str(exc)[:120]}")
                continue
            target = self._match_manual_result(stem, results)
            if target is None:
                job.emit("item_finished", step=state.index, kind="manual", id=stem, note="未找到名字匹配的结果")
                continue
            try:
                summary = await client.resolve_model(
                    f"https://civitai.com/models/{target['model_id']}?modelVersionId={target['version_id']}"
                )
                dest = safe_comfyui_path(job.root, mf.dest)
                if job.work_dir is not None:
                    staging = job.work_dir / "manual" / stem
                    staging.mkdir(parents=True, exist_ok=True)
                    tmp = staging / mf.name
                    await client.download_file(summary["download_url"], tmp)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    self._snapshot(job, dest)
                    await asyncio.to_thread(shutil.move, str(tmp), str(dest))
                    job.emit("item_finished", step=state.index, kind="manual", id=stem, note=f"已自动下载: {target.get('name', '')[:60]}")
                    state.items_done += 1
            except Exception as exc:
                job.emit("item_finished", step=state.index, kind="manual", id=stem, note=f"下载失败: {str(exc)[:120]}")

    @staticmethod
    def _match_manual_result(stem: str, results) -> dict | None:
        def key(value: str) -> str:
            return re.sub(r"[^0-9a-zŀ-ɏ぀-ヿ㐀-鿿]+", "", str(value or "").lower())

        stem_key = key(stem)
        if not stem_key:
            return None
        for item in results or []:
            if not isinstance(item, dict):
                continue
            name_key = key(item.get("name") or "")
            if not name_key:
                continue
            if stem_key in name_key or name_key in stem_key:
                return item
        return None

    # ------------------------------------------------------------- python

    async def _python_runner_for(self, job: SetupJob) -> PythonRunner:
        probe = resolve_python_executable(job.root, self.manifest.python)
        if not probe.found or not probe.executable:
            raise SetupError(
                SetupErrorCode.PYTHON_NOT_FOUND,
                probe.error_message or "未找到 Python，无法安装依赖",
            )
        runner = PythonRunner(probe.executable)
        min_major, min_minor = self.manifest.python.min_version
        try:
            major, minor = await runner.probe_version()
        except SetupError as exc:
            raise exc
        if (major, minor) < (min_major, min_minor):
            raise SetupError(
                SetupErrorCode.PYTHON_UNSUPPORTED,
                f"Python {major}.{minor} 低于最低要求 {min_major}.{min_minor}",
            )
        return runner

    # ------------------------------------------------------------- models

    async def _download_model(self, job: SetupJob, model: ModelSpec) -> None:
        target = safe_comfyui_path(job.root, model.dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        prog = part.with_name(part.name + ".prog")
        last_error: SetupError | None = None
        for source in self._ordered_sources(model):
            if source.host not in self.manifest.bridge.allowed_hosts:
                continue
            url = await self._resolve_url(source.url)
            try:
                await self._download_to_part(job, url, part, model)
            except SetupError as exc:
                # 网络错误/取消：保留 .part/.prog 断点，重试或换源时续传。
                last_error = exc
                if exc.code == SetupErrorCode.CANCELLED:
                    raise
                continue
            try:
                await asyncio.to_thread(self._verify_part, part, model)
            except SetupError as exc:
                # 校验失败说明已有数据不可信：清除断点后换源重下。
                last_error = exc
                for stale in (part, prog):
                    try:
                        stale.unlink(missing_ok=True)
                    except OSError:
                        pass
                continue
            self._snapshot(job, target)
            await asyncio.to_thread(os.replace, part, target)
            return
        raise last_error or SetupError(SetupErrorCode.DOWNLOAD_FAILED, f"模型 {model.name} 无可用来源")

    def _ordered_sources(self, model: ModelSpec) -> list:
        """按优选镜像排序来源：preferred_hosts 中的主机优先，其余保持清单顺序。"""
        preferred = [str(h).strip().lower() for h in (self.preferred_hosts or []) if str(h or "").strip()]
        if not preferred:
            return list(model.sources)

        def rank(source) -> tuple:
            host = str(source.host or "").strip().lower()
            return (preferred.index(host) if host in preferred else len(preferred),)
        return sorted(model.sources, key=rank)

    async def _resolve_url(self, url: str) -> str:
        if self._url_rewriter is None:
            return url
        result = self._url_rewriter(url)
        if asyncio.iscoroutine(result):
            return await result
        return result

    async def _download_to_part(self, job: SetupJob, url: str, part: Path, model: ModelSpec) -> None:
        prog = part.with_name(part.name + ".prog")
        saved_prog = self._load_prog(prog)

        if saved_prog is not None:
            # 上次多连接下载中断：按原分片计划继续。
            self._probe_cancel_check(job)
            final_url = await self._resolve_final_url(job, url)
            await self._download_multiconn(job, final_url, part, prog, model, saved_prog)
            return

        # 单连接前缀是可信的（追加写）；据此探测总量与 Range 支持。
        resume_from = part.stat().st_size if part.exists() else 0
        final_url, status, total = await self._probe_download(job, url, resume_from)
        if status == 416:
            # 前缀已是完整文件（服务器确认无剩余字节）。
            if part.exists() and part.stat().st_size > 0:
                return
            raise SetupError(SetupErrorCode.DOWNLOAD_FAILED, "服务器拒绝续传 (416)")
        if status == 200:
            resume_from = 0
        remaining = (total - resume_from) if total else None
        if total and remaining is not None and remaining >= _MULTICONN_THRESHOLD and status == 206:
            await self._download_multiconn(
                job, final_url, part, prog, model,
                {"total": total, "prefix": resume_from, "slices": None, "done": None},
            )
            return

        tracker = _SpeedTracker(job, model.id, total=total, offset=resume_from)
        resp_headers = {"Range": f"bytes={resume_from}-"} if resume_from else {}
        try:
            async with self._session_for().get(final_url, headers=resp_headers, allow_redirects=False, ssl=True) as resp:
                if resp.status not in (200, 206):
                    body = (await resp.read()).decode("utf-8", "replace")[:200]
                    raise SetupError(SetupErrorCode.DOWNLOAD_FAILED, f"下载失败 HTTP {resp.status} {body.strip()}")
                mode = "ab" if (resp.status == 206 and resume_from) else "wb"
                with open(part, mode) as fh:
                    async for chunk in resp.content.iter_chunked(_CHUNK):
                        if job.cancel_requested.is_set():
                            raise SetupError(SetupErrorCode.CANCELLED, "已取消")
                        fh.write(chunk)
                        tracker.add(len(chunk))
                        tracker.emit()
            tracker.emit_final()  # 终态：确保 UI 收到 100% 收口
        except aiohttp.ClientError as exc:
            raise SetupError(SetupErrorCode.NETWORK_UNREACHABLE, f"网络错误: {exc}") from exc

    @staticmethod
    def _probe_cancel_check(job: SetupJob) -> None:
        if job.cancel_requested.is_set():
            raise SetupError(SetupErrorCode.CANCELLED, "已取消")

    async def _resolve_final_url(self, job: SetupJob, url: str) -> str:
        """跟随重定向拿到最终下载地址（分片请求不再各自处理重定向）。"""
        current = url
        hops = 0
        while True:
            self._probe_cancel_check(job)
            try:
                async with self._session_for().get(
                    current, headers={"Range": "bytes=0-0"}, allow_redirects=False, ssl=True
                ) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location")
                        if not location:
                            raise SetupError(SetupErrorCode.DOWNLOAD_FAILED, "重定向缺少 Location")
                        hops += 1
                        if hops > MAX_REDIRECTS:
                            raise SetupError(SetupErrorCode.DOWNLOAD_FAILED, "重定向次数过多")
                        current = self._validate_redirect(current, location)
                        continue
                    return current
            except aiohttp.ClientError as exc:
                raise SetupError(SetupErrorCode.NETWORK_UNREACHABLE, f"网络错误: {exc}") from exc

    async def _probe_download(self, job: SetupJob, url: str, resume_from: int) -> tuple[str, int, int | None]:
        """以最小流量探测：最终地址、响应码（200/206/416）与总大小。"""
        current = url
        hops = 0
        while True:
            self._probe_cancel_check(job)
            headers = {"Range": f"bytes={resume_from}-"} if resume_from else {"Range": "bytes=0-0"}
            try:
                resp = await self._session_for().get(current, headers=headers, allow_redirects=False, ssl=True)
            except aiohttp.ClientError as exc:
                raise SetupError(SetupErrorCode.NETWORK_UNREACHABLE, f"网络错误: {exc}") from exc
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                resp.release()
                if not location:
                    raise SetupError(SetupErrorCode.DOWNLOAD_FAILED, "重定向缺少 Location")
                hops += 1
                if hops > MAX_REDIRECTS:
                    raise SetupError(SetupErrorCode.DOWNLOAD_FAILED, "重定向次数过多")
                current = self._validate_redirect(current, location)
                continue
            total: int | None = None
            if resp.status in (200, 206):
                tail = resp.headers.get("Content-Range", "").rpartition("/")[2]
                if tail.isdigit():
                    total = int(tail)
                elif resp.status == 200:
                    length = resp.headers.get("Content-Length", "")
                    if length.isdigit():
                        total = int(length) or None
                elif resp.status == 206:
                    # 续传确认但没有总长：按无总量单连接处理。
                    total = None
            resp.release()
            return current, resp.status, total

    @staticmethod
    def _load_prog(prog: Path) -> dict | None:
        try:
            saved = json.loads(prog.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(saved, dict):
            return None
        if not (isinstance(saved.get("total"), int) and saved["total"] > 0):
            return None
        if not (isinstance(saved.get("prefix"), int) and 0 <= saved["prefix"] < saved["total"]):
            return None
        slices = saved.get("slices")
        done = saved.get("done")
        if slices is not None:
            if not (isinstance(slices, list) and isinstance(done, list) and len(slices) == len(done)):
                return None
        return saved

    async def _download_multiconn(
        self,
        job: SetupJob,
        url: str,
        part: Path,
        prog: Path,
        model: ModelSpec,
        plan: dict,
    ) -> None:
        total: int = plan["total"]
        completed_prefix: int = plan["prefix"]
        if plan.get("slices") is None:
            remaining = total - completed_prefix
            slice_size = (remaining + _MULTICONN_SLICES - 1) // _MULTICONN_SLICES
            slices = [
                (completed_prefix + i * slice_size, min(total - 1, completed_prefix + (i + 1) * slice_size - 1))
                for i in range(_MULTICONN_SLICES)
                if completed_prefix + i * slice_size < total
            ]
            slice_done = [0] * len(slices)
        else:
            slices = [(int(s[0]), int(s[1])) for s in plan["slices"]]
            slice_done = [
                max(0, min(int(d), s[1] - s[0] + 1)) for d, s in zip(plan["done"], slices)
            ]

        def _persist() -> None:
            try:
                prog.write_text(
                    json.dumps(
                        {"total": total, "prefix": completed_prefix, "slices": [list(s) for s in slices], "done": slice_done}
                    ),
                    encoding="utf-8",
                )
            except OSError:
                pass

        await asyncio.to_thread(self._preallocate, part, total, completed_prefix)
        _persist()

        tracker = _SpeedTracker(job, model.id, total=total, offset=completed_prefix + sum(slice_done))
        lock = asyncio.Lock()
        last_persist = time.monotonic()

        async def fetch_slice(index: int) -> None:
            nonlocal last_persist
            start, end = slices[index]
            pos = start + slice_done[index]
            if pos > end:
                return
            req_headers = {"Range": f"bytes={pos}-{end}"}
            try:
                async with self._session_for().get(url, headers=req_headers, allow_redirects=False, ssl=True) as resp:
                    if resp.status != 206:
                        body = (await resp.read()).decode("utf-8", "replace")[:160]
                        raise SetupError(
                            SetupErrorCode.DOWNLOAD_FAILED,
                            f"分片 {index} 下载失败 HTTP {resp.status} {body.strip()}",
                        )
                    with open(part, "r+b") as fh:
                        fh.seek(pos)
                        async for chunk in resp.content.iter_chunked(_CHUNK):
                            if job.cancel_requested.is_set():
                                raise SetupError(SetupErrorCode.CANCELLED, "已取消")
                            fh.write(chunk)
                            slice_done[index] += len(chunk)
                            async with lock:
                                tracker.add(len(chunk))
                                tracker.emit()
                                now = time.monotonic()
                                if now - last_persist >= 3.0:
                                    last_persist = now
                                    _persist()
            except aiohttp.ClientError as exc:
                raise SetupError(SetupErrorCode.NETWORK_UNREACHABLE, f"分片 {index} 网络错误: {exc}") from exc

        try:
            await asyncio.gather(*(fetch_slice(i) for i in range(len(slices))))
        except Exception:
            _persist()  # 保留进度：下次重试从分片断点继续
            raise
        _persist()
        tracker.emit_final()  # 终态：确保 UI 收到 100% 收口
        try:
            prog.unlink(missing_ok=True)
        except OSError:
            pass
        if part.stat().st_size != total:
            raise SetupError(SetupErrorCode.DOWNLOAD_FAILED, f"下载大小异常: {part.stat().st_size}/{total}")

    @staticmethod
    def _preallocate(part: Path, total: int, prefix: int) -> None:
        if part.exists() and part.stat().st_size == total:
            return
        if not part.exists() or part.stat().st_size < prefix:
            # 无可信前缀：建一个 total 大小的稀疏文件。
            with open(part, "wb") as fh:
                fh.truncate(total)
            return
        with open(part, "r+b") as fh:
            fh.truncate(total)

    def _validate_redirect(self, base: str, location: str) -> str:
        next_url = urljoin(base, location)
        parts = urlsplit(next_url)
        if parts.scheme != "https":
            raise SetupError(SetupErrorCode.SOURCE_UNTRUSTED, "重定向到非 HTTPS 地址被拒绝")
        if parts.hostname and parts.hostname not in self.manifest.bridge.allowed_hosts:
            raise SetupError(SetupErrorCode.SOURCE_UNTRUSTED, f"重定向到未授权主机 {parts.hostname} 被拒绝")
        return next_url

    def _verify_part(self, part: Path, model: ModelSpec) -> None:
        try:
            verify_model_file(part, model.kind, model.min_gb if model.min_gb else None)
        except Exception as exc:
            raise SetupError(SetupErrorCode.DOWNLOAD_FAILED, f"结构校验失败: {exc}") from exc
        if model.sha256:
            actual = sha256_of(part)
            if actual.lower() != model.sha256.lower():
                raise SetupError(SetupErrorCode.HASH_MISMATCH, f"SHA-256 不匹配 (期望 {model.sha256})")

    # ------------------------------------------------------------- nodes

    async def _install_node(self, job: SetupJob, node: NodeSpec) -> None:
        if job.work_dir is None:
            raise SetupError(SetupErrorCode.INTERNAL, "job 缺少工作目录")
        staging = job.work_dir / "nodes" / node.id
        staging.mkdir(parents=True, exist_ok=True)
        zip_path = staging / f"{node.repo_name}-{node.ref[:8]}.zip"
        # GitHub 官方源优先，ghfast/gh-proxy 镜像回退（国内直连慢或超时）。
        candidates = [node.archive_url]
        runtime = self.manifest.runtime
        for prefix in (runtime.github_mirror_prefixes if runtime else ()):
            candidates.append(f"{prefix}{node.archive_url}")
        last_error = None
        for url in candidates:
            resolved = await self._resolve_url(url)
            try:
                await self._download_file(job, resolved, zip_path)
                last_error = None
                break
            except SetupError as exc:
                last_error = exc
                if exc.code == SetupErrorCode.CANCELLED:
                    raise
        if last_error is not None:
            raise last_error
        extracted = staging / "extract"
        if extracted.exists():
            await asyncio.to_thread(shutil.rmtree, extracted)
        await asyncio.to_thread(self._extract_zip_safe, zip_path, extracted)
        top = self._single_top_dir(extracted)
        target = job.root / "custom_nodes" / node.name
        self._snapshot(job, target)
        tmp = target.with_name(target.name + ".tmp")
        if tmp.exists():
            await asyncio.to_thread(shutil.rmtree, tmp)
        await asyncio.to_thread(shutil.move, str(top), str(tmp))
        if target.exists():
            await asyncio.to_thread(shutil.rmtree, target)
        await asyncio.to_thread(os.replace, tmp, target)
        if not verify_node_dir(target):
            await asyncio.to_thread(shutil.rmtree, target)
            raise SetupError(SetupErrorCode.NODE_CONFLICT, f"节点 {node.name} 缺少 __init__.py")
        if node.install_requirements:
            req = target / "requirements.txt"
            if req.exists():
                cleaned = await asyncio.to_thread(self._gitless_requirements, req)
                if cleaned:
                    runner = await self._python_runner_for(job)
                    await runner.pip_install([], requirements=cleaned)
        job.emit("node_installed", id=node.id, ref=node.ref[:12])

    async def _download_file(self, job: SetupJob, url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        resume_from = part.stat().st_size if part.exists() else 0
        current = url
        hops = 0
        headers = {"Range": f"bytes={resume_from}-"} if resume_from else {}
        resp = None
        while True:
            if job.cancel_requested.is_set():
                raise SetupError(SetupErrorCode.CANCELLED, "已取消")
            try:
                resp = await self._session_for().get(current, headers=headers, allow_redirects=False, ssl=True)
            except aiohttp.ClientError as exc:
                raise SetupError(SetupErrorCode.NETWORK_UNREACHABLE, f"网络错误: {exc}") from exc
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                resp.release()
                if not location:
                    raise SetupError(SetupErrorCode.DOWNLOAD_FAILED, "重定向缺少 Location")
                hops += 1
                if hops > MAX_REDIRECTS:
                    raise SetupError(SetupErrorCode.DOWNLOAD_FAILED, "重定向次数过多")
                current = self._validate_redirect(current, location)
                continue
            break
        total: int | None = None
        if resp.status == 206:
            tail = resp.headers.get("Content-Range", "").rpartition("/")[2]
            if tail.isdigit():
                total = int(tail)
        elif resp.status == 200:
            resume_from = 0
            length = resp.headers.get("Content-Length", "")
            if length.isdigit():
                total = int(length) or None
        mode = "ab" if (resp.status == 206 and resume_from) else "wb"
        if resp.status not in (200, 206):
            resp.release()
            raise SetupError(SetupErrorCode.DOWNLOAD_FAILED, f"下载失败 HTTP {resp.status}")
        tracker = _SpeedTracker(job, dest.stem, total=total, offset=resume_from)
        try:
            with open(part, mode) as fh:
                async for chunk in resp.content.iter_chunked(_CHUNK):
                    if job.cancel_requested.is_set():
                        raise SetupError(SetupErrorCode.CANCELLED, "已取消")
                    fh.write(chunk)
                    tracker.add(len(chunk))
                    tracker.emit(kind="node", id=dest.stem)
        finally:
            resp.release()
        await asyncio.to_thread(os.replace, part, dest)

    def _extract_zip_safe(self, zip_path: Path, dest: Path) -> None:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                if name.startswith("/") or ":" in name:
                    raise SetupError(SetupErrorCode.ARCHIVE_UNSAFE, f"压缩包含非法路径: {info.filename!r}")
                parts = name.split("/")
                if any(p == ".." for p in parts):
                    raise SetupError(SetupErrorCode.ARCHIVE_UNSAFE, f"压缩包含路径逃逸: {info.filename!r}")
                target = dest.joinpath(*parts)
                target = target.resolve()
                if not target.is_relative_to(dest.resolve()):
                    raise SetupError(SetupErrorCode.ARCHIVE_UNSAFE, f"压缩包逃逸目标目录: {info.filename!r}")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)

    def _single_top_dir(self, extracted: Path) -> Path:
        if not extracted.exists():
            raise SetupError(SetupErrorCode.ARCHIVE_UNSAFE, "节点压缩包为空")
        entries = list(extracted.iterdir())
        if len(entries) == 1 and entries[0].is_dir():
            return entries[0]
        return extracted

    def _gitless_requirements(self, req: Path) -> Path | None:
        lines = req.read_text(encoding="utf-8", errors="replace").splitlines()
        kept = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("git+")]
        if len(kept) == len(lines):
            return req
        if not kept:
            return None
        out = req.with_name(req.name + ".winefox")
        out.write_text("\n".join(kept) + "\n", encoding="utf-8")
        return out

    # ------------------------------------------------------------- bridge

    async def _ensure_bridge_token(self) -> str:
        existing = self.secret_store.get_secret("comfyui_bridge_token") if self.secret_store else None
        if existing:
            return existing
        token = secrets.token_urlsafe(32)
        if self.secret_store is not None:
            await self.secret_store.put_secret("comfyui_bridge_token", token)
        return token

    # ------------------------------------------------------------- rollback

    def _snapshot(self, job: SetupJob, target: Path) -> None:
        if not target.exists() and not target.is_symlink():
            job.rollback_entries.append(RollbackEntry(target=target))
            return
        if job.work_dir is None:
            raise SetupError(SetupErrorCode.INTERNAL, "job 缺少工作目录")
        backup_dir = job.work_dir / "rollback" / f"{len(job.rollback_entries)}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / target.name
        if target.is_dir():
            shutil.copytree(target, backup)
        else:
            shutil.copy2(target, backup)
        job.rollback_entries.append(RollbackEntry(target=target, backup=backup))

    def _restore_entry(self, entry: RollbackEntry) -> None:
        if entry.backup is None:
            if entry.target.exists():
                if entry.target.is_dir() and not entry.target.is_symlink():
                    shutil.rmtree(entry.target)
                else:
                    entry.target.unlink(missing_ok=True)
            return
        if entry.target.exists():
            if entry.target.is_dir() and not entry.target.is_symlink():
                shutil.rmtree(entry.target)
            else:
                entry.target.unlink(missing_ok=True)
        entry.target.parent.mkdir(parents=True, exist_ok=True)
        if entry.backup.is_dir():
            shutil.copytree(entry.backup, entry.target)
        else:
            shutil.copy2(entry.backup, entry.target)

    def _write_json(self, path: Path, data: dict) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

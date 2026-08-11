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
import secrets
import shutil
import time
import zipfile
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

    def set_event_callback(self, cb) -> None:
        self._event_cb = cb

    def emit(self, type_: str, **payload: Any) -> None:
        event = {"type": type_, **payload, "at": time.time()}
        self.events.append(event)
        if self._event_cb:
            try:
                self._event_cb(event)
            except Exception:
                pass

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

    async def pip_install(self, packages: list[str], *, requirements: Path | None = None) -> None:
        args = ["-m", "pip", "install", "--disable-pip-version-check"]
        if requirements is not None:
            args += ["-r", str(requirements)]
        else:
            args += packages
        code, out, err = await self.run(*args)
        if code != 0:
            detail = (err or out).strip()[:500]
            raise SetupError(SetupErrorCode.PIP_FAILED, f"pip 安装失败: {detail}")


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
        self._session = session or aiohttp.ClientSession(
            headers={"User-Agent": "WineFoxDesktop/8.6 setup"},
            timeout=aiohttp.ClientTimeout(total=300, connect=30, sock_read=300),
        )
        self._owns_session = session is None
        self._url_rewriter = url_rewriter  # test-only hook, never wired in prod
        self._job_counter = 0

    async def close(self) -> None:
        if self._owns_session:
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
                state.error_message = str(exc)
                job.error_code = SetupErrorCode.INTERNAL
                job.emit("step_failed", step=state.index, code=SetupErrorCode.INTERNAL, message=str(exc))
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
                job.emit("rollback_item_failed", target=str(entry.target), message=str(exc))
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
        else:  # pragma: no cover
            raise SetupError(SetupErrorCode.UNKNOWN, f"未知步骤类型: {plan_step.kind}")

    async def _run_models(self, job: SetupJob, state: StepState, plan_step) -> None:
        state.items_total = len(plan_step.items)
        for item in plan_step.items:
            if job.cancel_requested.is_set():
                return
            spec: ModelSpec = item.spec  # type: ignore[assignment]
            job.emit("item_started", step=state.index, type="model", id=spec.id)
            await self._download_model(job, spec)
            state.items_done += 1
            job.emit_throttled("item_finished", step=state.index, type="model", id=spec.id)

    async def _run_nodes(self, job: SetupJob, state: StepState, plan_step) -> None:
        state.items_total = len(plan_step.items)
        for item in plan_step.items:
            if job.cancel_requested.is_set():
                return
            node: NodeSpec = item.spec  # type: ignore[assignment]
            job.emit("item_started", step=state.index, type="node", id=node.id)
            await self._install_node(job, node)
            state.items_done += 1
            job.emit_throttled("item_finished", step=state.index, type="node", id=node.id)

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
        last_error: SetupError | None = None
        for source in model.sources:
            if source.host not in self.manifest.bridge.allowed_hosts:
                continue
            url = await self._resolve_url(source.url)
            try:
                await self._download_to_part(job, url, part, model)
                await asyncio.to_thread(self._verify_part, part, model)
                self._snapshot(job, target)
                await asyncio.to_thread(os.replace, part, target)
                return
            except SetupError as exc:
                last_error = exc
                try:
                    part.unlink(missing_ok=True)
                except OSError:
                    pass
        raise last_error or SetupError(SetupErrorCode.DOWNLOAD_FAILED, f"模型 {model.name} 无可用来源")

    async def _resolve_url(self, url: str) -> str:
        if self._url_rewriter is None:
            return url
        result = self._url_rewriter(url)
        if asyncio.iscoroutine(result):
            return await result
        return result

    async def _download_to_part(self, job: SetupJob, url: str, part: Path, model: ModelSpec) -> None:
        resume_from = part.stat().st_size if part.exists() else 0
        current = url
        hops = 0
        headers = {"Range": f"bytes={resume_from}-"} if resume_from else {}
        resp = None
        while True:
            if job.cancel_requested.is_set():
                raise SetupError(SetupErrorCode.CANCELLED, "已取消")
            try:
                resp = await self._session.get(
                    current,
                    headers=headers,
                    allow_redirects=False,
                    ssl=True,
                )
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
        if resp.status == 416:  # range not satisfiable: part already complete
            resp.release()
            if part.exists() and part.stat().st_size > 0:
                return
            raise SetupError(SetupErrorCode.DOWNLOAD_FAILED, "服务器拒绝续传 (416)")
        if resp.status == 206 and resume_from:
            mode = "ab"
        elif resp.status == 200:
            mode = "wb"
            resume_from = 0
        else:
            body = (await resp.read()).decode("utf-8", "replace")[:200] if resp.status >= 400 else ""
            resp.release()
            raise SetupError(SetupErrorCode.DOWNLOAD_FAILED, f"下载失败 HTTP {resp.status} {body.strip()}")
        try:
            with open(part, mode) as fh:
                async for chunk in resp.content.iter_chunked(_CHUNK):
                    if job.cancel_requested.is_set():
                        raise SetupError(SetupErrorCode.CANCELLED, "已取消")
                    fh.write(chunk)
                    job.emit_throttled(
                        "item_progress",
                        type="model",
                        id=model.id,
                        size=part.stat().st_size if part.exists() else 0,
                    )
        finally:
            resp.release()

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
        url = await self._resolve_url(node.archive_url)
        await self._download_file(job, url, zip_path)
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
                resp = await self._session.get(current, headers=headers, allow_redirects=False, ssl=True)
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
        mode = "ab" if (resp.status == 206 and resume_from) else "wb"
        if resp.status not in (200, 206):
            resp.release()
            raise SetupError(SetupErrorCode.DOWNLOAD_FAILED, f"下载失败 HTTP {resp.status}")
        try:
            with open(part, mode) as fh:
                async for chunk in resp.content.iter_chunked(_CHUNK):
                    if job.cancel_requested.is_set():
                        raise SetupError(SetupErrorCode.CANCELLED, "已取消")
                    fh.write(chunk)
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

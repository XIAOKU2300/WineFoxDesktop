"""Read-only environment scanner and install planner for the environment center.

The scanner maps the old BAT's check logic (node presence, model presence +
size, Bridge presence, Python candidates) onto a :class:`EnvironmentStatus`.
The planner turns a profile / custom step set / repair / retry request into an
ordered :class:`Plan` the executor (stage 3) can run.

All methods are synchronous and filesystem-only; callers must wrap the
service in ``asyncio.to_thread`` so the aiohttp event loop never blocks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .setup_errors import SetupErrorCode
from .setup_manifest import ModelSpec, NodeSpec, SetupManifest
from .setup_paths import (
    find_candidates,
    safe_comfyui_path,
    test_root,
    validate_comfyui_root,
)
from .setup_python import PythonProbe, resolve_python_executable
from .resource_seed import parse_bridge_version

_GB = 1024 ** 3

#: Every node install lands under custom_nodes/<name>.
NODE_DIR_FIELD = "name"


@dataclass(frozen=True)
class RootCandidate:
    path: Path
    kind: str  # "base" | "comfyui" | "portable" | "launcher" | "subdir" | "injected"
    priority: int  # lower wins

    @property
    def display(self) -> str:
        parts = self.path.parts
        return "/".join(parts[-2:]) if len(parts) >= 2 else self.path.name

    def to_dict(self) -> dict[str, object]:
        return {"name": self.path.name, "display": self.display, "kind": self.kind, "priority": self.priority}


@dataclass
class NodeState:
    id: str
    present: bool
    version: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {"id": self.id, "present": self.present, "version": self.version}


@dataclass
class ModelState:
    id: str
    present: bool
    corrupt: bool
    size_bytes: int | None
    min_gb: float

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "present": self.present,
            "corrupt": self.corrupt,
            "size_bytes": self.size_bytes,
            "min_gb": self.min_gb,
        }


@dataclass
class BridgeState:
    installed: bool = False
    version: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {"installed": self.installed, "version": self.version}


@dataclass
class ManualState:
    name: str
    present: bool

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "present": self.present}


@dataclass
class EnvironmentStatus:
    candidates: list[RootCandidate] = field(default_factory=list)
    root: Path | None = None
    nodes: dict[str, NodeState] = field(default_factory=dict)
    models: dict[str, ModelState] = field(default_factory=dict)
    bridge: BridgeState = field(default_factory=BridgeState)
    manual: list[ManualState] = field(default_factory=list)
    python: PythonProbe = field(default_factory=lambda: PythonProbe(found=False))

    @property
    def state_code(self) -> str:
        if not self.root:
            return "no_root"
        missing = any(not n.present for n in self.nodes.values()) or any(
            (not m.present) or m.corrupt for m in self.models.values()
        ) or not self.bridge.installed or any(not mf.present for mf in self.manual)
        return "repair_needed" if missing else "ready"

    @property
    def missing_model_ids(self) -> list[str]:
        return [m.id for m in self.models.values() if not m.present or m.corrupt]

    @property
    def missing_node_ids(self) -> list[str]:
        return [n.id for n in self.nodes.values() if not n.present]

    def to_dict(self) -> dict[str, object]:
        python = self.python.to_dict()
        if python.get("executable"):  # never leak absolute paths across the API
            python["executable"] = Path(python["executable"]).name
        return {
            "state_code": self.state_code,
            "candidates": [c.to_dict() for c in self.candidates],
            "root": {"name": self.root.name, "display": _root_display(self.root)} if self.root else None,
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "models": [m.to_dict() for m in self.models.values()],
            "bridge": self.bridge.to_dict(),
            "manual": [m.to_dict() for m in self.manual],
            "python": python,
        }


def _root_display(root: Path) -> str:
    parts = root.parts
    return "/".join(parts[-2:]) if len(parts) >= 2 else root.name


class Scanner:
    """Read-only filesystem scan of ComfyUI roots, nodes, models and Bridge."""

    def __init__(self, manifest: SetupManifest, *, install_dir: Path | None = None) -> None:
        self.manifest = manifest
        self.install_dir = install_dir

    def find_candidates(self, bases: Iterable[Path | str] = ()) -> list[RootCandidate]:
        base_list = list(bases)
        if not base_list and self.install_dir is not None:
            base_list = [self.install_dir.parent]
        if not base_list:
            base_list = [Path.cwd().parent]
        paths = find_candidates(base_list, install_dir=self.install_dir)
        candidates: list[RootCandidate] = []
        for idx, path in enumerate(paths):
            kind = "subdir" if idx >= 4 else "base"  # coarse; refined below
            candidates.append(RootCandidate(path=path, kind=kind, priority=idx))
        return candidates

    def detect_nodes(self, root: Path) -> dict[str, NodeState]:
        states: dict[str, NodeState] = {}
        custom_nodes = root / "custom_nodes"
        for node in self.manifest.nodes.values():
            target = custom_nodes / node.name
            present = target.is_dir() and (target / "__init__.py").is_file()
            version = None
            if present:
                init = target / "__init__.py"
                try:
                    version = parse_bridge_version(init) if node.name == self.manifest.bridge.dest_folder else None
                except Exception:
                    version = None
            states[node.id] = NodeState(id=node.id, present=present, version=version)
        return states

    def detect_bridge(self, root: Path) -> BridgeState:
        target = root / "custom_nodes" / self.manifest.bridge.dest_folder
        init = target / "__init__.py"
        if not (target.is_dir() and init.is_file()):
            return BridgeState(installed=False)
        try:
            version = parse_bridge_version(init)
        except Exception:
            version = None
        return BridgeState(installed=True, version=version)

    def detect_manual(self, root: Path) -> list[ManualState]:
        states: list[ManualState] = []
        for mf in self.manifest.manual_files:
            path = safe_comfyui_path(root, mf.dest)
            states.append(ManualState(name=mf.name, present=path.is_file()))
        return states

    def detect_models(self, root: Path) -> dict[str, ModelState]:
        states: dict[str, ModelState] = {}
        for model in self.manifest.models.values():
            path = safe_comfyui_path(root, model.dest)
            present = path.is_file()
            size_bytes = path.stat().st_size if present else None
            expected = int(model.min_gb * _GB)
            corrupt = present and size_bytes is not None and size_bytes < expected
            states[model.id] = ModelState(
                id=model.id, present=present, corrupt=corrupt, size_bytes=size_bytes, min_gb=model.min_gb
            )
        return states

    def scan(self, *, bases: Iterable[Path | str] = (), injected_roots: Iterable[Path | str] = ()) -> EnvironmentStatus:
        candidates = self.find_candidates(bases)
        for injected in injected_roots:
            try:
                resolved = validate_comfyui_root(injected, install_dir=self.install_dir)
            except Exception:
                continue
            candidates.append(RootCandidate(path=resolved, kind="injected", priority=-1))
        candidates.sort(key=lambda c: (c.priority, c.path.as_posix()))
        status = EnvironmentStatus(candidates=candidates)
        if candidates:
            status.root = candidates[0].path
            root = status.root
            status.nodes = self.detect_nodes(root)
            status.models = self.detect_models(root)
            status.bridge = self.detect_bridge(root)
            status.manual = self.detect_manual(root)
            status.python = resolve_python_executable(root, self.manifest.python)
        else:
            status.python = PythonProbe(
                found=False,
                error_code=SetupErrorCode.PYTHON_NOT_FOUND,
                error_message="未发现 ComfyUI 根目录",
            )
        return status


@dataclass(frozen=True)
class PlanItem:
    kind: str  # "model" | "node"
    spec: ModelSpec | NodeSpec

    def to_dict(self) -> dict[str, object]:
        if self.kind == "model":
            m: ModelSpec = self.spec  # type: ignore[assignment]
            return {
                "type": "model",
                "id": m.id,
                "name": m.name,
                "dest": m.dest,
                "min_gb": m.min_gb,
                "sources": [s.url for s in m.sources],
            }
        n: NodeSpec = self.spec  # type: ignore[assignment]
        return {
            "type": "node",
            "id": n.id,
            "name": n.name,
            "repo": n.repo,
            "ref": n.ref,
            "archive_url": n.archive_url,
            "install_requirements": n.install_requirements,
        }


@dataclass
class PlanStep:
    kind: str  # "nodes" | "models" | "bridge" | "pip"
    group: str | None
    label: str
    items: list[PlanItem] = field(default_factory=list)
    pip_packages: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"kind": self.kind, "group": self.group, "label": self.label}
        if self.kind == "pip":
            result["packages"] = list(self.pip_packages)
        else:
            result["items"] = [i.to_dict() for i in self.items]
        return result


@dataclass
class Plan:
    root: Path | None
    profile: str
    steps: list[PlanStep] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "root": _root_display(self.root) if self.root else None,
            "profile": self.profile,
            "steps": [s.to_dict() for s in self.steps],
        }


class Planner:
    """Builds ordered install plans from a status and a profile/custom choice."""

    def __init__(self, manifest: SetupManifest) -> None:
        self.manifest = manifest

    def plan_for_profile(self, status: EnvironmentStatus, profile: str, *, only_missing: bool = False) -> Plan:
        if profile not in self.manifest.tiers:
            raise ValueError(f"unknown profile {profile!r}")
        steps: list[PlanStep] = []
        for tier_step in self.manifest.tiers[profile]:
            if tier_step.step == "bridge":
                if only_missing and status.bridge.installed:
                    continue
                steps.append(PlanStep(kind="bridge", group=None, label="安装 LoraBridge"))
            elif tier_step.step == "nodes":
                nodes = self.manifest.nodes_in_group(tier_step.group)
                if only_missing:
                    nodes = [n for n in nodes if not status.nodes.get(n.id, NodeState(n.id, False)).present]
                items = [PlanItem(kind="node", spec=n) for n in nodes]
                if items:
                    pip = self.manifest.node_groups[tier_step.group].pip_packages
                    steps.append(
                        PlanStep(kind="nodes", group=tier_step.group, label=f"安装节点组 {tier_step.group}", items=items, pip_packages=pip)
                    )
            elif tier_step.step == "models":
                models = self.manifest.models_in_group(tier_step.group)
                if only_missing:
                    models = [m for m in models if _model_missing(status, m.id)]
                if models:
                    steps.append(
                        PlanStep(
                            kind="models",
                            group=tier_step.group,
                            label=f"下载模型组 {tier_step.group}",
                            items=[PlanItem(kind="model", spec=m) for m in models],
                        )
                    )
        return Plan(root=status.root, profile=profile, steps=steps)

    def plan_for_custom(self, status: EnvironmentStatus, steps: list[tuple[str, str | None]], *, only_missing: bool = False) -> Plan:
        """steps: list of ("nodes"|"models"|"bridge", group-or-None)."""
        plan_steps: list[PlanStep] = []
        for kind, group in steps:
            if kind == "bridge":
                if only_missing and status.bridge.installed:
                    continue
                plan_steps.append(PlanStep(kind="bridge", group=None, label="安装 LoraBridge"))
                continue
            if kind == "nodes":
                nodes = self.manifest.nodes_in_group(group or "")
                if only_missing:
                    nodes = [n for n in nodes if not status.nodes.get(n.id, NodeState(n.id, False)).present]
                items = [PlanItem(kind="node", spec=n) for n in nodes]
                pip = self.manifest.node_groups.get(group or "", self.manifest.node_groups["minimal"]).pip_packages if items else ()
                if items:
                    plan_steps.append(PlanStep(kind="nodes", group=group, label=f"安装节点组 {group}", items=items, pip_packages=pip))
            elif kind == "models":
                models = self.manifest.models_in_group(group or "")
                if only_missing:
                    models = [m for m in models if _model_missing(status, m.id)]
                if models:
                    plan_steps.append(
                        PlanStep(kind="models", group=group, label=f"下载模型组 {group}", items=[PlanItem(kind="model", spec=m) for m in models])
                    )
        return Plan(root=status.root, profile="custom", steps=plan_steps)

    def plan_for_repair(self, status: EnvironmentStatus) -> Plan:
        steps: list[PlanStep] = []
        for group in self.manifest.node_groups:
            missing = [n for n in self.manifest.nodes_in_group(group) if not status.nodes.get(n.id, NodeState(n.id, False)).present]
            if missing:
                pip = self.manifest.node_groups[group].pip_packages
                steps.append(
                    PlanStep(kind="nodes", group=group, label=f"修复节点组 {group}", items=[PlanItem(kind="node", spec=n) for n in missing], pip_packages=pip)
                )
        for group in self.manifest.model_groups:
            missing = [m for m in self.manifest.models_in_group(group) if _model_missing(status, m.id)]
            if missing:
                steps.append(
                    PlanStep(kind="models", group=group, label=f"修复模型组 {group}", items=[PlanItem(kind="model", spec=m) for m in missing])
                )
        if not status.bridge.installed:
            steps.append(PlanStep(kind="bridge", group=None, label="安装 LoraBridge"))
        return Plan(root=status.root, profile="repair", steps=steps)

    def plan_for_retry(self, status: EnvironmentStatus, failed_step_indexes: list[int], base: Plan) -> Plan:
        kept: list[PlanStep] = []
        for idx, step in enumerate(base.steps):
            if idx in failed_step_indexes:
                kept.append(step)
        return Plan(root=status.root, profile=f"retry:{base.profile}", steps=kept)


def _model_missing(status: EnvironmentStatus, model_id: str) -> bool:
    state = status.models.get(model_id)
    if state is None:
        return True
    return not state.present or state.corrupt

"""Load and validate the ComfyUI environment setup manifest.

The manifest is the single source of truth for what the environment center can
install: models (with allow-listed hosts and min-size checks), pinned custom
nodes, the LoraBridge bundle, Python probing rules and install tiers.  It is
ported item-by-item from ``scripts/astrbot_comfyui_setup.bat`` (see
``docs/desktop_setup_migration.md`` section 3).

This module is a leaf: it never imports the engine/runtime so it can be used
freely by the backend handlers and the executor without pulling the whole
sidecar in.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_COMMIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_MODEL_KINDS = {"safetensors", "gguf", "pt", "ckpt", "pth", "bin"}
_REF_KINDS = {"commit", "tag"}
_BRIDGE_CONFIG_KEYS = {
    "token",
    "chunk_size_mb",
    "connect_timeout",
    "sock_read_timeout",
    "fetch_timeout",
    "max_concurrent_downloads",
    "proxy_cooldown",
    "proxy",
}


class ManifestError(ValueError):
    """Raised when the setup manifest is missing, malformed or unsafe."""

    def __init__(self, message: str, *, code: str = "SETUP_CONFIG_INVALID") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ModelSource:
    host: str
    url: str


@dataclass(frozen=True)
class ModelSpec:
    id: str
    name: str
    kind: str
    dest: str  # relative to ComfyUI root, forward-slash separated
    min_gb: float
    sha256: str  # "" == verify dynamically after download (see migration doc 3.7)
    sources: tuple[ModelSource, ...]
    group: str

    @property
    def dest_path(self) -> Path:
        return Path(*self.dest.split("/"))


@dataclass(frozen=True)
class NodeSpec:
    id: str
    name: str
    repo: str  # owner/name
    ref: str  # pinned commit SHA or tag
    ref_kind: str
    install_requirements: bool

    @property
    def owner(self) -> str:
        return self.repo.split("/", 1)[0]

    @property
    def repo_name(self) -> str:
        return self.repo.split("/", 1)[1]

    @property
    def archive_url(self) -> str:
        """GitHub archive URL pinned to an exact ref (never a moving branch)."""
        return f"https://github.com/{self.repo}/archive/{self.ref}.zip"


@dataclass(frozen=True)
class NodeGroup:
    name: str
    nodes: tuple[str, ...]
    pip_packages: tuple[str, ...] = ()


@dataclass(frozen=True)
class TierStep:
    step: str  # "nodes" | "models" | "bridge"
    group: str | None = None


@dataclass(frozen=True)
class ManualFile:
    name: str
    dest: str
    note: str = ""


@dataclass(frozen=True)
class BridgeSpec:
    source: str  # packaged resource relative path
    dest_folder: str
    version: str
    allow_local_requests_without_token: bool
    allowed_hosts: tuple[str, ...]
    config_defaults: dict[str, Any]


@dataclass(frozen=True)
class PythonSpec:
    min_version: tuple[int, int]
    probe_requires_pip: bool
    candidate_priority: tuple[str, ...]


@dataclass(frozen=True)
class RuntimePythonEmbed:
    version: str
    install_dir: str
    get_pip_url: str
    sources: tuple[ModelSource, ...]


@dataclass(frozen=True)
class RuntimeSpec:
    python_embed: RuntimePythonEmbed | None
    comfyui_sources: tuple[ModelSource, ...]
    pip_index_urls: tuple[str, ...]
    torch_packages: tuple[str, ...]
    torch_index_by_cuda: dict[str, str]
    torch_cpu_index: str


@dataclass(frozen=True)
class SetupManifest:
    format_version: int
    manifest_version: str
    bridge: BridgeSpec
    python: PythonSpec
    directories: dict[str, str]
    model_groups: tuple[str, ...]
    models: dict[str, ModelSpec]
    node_groups: dict[str, NodeGroup]
    nodes: dict[str, NodeSpec]
    tiers: dict[str, tuple[TierStep, ...]]
    manual_files: tuple[ManualFile, ...]
    runtime: RuntimeSpec | None = None

    def models_in_group(self, group: str) -> list[ModelSpec]:
        return [m for m in self.models.values() if m.group == group]

    def nodes_in_group(self, group: str) -> list[NodeSpec]:
        node_group = self.node_groups.get(group)
        if node_group is None:
            return []
        return [self.nodes[i] for i in node_group.nodes]

    def tier_steps(self, tier: str) -> tuple[TierStep, ...]:
        return self.tiers[tier]


def resource_root() -> Path:
    frozen = getattr(sys, "_MEIPASS", None)
    return Path(frozen).resolve() if frozen else Path(__file__).resolve().parent.parent


def _resource_path(name: str) -> Path:
    root = resource_root()
    candidates = (root / "plugin_data" / name, root / name)
    return next((path for path in candidates if path.exists()), candidates[0])


def default_manifest_path() -> Path:
    return _resource_path("resources/comfyui_setup_manifest.v1.json")


def _require(cond: bool, message: str, code: str = "SETUP_CONFIG_INVALID") -> None:
    if not cond:
        raise ManifestError(message, code=code)


def _safe_relative_path(value: str, what: str) -> str:
    if not value or "\x00" in value:
        _require(False, f"{what}: empty or contains NUL byte")
    if value.startswith(("/", "\\")) or ":" in value.split("/")[0]:
        _require(False, f"{what} must be relative: {value!r}")
    parts = value.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        _require(False, f"{what} escapes destination: {value!r}")
    if any("\x00" in part for part in parts):
        _require(False, f"{what} contains NUL byte: {value!r}")
    return value


def _parse_models(raw: list[dict], model_groups: set[str]) -> dict[str, ModelSpec]:
    models: dict[str, ModelSpec] = {}
    for entry in raw:
        mid = str(entry["id"])
        group = str(entry["group"])
        _require(mid not in models, f"duplicate model id {mid!r}")
        _require(group in model_groups, f"model {mid!r} references unknown group {group!r}")
        kind = str(entry["kind"])
        _require(kind in _MODEL_KINDS, f"model {mid!r}: unsupported kind {kind!r}")
        dest = _safe_relative_path(str(entry["dest"]), f"model {mid!r} dest")
        sha256 = str(entry.get("sha256", ""))
        _require(sha256 == "" or _SHA256_RE.match(sha256), f"model {mid!r}: bad sha256 {sha256!r}")
        min_gb = float(entry["min_gb"])
        _require(min_gb >= 0, f"model {mid!r}: min_gb must be >= 0")
        sources_raw = entry["sources"]
        _require(len(sources_raw) >= 1, f"model {mid!r}: no sources")
        sources: list[ModelSource] = []
        for s in sources_raw:
            host = str(s["host"])
            url = str(s["url"])
            _require(host and url, f"model {mid!r}: source needs host+url")
            _require(
                urlsplit(url).hostname == host,
                f"model {mid!r}: url host does not match declared host {host!r}",
            )
            sources.append(ModelSource(host=host, url=url))
        models[mid] = ModelSpec(
            id=mid,
            name=str(entry["name"]),
            kind=kind,
            dest=dest,
            min_gb=min_gb,
            sha256=sha256,
            sources=tuple(sources),
            group=group,
        )
    _require(len(models) == len(raw), "model ids must be unique")
    return models


def _parse_nodes(raw: list[dict]) -> dict[str, NodeSpec]:
    nodes: dict[str, NodeSpec] = {}
    for entry in raw:
        nid = str(entry["id"])
        _require(nid not in nodes, f"duplicate node id {nid!r}")
        ref = str(entry["ref"])
        ref_kind = str(entry.get("ref_kind", "commit"))
        _require(ref, f"node {nid!r} must pin a ref (never a moving branch)")
        _require(ref_kind in _REF_KINDS, f"node {nid!r}: unsupported ref_kind {ref_kind!r}")
        if ref_kind == "commit":
            _require(_COMMIT_SHA_RE.match(ref), f"node {nid!r}: pinned commit must be a full SHA")
        repo = str(entry["repo"])
        _require(
            "/" in repo and " " not in repo,
            f"node {nid!r}: repo must be owner/name without spaces",
        )
        nodes[nid] = NodeSpec(
            id=nid,
            name=str(entry["name"]),
            repo=repo,
            ref=ref,
            ref_kind=ref_kind,
            install_requirements=bool(entry.get("install_requirements", False)),
        )
    return nodes


def _parse_node_groups(raw: dict, nodes: dict[str, NodeSpec]) -> dict[str, NodeGroup]:
    groups: dict[str, NodeGroup] = {}
    for name, entry in raw.items():
        node_ids = [str(i) for i in entry["nodes"]]
        for nid in node_ids:
            _require(nid in nodes, f"node group {name!r} references unknown node {nid!r}")
        groups[name] = NodeGroup(
            name=name,
            nodes=tuple(node_ids),
            pip_packages=tuple(str(p) for p in entry.get("pip_packages", [])),
        )
    return groups


def _parse_tiers(raw: dict, model_groups: set[str], node_groups: set[str]) -> dict[str, tuple[TierStep, ...]]:
    tiers: dict[str, tuple[TierStep, ...]] = {}
    for name, steps_raw in raw.items():
        steps: list[TierStep] = []
        for item in steps_raw:
            step = str(item["step"])
            if step == "bridge":
                _require("group" not in item, f"tier {name!r}: bridge step must not carry a group")
                steps.append(TierStep(step="bridge"))
                continue
            if step in {"comfyui", "python_runtime", "requirements"}:
                _require("group" not in item, f"tier {name!r}: {step} step must not carry a group")
                steps.append(TierStep(step=step))
                continue
            group = str(item.get("group", ""))
            if step == "nodes":
                _require(group in node_groups, f"tier {name!r}: unknown node group {group!r}")
            elif step == "models":
                _require(group in model_groups, f"tier {name!r}: unknown model group {group!r}")
            else:
                raise ManifestError(f"tier {name!r}: unknown step {step!r}")
            steps.append(TierStep(step=step, group=group))
        _require(len(steps) >= 1, f"tier {name!r} is empty")
        tiers[name] = tuple(steps)
    _require(len(tiers) >= 1, "manifest declares no tiers")
    return tiers


def validate_manifest(data: dict[str, Any]) -> None:
    _require(data.get("format_version") == 1, "unsupported manifest format_version")
    bridge_raw = data.get("bridge")
    _require(isinstance(bridge_raw, dict), "manifest missing bridge section")
    allowed_hosts = tuple(str(h) for h in bridge_raw.get("allowed_hosts", []))
    _require(len(allowed_hosts) >= 1, "bridge allowed_hosts must be non-empty")

    # Every download URL host must be allow-listed (supply-chain guard).
    for model in data.get("models", []):
        for source in model["sources"]:
            host = source["host"]
            _require(host in allowed_hosts, f"host {host!r} not in bridge allowed_hosts")

    defaults = bridge_raw.get("config_defaults", {})
    _require(
        set(defaults.keys()) == _BRIDGE_CONFIG_KEYS,
        "bridge config_defaults must match the old BAT defaults exactly "
        f"(expected {sorted(_BRIDGE_CONFIG_KEYS)})",
    )

    dirs = data.get("directories", {})
    for key, value in dirs.items():
        _require(key, "directory alias with empty key")
        _safe_relative_path(str(value), f"directory {key!r}")

    python_raw = data.get("python", {})
    min_ver = tuple(python_raw.get("min_version", (3, 10)))
    _require(len(min_ver) == 2 and all(isinstance(v, int) for v in min_ver), "python.min_version must be [major, minor]")
    _require(python_raw.get("candidate_priority"), "python.candidate_priority must be non-empty")

    runtime_raw = data.get("runtime")
    if runtime_raw is not None:
        _require(isinstance(runtime_raw, dict), "runtime must be an object")
        embed_raw = runtime_raw.get("python_embed")
        if embed_raw is not None:
            for s in embed_raw.get("sources", []):
                _require(
                    s["host"] in allowed_hosts,
                    f"runtime.python_embed host {s['host']!r} not in bridge allowed_hosts",
                )
        for s in runtime_raw.get("comfyui", {}).get("sources", []):
            _require(
                s["host"] in allowed_hosts,
                f"runtime.comfyui host {s['host']!r} not in bridge allowed_hosts",
            )
        get_pip = str(runtime_raw.get("python_embed", {}).get("get_pip_url", ""))
        if get_pip:
            host = urlsplit(get_pip).hostname or ""
            _require(host in allowed_hosts, f"runtime get_pip host {host!r} not in allowed_hosts")


def parse_manifest(data: dict[str, Any]) -> SetupManifest:
    validate_manifest(data)
    model_groups = tuple(str(g) for g in data.get("model_groups", []))
    models = _parse_models(data.get("models", []), set(model_groups))
    nodes = _parse_nodes(data.get("nodes", []))
    node_groups = _parse_node_groups(data.get("node_groups", {}), nodes)
    tiers = _parse_tiers(
        data.get("tiers", {}),
        set(model_groups),
        set(node_groups.keys()),
    )
    bridge_raw = data["bridge"]
    bridge = BridgeSpec(
        source=_safe_relative_path(str(bridge_raw["source"]), "bridge source"),
        dest_folder=_safe_relative_path(str(bridge_raw["dest_folder"]), "bridge dest_folder"),
        version=str(bridge_raw.get("version", "")),
        allow_local_requests_without_token=bool(bridge_raw.get("allow_local_requests_without_token", False)),
        allowed_hosts=tuple(str(h) for h in bridge_raw.get("allowed_hosts", [])),
        config_defaults=dict(bridge_raw.get("config_defaults", {})),
    )
    python_raw = data["python"]
    python = PythonSpec(
        min_version=tuple(python_raw["min_version"]),
        probe_requires_pip=bool(python_raw.get("probe_requires_pip", True)),
        candidate_priority=tuple(str(c) for c in python_raw.get("candidate_priority", [])),
    )
    manual_files = tuple(
        ManualFile(name=str(m["name"]), dest=str(m["dest"]), note=str(m.get("note", "")))
        for m in data.get("manual_files", [])
    )
    runtime: RuntimeSpec | None = None
    runtime_raw = data.get("runtime")
    if isinstance(runtime_raw, dict):
        embed_raw = runtime_raw.get("python_embed") or {}
        python_embed = None
        if embed_raw:
            python_embed = RuntimePythonEmbed(
                version=str(embed_raw.get("version", "")),
                install_dir=_safe_relative_path(str(embed_raw.get("install_dir", "python_embeded")), "runtime.python_embed.install_dir"),
                get_pip_url=str(embed_raw.get("get_pip_url", "")),
                sources=tuple(
                    ModelSource(host=str(s["host"]), url=str(s["url"])) for s in embed_raw.get("sources", [])
                ),
            )
        torch_raw = runtime_raw.get("torch") or {}
        runtime = RuntimeSpec(
            python_embed=python_embed,
            comfyui_sources=tuple(
                ModelSource(host=str(s["host"]), url=str(s["url"]))
                for s in (runtime_raw.get("comfyui") or {}).get("sources", [])
            ),
            pip_index_urls=tuple(str(u) for u in (runtime_raw.get("pip") or {}).get("index_urls", [])),
            torch_packages=tuple(str(p) for p in torch_raw.get("packages", [])),
            torch_index_by_cuda={
                str(k): str(v) for k, v in (torch_raw.get("index_by_cuda") or {}).items()
            },
            torch_cpu_index=str(torch_raw.get("cpu_index", "")),
        )
    return SetupManifest(
        format_version=int(data["format_version"]),
        manifest_version=str(data.get("manifest_version", "")),
        bridge=bridge,
        python=python,
        directories=dict(data.get("directories", {})),
        model_groups=model_groups,
        models=models,
        node_groups=node_groups,
        nodes=nodes,
        tiers=tiers,
        manual_files=manual_files,
        runtime=runtime,
    )


def load_setup_manifest(path: Path | None = None) -> SetupManifest:
    manifest_path = path or default_manifest_path()
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ManifestError(f"setup manifest not found: {manifest_path}", code="SETUP_CONFIG_INVALID")
    except json.JSONDecodeError as exc:
        raise ManifestError(f"setup manifest is invalid JSON: {exc}", code="SETUP_CONFIG_INVALID") from exc
    return parse_manifest(raw)

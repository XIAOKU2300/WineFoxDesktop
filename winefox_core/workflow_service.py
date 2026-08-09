# -*- coding: utf-8 -*-
"""WorkflowService —— 平台无关的 ComfyUI 工作流注册与校验服务（WineFox Desktop）。

从 AstrBot 插件 main.py / workflow/*.json 提炼：扫描、稳定 ID/SHA256、
模型族与 txt2img/img2img/video 识别、节点校验、内置/自定义双 registry。

约定：
- 稳定 ID：对规范化 JSON（key 排序、紧凑序列化）取 SHA256 —— 格式变化不影响；
- SHA256：原始文件字节哈希（完整性校验）；
- 自定义工作流永不覆盖内置 registry；内置新版以独立条目进入 registry。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 模型族
FAMILY_ANIMA = "anima"
FAMILY_ILLUSTRIOUS = "illustrious"
FAMILY_WAN = "wan"
FAMILY_GENERIC = "generic"
FAMILY_UNKNOWN = "unknown"

#: 模式
MODE_TXT2IMG = "txt2img"
MODE_IMG2IMG = "img2img"
MODE_VIDEO = "video"
MODE_UNKNOWN = "unknown"

#: 内置 registry 版本标签（内置新版以独立条目进入，绝不覆盖旧条目）
BUILTIN_REGISTRY_VERSION = "builtin-v1"

#: 输出节点
_OUTPUT_CLASSES = {
    "SaveImage",
    "SaveVideo",
    "VHS_VideoCombine",
    "VHS_VideoMerge",
    "SaveAnimatedWEBP",
    "SaveAnimatedPNG",
    "SaveWEBM",
    "GifViewer",
}

#: LoRA 节点
_LORA_CLASSES = {"LoraLoader", "LoraLoaderModelOnly", "LoraLoaderModelOnlyAdvanced"}

#: 正向/负向判定：文本出现负向特征词则视为负向节点
_NEGATIVE_MARKER_RE = re.compile(
    r"worst\s+quality|low\s+quality|normal\s+quality|bad\s+anatomy|bad\s+hands?|"
    r"bad\s+feet?|extra\s+fingers?|missing\s+fingers?|deformed|mutated|blurry|lowres|"
    r"text,\s*watermark|signature|jpeg\s+artifacts|"
    r"worstquality|lowquality|badanatomy|badhands|extrafingers|missingfingers",
    re.I,
)

_CJK_NEGATIVE_MARKER_RE = re.compile(
    r"最差|低质量|坏手|畸形|变形|模糊|水印|签名|低分辨率", re.I
)

#: 模型族识别特征
_ILLUSTRIOUS_MARKERS = ("illustrious", "waiillustrious", "ilv90", "nova3dcg")
_ANIMA_MARKERS = ("auraflow", "modelflow", "anima")
_WAN_MARKERS = ("wan", "wani2v", "wan2")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class WorkflowValidation:
    """节点校验结果。"""

    ok: bool
    positive_nodes: list[int] = field(default_factory=list)
    negative_nodes: list[int] = field(default_factory=list)
    output_nodes: list[int] = field(default_factory=list)
    lora_nodes: list[int] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "positive_nodes": list(self.positive_nodes),
            "negative_nodes": list(self.negative_nodes),
            "output_nodes": list(self.output_nodes),
            "lora_nodes": list(self.lora_nodes),
            "findings": [dict(f) for f in self.findings],
        }


@dataclass
class WorkflowEntry:
    """工作流注册条目。stable_id 与内容绑定，独立于文件名。"""

    workflow_id: str
    name: str
    sha256: str
    family: str = FAMILY_UNKNOWN
    mode: str = MODE_UNKNOWN
    builtin: bool = True
    registry_version: str = BUILTIN_REGISTRY_VERSION
    path: str = ""
    node_classes: dict[str, int] = field(default_factory=dict)
    validation: Optional[WorkflowValidation] = None
    error: str = ""

    @property
    def valid(self) -> bool:
        return not self.error and self.validation is not None and self.validation.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "name": self.name,
            "sha256": self.sha256,
            "family": self.family,
            "mode": self.mode,
            "builtin": self.builtin,
            "registry_version": self.registry_version,
            "path": self.path,
            "node_classes": dict(self.node_classes),
            "validation": self.validation.to_dict() if self.validation else None,
            "error": self.error,
            "valid": self.valid,
        }


# ---------------------------------------------------------------------------
# 纯函数：哈希 / 识别 / 校验
# ---------------------------------------------------------------------------


def canonical_json(data: Any) -> str:
    """规范化 JSON：key 排序、紧凑分隔、非 ASCII 保留。稳定 ID 的基础。"""
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_workflow_id(data: Any) -> str:
    """稳定 ID：规范化 JSON 的 SHA256（格式/键序变化不影响）。"""
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _node_class_counts(data: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, dict) and value.get("class_type"):
                class_type = str(value["class_type"]).lower()
                counts[class_type] = counts.get(class_type, 0) + 1
    return counts


def _node_ids(data: Any) -> set[str]:
    return set(str(key) for key in data.keys()) if isinstance(data, dict) else set()


def detect_family(data: Any, name: str = "") -> str:
    """识别模型族：illustrious / anima / wan / generic / unknown。"""
    text = canonical_json(data).lower()
    lowered_name = str(name or "").lower()
    classes = _node_class_counts(data)

    if any(marker in lowered_name for marker in ("illustrious", "wan", "anima")):
        if "illustrious" in lowered_name:
            return FAMILY_ILLUSTRIOUS
        if "wan" in lowered_name:
            return FAMILY_WAN
        if "anima" in lowered_name:
            return FAMILY_ANIMA

    if "unetloadergguf" in classes or "wanimage2video" in classes or "wani2v" in classes:
        return FAMILY_WAN
    if any(marker in text for marker in _ILLUSTRIOUS_MARKERS):
        return FAMILY_ILLUSTRIOUS
    if "modelsamplingauraflow" in text or any(marker in text for marker in _ANIMA_MARKERS):
        return FAMILY_ANIMA
    if any(marker in text for marker in _WAN_MARKERS):
        return FAMILY_WAN
    if "checkpointloadersimple" in classes or "checkpointloader" in classes:
        return FAMILY_GENERIC
    return FAMILY_UNKNOWN


def detect_mode(data: Any) -> str:
    """识别模式：video / img2img / txt2img。"""
    classes = _node_class_counts(data)
    if "ksampleradvanced" in classes or "wanimagetovideo" in classes or "unetloadergguf" in classes:
        return MODE_VIDEO
    if "loadimage" in classes:
        return MODE_IMG2IMG
    if "emptylatentimage" in classes or "emptysd3latentimage" in classes or "emptyvlatent" in classes:
        return MODE_TXT2IMG
    return MODE_UNKNOWN


def validate_workflow_nodes(data: Any) -> WorkflowValidation:
    """校验正向 / 负向 / 输出 / LoRA 节点与引用完整性。"""
    validation = WorkflowValidation(ok=True)
    if not isinstance(data, dict) or not data:
        validation.ok = False
        validation.findings.append({"severity": "error", "node": None, "message": "工作流不是非空节点字典"})
        return validation

    ids = _node_ids(data)
    positive: list[int] = []
    negative: list[int] = []
    output_classes = {cls.lower() for cls in _OUTPUT_CLASSES}
    lora_classes = {cls.lower() for cls in _LORA_CLASSES}
    for node_id, node in data.items():
        if not isinstance(node, dict):
            validation.findings.append(
                {"severity": "error", "node": node_id, "message": "节点不是 dict"}
            )
            continue
        class_type = str(node.get("class_type") or "")
        if not class_type:
            validation.findings.append(
                {"severity": "error", "node": node_id, "message": "节点缺少 class_type"}
            )
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            validation.findings.append(
                {"severity": "error", "node": node_id, "message": f"节点 {class_type} 缺少 inputs"}
            )
            continue
        if class_type.lower() == "cliptextencode":
            text = str(inputs.get("text") or "")
            if _NEGATIVE_MARKER_RE.search(text) or _CJK_NEGATIVE_MARKER_RE.search(text):
                negative.append(int(node_id))
            elif text.strip():
                positive.append(int(node_id))
        if class_type.lower() in output_classes:
            validation.output_nodes.append(int(node_id))
        if class_type.lower() in lora_classes:
            validation.lora_nodes.append(int(node_id))
        # 引用完整性：inputs 中的 ["<node_id>", idx] 与嵌套列表
        for value in inputs.values():
            for ref in _iter_references(value):
                if ref not in ids:
                    validation.findings.append(
                        {
                            "severity": "warning",
                            "node": node_id,
                            "message": f"悬空引用 -> 节点 {ref}",
                        }
                    )

    if not positive:
        validation.ok = False
        validation.findings.append(
            {"severity": "error", "node": None, "message": "缺少正向提示词节点 (CLIPTextEncode)"}
        )
    if not negative:
        validation.ok = False
        validation.findings.append(
            {"severity": "error", "node": None, "message": "缺少负向提示词节点 (CLIPTextEncode)"}
        )
    if not validation.output_nodes:
        validation.ok = False
        validation.findings.append(
            {"severity": "error", "node": None, "message": "缺少输出节点 (SaveImage/SaveVideo 等)"}
        )
    if not validation.lora_nodes:
        validation.findings.append(
            {"severity": "warning", "node": None, "message": "未发现 LoRA 节点（可选）"}
        )

    validation.positive_nodes = sorted(positive)
    validation.negative_nodes = sorted(negative)
    return validation


def _iter_references(value: Any):
    """遍历 ComfyUI API 格式的节点引用（[node_id, slot] 或嵌套列表）。"""
    if isinstance(value, list):
        if len(value) == 2 and all(isinstance(item, (str, int)) for item in value):
            if str(value[0]) not in {"", "None", "null"}:
                yield str(value[0])
            return
        for item in value:
            yield from _iter_references(item)


# ---------------------------------------------------------------------------
# WorkflowService
# ---------------------------------------------------------------------------


class WorkflowService:
    """工作流注册服务。

    :param directory: 扫描目录（默认为插件内置 workflow/ 目录）。
    :param prefer_custom: ``get`` 时自定义条目是否优先于内置条目。
    """

    def __init__(self, directory: Optional[Path | str] = None, prefer_custom: bool = False) -> None:
        self.directory = Path(directory) if directory else None
        self.prefer_custom = prefer_custom
        # 内置 registry：name -> 条目列表（新版内置以独立条目追加，不覆盖旧条目）
        self.builtin_registry: dict[str, list[WorkflowEntry]] = {}
        # 自定义 registry：name -> 条目列表（独立于内置，永不覆盖内置）
        self.custom_registry: dict[str, list[WorkflowEntry]] = {}
        self._errors: list[dict[str, Any]] = []

    # -- 扫描 -----------------------------------------------------------------

    def scan(self, directory: Optional[Path | str] = None) -> list[WorkflowEntry]:
        """扫描 ``*.json`` 工作流文件并登记为内置条目。损坏文件跳过并记录错误。"""
        directory = Path(directory) if directory else self.directory
        if not directory or not directory.is_dir():
            self._errors.append({"file": str(directory), "error": "目录不存在"})
            return []
        entries: list[WorkflowEntry] = []
        for path in sorted(directory.glob("*.json")):
            entry = self.scan_file(path)
            if entry is not None:
                entries.append(entry)
        return entries

    def scan_file(self, path: Path | str) -> Optional[WorkflowEntry]:
        path = Path(path)
        try:
            raw = path.read_bytes()
            data = json.loads(raw.decode("utf-8-sig"))
            entry = self._entry_from_data(data, name=path.name, sha256=sha256_bytes(raw), path=str(path))
        except json.JSONDecodeError as exc:
            self._errors.append({"file": path.name, "error": f"JSON 损坏: {exc}"})
            return None
        except Exception as exc:  # noqa: BLE001
            self._errors.append({"file": path.name, "error": f"读取失败: {exc}"})
            return None
        self._register_builtin(entry)
        return entry

    def register_builtin_snapshot(self, data: Any, name: str, version: str = "builtin-v2") -> WorkflowEntry:
        """内置新版作为独立 registry 条目注册（不覆盖旧条目）。"""
        entry = self._entry_from_data(data, name=name, sha256=sha256_bytes(canonical_json(data).encode("utf-8")))
        entry.builtin = True
        entry.registry_version = str(version or BUILTIN_REGISTRY_VERSION)
        self.builtin_registry.setdefault(entry.name, []).append(entry)
        return entry

    def register_custom(self, data: Any, name: str) -> WorkflowEntry:
        """登记自定义工作流：进入独立 custom registry，永不覆盖内置条目。"""
        entry = self._entry_from_data(data, name=name, sha256=sha256_bytes(canonical_json(data).encode("utf-8")))
        entry.builtin = False
        entry.registry_version = "custom"
        self.custom_registry.setdefault(entry.name, []).append(entry)
        return entry

    # -- 查询 -----------------------------------------------------------------

    def list_builtin(self) -> list[WorkflowEntry]:
        return [entry for entries in self.builtin_registry.values() for entry in entries]

    def list_custom(self) -> list[WorkflowEntry]:
        return [entry for entries in self.custom_registry.values() for entry in entries]

    def all_entries(self) -> list[WorkflowEntry]:
        return self.list_builtin() + self.list_custom()

    def get(self, name: Optional[str] = None, workflow_id: Optional[str] = None) -> Optional[WorkflowEntry]:
        """按文件名或稳定 ID 查询。默认返回内置条目（除非 prefer_custom）。"""
        if workflow_id:
            for entry in self.all_entries():
                if entry.workflow_id == workflow_id:
                    return entry
            return None
        if not name:
            return None
        name = Path(str(name)).name
        builtin = self.builtin_registry.get(name)
        custom = self.custom_registry.get(name)
        if self.prefer_custom and custom:
            return custom[-1]
        if builtin:
            return builtin[-1]
        if custom:
            return custom[-1]
        return None

    def is_custom(self, name: str) -> bool:
        """该文件名是否存在自定义条目（且内置 registry 未被覆盖）。"""
        return bool(self.custom_registry.get(Path(str(name)).name))

    def has_builtin(self, name: str) -> bool:
        return bool(self.builtin_registry.get(Path(str(name)).name))

    def errors(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._errors]

    def to_dict(self) -> dict[str, Any]:
        return {
            "builtin_registry": [e.to_dict() for e in self.list_builtin()],
            "custom_registry": [e.to_dict() for e in self.list_custom()],
            "errors": self.errors(),
        }

    # -- 内部 -----------------------------------------------------------------

    def _register_builtin(self, entry: WorkflowEntry) -> None:
        self.builtin_registry.setdefault(entry.name, []).append(entry)

    @staticmethod
    def _entry_from_data(data: Any, name: str, sha256: str, path: str = "") -> WorkflowEntry:
        workflow_id = stable_workflow_id(data)
        family = detect_family(data, name)
        mode = detect_mode(data)
        validation = validate_workflow_nodes(data)
        entry = WorkflowEntry(
            workflow_id=workflow_id,
            name=name,
            sha256=sha256,
            family=family,
            mode=mode,
            builtin=True,
            path=path,
            node_classes=_node_class_counts(data),
            validation=validation,
        )
        if not validation.ok:
            entry.error = "; ".join(
                f["message"] for f in validation.findings if f["severity"] == "error"
            )
        return entry


__all__ = [
    "WorkflowService",
    "WorkflowEntry",
    "WorkflowValidation",
    "stable_workflow_id",
    "canonical_json",
    "sha256_bytes",
    "detect_family",
    "detect_mode",
    "validate_workflow_nodes",
    "FAMILY_ANIMA",
    "FAMILY_ILLUSTRIOUS",
    "FAMILY_WAN",
    "FAMILY_GENERIC",
    "FAMILY_UNKNOWN",
    "MODE_TXT2IMG",
    "MODE_IMG2IMG",
    "MODE_VIDEO",
    "MODE_UNKNOWN",
]

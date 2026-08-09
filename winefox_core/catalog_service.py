# -*- coding: utf-8 -*-
"""CatalogService —— 平台无关的目录合并服务（WineFox Desktop）。

把模型族、工作流、checkpoint、LoRA 与能力缺口合并为纯 JSON 目录，
供桌面端 / 后端直接消费。不依赖 AstrBot / HTTP / 数据库。

输出（build 返回值）保证可被 json.dumps 往返，默认不含时间戳（确定性）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .workflow_service import WorkflowEntry, WorkflowService

SCHEMA_VERSION = 1


@dataclass
class CatalogSummary:
    families: int = 0
    workflows: int = 0
    checkpoints: int = 0
    loras: int = 0
    missing_capabilities: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "families": self.families,
            "workflows": self.workflows,
            "checkpoints": self.checkpoints,
            "loras": self.loras,
            "missing_capabilities": list(self.missing_capabilities),
        }


class CatalogService:
    """目录合并服务：输入为纯数据，输出为纯 JSON 字典。"""

    def __init__(self, workflow_service: Optional[WorkflowService] = None) -> None:
        #: 可选注入 WorkflowService；否则 build 时从 workflows 参数读取条目
        self.workflow_service = workflow_service

    # -- 主入口 -----------------------------------------------------------------

    def build(
        self,
        *,
        model_families: Optional[Iterable[dict[str, Any]]] = None,
        workflows: Optional[Iterable[dict[str, Any]]] = None,
        checkpoints: Optional[Iterable[dict[str, Any]]] = None,
        loras: Optional[Iterable[dict[str, Any]]] = None,
        declared_capabilities: Optional[Iterable[dict[str, Any]]] = None,
        summary: Optional[CatalogSummary] = None,
    ) -> dict[str, Any]:
        """合并为纯 JSON 目录。

        :param workflows: 工作流条目 dict 列表（或直接传 WorkflowEntry/registry）。
        :param declared_capabilities: 声明的能力，如
            ``[{"capability": "txt2img", "family": "illustrious", "requires": ["checkpoint", "workflow", "lora"]}]``
        """
        family_items = self._normalize_list(model_families, "model_families")
        workflow_items = self._normalize_workflows(workflows)
        checkpoint_items = self._normalize_list(checkpoints, "checkpoints")
        lora_items = self._normalize_list(loras, "loras")
        capabilities = self._analyze_capabilities(
            declared_capabilities or [],
            family_items=family_items,
            workflow_items=workflow_items,
            checkpoint_items=checkpoint_items,
            lora_items=lora_items,
        )

        summary_data = summary or CatalogSummary(
            families=len(family_items),
            workflows=len(workflow_items),
            checkpoints=len(checkpoint_items),
            loras=len(lora_items),
            missing_capabilities=[item["reason"] for item in capabilities.get("missing", [])],
        )

        catalog: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "model_families": family_items,
            "workflows": workflow_items,
            "checkpoints": checkpoint_items,
            "loras": lora_items,
            "capabilities": capabilities,
            "summary": summary_data.to_dict() if isinstance(summary_data, CatalogSummary) else dict(summary_data or {}),
        }
        return catalog

    def to_json(self, catalog: dict[str, Any], indent: int = 2) -> str:
        """纯 JSON 输出（保证可往返）。"""
        return json.dumps(catalog, ensure_ascii=False, indent=indent)

    # -- 能力分析 -----------------------------------------------------------------

    def _analyze_capabilities(
        self,
        declared: Iterable[dict[str, Any]],
        *,
        family_items: list[dict[str, Any]],
        workflow_items: list[dict[str, Any]],
        checkpoint_items: list[dict[str, Any]],
        lora_items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        workflow_families = {str(item.get("family") or "").strip() for item in workflow_items if item.get("family")}
        workflow_modes = {(str(item.get("family") or ""), str(item.get("mode") or "")) for item in workflow_items}
        checkpoint_families = {str(item.get("family") or "").strip() for item in checkpoint_items}
        lora_families = {str(item.get("family") or "").strip() for item in lora_items}
        family_keys = {str(item.get("key") or item.get("family") or "").strip() for item in family_items}

        satisfied: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        for capability in declared or []:
            cap = dict(capability or {})
            name = str(cap.get("capability") or "").strip()
            family = str(cap.get("family") or "").strip()
            requires = [str(r).strip() for r in (cap.get("requires") or []) if str(r).strip()]
            if not name:
                continue
            gaps: list[str] = []
            for resource in requires:
                if resource == "workflow":
                    if family and family not in workflow_families:
                        gaps.append("workflow")
                elif resource == "checkpoint":
                    if family and family not in checkpoint_families:
                        gaps.append("checkpoint")
                elif resource == "lora":
                    if family and family not in lora_families:
                        gaps.append("lora")
                elif resource == "family":
                    if family and family not in family_keys:
                        gaps.append("family")
            if gaps:
                missing.append(
                    {
                        "capability": name,
                        "family": family,
                        "missing": gaps,
                        "reason": f"能力 {name}（{family}）缺少: {', '.join(gaps)}",
                    }
                )
            else:
                satisfied.append({"capability": name, "family": family})

        # 缺失能力推断：已声明但资源不齐 → 提示；有资源无家族声明 → 提示
        for family in sorted(workflow_families - family_keys):
            if family:
                satisfied.append({"capability": "workflow_family", "family": family, "inferred": True})
        for family in sorted(checkpoint_families - family_keys):
            if family:
                missing.append(
                    {
                        "capability": "checkpoint_only_family",
                        "family": family,
                        "missing": ["family"],
                        "reason": f"存在 checkpoint 但缺少模型族声明: {family}",
                    }
                )

        return {"declared": [dict(c) for c in declared or []], "satisfied": satisfied, "missing": missing}

    # -- 归一化 -----------------------------------------------------------------

    def _normalize_list(self, items: Optional[Iterable[Any]], field_name: str) -> list[dict[str, Any]]:
        if items is None:
            return []
        result: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, WorkflowEntry):
                result.append(item.to_dict())
            elif isinstance(item, dict):
                result.append(dict(item))
            else:
                result.append({"raw": str(item), "field": field_name})
        return result

    def _normalize_workflows(self, workflows: Optional[Iterable[Any]]) -> list[dict[str, Any]]:
        if workflows is None:
            if self.workflow_service is not None:
                return [entry.to_dict() for entry in self.workflow_service.all_entries()]
            return []
        if isinstance(workflows, WorkflowService):
            return [entry.to_dict() for entry in workflows.all_entries()]
        return self._normalize_list(workflows, "workflows")


__all__ = ["CatalogService", "CatalogSummary", "SCHEMA_VERSION"]

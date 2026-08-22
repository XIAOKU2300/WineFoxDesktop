"""Prepare immutable ComfyUI API workflows from desktop job payloads."""

from __future__ import annotations

import copy
import random
import re
from typing import Any, Mapping


def _replace_ref(value: Any, node_id: str, replacement: Any, output_index: int) -> Any:
    if isinstance(value, list):
        if len(value) == 2 and str(value[0]) == node_id and value[1] == output_index:
            return copy.deepcopy(replacement)
        return [_replace_ref(v, node_id, replacement, output_index) for v in value]
    if isinstance(value, dict):
        return {k: _replace_ref(v, node_id, replacement, output_index) for k, v in value.items()}
    return value


def _bypass_lora(workflow: dict[str, Any], node_id: str, node: Mapping[str, Any]) -> None:
    inputs = node.get("inputs") if isinstance(node, Mapping) else {}
    model_ref = (inputs or {}).get("model")
    clip_ref = (inputs or {}).get("clip")
    for other_id, other in list(workflow.items()):
        if other_id == node_id or not isinstance(other, dict):
            continue
        if model_ref is not None:
            other["inputs"] = _replace_ref(other.get("inputs", {}), node_id, model_ref, 0)
        if clip_ref is not None:
            other["inputs"] = _replace_ref(other.get("inputs", {}), node_id, clip_ref, 1)
    workflow.pop(node_id, None)


_QUALITY_MARKERS = (
    "masterpiece", "best quality", "amazing quality", "high quality",
    "score_9", "score_8", "score_7", "very aesthetic", "absurdres", "highres",
)

#: 角色数量/阵容类内容指令。模板基底携带它们是为配合模板的原始用途
#: （多角色工作流预置 multiple characters，单人安全模板预置 solo），
#: 合并进用户 prompt 会与用户实际的单/多人意图直接冲突，一律剔除。
_CONTENT_DIRECTIVE_TAGS = frozenset({
    "solo", "single character", "multiple characters", "multiple views",
    "different hair colors", "different hairstyles", "different outfits",
    "multiple girls", "multiple boys", "1girl", "2girls", "3girls",
    "1boy", "2boys", "group",
})

_WEIGHT_TAG_RE = re.compile(r"^\((.+?)(?::[\d.]+)?\)$")


def _tag_marker(value: str) -> str:
    """tag 去重键：剥掉加权写法 (tag:1.2) 的外壳与权重，再大小写归一。"""
    text = str(value or "").strip()
    match = _WEIGHT_TAG_RE.match(text)
    if match:
        text = match.group(1).strip()
    return text.casefold()


def _is_quality_or_style_stack(text: str) -> bool:
    """模板默认正向是否是值得保留的质量/风格栈（而非占位垃圾）。"""
    lowered = str(text or "").lower()
    tags = [tag for tag in lowered.split(",") if tag.strip()]
    return len(tags) >= 4 and any(marker in lowered for marker in _QUALITY_MARKERS)


def _filter_content_directives(text: str) -> str:
    """从模板基底里剔除角色数量/阵容类内容指令，保留质量与风格 tag。"""
    kept = [
        tag.strip()
        for tag in str(text or "").split(",")
        if tag.strip() and _tag_marker(tag) not in _CONTENT_DIRECTIVE_TAGS
    ]
    return ", ".join(kept)


def _merge_tag_texts(base: str, extra: str) -> str:
    """base 在前、extra 在后合并，按大小写不敏感去重（保留原 tag 写法与权重）。"""
    parts: list[str] = []
    seen: set[str] = set()
    for tag in f"{base},{extra}".split(","):
        value = str(tag or "").strip()
        marker = _tag_marker(value)
        if not value or marker in seen:
            continue
        parts.append(value)
        seen.add(marker)
    return ", ".join(parts)


def workflow_prompt_texts(workflow: Mapping[str, Any]) -> tuple[str, str]:
    """读取已构建工作流里最终生效的正/负提示词文本（调试展示用）。"""
    return _template_prompt_defaults(workflow)


def _template_prompt_defaults(workflow: Mapping[str, Any]) -> tuple[str, str]:
    """返回模板自带的 (positive, negative) 默认文本（首个 CLIPTextEncode 各取其一）。"""
    positive = ""
    negative = ""
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict) or "text" not in inputs:
            continue
        if "cliptextencode" not in str(node.get("class_type") or "").lower():
            continue
        title = str((node.get("_meta") or {}).get("title") or "").lower()
        text = str(inputs.get("text") or "")
        if "negative" in title or str(node_id) in {"12", "33"}:
            if not negative:
                negative = text
        elif not positive:
            positive = text
    return positive, negative


def _coerce_lora_strength(value: Any, default: float = 0.75) -> float:
    try:
        strength = float(value)
    except (TypeError, ValueError):
        return default
    if strength <= 0 or strength > 1.5:
        return default
    return strength


def prepare_workflow(
    source: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    checkpoint: str | None = None,
    uploaded_name: str | None = None,
) -> tuple[dict[str, Any], int]:
    workflow: dict[str, Any] = copy.deepcopy(dict(source))
    prompt = str(payload.get("prompt") or "").strip()
    negative = str(payload.get("negative_prompt") or "").strip()
    # 模板自带的质量/风格基底与用户 prompt 合并而不是整段替换：
    # Illustrious/Pony 系底模缺了 quality 前缀会明显"不听提示词"，
    # LoRA 自带内容乘虚主导画面（表现为部分 LoRA 下用户描述无效）。
    template_positive, template_negative = _template_prompt_defaults(workflow)
    if template_positive and _is_quality_or_style_stack(template_positive):
        # 只保留质量/风格 tag：角色数量类内容指令（multiple characters / solo 等）
        # 是模板原始用途的预置，与用户实际的单/多人意图冲突，不能带进去
        prompt = _merge_tag_texts(_filter_content_directives(template_positive), prompt)
    if not negative:
        negative = template_negative
    seed_value = payload.get("seed")
    seed = random.SystemRandom().randrange(0, 2**63 - 1) if seed_value in (None, -1, "-1") else int(seed_value)
    width = max(64, int(payload.get("width") or 832))
    height = max(64, int(payload.get("height") or 1216))
    width -= width % 8
    height -= height % 8
    loras = payload.get("loras") if isinstance(payload.get("loras"), list) else []
    selected_loras = [item for item in loras if isinstance(item, dict)]
    lora_index = 0
    positive_done = False
    negative_done = False
    seed_index = 0

    for node_id, node in list(workflow.items()):
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        class_type = str(node.get("class_type") or "")
        title = str((node.get("_meta") or {}).get("title") or "").lower()
        lowered = class_type.lower()

        if "cliptextencode" in lowered and "text" in inputs:
            if "negative" in title or str(node_id) in {"12", "33"}:
                if negative and not negative_done:
                    inputs["text"] = negative
                    negative_done = True
            elif not positive_done or "positive prompt" in title:
                inputs["text"] = prompt
                positive_done = True
        for key in ("seed", "noise_seed"):
            if key in inputs and not isinstance(inputs[key], (list, tuple, dict)):
                inputs[key] = (seed + seed_index) % (2**63 - 1)
                seed_index += 1
        if "width" in inputs and not isinstance(inputs["width"], (list, tuple, dict)):
            inputs["width"] = width
        if "height" in inputs and not isinstance(inputs["height"], (list, tuple, dict)):
            inputs["height"] = height
        if "steps" in inputs and payload.get("steps") is not None:
            inputs["steps"] = int(payload["steps"])
        if "cfg" in inputs and payload.get("cfg") is not None:
            inputs["cfg"] = float(payload["cfg"])
        if "sampler_name" in inputs and payload.get("sampler"):
            inputs["sampler_name"] = str(payload["sampler"])
        if "scheduler" in inputs and payload.get("scheduler"):
            inputs["scheduler"] = str(payload["scheduler"])
        if "ckpt_name" in inputs and checkpoint:
            inputs["ckpt_name"] = checkpoint
        if class_type == "LoadImage" and uploaded_name:
            inputs["image"] = uploaded_name
        if "lora" in lowered and "lora_name" in inputs:
            selected_lora = selected_loras[lora_index] if lora_index < len(selected_loras) else None
            if selected_lora:
                inputs["lora_name"] = str(selected_lora.get("name") or selected_lora.get("id") or inputs["lora_name"])
                strength = _coerce_lora_strength(
                    selected_lora.get("strength", selected_lora.get("strength_model")), 0.75
                )
                if "strength_model" in inputs:
                    inputs["strength_model"] = strength
                if "strength_clip" in inputs:
                    inputs["strength_clip"] = _coerce_lora_strength(selected_lora.get("strength_clip"), strength)
                lora_index += 1
            else:
                _bypass_lora(workflow, str(node_id), node)

    return workflow, seed

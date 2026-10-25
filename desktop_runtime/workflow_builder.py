"""Prepare immutable ComfyUI API workflows from desktop job payloads."""

from __future__ import annotations

import copy
import random
import re
from typing import Any, Mapping
from .lora_workflow import bind_loras, prompt_roles


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


def workflow_sampling_summary(workflow: Mapping[str, Any]) -> str:
    """Expose actual active sampling passes so expensive presets are visible."""
    from .lora_workflow import active_nodes
    active = active_nodes(dict(workflow))
    samplers = [node.get("inputs", {}) for key, node in workflow.items()
                if key in active and "sampler" in str(node.get("class_type", "")).lower()
                and "steps" in node.get("inputs", {})]
    if not samplers:
        return ""
    steps = "＋".join(str(item.get("steps")) for item in samplers)
    detail = f"{steps} 步 · {samplers[0].get('sampler_name', '')}"
    scales = [node.get("inputs", {}).get("scale_by") for key, node in workflow.items()
              if key in active and node.get("class_type") == "LatentUpscaleBy"]
    if scales:
        detail += f" · 潜空间放大 {scales[0]}×"
    return detail


def _template_prompt_defaults(workflow: Mapping[str, Any]) -> tuple[str, str]:
    """返回模板自带的 (positive, negative) 默认文本（首个 CLIPTextEncode 各取其一）。"""
    positive = ""
    negative = ""
    positives, negatives = prompt_roles(workflow)
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
        if (positives or negatives) and str(node_id) not in positives | negatives:
            continue
        is_negative = str(node_id) in negatives and str(node_id) not in positives if positives or negatives else "negative" in title or str(node_id) in {"12", "33"}
        if is_negative:
            if not negative:
                negative = text
        elif not positive:
            positive = text
    return positive, negative


def prepare_workflow(
    source: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    checkpoint: str | None = None,
    uploaded_name: str | None = None,
) -> tuple[dict[str, Any], int]:
    workflow: dict[str, Any] = copy.deepcopy(dict(source))
    # HTTP execution downloads saved images from history. The old optional
    # websocket example node is not installed on a standard ComfyUI server.
    saved_images = [node.get("inputs", {}).get("images") for node in workflow.values() if node.get("class_type") == "SaveImage"]
    for key, node in list(workflow.items()):
        if node.get("class_type") == "SaveImageWebsocket":
            images = node.get("inputs", {}).get("images")
            if images in saved_images:
                workflow.pop(key)
            else:
                node["class_type"] = "SaveImage"
                node["inputs"] = {"images": images, "filename_prefix": "WineFox"}
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
    positives, negatives = prompt_roles(workflow)
    positive_done = False
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
            connected = bool(positives or negatives)
            is_negative = (str(node_id) in negatives and str(node_id) not in positives) if connected else ("negative" in title or str(node_id) in {"12", "33"})
            if is_negative:
                if negative:
                    inputs["text"] = negative
            elif str(node_id) in positives or (not positives and not negatives and (not positive_done or "positive prompt" in title)):
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
    bind_loras(workflow, selected_loras)
    return workflow, seed

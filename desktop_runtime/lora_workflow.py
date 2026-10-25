"""Bind LoRA selections to executable ComfyUI graph edges, without plugins."""
from __future__ import annotations

import copy
from typing import Any

from winefox_core.lora_binding import lora_strength

LOADERS = {"LoraLoader", "LoraLoaderModelOnly"}
HOOKS = {"CreateHookLora", "CreateHookLoraModelOnly"}


def is_link(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 2 and isinstance(value[0], (str, int)) and type(value[1]) is int


def ancestors(workflow: dict, starts) -> set[str]:
    found, pending = set(), [str(value) for value in starts]
    while pending:
        key = pending.pop()
        if key in found or key not in workflow:
            continue
        found.add(key)
        pending.extend(str(value[0]) for value in workflow[key].get("inputs", {}).values() if is_link(value))
    return found


def active_nodes(workflow: dict) -> set[str]:
    outputs = [key for key, node in workflow.items() if str(node.get("class_type", "")).startswith(("Save", "PreviewImage"))]
    return ancestors(workflow, outputs) if outputs else set(workflow)


def _fixed(node: dict) -> bool:
    title = str(node.get("_meta", {}).get("title", "")).casefold()
    return "character" not in title and any(word in title for word in ("fixed", "style lora", "detail style lora"))


def _replace(workflow: dict, old: list, new: list, *, skip=()) -> None:
    def replace(value):
        if value == old:
            return copy.deepcopy(new)
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        return value
    for key, node in workflow.items():
        if key not in skip:
            node["inputs"] = replace(node.get("inputs", {}))


def _bypass(workflow: dict, key: str) -> None:
    node = workflow[key]
    for field, slot in (("model", 0), ("clip", 1)):
        ref = node.get("inputs", {}).get(field)
        if is_link(ref):
            _replace(workflow, [key, slot], ref, skip=(key,))
    workflow.pop(key)


def _set_selection(node: dict, selection: dict) -> None:
    inputs = node["inputs"]
    name = selection.get("name") or selection.get("lora_name") or selection.get("id")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("LoRA 选择缺少有效文件名")
    inputs["lora_name"] = name
    inputs["strength_model"] = lora_strength(selection.get("strength_model", selection.get("strength")), .75)
    if node["class_type"] in {"LoraLoader", "CreateHookLora"}:
        inputs["strength_clip"] = lora_strength(selection.get("strength_clip"), inputs["strength_model"])


def _append(workflow: dict, selections: list[dict], model_ref: list, clip_ref: list | None) -> None:
    original_model, original_clip = model_ref, clip_ref
    inserted = []
    next_id = max((int(key) for key in workflow if str(key).isdigit()), default=0) + 1
    for selection in selections:
        key = str(next_id); next_id += 1
        inputs = {"model": model_ref}
        node = {"class_type": "LoraLoader" if clip_ref else "LoraLoaderModelOnly", "inputs": inputs, "_meta": {"title": "WineFox selected LoRA"}}
        if clip_ref:
            inputs["clip"] = clip_ref
        _set_selection(node, selection)
        workflow[key] = node; inserted.append(key)
        model_ref = [key, 0]
        if clip_ref:
            clip_ref = [key, 1]
    _replace(workflow, original_model, model_ref, skip=inserted)
    if original_clip:
        _replace(workflow, original_clip, clip_ref, skip=inserted)


def _root_ref(workflow: dict, ref: list, field: str) -> list:
    visited = set()
    while is_link(ref) and str(ref[0]) in workflow and str(ref[0]) not in visited:
        key = str(ref[0]); visited.add(key)
        node = workflow[key]
        parent = node.get("inputs", {}).get(field)
        if node.get("class_type") in LOADERS or not is_link(parent):
            break
        ref = parent
    return ref


def _bind_hooks(workflow: dict, slots: list[str], selections: list[dict]) -> None:
    if len(selections) > len(slots):
        raise ValueError(f"该区域 LoRA 工作流只有 {len(slots)} 个角色槽，已选择 {len(selections)} 个；请减少选择或切换普通多 LoRA 工作流")
    for index, key in enumerate(slots):
        if index < len(selections):
            _set_selection(workflow[key], selections[index])
            continue
        previous = workflow[key].get("inputs", {}).get("prev_hooks")
        for other in workflow.values():
            for field, value in list(other.get("inputs", {}).items()):
                if value != [key, 0]:
                    continue
                if previous:
                    other["inputs"][field] = previous
                elif field in {"hooks", "prev_hooks"}:
                    other["inputs"].pop(field)
                else:
                    raise ValueError("无法安全移除该工作流的区域 LoRA，请检查 hooks 连接")
        workflow.pop(key)


def bind_loras(workflow: dict, selections: list[dict]) -> None:
    active = active_nodes(workflow)
    hooks = [key for key, node in workflow.items() if key in active and node.get("class_type") in HOOKS and not _fixed(node)]
    if hooks:
        _bind_hooks(workflow, hooks, selections)
        return
    slots = [key for key, node in workflow.items() if node.get("class_type") in LOADERS and not _fixed(node)]
    # Dependency order, never JSON serialization order. Disconnected loaders
    # cannot consume a user selection intended for the actual sampler.
    slots.sort(key=lambda key: len(ancestors(workflow, [key])))
    used = []
    for key in slots:
        if key in active and len(used) < len(selections):
            _set_selection(workflow[key], selections[len(used)])
            used.append(key)
        else:
            _bypass(workflow, key)
    remaining = selections[len(used):]
    if not remaining:
        return
    if used:
        last = used[-1]
        _append(workflow, remaining, [last, 0], [last, 1] if workflow[last]["class_type"] == "LoraLoader" else None)
        return
    active = active_nodes(workflow)
    models, clips = {}, {}
    for key in active:
        node = workflow[key]; inputs = node.get("inputs", {})
        if node.get("class_type") not in LOADERS and is_link(inputs.get("model")):
            ref = _root_ref(workflow, inputs["model"], "model")
            models[tuple(ref)] = ref
        if "cliptextencode" in node.get("class_type", "").casefold() and is_link(inputs.get("clip")):
            ref = _root_ref(workflow, inputs["clip"], "clip")
            clips[tuple(ref)] = ref
    if len(models) != 1 or len(clips) > 1:
        raise ValueError("无法唯一确定 LoRA 的 MODEL/CLIP 链路，请在工作流中添加角色 LoRA 槽后重试")
    _append(workflow, remaining, next(iter(models.values())), next(iter(clips.values()), None))


def prompt_roles(workflow: dict) -> tuple[set[str], set[str]]:
    positive, negative = set(), set()
    for key in active_nodes(workflow):
        inputs = workflow[key].get("inputs", {})
        for field, result in (("positive", positive), ("negative", negative)):
            ref = inputs.get(field)
            if is_link(ref):
                result.update(ancestors(workflow, [ref[0]]))
    return positive, negative

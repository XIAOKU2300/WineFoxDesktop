"""Deterministic LoRA identity and trigger contracts, independent of UI and IO."""
from __future__ import annotations

import math
import re
import unicodedata
from collections import defaultdict
from typing import Any, Iterable


def lora_path_key(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().replace("\\", "/")
    if not text or text.startswith("/") or re.match(r"^[a-zA-Z]:|^[a-zA-Z]+://", text):
        return ""
    parts = text.split("/")
    if any(part == ".." for part in parts):
        return ""
    return "/".join(part for part in parts if part and part != ".").casefold()


def _legacy_key(value: str) -> str:
    name = lora_path_key(value).rsplit("/", 1)[-1]
    return re.sub(r"[\s_.\-]+", "", re.sub(r"\.(safetensors|ckpt|pt|bin|sft)$", "", name))


class LoraNameIndex:
    """Exact relative paths win; legacy basename fallback must be unambiguous."""
    def __init__(self, names: Iterable[str]):
        self.exact: dict[str, set[str]] = defaultdict(set)
        self.legacy: dict[str, set[str]] = defaultdict(set)
        for name in names:
            key = lora_path_key(name)
            if key:
                self.exact[key].add(str(name).replace("\\", "/"))
                self.legacy[_legacy_key(name)].add(str(name).replace("\\", "/"))

    def resolve(self, name: Any) -> str | None:
        key = lora_path_key(name)
        if not key:
            return None
        choices = self.exact.get(key) or self.legacy.get(_legacy_key(key), set())
        return next(iter(choices)) if len(choices) == 1 else None

    def ambiguous(self, name: Any) -> bool:
        key = lora_path_key(name)
        return bool(key) and len(self.exact.get(key) or self.legacy.get(_legacy_key(key), set())) > 1


def lora_is_excluded(name: str, excluded: Iterable[str]) -> bool:
    key = lora_path_key(name)
    for entry in excluded:
        marker = lora_path_key(entry)
        if marker and (key == marker or ("/" not in marker and _legacy_key(name) == _legacy_key(entry))):
            return True
    return False


def split_prompt_tags(text: str) -> list[str]:
    parts, start, depth, escaped = [], 0, 0, False
    for index, char in enumerate(str(text)):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth = max(0, depth - 1)
        elif char in ",，;；\r\n" and depth == 0:
            value = text[start:index].strip()
            if value:
                parts.append(value)
            start = index + 1
    value = text[start:].strip()
    if value:
        parts.append(value)
    return parts


def text_list(value: Any) -> list[str]:
    values = [value] if isinstance(value, str) else value if isinstance(value, (list, tuple)) else []
    result, seen = [], set()
    for item in values:
        if isinstance(item, dict):
            item = item.get("word")
        if not isinstance(item, str):
            continue
        for part in split_prompt_tags(item):
            key = unicodedata.normalize("NFKC", part).casefold()
            if key not in seen:
                seen.add(key)
                result.append(part)
    return result


def trigger_key(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).strip()
    while len(text) > 1 and (text[0], text[-1]) in {("(", ")"), ("[", "]")}:
        inner = text[1:-1]
        depth, balanced = 0, True
        for match in re.finditer(r"(?<!\\)[()]", inner):
            depth += 1 if match[0] == "(" else -1
            if depth < 0:
                balanced = False
        if not balanced or depth:
            break
        text = re.sub(r":\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)\s*$", "", inner).strip()
    return re.sub(r"\s+", " ", text).casefold()


def lora_strength(value: Any, default: float = .75) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) and -20 <= number <= 20 else default

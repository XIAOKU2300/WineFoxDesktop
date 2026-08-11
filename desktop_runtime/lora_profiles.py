"""Load legacy WineFox LoRA knowledge without depending on AstrBot."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from winefox_core.lora_service import (
    LoraService,
    MODEL_FAMILY_ANIMA,
    MODEL_FAMILY_ILLUSTRIOUS,
    normalize_lora_key,
)

logger = logging.getLogger("winefox.runtime.lora_profiles")

_PLUGIN_IDS = (
    "astrbot_plugin_comfyui_pro_UltraPlusMax",
    "astrbot_plugin_comfyui_pro",
    "astrbot_plugin_comfyui",
)
_FAMILIES = (MODEL_FAMILY_ANIMA, MODEL_FAMILY_ILLUSTRIOUS)
_MAX_PROFILE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class LoadedLoraProfiles:
    profiles: dict[str, dict[str, dict[str, Any]]]
    sources: tuple[str, ...]

    @property
    def count(self) -> int:
        return sum(len(items) for items in self.profiles.values())


@dataclass(frozen=True)
class AutoLoraResolution:
    loras: tuple[dict[str, Any], ...] = ()
    triggers: tuple[str, ...] = ()
    metadata: dict[str, Any] | None = None


def _candidate_roots(data_root: Path, env: dict[str, str]) -> list[Path]:
    home = Path.home()
    appdata = Path(env.get("APPDATA", "")) if env.get("APPDATA") else None
    localappdata = Path(env.get("LOCALAPPDATA", "")) if env.get("LOCALAPPDATA") else None
    bases = [
        home / "AstrBot",
        home / ".astrbot",
        *(path / "AstrBot" for path in (appdata, localappdata) if path is not None),
        Path.cwd(),
    ]
    roots: list[Path] = []
    for base in bases:
        for rel in ("data/plugin_data", "data/plugins_data"):
            roots.extend(base / rel / plugin_id for plugin_id in _PLUGIN_IDS)
    override = env.get("WINFOX_LEGACY_PLUGIN_DATA")
    if override:
        roots.append(Path(override))
    roots.extend((data_root / "legacy", data_root / "imports", data_root))

    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            key = os.path.normcase(str(root.resolve(strict=False)))
        except OSError:
            continue
        if key not in seen and root.is_dir():
            seen.add(key)
            unique.append(root)
    return unique


def _read_map(path: Path) -> dict[str, Any]:
    try:
        if not path.is_file() or path.stat().st_size > _MAX_PROFILE_BYTES:
            return {}
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(value, dict) and isinstance(value.get("profiles"), dict):
            value = value["profiles"]
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring invalid LoRA profile file %s: %s", path.name, exc)
        return {}


def _names(kind: str, family: str) -> tuple[str, ...]:
    if family == MODEL_FAMILY_ANIMA:
        return (f"{kind}.json", f"{kind}_anima.json")
    return (f"{kind}_illustrious.json",)


def _profile_for_lora(items: dict[str, dict[str, Any]], lora_name: str) -> dict[str, Any] | None:
    wanted = normalize_lora_key(lora_name)
    return next(
        (profile for profile in items.values() if normalize_lora_key(profile.get("lora") or profile.get("lora_name")) == wanted),
        None,
    )


def load_lora_profiles(data_root: Path, env: dict[str, str] | None = None) -> LoadedLoraProfiles:
    profiles: dict[str, dict[str, dict[str, Any]]] = {family: {} for family in _FAMILIES}
    source_names: list[str] = []
    for root in _candidate_roots(Path(data_root), dict(os.environ if env is None else env)):
        for family in _FAMILIES:
            items = profiles[family]
            for filename in _names("lora_profiles", family):
                path = root / filename
                loaded = _read_map(path)
                if loaded:
                    items.update({str(key): dict(value) for key, value in loaded.items() if isinstance(value, dict)})
                    source_names.append(filename)
            for filename in _names("lora_aliases", family):
                path = root / filename
                aliases = _read_map(path)
                if aliases:
                    source_names.append(filename)
                for alias, lora_name in aliases.items():
                    alias = str(alias or "").strip()
                    lora_name = str(lora_name or "").strip()
                    if not alias or not lora_name:
                        continue
                    profile = _profile_for_lora(items, lora_name)
                    if profile is None:
                        profile = {"lora": lora_name, "character": alias, "aliases": [], "source": "legacy_alias"}
                        items.setdefault(alias, profile)
                    raw_aliases = profile.get("aliases") or []
                    if isinstance(raw_aliases, str):
                        raw_aliases = [raw_aliases]
                    profile["aliases"] = list(dict.fromkeys([*(str(value) for value in raw_aliases), alias]))
            for filename in _names("lora_character_prompts", family):
                path = root / filename
                prompts = _read_map(path)
                if prompts:
                    source_names.append(filename)
                for lora_name, prompt in prompts.items():
                    profile = _profile_for_lora(items, str(lora_name))
                    if profile is not None and str(prompt or "").strip() and not profile.get("trigger_prompt"):
                        profile["trigger_prompt"] = str(prompt).strip()
    return LoadedLoraProfiles(profiles=profiles, sources=tuple(dict.fromkeys(source_names)))


def resolve_prompt_loras(
    service: LoraService,
    text: str,
    family: str,
    inventory: Iterable[str],
) -> AutoLoraResolution:
    available = {normalize_lora_key(name): str(name) for name in inventory if normalize_lora_key(name)}
    matched = service.match(text, family=family, limit=3)
    candidates = matched.matches if matched.multi else matched.matches[:1]
    selected: list[dict[str, Any]] = []
    triggers: list[str] = []
    evidence: list[dict[str, Any]] = []
    for match in candidates:
        actual_name = available.get(normalize_lora_key(match.lora_name))
        if not actual_name:
            continue
        selected.append({
            "id": actual_name,
            "name": actual_name,
            "strength": match.strength_model,
            "strength_model": match.strength_model,
            "strength_clip": match.strength_clip,
        })
        triggers.extend(match.trigger_words)
        evidence.append(match.to_dict())
    metadata: dict[str, Any] = {
        "family": family,
        "multi": matched.multi,
        "matches": evidence,
    }
    if not evidence:
        suggestion = service.suggest_cross_family(text, current_family=family)
        if suggestion is not None:
            metadata["cross_family_suggestion"] = suggestion.to_dict()
    return AutoLoraResolution(
        loras=tuple(selected),
        triggers=tuple(dict.fromkeys(str(value).strip() for value in triggers if str(value).strip())),
        metadata=metadata,
    )


def resolve_selected_lora_triggers(
    service: LoraService,
    family: str,
    selections: Iterable[dict[str, Any]],
) -> tuple[str, ...]:
    """Return trigger tags for LoRAs explicitly selected by the desktop UI."""
    profiles = {
        normalize_lora_key(profile.lora_name): profile
        for profile in service.profiles_for(family).values()
        if normalize_lora_key(profile.lora_name)
    }
    triggers: list[str] = []
    seen: set[str] = set()
    for selection in selections:
        if not isinstance(selection, dict):
            continue
        name = selection.get("name") or selection.get("id") or selection.get("lora_name")
        profile = profiles.get(normalize_lora_key(name))
        if profile is None:
            continue
        for trigger in profile.trigger_words:
            value = str(trigger or "").strip()
            marker = value.casefold()
            if value and marker not in seen:
                seen.add(marker)
                triggers.append(value)
    return tuple(triggers)


def merge_prompt_tags(prompt: str, tags: Iterable[str]) -> str:
    parts = [part.strip() for part in str(prompt or "").split(",") if part.strip()]
    seen = {part.casefold() for part in parts}
    for tag in tags:
        value = str(tag or "").strip()
        if value and value.casefold() not in seen:
            parts.append(value)
            seen.add(value.casefold())
    return ", ".join(parts)

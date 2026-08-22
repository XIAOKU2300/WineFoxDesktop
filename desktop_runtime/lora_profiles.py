"""Load legacy WineFox LoRA knowledge without depending on AstrBot."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from winefox_core.lora_service import (
    LoraService,
    MODEL_FAMILY_ANIMA,
    MODEL_FAMILY_ILLUSTRIOUS,
    is_generic_term,
    normalize_match_key,
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
_AUTO_CACHE_PATH = Path("cache") / "lora_profiles_metadata.json"
_AUTO_CACHE_SCHEMA = 1
_AUTO_TAG_DENY_RE = re.compile(
    r"(?i)(?:^|[ _()-])(?:artist|copyright|series|style|quality|masterpiece|best quality|"
    r"solo|girl|boy|woman|man|breast|penis|nipple|hair|eyes?|smile|blush|looking|"
    r"standing|sitting|background|clothing|dress|shirt|skirt|swimsuit|bikini|pose)(?:$|[ _()-])"
)
_AUTO_FRANCHISE_ONLY = {
    "bluearchive",
    "genshinimpact",
    "honkaistarrail",
    "wutheringwaves",
    "azurlane",
    "cyberpunk2077",
    "原神",
    "星穹铁道",
    "崩坏星穹铁道",
    "鸣潮",
    "碧蓝航线",
    "蓝色档案",
    "赛博朋克2077",
}


def lora_metadata_cache_key(server_address: str) -> str:
    normalized = str(server_address or "").strip().rstrip("/").casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def _read_auto_cache(data_root: Path) -> dict[str, Any]:
    path = Path(data_root) / _AUTO_CACHE_PATH
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > _MAX_PROFILE_BYTES:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) and value.get("schemaVersion") == _AUTO_CACHE_SCHEMA else {}
    except (OSError, ValueError):
        return {}


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def cached_generated_lora_profiles(data_root: Path, cache_key: str) -> dict[str, dict[str, dict[str, Any]]]:
    cache = _read_auto_cache(data_root)
    servers = cache.get("servers") if isinstance(cache.get("servers"), dict) else {}
    bucket = servers.get(str(cache_key)) if isinstance(servers, dict) else {}
    raw_profiles = bucket.get("profiles") if isinstance(bucket, dict) else {}
    result: dict[str, dict[str, dict[str, Any]]] = {family: {} for family in _FAMILIES}
    if not isinstance(raw_profiles, dict):
        return result
    for family in _FAMILIES:
        items = raw_profiles.get(family)
        if isinstance(items, dict):
            result[family] = {
                str(key): dict(value)
                for key, value in items.items()
                if isinstance(value, dict)
            }
    return result


def cache_generated_lora_profiles(
    data_root: Path,
    cache_key: str,
    profiles: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> None:
    cache = _read_auto_cache(data_root)
    servers = dict(cache.get("servers") or {}) if isinstance(cache.get("servers"), dict) else {}
    previous = servers.get(cache_key) if isinstance(servers.get(cache_key), dict) else {}
    merged: dict[str, dict[str, dict[str, Any]]] = {family: {} for family in _FAMILIES}
    old_profiles = previous.get("profiles") if isinstance(previous, dict) else {}
    for family in _FAMILIES:
        old_items = old_profiles.get(family) if isinstance(old_profiles, dict) else {}
        if isinstance(old_items, dict):
            merged[family].update(
                {str(key): dict(value) for key, value in old_items.items() if isinstance(value, dict)}
            )
        new_items = profiles.get(family) if isinstance(profiles, Mapping) else {}
        if isinstance(new_items, Mapping):
            merged[family].update(
                {str(key): dict(value) for key, value in new_items.items() if isinstance(value, Mapping)}
            )
    servers[cache_key] = {"updatedAt": int(time.time()), "profiles": merged}
    _atomic_write_json(
        Path(data_root) / _AUTO_CACHE_PATH,
        {"schemaVersion": _AUTO_CACHE_SCHEMA, "servers": servers},
    )


def _metadata_json_map(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or len(value) > _MAX_PROFILE_BYTES:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _metadata_family(filename: str, metadata: Mapping[str, Any]) -> str:
    evidence = " ".join(
        str(value or "")
        for value in (
            filename,
            metadata.get("modelspec.title"),
            metadata.get("ss_output_name"),
            metadata.get("ss_base_model_version"),
            metadata.get("ss_sd_model_name"),
        )
    ).casefold()
    if re.search(r"(?:^|[^a-z])anima(?:[^a-z]|$)", evidence):
        return MODEL_FAMILY_ANIMA
    if re.search(r"illustrious|illu(?:strious)?|ilxl|noobai|noobxl", evidence):
        return MODEL_FAMILY_ILLUSTRIOUS
    return ""


def _metadata_aliases(filename: str, metadata: Mapping[str, Any]) -> list[str]:
    values = [
        metadata.get("modelspec.title"),
        metadata.get("ss_output_name"),
        Path(str(filename)).stem,
    ]
    aliases: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = re.sub(r"\s+", " ", str(raw or "").strip())[:120]
        marker = normalize_match_key(value)
        if (
            not value
            or not re.search(r"[A-Za-z\u3040-\u30ff\u3400-\u9fff]", value)
            or not marker
            or marker in seen
            or marker in _AUTO_FRANCHISE_ONLY
            or is_generic_term(value)
        ):
            continue
        seen.add(marker)
        aliases.append(value)
    return aliases


def _metadata_activation_tags(metadata: Mapping[str, Any], aliases: Iterable[str]) -> list[str]:
    frequencies = _metadata_json_map(metadata.get("ss_tag_frequency"))
    totals: dict[str, float] = {}
    display: dict[str, str] = {}
    for tags in frequencies.values():
        if not isinstance(tags, dict):
            continue
        for raw_tag, raw_count in tags.items():
            tag = re.sub(r"\s+", " ", str(raw_tag or "").strip())[:100]
            try:
                count = float(raw_count)
            except (TypeError, ValueError):
                continue
            marker = normalize_match_key(tag)
            if not marker or count <= 0:
                continue
            totals[marker] = totals.get(marker, 0.0) + count
            display.setdefault(marker, tag)
    if not totals:
        return []
    high_water = max(totals.values())
    alias_markers = [normalize_match_key(alias) for alias in aliases if normalize_match_key(alias)]
    alias_tokens = {
        token
        for alias in aliases
        for token in re.findall(r"[a-z0-9]{4,}", str(alias).casefold())
        if normalize_match_key(token) not in _AUTO_FRANCHISE_ONLY
    }
    accepted: list[tuple[float, str]] = []
    for marker, count in totals.items():
        tag = display[marker]
        related = (
            any(
                min(len(marker), len(alias_marker)) >= 4
                and (marker in alias_marker or alias_marker in marker)
                for alias_marker in alias_markers
            )
            or any(token in marker or marker in token for token in alias_tokens)
        )
        if (
            not related
            or count < max(2.0, high_water * 0.45)
            or marker in _AUTO_FRANCHISE_ONLY
            or is_generic_term(tag)
            or _AUTO_TAG_DENY_RE.search(tag)
            or not re.search(r"[A-Za-z]", tag)
            or re.search(r"[\u3040-\u30ff\u3400-\u9fff]", tag)
        ):
            continue
        accepted.append((count, tag))
    accepted.sort(key=lambda item: (-item[0], item[1].casefold()))
    return [tag for _, tag in accepted[:4]]


def profile_from_comfy_metadata(
    filename: str,
    metadata: Mapping[str, Any],
    *,
    default_family: str = MODEL_FAMILY_ILLUSTRIOUS,
) -> tuple[str, dict[str, Any]] | None:
    fallback_family = str(default_family or "").strip().lower()
    family = _metadata_family(filename, metadata)
    if not family and fallback_family in _FAMILIES:
        family = fallback_family
    aliases = _metadata_aliases(filename, metadata)
    triggers = _metadata_activation_tags(metadata, aliases)
    if not family or not aliases or not triggers:
        return None
    key = aliases[0]
    return family, {
        "lora": str(filename),
        "character": key,
        "aliases": aliases[1:],
        "trigger_words": triggers,
        "source": "comfyui_metadata",
    }


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


def _candidate_files(roots: Iterable[Path], kind: str, family: str) -> list[Path]:
    """Find direct and importer-nested files without unbounded recursion."""
    paths: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            root_resolved = root.resolve(strict=True)
        except OSError:
            continue
        bases = [root, root / kind]
        try:
            children = [
                child
                for child in root.iterdir()
                if child.is_dir() and not child.is_symlink()
            ]
        except OSError:
            children = []
        # Covers data_root/legacy/<kind> and imports/<batch>/<kind> while
        # deliberately refusing arbitrary recursive scans.
        bases.extend(child / kind for child in children)
        for base in bases:
            for filename in _names(kind, family):
                path = base / filename
                try:
                    if not path.is_file() or path.is_symlink():
                        continue
                    resolved = path.resolve(strict=True)
                    resolved.relative_to(root_resolved)
                    marker = os.path.normcase(str(resolved))
                except (OSError, ValueError):
                    continue
                if marker in seen:
                    continue
                seen.add(marker)
                paths.append(path)
    return paths


def _profile_for_lora(items: dict[str, dict[str, Any]], lora_name: str) -> dict[str, Any] | None:
    wanted = normalize_lora_key(lora_name)
    return next(
        (profile for profile in items.values() if normalize_lora_key(profile.get("lora") or profile.get("lora_name")) == wanted),
        None,
    )


def load_lora_profiles(
    data_root: Path,
    env: dict[str, str] | None = None,
    *,
    auto_cache_key: str = "",
) -> LoadedLoraProfiles:
    profiles: dict[str, dict[str, dict[str, Any]]] = {family: {} for family in _FAMILIES}
    source_names: list[str] = []
    roots = _candidate_roots(Path(data_root), dict(os.environ if env is None else env))
    for family in _FAMILIES:
        items = profiles[family]
        # Load every profile first so aliases imported from another nested
        # directory attach to the real profile instead of creating a stub.
        for path in _candidate_files(roots, "lora_profiles", family):
            loaded = _read_map(path)
            if loaded:
                items.update({str(key): dict(value) for key, value in loaded.items() if isinstance(value, dict)})
                source_names.append(path.name)
        for path in _candidate_files(roots, "lora_aliases", family):
            aliases = _read_map(path)
            if aliases:
                source_names.append(path.name)
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
        for path in _candidate_files(roots, "lora_character_prompts", family):
            prompts = _read_map(path)
            if prompts:
                source_names.append(path.name)
            for lora_name, prompt in prompts.items():
                profile = _profile_for_lora(items, str(lora_name))
                if profile is not None and str(prompt or "").strip() and not profile.get("trigger_prompt"):
                    profile["trigger_prompt"] = str(prompt).strip()
    generated = (
        cached_generated_lora_profiles(data_root, auto_cache_key)
        if str(auto_cache_key).strip()
        else {family: {} for family in _FAMILIES}
    )
    generated_added = False
    for family in _FAMILIES:
        items = profiles[family]
        for key, profile in generated.get(family, {}).items():
            lora_name = str(profile.get("lora") or profile.get("lora_name") or "").strip()
            # Imported/user-maintained profiles always win, even when their
            # display key differs from the generated cache key.
            if not lora_name or key in items or _profile_for_lora(items, lora_name) is not None:
                continue
            items[str(key)] = dict(profile)
            generated_added = True
    if generated_added:
        source_names.append(_AUTO_CACHE_PATH.name)
    return LoadedLoraProfiles(profiles=profiles, sources=tuple(dict.fromkeys(source_names)))


#: 多角色 LoRA 强度上限（对齐插件 safe_chain：2 个 0.65/0.72，>2 个 0.58/0.64）。
#: 多个角色 LoRA 全强度叠跑会互相渗透，是"画面与描述不符"的主要来源之一。
_MULTI_MODEL_CAPS = {2: 0.65}
_MULTI_CLIP_CAPS = {2: 0.72}
_DEFAULT_MODEL_STRENGTH = 0.75
_DEFAULT_CLIP_STRENGTH = 0.70


def apply_multi_lora_strength_caps(selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对多 LoRA 选择应用 safe_chain 强度上限，并补齐/清洗缺失强度。"""
    count = len(selected or [])
    if not count:
        return selected
    model_cap = _MULTI_MODEL_CAPS.get(count, 0.58 if count > 2 else 1.0)
    clip_cap = _MULTI_CLIP_CAPS.get(count, 0.64 if count > 2 else 1.0)
    for entry in selected:
        if not isinstance(entry, dict):
            continue
        model = _coerce_strength(entry.get("strength_model"), _DEFAULT_MODEL_STRENGTH)
        clip = _coerce_strength(entry.get("strength_clip"), _DEFAULT_CLIP_STRENGTH)
        entry["strength_model"] = round(min(model, model_cap), 4)
        entry["strength_clip"] = round(min(clip, clip_cap), 4)
        if "strength" in entry:
            entry["strength"] = entry["strength_model"]
    return selected


def _coerce_strength(value: Any, default: float) -> float:
    try:
        strength = float(value)
    except (TypeError, ValueError):
        return default
    if strength <= 0 or strength > 1.5:
        return default
    return strength


def resolve_prompt_loras(
    service: LoraService,
    text: str,
    family: str,
    inventory: Iterable[str],
    excluded_keys: Iterable[str] = (),
) -> AutoLoraResolution:
    excluded = {
        normalize_lora_key(key) for key in excluded_keys if normalize_lora_key(key)
    }
    available = {
        normalize_lora_key(name): str(name)
        for name in inventory
        if normalize_lora_key(name) and normalize_lora_key(name) not in excluded
    }
    # 静默自动注入只认精确命中：模糊匹配的近似词不该替用户决定用哪个 LoRA。
    matched = service.match(text, family=family, limit=3, include_fuzzy=False)
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
    apply_multi_lora_strength_caps(selected)
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
    excluded_keys: Iterable[str] = (),
) -> tuple[str, ...]:
    """Return trigger tags for LoRAs explicitly selected by the desktop UI."""
    excluded = {
        normalize_lora_key(key) for key in excluded_keys if normalize_lora_key(key)
    }
    profiles = {
        normalize_lora_key(profile.lora_name): profile
        for profile in service.profiles_for(family).values()
        if normalize_lora_key(profile.lora_name)
        and normalize_lora_key(profile.lora_name) not in excluded
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


#: JSON 一次性迁移版本与事件类型（源摘要不变则跳过整体导入）
_LORA_JSON_MIGRATION_VERSION = "v1"
_LORA_JSON_MIGRATION_EVENT = "lora.profiles.migrated"


def _profiles_digest(profiles: dict[str, dict[str, dict[str, Any]]]) -> str:
    canonical = json.dumps(profiles, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _last_migration_digest(app_events: Any) -> str:
    if app_events is None:
        return ""
    try:
        events = app_events.list_events(
            event_type=_LORA_JSON_MIGRATION_EVENT, limit=50
        )
    except Exception:  # pragma: no cover - 事件读取失败按无记录处理
        return ""
    for event in reversed(events):
        payload = event.payload if isinstance(event.payload, dict) else {}
        if str(payload.get("version") or "") == _LORA_JSON_MIGRATION_VERSION:
            return str(payload.get("digest") or "")
    return ""


def import_lora_profiles_into_db(
    db: Any,
    data_root: Path,
    *,
    env: Mapping[str, str] | None = None,
    auto_cache_key: str = "",
    app_events: Any | None = None,
) -> int:
    """把原插件 JSON LoRA 档案一次性迁移进 SQLite（lora_profiles 唯一真相源）。

    - 只插入不存在的档案，绝不覆盖已有（含用户编辑）；重复运行幂等。
    - 记录迁移版本 + 源 JSON 摘要（digest）；源摘要未变时跳过整体导入。
    返回本次处理的档案条目数（0 表示源未变已跳过）。
    """
    from winefox_core.storage import LoraProfileStore

    previous = _last_migration_digest(app_events)
    loaded = load_lora_profiles(Path(data_root), env, auto_cache_key=auto_cache_key)
    digest = _profiles_digest(loaded.profiles)
    if previous and previous == digest:
        return 0
    store = LoraProfileStore(db)
    imported = 0
    added = 0
    for family, items in loaded.profiles.items():
        existing_loras = {
            normalize_lora_key(
                (record.profile or {}).get("lora")
                or (record.profile or {}).get("lora_name")
                or ""
            )
            for record in store.get_family(family)
        }
        existing_loras.discard("")
        for key, profile in items.items():
            if not isinstance(profile, dict):
                continue
            imported += 1
            if store.get(family, str(key)) is not None:
                continue  # 已有档案（含用户编辑）绝不覆盖
            incoming_lora = normalize_lora_key(
                profile.get("lora") or profile.get("lora_name") or ""
            )
            if incoming_lora and incoming_lora in existing_loras:
                continue  # 同一文件但 JSON display key 不同，也不能制造第二真源
            try:
                store.save(family, str(key), profile)
                added += 1
                if incoming_lora:
                    existing_loras.add(incoming_lora)
            except Exception as exc:  # 单条坏数据不阻断整体导入
                logger.warning(
                    "LoRA 档案导入失败 family=%s key=%s: %s", family, key, exc
                )
    if app_events is not None:
        try:
            app_events.record(
                _LORA_JSON_MIGRATION_EVENT,
                {
                    "version": _LORA_JSON_MIGRATION_VERSION,
                    "digest": digest,
                    "added": added,
                },
            )
        except Exception:  # pragma: no cover - 事件写入失败不应阻断导入
            logger.warning("LoRA 档案迁移事件写入失败", exc_info=True)
    return imported

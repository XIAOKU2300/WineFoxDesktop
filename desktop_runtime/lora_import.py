"""Read local LoRA metadata without loading tensors, pickle, or remote code."""
from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

from winefox_core.lora_binding import text_list
from winefox_core.lora_prompt import profile_prompt_groups
from winefox_core.lora_identity import extract_identity_candidates, expand_aliases, clean_alias
from .lora_profiles import profile_from_comfy_metadata

MAX_HEADER = 8 * 1024 * 1024
MAX_SIDECAR = 2 * 1024 * 1024


def _read_sidecar(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SIDECAR:
            return {}
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def local_lora_profile(path: Path, relative_name: str, family: str) -> tuple[str, dict[str, Any] | None]:
    metadata: dict[str, Any] = {}
    try:
        if path.suffix.lower() == ".safetensors" and not path.is_symlink():
            with path.open("rb") as stream:
                length = struct.unpack("<Q", stream.read(8))[0]
                if 2 <= length <= MAX_HEADER:
                    header = json.loads(stream.read(length))
                    if isinstance(header, dict) and isinstance(header.get("__metadata__"), dict):
                        metadata.update(header["__metadata__"])
    except (OSError, ValueError, struct.error):
        pass
    # Civitai and rgthree sidecars use explicit trained words separately from
    # raw training frequencies. Later (user-maintained) metadata wins.
    for sidecar in (
        path.with_suffix(".civitai.info"), Path(str(path) + ".civitai.info"),
        path.with_suffix(".json"), Path(str(path) + ".rgthree-info.json"),
    ):
        data = _read_sidecar(sidecar)
        if not data:
            continue
        raw = data.get("raw") if isinstance(data.get("raw"), dict) else {}
        civitai = raw.get("civitai") if isinstance(raw.get("civitai"), dict) else {}
        if data.get("name"):
            metadata["modelspec.title"] = data["name"]
        for field in ("aliases", "character", "trigger_words", "activation_tags", "baseModel"):
            if field in data:
                metadata[field] = data[field]
        if "trainedWords" in data or "trainedWords" in civitai:
            words = data.get("trainedWords", civitai.get("trainedWords"))
            if isinstance(words, list):
                # rgthree also stores every training tag in this array. Only
                # model-card/user entries are authoritative trigger words.
                words = [word for word in words if not isinstance(word, dict) or not word.get("metadata") or word.get("user") or word.get("civitai")]
            if words:
                metadata["trainedWords"] = words
    parsed = profile_from_comfy_metadata(relative_name, metadata, default_family=family)
    if parsed is None:
        if not metadata:
            return family, None
        profile = {"lora": relative_name, "character": str(metadata.get("modelspec.title") or path.stem), "trigger_words": []}
    else:
        family, profile = parsed
    profile.update({
        "lora": relative_name, "source": "local_metadata",
        "aliases": text_list(metadata.get("aliases", profile.get("aliases", []))),
        "base_model": str(metadata.get("baseModel") or metadata.get("ss_base_model_version") or ""),
    })
    if metadata.get("character"):
        profile["character"] = str(metadata["character"])
    return family, profile


def provider_lora_profile(summary: dict[str, Any], filename: str, family: str, alias: str = "") -> dict[str, Any]:
    """Normalize provider metadata once, without temporary legacy JSON stores."""
    # A discovery query describes a search, not an identity. Never silently
    # turn it (or training appearance tags) into a new character alias.
    identity_input = {**summary, "file_name": filename, "search_query": "", "trained_words": []}
    identity = extract_identity_candidates(identity_input)
    character = identity.character or Path(filename).stem
    aliases = expand_aliases(
        [*identity.aliases, *text_list(summary.get("aliases")), *text_list(alias)],
        profile_name=character, trained_words=[],
    )
    fields = ("trigger_words", "trained_words", "trainedWords", "activation_tags")
    words = next((text_list(summary[field]) for field in fields if field in summary), [])
    return {
        "lora": filename, "family": family, "character": character,
        "aliases": list(dict.fromkeys(value for raw in aliases if (value := clean_alias(raw)))),
        "aliases_authoritative": True, "trigger_words": words,
        "prompt_tag_groups": profile_prompt_groups(words),
        "trigger_source": str(summary.get("trained_words_source") or ("provider" if words else "unknown")),
        "source": str(summary.get("source") or "civitai"),
        "source_title": str(summary.get("model_name") or ""),
        "source_version_title": str(summary.get("version_name") or ""),
        "base_model": str(summary.get("base_model") or ""),
        "strength_model": .8, "strength_clip": .8,
    }

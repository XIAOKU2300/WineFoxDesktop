"""本地库编排服务：列表/详情/编辑/删除/隔离/恢复/匹配预览。"""

from __future__ import annotations

import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from desktop_backend.errors import AppError
from winefox_core.lora_identity import clean_alias, expand_aliases, extract_identity_candidates, normalize_key
from winefox_core.lora_service import (
    LoraService,
    LoraProfile,
    SUPPORTED_MODEL_FAMILIES,
    normalize_lora_key,
)
from winefox_core.storage_models import json_dumps, utcnow_iso, validate_public_json

logger = logging.getLogger("winefox.runtime.lora_library")

#: 本地权重文件候选来源子目录（相对 data_root）；deleted_loras 不在此列，防止误找回已隔离文件。
_SOURCE_SUBDIRS = ("models/loras", "loras", "imports", "legacy")
_DELETED_DIR_NAME = "deleted_loras"
_MAX_LIST = 2000


def _bounded_text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:limit]


def _public_text(value: Any, limit: int = 200) -> str:
    """脱敏公开文本：命中 URL/凭据/绝对路径模式的字符串一律置空。"""
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return ""
    if re.search(r"(?i)https?://|www\.|token=|api[_-]?key|authorization|cookie", text):
        return ""
    if re.search(r"(?i)(?:[a-z]:[\\/]|\\\\|^[\\/])", text):
        return ""
    return text[:limit]


def _split_list(value: Any):
    if value is None:
        return None
    if isinstance(value, str):
        return [piece for piece in re.split(r"[,，;；\n\r]+", value) if piece]
    return list(value)


def _clean_list(values: Any) -> list[str]:
    """逐个 clean_alias 清洗并去重（保留顺序）。"""
    result: list[str] = []
    seen: set[str] = set()
    for item in _split_list(values) or []:
        cleaned = clean_alias(str(item))
        if not cleaned:
            continue
        marker = normalize_key(cleaned)
        if marker and marker not in seen:
            seen.add(marker)
            result.append(cleaned)
    return result


class LoraLibraryService:
    """本地库编排服务。删除只删索引且文件移入可恢复目录；隔离可恢复且仅回原位。"""

    def __init__(
        self,
        *,
        db,
        data_root: Path,
        asset_store,
        profile_store,
        service_factory: Callable | None = None,
    ) -> None:
        self._db = db
        self.data_root = Path(data_root)
        self.asset_store = asset_store
        self.profile_store = profile_store
        self.service_factory = service_factory

    # -- 服务构造 ------------------------------------------------------------

    def _build_service(self) -> LoraService:
        """构造匹配服务：优先注入的 service_factory，否则从 profile_store 加载。"""
        if self.service_factory is not None:
            return self.service_factory()
        return LoraService(self._load_profile_map())

    def _load_profile_map(self) -> dict[str, dict[str, dict[str, Any]]]:
        data: dict[str, dict[str, dict[str, Any]]] = {}
        for family in SUPPORTED_MODEL_FAMILIES:
            data[family] = {
                rec.display_key: dict(rec.profile or {})
                for rec in self.profile_store.get_family(family)
            }
        return data

    # -- 档案关联 ------------------------------------------------------------

    def _profile_for_asset(self, asset: Any) -> tuple[Any, dict[str, Any]]:
        wanted = normalize_lora_key(asset.filename)
        for rec in self.profile_store.get_family(asset.model_family):
            profile = dict(rec.profile or {})
            if wanted and normalize_lora_key(profile.get("lora") or profile.get("lora_name") or "") == wanted:
                return rec, profile
        return None, {}

    # -- 公开安全字段 ---------------------------------------------------------

    def _asset_public(self, asset: Any) -> dict[str, Any]:
        rec, profile = self._profile_for_asset(asset)
        lp = LoraProfile.from_dict(rec.display_key if rec else "", profile, family=asset.model_family) if profile else None
        identity = extract_identity_candidates(profile) if profile else None
        info = dict(asset.info or {})
        character = str(lp.character if lp else (identity.character if identity else "")).strip()
        aliases = list(lp.aliases if lp and lp.aliases else (identity.aliases if identity else []))
        triggers = list(lp.trigger_words if lp else [])
        role = list(lp.role_terms if lp else [])
        name = str(asset.display_name or character or Path(asset.filename).stem)
        base_model = str(profile.get("base_model") or info.get("base_model") or "").strip()
        return {
            "id": asset.id,
            "model_id": asset.model_id,
            "version_id": asset.version_id,
            "name": _public_text(name, 200),
            "family": asset.model_family,
            "filename": _public_text(asset.filename, 200),
            "comfy_key": _public_text(asset.comfy_key, 200),
            "character": _public_text(character, 100),
            "aliases": [_public_text(item, 100) for item in aliases if _public_text(item, 100)],
            "triggers": [_public_text(item, 100) for item in triggers if _public_text(item, 100)],
            "role": [_public_text(item, 100) for item in role if _public_text(item, 100)],
            "preview": _public_text(asset.preview_key, 200) or None,
            "state": asset.state,
            "base_model": _public_text(base_model, 80),
            "disabled": asset.disabled,
            "disabled_reason": _public_text(asset.disable_reason, 200) or None,
            "score": round(float(identity.confidence), 3) if identity else None,
            "source": _public_text(asset.source, 60),
            "quarantine_relpath": _public_text(asset.quarantine_relpath, 200) or None,
            "quarantine_reason": _public_text(info.get("quarantine_reason"), 200) or None,
            "created_at": asset.created_at,
            "updated_at": asset.updated_at,
            "installed_at": asset.installed_at,
        }

    def _require_asset(self, asset_id: int) -> Any:
        asset = self.asset_store.get_by_id(int(asset_id))
        if asset is None:
            raise AppError(404, "LORA_ASSET_NOT_FOUND", f"LoRA 资产不存在: {asset_id}")
        return asset

    # -- 列表 / 详情 ---------------------------------------------------------

    def list(self, family: str | None = None, *, include_quarantined: bool = False) -> list[dict]:
        records = self.asset_store.list(
            model_family=family,
            state=None,
            include_disabled=True,
            limit=_MAX_LIST,
            offset=0,
        )
        if not include_quarantined:
            records = [r for r in records if r.state != "quarantined"]
        return [self._asset_public(r) for r in records]

    def get(self, asset_id: int) -> dict:
        return self._asset_public(self._require_asset(asset_id))

    # -- 编辑 ----------------------------------------------------------------

    def update(
        self,
        asset_id: int,
        *,
        aliases=None,
        role=None,
        triggers=None,
        disabled=None,
    ) -> dict:
        asset = self._require_asset(asset_id)
        rec, profile = self._profile_for_asset(asset)
        if not profile:
            key = Path(asset.filename).stem
            profile = {
                "lora": asset.filename,
                "character": "",
                "aliases": [],
                "trigger_words": [],
                "role_terms": [],
                "source": "local",
                "name": str(asset.display_name or Path(asset.filename).stem),
            }
        else:
            key = rec.display_key
        existing_aliases = [str(item) for item in (profile.get("aliases") or []) if str(item or "").strip()]
        new_aliases = _clean_list(aliases) if aliases is not None else existing_aliases
        profile_name = str(profile.get("character") or profile.get("name") or asset.display_name or Path(asset.filename).stem)
        trained = (
            [str(item) for item in (profile.get("trigger_words") or []) if str(item or "").strip()]
            if triggers is None
            else [str(item) for item in _clean_list(triggers) if str(item or "").strip()]
        )
        profile["aliases"] = expand_aliases(existing_aliases + new_aliases, profile_name=profile_name, trained_words=trained)
        if role is not None:
            profile["role_terms"] = _clean_list(role)
        if triggers is not None:
            profile["trigger_words"] = _clean_list(triggers)
        if not str(profile.get("character") or "").strip():
            identity = extract_identity_candidates(profile)
            if identity and identity.character:
                profile["character"] = identity.character
        self.profile_store.save(asset.model_family, key, profile)
        if disabled is not None:
            if bool(disabled):
                self.asset_store.set_disabled(asset_id, reason="用户禁用")
            else:
                self.asset_store.clear_disabled(asset_id)
        return self.get(asset_id)

    # -- 隔离 / 恢复 / 删除 --------------------------------------------------

    def _source_dirs(self) -> list[Path]:
        return [d for sub in _SOURCE_SUBDIRS for d in [self.data_root / sub] if d.is_dir()]

    def _source_base(self) -> Path:
        for base in self._source_dirs():
            return base
        base = self.data_root / "models" / "loras"
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return base

    def _locate_source_file(self, asset: Any) -> Path | None:
        """在候选来源目录中定位资产权重文件；找不到返回 None。"""
        wanted = normalize_lora_key(asset.filename)
        for base in self._source_dirs():
            try:
                direct = base / asset.filename
                if direct.is_file() and direct.stat().st_size > 0:
                    return direct
            except OSError:
                pass
            try:
                found = LoraService._find_local_lora_file(base, asset.filename)
                if found is not None:
                    return found
            except Exception:
                continue
        return None

    def _deleted_dir(self) -> Path:
        return self.data_root / _DELETED_DIR_NAME

    def _unique_relpath(self, relpath: str) -> str:
        if not (self.data_root / relpath).exists():
            return relpath
        prefix = relpath[: relpath.rfind("/") + 1]
        name = relpath[relpath.rfind("/") + 1:]
        for index in range(1, 1000):
            candidate = f"{prefix}{index}_{name}"
            if not (self.data_root / candidate).exists():
                return candidate
        return relpath

    def quarantine(self, asset_id: int, reason: str = "") -> dict:
        asset = self._require_asset(asset_id)
        if asset.state == "quarantined":
            raise AppError(409, "LORA_ALREADY_QUARANTINED", "LoRA 已处于隔离状态")
        stamp = int(time.time())
        relpath = f"{_DELETED_DIR_NAME}/{stamp}_{asset.filename}"
        try:
            src = self._locate_source_file(asset)
            if src is not None:
                relpath = self._unique_relpath(relpath)
                self._deleted_dir().mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(self.data_root / relpath))
        except Exception as exc:
            logger.warning("LoRA 隔离移动文件失败 asset=%s: %s", asset_id, exc)
        updated = self.asset_store.quarantine(asset_id, relpath)
        reason = _public_text(reason, 200)
        if reason:
            self._set_info_field(asset_id, "quarantine_reason", reason)
            updated = self.asset_store.get_by_id(asset_id) or updated
        return self._asset_public(updated)

    def restore(self, asset_id: int) -> dict:
        asset = self._require_asset(asset_id)
        if asset.state != "quarantined" or not asset.quarantine_relpath:
            raise AppError(409, "LORA_NOT_QUARANTINED", "LoRA 不在隔离状态，无法恢复")
        root = self.data_root.resolve()
        quarantined = (self.data_root / asset.quarantine_relpath).resolve(strict=False)
        if not quarantined.is_relative_to(root) or quarantined == root:
            raise AppError(400, "LORA_RESTORE_PATH_UNSAFE", "隔离路径越界，拒绝恢复")
        try:
            if quarantined.is_file():
                base = self._source_base()
                target = base / asset.filename
                shutil.move(str(quarantined), str(target))
        except Exception as exc:
            logger.warning("LoRA 恢复移动文件失败 asset=%s: %s", asset_id, exc)
        updated = self.asset_store.restore(asset_id)
        return self._asset_public(updated)

    def delete(self, asset_id: int) -> None:
        asset = self._require_asset(asset_id)
        try:
            src = self._locate_source_file(asset)
            if src is not None:
                relpath = self._unique_relpath(f"{_DELETED_DIR_NAME}/{int(time.time())}_{asset.filename}")
                self._deleted_dir().mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(self.data_root / relpath))
        except Exception as exc:
            logger.warning("LoRA 删除保留文件失败 asset=%s: %s", asset_id, exc)
        self.asset_store.delete(asset_id)

    def _set_info_field(self, asset_id: int, key: str, value: Any) -> None:
        asset = self.asset_store.get_by_id(int(asset_id))
        if asset is None:
            return
        info = dict(asset.info or {})
        info[key] = value
        validate_public_json(info, name="info")
        with self._db.transaction():
            self._db.connection.execute(
                "UPDATE lora_assets SET info = ?, updated_at = ? WHERE id = ?",
                (json_dumps(info), utcnow_iso(), int(asset_id)),
            )

    # -- 匹配预览 ------------------------------------------------------------

    async def match_preview(self, prompt: str, *, family: str, limit: int = 8) -> dict:
        prompt = _bounded_text(prompt, 500)
        if not prompt:
            raise AppError(422, "LORA_MATCH_INVALID", "请输入要匹配的提示词")
        family = str(family or "").strip().lower()
        if family not in SUPPORTED_MODEL_FAMILIES:
            raise AppError(422, "LORA_MATCH_INVALID", f"不支持的模型族: {family}")
        limit = max(1, min(int(limit or 8), 20))
        service = self._build_service()
        result = service.match(prompt, family=family, limit=limit)
        profiles = service.profiles_for(family)
        matches: list[dict[str, Any]] = []
        for selection in result.matches:
            lp = profiles.get(selection.key)
            aliases = list(lp.aliases if lp else [])
            triggers = list(selection.trigger_words) or list(lp.trigger_words if lp else [])
            character = _public_text(selection.character or selection.key, 100)
            matches.append({
                "key": _public_text(selection.key, 100),
                "name": _public_text(selection.lora_name, 200),
                "character": character,
                "aliases": [_public_text(item, 100) for item in aliases if _public_text(item, 100)],
                "triggers": [_public_text(item, 100) for item in triggers if _public_text(item, 100)],
                "score": round(float(selection.score), 2),
                "relevance": "精确匹配" if selection.match_kind == "exact" else "模糊匹配",
                "connector": bool(result.connector_found),
                "note": _public_text(
                    f"本地命中：{character or selection.key}（{selection.matched_term or selection.key}）",
                    200,
                ),
            })
        return {
            "prompt": prompt,
            "family": family,
            "multi": result.multi,
            "connector_found": result.connector_found,
            "matches": matches,
        }


__all__ = ["LoraLibraryService"]

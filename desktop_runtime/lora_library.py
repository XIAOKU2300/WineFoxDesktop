"""本地库编排服务：列表/详情/编辑/删除/隔离/恢复/匹配预览。"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from desktop_backend.errors import AppError
from winefox_core.lora_identity import clean_alias, expand_aliases, extract_identity_candidates, normalize_key
from winefox_core.lora_service import (
    LoraService,
    LoraProfile,
    SUPPORTED_MODEL_FAMILIES,
    normalize_lora_key,
)
from winefox_core.storage_models import (
    json_dumps,
    normalize_relative_path,
    utcnow_iso,
    validate_public_json,
)

logger = logging.getLogger("winefox.runtime.lora_library")

#: 本地权重文件候选来源子目录（相对 data_root）；deleted_loras / .winefox-quarantine
#: 不在候选，防止误找回已隔离文件。
_SOURCE_SUBDIRS = ("models/loras", "loras", "imports", "legacy")
_DELETED_DIR_NAME = "deleted_loras"
_QUARANTINE_DIR_NAME = ".winefox-quarantine"
_MAX_LIST = 2000


def _bounded_text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:limit]


def _public_text(value: Any, limit: int = 200) -> str:
    """脱敏公开文本：命中 URL/凭据/绝对路径/穿越/网络路径模式的字符串一律置空。"""
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return ""
    if re.search(r"(?i)https?://|www\.|ftp://|file://|token=|api[_-]?key|authorization|cookie", text):
        return ""
    if re.search(r"(?i)(?:[a-z]:[\\/]|\\\\|^[\\/]|\.\.|//)", text):
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
        on_profiles_changed: Callable | None = None,
        settings_fn: Callable[[], Any] | None = None,
        llm_service: Any = None,
        preview_service: Any = None,
        civitai_client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._db = db
        self.data_root = Path(data_root)
        self.asset_store = asset_store
        self.profile_store = profile_store
        self.service_factory = service_factory
        self.on_profiles_changed = on_profiles_changed
        self.settings_fn = settings_fn
        self.llm_service = llm_service
        self.preview_service = preview_service
        self.civitai_client_factory = civitai_client_factory
        self._preview_backfilled: set[int] = set()

    # -- 配置 ----------------------------------------------------------------

    def _settings(self) -> dict[str, Any]:
        if self.settings_fn is None:
            return {}
        try:
            snapshot = self.settings_fn()
            if not isinstance(snapshot, Mapping):
                return {}
            value = snapshot.get("lora_download")
            return dict(value) if isinstance(value, Mapping) else {}
        except Exception:
            return {}

    def _local_dir_root(self) -> Path | None:
        """解析配置的 local_dir 固定根目录；未配置/非法返回 None。"""
        raw = str(self._settings().get("local_dir") or "").strip()
        if not raw:
            return None
        try:
            return Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
        except (OSError, ValueError):
            return None

    def _require_local_file_management(self, asset: Any) -> None:
        """Bridge/remote mode has no file-management protocol by design."""
        settings = self._settings()
        mode = str(settings.get("mode") or "auto").strip().lower()
        source = str(getattr(asset, "source", "") or "").strip().lower()
        if (
            mode == "comfyui_bridge"
            or source == "comfyui_bridge"
            or (source == "inventory" and self._local_dir_root() is None)
        ):
            raise AppError(
                403,
                "LORA_BRIDGE_FILE_MANAGEMENT_DISABLED",
                "Bridge 模式仅允许下载和读取，暂不允许隔离、恢复或删除远端文件",
            )

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
            "nsfw": bool(info.get("nsfw")),
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
        self._schedule_preview_backfill(records)
        return [self._asset_public(r) for r in records]

    def _schedule_preview_backfill(self, records: list) -> None:
        """旧版本安装的资产可能没有预览：后台按 Civitai 版本信息补全缩略图。

        每次列表最多补 3 个，避免一次性打爆 Civitai；结果落盘缓存，刷新后可见。
        """
        if self.preview_service is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        pending = [
            r for r in records
            if r.preview_key is None
            and getattr(r, "version_id", None)
            and str(r.version_id).isdigit()
            and r.id not in self._preview_backfilled
        ][:3]
        for record in pending:
            self._preview_backfilled.add(record.id)
            loop.create_task(self._backfill_one_preview(record))

    async def _backfill_one_preview(self, record) -> None:
        if self.civitai_client_factory is None:
            return
        try:
            from civitai_api import CivitaiClient

            from . import net_env

            settings = self._settings()
            await net_env.ensure_probed(str(settings.get("proxy") or ""))
            proxy, _src = net_env.effective_proxy(str(settings.get("proxy") or ""))
            token = ""
            if self.settings_fn is not None:
                # secret 不在 settings 快照里，交由 engine 注入的工厂处理凭据。
                token = ""
            client = self.civitai_client_factory(
                api_token=token, proxy=proxy, timeout=45
            ) or CivitaiClient(api_token=token, proxy=proxy, timeout=45)
            version = await client.get_model_version(int(record.version_id))
            images = [i for i in (version.get("images") or []) if isinstance(i, dict)]
            image_url = ""
            for image in images:
                url = str(image.get("url") or "").strip()
                if url:
                    image_url = url
                    break
            if not image_url:
                return
            key = self.preview_service.register("", image_url, source="civitai")
            if not key:
                return
            await self.preview_service.fetch(str(key))
            self.asset_store.set_preview(record.id, str(key))
        except Exception:
            # 补图是尽力而为：失败不再重试本次会话（_preview_backfilled 已标记）。
            return

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
        # PATCH 传入 aliases 时按“替换”处理，否则 UI 中删掉的旧别名会被
        # existing_aliases 再次合并回来，造成看似保存成功但永远删不掉。
        alias_seed = new_aliases if aliases is not None else existing_aliases
        profile["aliases"] = expand_aliases(
            alias_seed,
            profile_name=profile_name,
            trained_words=trained,
        )
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
        if self.on_profiles_changed is not None:
            self.on_profiles_changed()
        return self.get(asset_id)

    # -- 隔离 / 恢复 / 删除 --------------------------------------------------

    def _source_dirs(self) -> list[Path]:
        """候选来源目录：优先 local_dir（下载/隔离需明确配置的本地根）。"""
        dirs: list[Path] = []
        local_root = self._local_dir_root()
        if local_root is not None:
            for sub in ("models/loras", "loras", ""):
                d = local_root / sub if sub else local_root
                if d.is_dir():
                    dirs.append(d)
        for sub in _SOURCE_SUBDIRS:
            d = self.data_root / sub
            if d.is_dir():
                dirs.append(d)
        return dirs

    def _source_base(self) -> Path:
        for base in self._source_dirs():
            return base
        local_root = self._local_dir_root()
        if local_root is not None:
            return local_root
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

    def _unique_relpath(self, root: Path, relpath: str) -> str:
        if not (root / relpath).exists():
            return relpath
        prefix = relpath[: relpath.rfind("/") + 1]
        name = relpath[relpath.rfind("/") + 1:]
        for index in range(1, 1000):
            candidate = f"{prefix}{index}_{name}"
            if not (root / candidate).exists():
                return candidate
        return relpath

    def _quarantine_root_for(self, src: Path) -> tuple[Path, str]:
        """返回 (隔离根, 标记)：源在 local_dir 下用 local_dir，否则 data_root。"""
        local_root = self._local_dir_root()
        if local_root is not None:
            try:
                if src.resolve().is_relative_to(local_root.resolve()):
                    return local_root, "local"
            except (OSError, ValueError):
                pass
        return self.data_root, "data"

    def _root_for_quarantine(self, asset: Any) -> Path:
        info = dict(asset.info or {})
        if str(info.get("quarantine_root") or "").strip().lower() == "local":
            local_root = self._local_dir_root()
            if local_root is not None:
                return local_root
        return self.data_root

    def quarantine(self, asset_id: int, reason: str = "") -> dict:
        """先移动权重文件到同磁盘 .winefox-quarantine，成功才提交 DB 隔离状态。

        文件不在本地目录或移动失败：抛错且 DB 保持原状（不显示已隔离）。
        """
        asset = self._require_asset(asset_id)
        self._require_local_file_management(asset)
        if asset.state == "quarantined":
            raise AppError(409, "LORA_ALREADY_QUARANTINED", "LoRA 已处于隔离状态")
        src = self._locate_source_file(asset)
        if src is None:
            raise AppError(409, "LORA_QUARANTINE_FILE_MISSING", "权重文件不在本地目录，无法隔离")
        root, marker = self._quarantine_root_for(src)
        root_resolved = root.resolve()
        try:
            original_relpath = src.resolve().relative_to(root_resolved).as_posix()
        except ValueError as exc:
            raise AppError(400, "LORA_QUARANTINE_PATH_UNSAFE", "LoRA 源文件不在受管目录内") from exc
        original_relpath = normalize_relative_path(
            original_relpath, name="original_relpath"
        )
        stamp = int(time.time())
        relpath = f"{_QUARANTINE_DIR_NAME}/{stamp}_{asset.filename}"
        try:
            (root / _QUARANTINE_DIR_NAME).mkdir(parents=True, exist_ok=True)
            relpath = self._unique_relpath(root, relpath)
            shutil.move(str(src), str(root / relpath))
        except Exception as exc:
            logger.warning("LoRA 隔离移动文件失败 asset=%s: %s", asset_id, exc)
            raise AppError(409, "LORA_QUARANTINE_MOVE_FAILED", "权重文件移动失败，未标记隔离") from exc
        try:
            with self._db.transaction():
                self.asset_store.quarantine(asset_id, relpath)
                reason = _public_text(reason, 200)
                if reason:
                    self._set_info_field(asset_id, "quarantine_reason", reason)
                self._set_info_field(asset_id, "quarantine_root", marker)
                self._set_info_field(asset_id, "original_relpath", original_relpath)
        except Exception as exc:
            try:
                shutil.move(str(root / relpath), str(src))
            except Exception:
                logger.exception("LoRA 隔离 DB 失败且文件回滚失败 asset=%s", asset_id)
                raise AppError(
                    500,
                    "LORA_QUARANTINE_ROLLBACK_FAILED",
                    "隔离记录失败且文件无法自动复原，请检查隔离目录",
                ) from exc
            raise AppError(500, "LORA_QUARANTINE_DB_FAILED", "隔离记录失败，文件已恢复原位") from exc
        updated = self._asset_public(self._require_asset(asset_id))
        if self.on_profiles_changed is not None:
            self.on_profiles_changed()
        return updated

    def restore(self, asset_id: int) -> dict:
        """先移动隔离文件回原位，成功才提交 DB 恢复状态。

        隔离文件不存在或移动失败：保持隔离状态并抛错。
        """
        asset = self._require_asset(asset_id)
        self._require_local_file_management(asset)
        if asset.state != "quarantined" or not asset.quarantine_relpath:
            raise AppError(409, "LORA_NOT_QUARANTINED", "LoRA 不在隔离状态，无法恢复")
        root = self._root_for_quarantine(asset)
        root_resolved = root.resolve()
        quarantined = (root / asset.quarantine_relpath).resolve(strict=False)
        if not quarantined.is_relative_to(root_resolved) or quarantined == root_resolved:
            raise AppError(400, "LORA_RESTORE_PATH_UNSAFE", "隔离路径越界，拒绝恢复")
        if not quarantined.is_file():
            raise AppError(409, "LORA_RESTORE_FILE_MISSING", "隔离文件不存在，保持隔离状态")
        info = dict(asset.info or {})
        original_relpath = normalize_relative_path(
            str(info.get("original_relpath") or asset.filename),
            name="original_relpath",
        )
        base = root_resolved
        target = (base / original_relpath).resolve(strict=False)
        if not target.is_relative_to(base) or target == base:
            raise AppError(400, "LORA_RESTORE_TARGET_UNSAFE", "恢复目标越界，拒绝恢复")
        if target.exists():
            raise AppError(409, "LORA_RESTORE_TARGET_EXISTS", "原位置已有同名文件，拒绝覆盖")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(quarantined), str(target))
        except Exception as exc:
            logger.warning("LoRA 恢复移动文件失败 asset=%s: %s", asset_id, exc)
            raise AppError(409, "LORA_RESTORE_MOVE_FAILED", "权重文件恢复失败，保持隔离状态") from exc
        try:
            updated = self.asset_store.restore(asset_id)
        except Exception as exc:
            try:
                quarantined.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(quarantined))
            except Exception:
                logger.exception("LoRA 恢复 DB 失败且文件回滚失败 asset=%s", asset_id)
                raise AppError(
                    500,
                    "LORA_RESTORE_ROLLBACK_FAILED",
                    "恢复记录失败且文件无法退回隔离目录，请检查文件位置",
                ) from exc
            raise AppError(500, "LORA_RESTORE_DB_FAILED", "恢复记录失败，文件仍保持隔离") from exc
        result = self._asset_public(updated)
        if self.on_profiles_changed is not None:
            self.on_profiles_changed()
        return result

    def delete(self, asset_id: int) -> None:
        """仅作内部兼容（删除即隔离复用）；UI/API 不暴露永久删除。"""
        raise AppError(
            405,
            "LORA_PERMANENT_DELETE_DISABLED",
            "永久删除已禁用；请使用隔离功能，以便之后恢复",
        )

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
        # 已安装/未禁用/未隔离且文件在库的资产可进入候选；
        # 无资产记录的档案无禁用/隔离/缺失状态，视为可用。
        assets = {
            normalize_lora_key(a.filename): a
            for a in self.asset_store.list(family, limit=2000)
        }

        def _is_eligible(lora_name: str) -> bool:
            asset = assets.get(normalize_lora_key(lora_name))
            if asset is None:
                return True
            return (
                asset.state == "installed"
                and not asset.disabled
                and not bool((asset.info or {}).get("file_missing"))
            )

        matches: list[dict[str, Any]] = []
        for selection in result.matches:
            if not _is_eligible(selection.lora_name):
                continue
            lp = profiles.get(selection.key)
            asset = assets.get(normalize_lora_key(selection.lora_name))
            extra = dict(lp.extra) if lp else {}
            aliases = list(lp.aliases if lp else [])
            triggers = list(selection.trigger_words) or list(lp.trigger_words if lp else [])
            character = _public_text(selection.character or selection.key, 100)
            connector = _public_text(result.connector, 60)
            strength_model = getattr(selection, "strength_model", None)
            strength_clip = getattr(selection, "strength_clip", None)
            if strength_model is None and lp is not None:
                strength_model = lp.strength_model
            if strength_clip is None and lp is not None:
                strength_clip = lp.strength_clip
            matches.append({
                "key": _public_text(selection.key, 100),
                "name": _public_text(selection.lora_name, 200),
                "character": character,
                "aliases": [_public_text(item, 100) for item in aliases if _public_text(item, 100)],
                "triggers": [_public_text(item, 100) for item in triggers if _public_text(item, 100)],
                "score": round(min(1.0, float(selection.score) / 100.0), 3),
                "strength_model": strength_model,
                "strength_clip": strength_clip,
                "relevance": "精确匹配" if selection.match_kind == "exact" else "模糊匹配",
                "connector": connector,
                "nsfw": bool((asset.info if asset else {}).get("nsfw") or extra.get("nsfw")),
                "disabled": bool(asset.disabled if asset else False),
                "family": asset.model_family if asset else selection.family,
                "filename": _public_text(asset.filename if asset else selection.lora_name, 200),
                "id": asset.id if asset else None,
                "source": "rule",
                "note": _public_text(
                    f"本地命中：{character or selection.key}（{selection.matched_term or selection.key}）",
                    200,
                ),
            })
        if not matches and self.llm_service is not None:
            llm_item = await self._llm_rescue(service, prompt, family, assets, _is_eligible, limit)
            if llm_item is not None:
                matches = [llm_item]
        return {
            "prompt": prompt,
            "family": family,
            "multi": result.multi,
            "connector_found": result.connector_found,
            "connector": _public_text(result.connector, 60),
            "matches": matches,
        }

    # -- LLM 补救层（保守：规则无可靠命中时才调用，结果仍须通过本地校验） ----

    def _family_revision(self, family: str) -> int:
        try:
            # 每条档案各自递增 revision；取 max 会漏掉“修改非最大 revision
            # 档案”的变化。求和在无删除的档案存储中是单调聚合版本。
            return sum(record.revision for record in self.profile_store.get_family(family))
        except Exception:
            return 0

    def _llm_candidates(self, service, family: str, assets: dict, is_eligible: Callable) -> list[dict]:
        """从当前 family 已装/未禁用/未隔离且文件在库的档案构造 LLM 候选。

        候选携带解析所需的元数据，但送进 LLM 的净化载荷只含公开安全字段。
        """
        candidates: list[dict] = []
        for profile in service.profiles_for(family).values():
            lora_name = str(profile.lora_name or "").strip()
            if not lora_name or not is_eligible(lora_name):
                continue
            asset = assets.get(normalize_lora_key(lora_name))
            extra = dict(profile.extra or {})
            candidates.append({
                "key": str(profile.key or profile.lora_name),
                "id": asset.id if asset else None,
                "character": str(profile.character or profile.key or ""),
                "name": str(Path(lora_name).stem),
                "lora": lora_name,
                "lora_name": lora_name,
                "family": family,
                "filename": lora_name,
                "aliases": list(profile.aliases or []),
                "trigger_words": list(profile.trigger_words or []),
                "search_aliases": list(extra.get("search_aliases") or []),
                "tags": list(extra.get("tags") or []),
                "title": str(extra.get("title") or ""),
                "strength_model": profile.strength_model,
                "strength_clip": profile.strength_clip,
                "nsfw": bool((asset.info or {}).get("nsfw") if asset else extra.get("nsfw")),
                "disabled": bool(asset.disabled if asset else False),
            })
        return candidates[:8]

    async def _llm_rescue(self, service, prompt: str, family: str, assets: dict, is_eligible: Callable, limit: int) -> dict | None:
        llm = self.llm_service
        if llm is None or not llm.llm_enabled():
            return None
        candidates = self._llm_candidates(service, family, assets, is_eligible)
        if not candidates:
            return None
        try:
            result = await llm.match(
                prompt=prompt,
                candidates=candidates,
                family=family,
                revision=self._family_revision(family),
                limit=max(1, min(int(limit or 5), 8)),
            )
        except Exception:
            return None
        if not isinstance(result, dict):
            return None
        matched = result.get("matched")
        if not isinstance(matched, dict):
            return None
        return self._llm_match_item(matched, result)

    def _llm_match_item(self, matched: dict, result: dict) -> dict:
        key = _public_text(str(matched.get("key") or matched.get("id") or ""), 100)
        lora_name = str(matched.get("lora_name") or matched.get("lora") or "")
        character = _public_text(str(matched.get("character") or key), 100)
        strength_model = matched.get("strength_model")
        strength_clip = matched.get("strength_clip")
        score = 0.0
        try:
            score = float(result.get("confidence") or 0)
        except (TypeError, ValueError):
            score = 0.0
        reason = _public_text(str(result.get("reason") or ""), 200) or f"LLM 辅助命中：{character}"
        return {
            "key": key,
            "name": _public_text(lora_name or str(matched.get("name") or ""), 200),
            "character": character,
            "aliases": [_public_text(x, 100) for x in (matched.get("aliases") or []) if _public_text(x, 100)],
            "triggers": [_public_text(x, 100) for x in (matched.get("trigger_words") or []) if _public_text(x, 100)],
            "score": round(min(1.0, score), 3),
            "strength_model": strength_model,
            "strength_clip": strength_clip,
            "relevance": "LLM 辅助",
            "connector": "",
            "nsfw": bool(matched.get("nsfw")),
            "disabled": bool(matched.get("disabled")),
            "family": str(matched.get("family") or "illustrious"),
            "filename": _public_text(lora_name, 200),
            "id": matched.get("id"),
            "source": "llm",
            "note": reason,
        }


__all__ = ["LoraLibraryService"]

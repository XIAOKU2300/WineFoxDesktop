"""Adapters joining the versioned settings and OS secret stores to the backend API."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from typing import Any

from desktop_backend.errors import AppError
from winefox_core.config import ConfigError, ConfigStatus, SettingsStore
from winefox_core.secrets import SecretStore, SecretStoreError


SECRET_NAMES = (
    "comfyui_api_key",
    "comfyui_password",
    "comfyui_username",
    "comfyui_custom_header",
    "ai_api_key",
    "vision_api_key",
    "civitai_api_token",
    "civitai_cookie",
    "comfyui_bridge_token",
)

_PROVIDER_FIELDS = {
    "ai_provider": frozenset({"enabled", "base_url", "model", "utility_model", "timeout_seconds"}),
    "vision_provider": frozenset(
        {"enabled", "follow_text_provider", "base_url", "model", "timeout_seconds", "max_tokens"}
    ),
}

_LORA_DOWNLOAD_FIELDS = frozenset({
    "mode", "local_dir", "proxy", "bridge_endpoint", "bridge_proxy",
    "request_timeout", "download_timeout", "split",
})


def _merge(target: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = deepcopy(value)


def _provider_patch(name: str, value: dict[str, Any]) -> dict[str, Any]:
    """Return only public provider fields; credentials always use SecretStore."""

    allowed = _PROVIDER_FIELDS[name]
    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        if key not in allowed:
            continue
        if key in {"enabled", "follow_text_provider"}:
            cleaned[key] = bool(item)
        elif key in {"timeout_seconds", "max_tokens"}:
            try:
                number = int(item)
            except (TypeError, ValueError):
                continue
            cleaned[key] = max(5, min(number, 180)) if key == "timeout_seconds" else max(128, min(number, 4096))
        else:
            cleaned[key] = str(item or "").strip()
    return cleaned


def _lora_download_patch(value: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        if key not in _LORA_DOWNLOAD_FIELDS:
            continue
        if key == "mode":
            mode = str(item or "auto").strip().lower()
            if mode in {"auto", "local", "comfyui_bridge"}:
                cleaned[key] = mode
        elif key in {"request_timeout", "download_timeout", "split"}:
            try:
                number = int(item)
            except (TypeError, ValueError):
                continue
            if key == "request_timeout":
                cleaned[key] = max(10, min(number, 180))
            elif key == "download_timeout":
                cleaned[key] = max(60, min(number, 24 * 60 * 60))
            else:
                cleaned[key] = max(1, min(number, 8))
        else:
            limit = 1024 if key == "local_dir" else 512
            cleaned[key] = str(item or "").strip()[:limit]
    return cleaned


class DesktopConfigAdapter:
    """Expose Agent 03 stores through Agent 07's asynchronous protocol.

    UI-only preferences which are outside the frozen Agent 03 schema live in a
    separate non-sensitive ``desktop.json`` file. Secrets never enter either
    JSON file.
    """

    def __init__(self, config_dir: Path, secret_store: SecretStore | None = None) -> None:
        self._store = SettingsStore(config_dir)
        self._secret_store = secret_store or SecretStore()
        self._desktop_path = Path(config_dir) / "desktop.json"
        self._lock = asyncio.Lock()
        self._desktop: dict[str, Any] = {
            "auth": {"mode": "none"},
            "tls": {"enabled": False, "verify": True, "ca_configured": False},
            "ai_provider": {
                "enabled": False,
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-4.1-mini",
                "utility_model": "gpt-4.1-mini",
                "timeout_seconds": 60,
            },
            "vision_provider": {
                "enabled": False,
                "follow_text_provider": True,
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-4.1-mini",
                "timeout_seconds": 90,
                "max_tokens": 700,
            },
            "lora_download": {
                "mode": "auto",
                "local_dir": "",
                "proxy": "",
                "bridge_endpoint": "/astrbot/lora",
                "bridge_proxy": "",
                "request_timeout": 60,
                "download_timeout": 7200,
                "split": 4,
            },
            "defaults": {"family": "illustrious", "workflow": "workflow_api.json", "width": 832, "height": 1216},
            "wizard": {"completed": False},
        }
        self._load_desktop()

    def _load_desktop(self) -> None:
        if not self._desktop_path.is_file():
            return
        try:
            import json

            value = json.loads(self._desktop_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                public_value = {
                    key: item
                    for key, item in value.items()
                    if key not in _PROVIDER_FIELDS and key != "lora_download"
                }
                _merge(self._desktop, public_value)
                for name in _PROVIDER_FIELDS:
                    provider = value.get(name)
                    if isinstance(provider, dict):
                        _merge(self._desktop[name], _provider_patch(name, provider))
                lora_download = value.get("lora_download")
                if isinstance(lora_download, dict):
                    _merge(self._desktop["lora_download"], _lora_download_patch(lora_download))

                # Older desktop builds placed the visual model inside ai_provider.
                # Preserve that preference while moving it into the separate contract.
                if not isinstance(value.get("vision_provider"), dict):
                    legacy = value.get("ai_provider")
                    if isinstance(legacy, dict) and legacy.get("vision_model"):
                        self._desktop["vision_provider"]["model"] = str(legacy["vision_model"])
        except (OSError, ValueError):
            # A damaged UI preference file must not make connection settings unusable.
            pass

    def _save_desktop(self) -> None:
        import json
        import os

        self._desktop_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._desktop_path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(self._desktop, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self._desktop_path)

    def _core(self) -> dict[str, Any]:
        loaded = self._store.load()
        if loaded.status is ConfigStatus.NOT_FOUND:
            data = self._store.require()
            # TLS verification is secure by default for new desktop installs.
            data["comfyui"]["ssl_verify"] = True
            self._store.save(data)
            return data
        if not loaded.ok:
            raise AppError(500, "CONFIG_INVALID", loaded.error or "配置文件不可用")
        return loaded.config or {}

    def snapshot(self) -> dict[str, Any]:
        core = self._core()
        comfy = core.get("comfyui", {})
        desktop = deepcopy(self._desktop)
        desktop["comfyui"] = {
            "server_address": comfy.get("address", "127.0.0.1:8188"),
            "address": comfy.get("address", "127.0.0.1:8188"),
            "ssl_verify": bool(comfy.get("ssl_verify", True)),
        }
        desktop["model_family"] = core.get("model_family", "illustrious")
        desktop["workflows"] = deepcopy(core.get("workflows", {}))
        desktop["size"] = deepcopy(core.get("size", {}))
        desktop["queue"] = deepcopy(core.get("queue", {}))
        desktop["ui"] = deepcopy(core.get("ui", {}))
        desktop["data_path"] = r"%LOCALAPPDATA%\WineFox"
        return desktop

    async def get_settings(self) -> dict[str, Any]:
        async with self._lock:
            return self.snapshot()

    async def patch_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise AppError(422, "CONFIG_INVALID", "设置必须是 JSON 对象")
        async with self._lock:
            core_patch: dict[str, Any] = {}
            comfy = patch.get("comfyui")
            if isinstance(comfy, dict):
                mapped: dict[str, Any] = {}
                address = comfy.get("server_address", comfy.get("address"))
                if address is not None:
                    mapped["address"] = str(address).strip()
                if "ssl_verify" in comfy:
                    mapped["ssl_verify"] = bool(comfy["ssl_verify"])
                if mapped:
                    core_patch["comfyui"] = mapped
            for key in ("model_family", "workflows", "size", "queue", "ui"):
                if key in patch:
                    core_patch[key] = deepcopy(patch[key])
            defaults = patch.get("defaults")
            if isinstance(defaults, dict):
                _merge(self._desktop.setdefault("defaults", {}), defaults)
                if "family" in defaults:
                    core_patch["model_family"] = defaults["family"]
                size_patch = {k: defaults[k] for k in ("width", "height") if k in defaults}
                if size_patch:
                    core_patch.setdefault("size", {}).update(size_patch)
                if "workflow" in defaults:
                    core_patch.setdefault("workflows", {})["active"] = defaults["workflow"]
            for key in ("auth", "tls", "wizard"):
                value = patch.get(key)
                if isinstance(value, dict):
                    _merge(self._desktop.setdefault(key, {}), value)
            for key in _PROVIDER_FIELDS:
                value = patch.get(key)
                if isinstance(value, dict):
                    _merge(self._desktop[key], _provider_patch(key, value))
            lora_download = patch.get("lora_download")
            if isinstance(lora_download, dict):
                _merge(self._desktop["lora_download"], _lora_download_patch(lora_download))
            tls = self._desktop.get("tls", {})
            if isinstance(tls, dict) and "verify" in tls:
                core_patch.setdefault("comfyui", {})["ssl_verify"] = bool(tls["verify"])
            try:
                if core_patch:
                    self._store.update(core_patch)
                self._save_desktop()
            except ConfigError as exc:
                raise AppError(422, "CONFIG_INVALID", str(exc)) from exc
            return self.snapshot()

    async def put_secret(self, name: str, value: str) -> None:
        try:
            self._secret_store.set(name, value)
        except SecretStoreError as exc:
            raise AppError(500, "SECRET_STORE_FAILED", exc.message) from exc

    async def delete_secret(self, name: str) -> bool:
        try:
            return bool(self._secret_store.delete(name))
        except SecretStoreError as exc:
            raise AppError(500, "SECRET_STORE_FAILED", exc.message) from exc

    def get_secret(self, name: str) -> str | None:
        try:
            return self._secret_store.get(name)
        except SecretStoreError as exc:
            raise AppError(500, "SECRET_STORE_FAILED", exc.message) from exc

    async def secret_statuses(self) -> dict[str, Any]:
        items = []
        for name in SECRET_NAMES:
            try:
                configured = bool(self._secret_store.has(name))
            except SecretStoreError:
                configured = False
            items.append({"name": name, "key": name, "configured": configured})
        return {"items": items}

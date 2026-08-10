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
)


def _merge(target: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = deepcopy(value)


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
                "vision_model": "gpt-4.1-mini",
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
                _merge(self._desktop, value)
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
            for key in ("auth", "tls", "ai_provider", "wizard"):
                value = patch.get(key)
                if isinstance(value, dict):
                    _merge(self._desktop.setdefault(key, {}), value)
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

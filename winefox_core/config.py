"""WineFox Desktop 版本化配置存储。

- settings.json，schemaVersion=1。
- 保存 ComfyUI 地址/TLS/认证引用、模型族、工作流、尺寸、队列、UI 偏好。
- 禁止保存密钥：任何形如 api_key/token/password/secret 的非空叶子值都会被拒绝；
  认证只允许以「引用名」形式出现（auth_ref / auth_refs），真实密钥由
  winefox_core.secrets.SecretStore 保管。
- 原子保存：写 .tmp -> flush -> fsync -> os.replace；旧文件保留为 settings.json.bak。
- 损坏检测：load() 遇到无法解析/结构非法的文件返回 CONFIG_INVALID，绝不覆盖；
  save() 默认拒绝覆盖损坏文件（除非显式 force）。
- public_view() 提供脱敏视图，任何来源都不会出现密钥明文。
"""

from __future__ import annotations

import enum
import json
import logging
import os
import re
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

logger = logging.getLogger("winefox_core.config")

SETTINGS_FILENAME = "settings.json"
BACKUP_FILENAME = "settings.json.bak"
SCHEMA_VERSION = 1

#: 叶子键名若匹配该模式且值为非空字符串 -> 视为密钥，禁止进入配置文件。
_SECRET_KEY_RE = re.compile(
    r"(^|[-_])"
    r"(api[_-]?key|apikey|token|password|passwd|secret|credential|priv[_-]?key)"
    r"($|[-_])",
    re.IGNORECASE,
)
#: 引用名允许的字符（SecretStore 中的条目名，仅作为指针存在）。
_AUTH_REF_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")

#: 各字段长度上限（防呆，避免把配置写爆）。
_MAX_LENGTHS = {
    "comfyui.address": 300,
    "comfyui.auth_ref": 64,
    "model_family": 64,
    "workflows.active": 256,
    "workflows.available": 512,
    "ui.command_prefix": 32,
    "ui.language": 16,
    "ui.prompt_injection": 1000,
}

DEFAULTS: dict[str, Any] = {
    "comfyui": {
        "address": "127.0.0.1:8188",
        "tls": False,
        "ssl_verify": False,
        # 认证引用：指向 SecretStore 条目名，例如 "comfyui_api"；null 表示匿名。
        "auth_ref": None,
    },
    "model_family": "illustrious",
    "workflows": {
        "active": None,
        "available": [],
    },
    "size": {
        "width": 832,
        "height": 1216,
        "upscale": 1.5,
    },
    "queue": {
        "max_concurrent": 1,
        "timeout_seconds": 600,
        "retries": 0,
    },
    "ui": {
        "language": "zh-CN",
        "theme": "system",
        "command_prefix": "jh",
        # 缺了它会因 _coerce 白名单被丢弃，「减少动画」勾选状态永远存不下来
        "reduced_motion": True,
        # 自定义提示词注入：LLM 转译后确定性追加，不进 LLM 请求（省 token）
        "prompt_injection": "",
    },
    # 认证引用集合（可被多个服务复用），值只能是引用名。
    "auth_refs": {
        "comfyui": None,
        "civitai": None,
    },
}


class ConfigStatus(str, enum.Enum):
    OK = "ok"
    NOT_FOUND = "not_found"
    CONFIG_INVALID = "config_invalid"
    UNSUPPORTED_SCHEMA = "unsupported_schema"


class ConfigError(Exception):
    """配置写入/校验错误。"""


class LoadResult:
    __slots__ = ("status", "config", "error")

    def __init__(self, status: ConfigStatus, config: dict | None, error: str | None = None):
        self.status = status
        self.config = config
        self.error = error

    @property
    def ok(self) -> bool:
        return self.status is ConfigStatus.OK

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"LoadResult(status={self.status.value}, error={self.error!r})"


def _default_config() -> dict[str, Any]:
    return deepcopy(DEFAULTS)


def _leaf_paths(value: Any, prefix: tuple[str, ...] = ()):
    """深度遍历叶子 (path_tuple, value)。"""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _leaf_paths(item, prefix + (str(key),))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _leaf_paths(item, prefix + (str(index),))
    else:
        yield prefix, value


def _looks_like_secret(path: tuple[str, ...], value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False  # 空值视为未设置，允许
    last = path[-1] if path else ""
    return bool(_SECRET_KEY_RE.search(str(last)))


def _auth_ref_value(value: Any) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    text = str(value).strip()
    if not _AUTH_REF_RE.match(text):
        raise ConfigError(f"非法认证引用名 {text!r}（仅允许字母/数字/._:-）")
    if _looks_like_secret_value(text):
        raise ConfigError(
            f"认证引用 {text!r} 看起来像密钥明文，请把密钥存入 SecretStore 并只填写引用名"
        )
    return text


#: 常见密钥前缀；命中即视为疑似密钥明文（用于 auth_ref 值启发式）。
_SECRET_VALUE_PREFIXES = (
    "sk-", "sk_", "sk-live", "ghp_", "gho_", "xoxb-", "xoxp-",
    "ak_", "eyj", "ya29.", "-----begin",
)


def _looks_like_secret_value(value: Any) -> bool:
    """启发式：值看起来像密钥明文（前缀命中，或高熵长串）。"""
    if not isinstance(value, str) or not value.strip():
        return False
    low = value.strip().lower()
    if any(low.startswith(p) for p in _SECRET_VALUE_PREFIXES):
        return True
    if (
        len(value) >= 24
        and any(c.isalpha() for c in value)
        and any(c.isdigit() for c in value)
        and any(c in value for c in "-_.")
    ):
        return True
    return False


def _coerce(
    dest: dict[str, Any],
    src: dict[str, Any],
    defaults: dict[str, Any],
    prefix: tuple[str, ...] = (),
) -> None:
    """把 src 按 defaults 的形状合并进 dest，只保留已知键并做基础校验。"""
    for key, default in defaults.items():
        path = prefix + (key,)
        full = ".".join(str(p) for p in path)
        if key not in src:
            dest[key] = deepcopy(default)
            continue
        raw = src[key]
        if isinstance(default, dict):
            if not isinstance(raw, dict):
                raise ConfigError(f"配置段 {full!r} 应为对象")
            dest[key] = {}
            if key == "auth_refs":
                for ref_key, ref_value in raw.items():
                    dest[key][str(ref_key)] = _auth_ref_value(ref_value)
            else:
                _coerce(dest[key], raw, default, path)
        elif isinstance(default, bool):
            if not isinstance(raw, bool):
                raise ConfigError(f"配置项 {full!r} 应为布尔")
            dest[key] = raw
        elif isinstance(default, (int, float)):
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise ConfigError(f"配置项 {full!r} 应为数值")
            dest[key] = raw
        elif isinstance(default, list):
            if not isinstance(raw, list):
                raise ConfigError(f"配置项 {full!r} 应为数组")
            dest[key] = raw
        else:  # str / None
            if key == "auth_ref" or (isinstance(default, str) and key.endswith("_ref")):
                dest[key] = _auth_ref_value(raw)
            elif raw is None:
                dest[key] = None
            elif isinstance(raw, str):
                dest[key] = raw.strip()
            else:
                raise ConfigError(f"配置项 {full!r} 应为字符串")
        # 长度上限
        limit = _MAX_LENGTHS.get(full)
        if limit and isinstance(dest[key], str) and len(dest[key]) > limit:
            raise ConfigError(f"配置项 {full!r} 超过长度上限 {limit}")


def _is_auth_ref_leaf(path: tuple[str, ...]) -> bool:
    """叶子是否属于认证引用字段（值是引用名，不是密钥）。"""
    return bool(path) and (path[-1] == "auth_ref" or "auth_refs" in path)


def _validate_no_secrets(config: dict[str, Any]) -> None:
    """拒绝任何非空密钥叶子进入配置文件。"""
    for path, value in _leaf_paths(config):
        if _looks_like_secret(path, value):
            dotted = ".".join(str(p) for p in path)
            raise ConfigError(
                f"禁止在 settings.json 保存密钥字段 {dotted!r}；"
                f"请改用 winefox_core.secrets.SecretStore，配置中只放认证引用名"
            )
        if _is_auth_ref_leaf(path) and _looks_like_secret_value(value):
            dotted = ".".join(str(p) for p in path)
            raise ConfigError(
                f"认证引用字段 {dotted!r} 的值看起来像密钥明文；"
                f"请把密钥存入 SecretStore 并只填写引用名"
            )


class SettingsStore:
    """版本化 settings.json 存取器。

    >>> store = SettingsStore(Path("C:/tmp/winefox/config"))
    >>> result = store.load()
    >>> store.update({"comfyui": {"address": "10.0.0.8:8188"}})
    """

    def __init__(self, config_dir: Path | str):
        self.config_dir = Path(config_dir)
        self.path = self.config_dir / SETTINGS_FILENAME
        self.backup_path = self.config_dir / BACKUP_FILENAME

    # ------------------------------------------------------------------ 读
    def load(self) -> LoadResult:
        """读取配置。损坏时返回 CONFIG_INVALID，不创建、不覆盖任何文件。"""
        if not self.path.exists():
            return LoadResult(ConfigStatus.NOT_FOUND, None, "settings.json 不存在")
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            return LoadResult(
                ConfigStatus.CONFIG_INVALID, None, f"配置损坏（无法解析）: {exc}"
            )
        if not isinstance(raw, dict):
            return LoadResult(
                ConfigStatus.CONFIG_INVALID, None, "配置损坏（顶层不是对象）"
            )
        version = raw.get("schemaVersion")
        if not isinstance(version, int):
            return LoadResult(
                ConfigStatus.CONFIG_INVALID, None, "配置损坏（缺少 schemaVersion）"
            )
        if version > SCHEMA_VERSION:
            return LoadResult(
                ConfigStatus.UNSUPPORTED_SCHEMA,
                None,
                f"配置 schemaVersion={version} 高于本程序支持的 {SCHEMA_VERSION}",
            )
        try:
            config = _default_config()
            _coerce(config, raw, DEFAULTS)
            _validate_no_secrets(config)
        except ConfigError as exc:
            return LoadResult(ConfigStatus.CONFIG_INVALID, None, str(exc))
        config["schemaVersion"] = SCHEMA_VERSION
        return LoadResult(ConfigStatus.OK, config, None)

    def require(self) -> dict[str, Any]:
        """加载成功返回配置；NOT_FOUND 返回默认值；损坏抛 ConfigError。"""
        result = self.load()
        if result.status is ConfigStatus.NOT_FOUND:
            return _default_config()
        if not result.ok:
            raise ConfigError(f"配置不可用（{result.status.value}）: {result.error}")
        return result.config  # type: ignore[return-value]

    # ------------------------------------------------------------------ 写
    def save(self, config: dict[str, Any], force: bool = False) -> None:
        """原子保存。force=True 时允许覆盖损坏的现有文件（用户显式确认修复）。

        顺序：tmp -> flush -> fsync -> 现有文件改名为 .bak -> os.replace(tmp, 目标)。
        """
        if not isinstance(config, dict):
            raise ConfigError("配置必须是对象")
        payload = deepcopy(config)
        payload["schemaVersion"] = SCHEMA_VERSION
        _validate_no_secrets(payload)
        self._validate_shape(payload)

        # 损坏保护：默认拒绝覆盖不可解析的现有配置。
        existing = self.load()
        if (
            not force
            and existing.status is ConfigStatus.CONFIG_INVALID
        ):
            raise ConfigError(
                f"拒绝覆盖损坏的配置文件 {self.path}（{existing.error}）；"
                f"如需修复请显式 force=True 或先从 .bak 恢复"
            )

        self.config_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(self.path.name + ".tmp")
        data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

        with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

        if self.path.exists():
            os.replace(self.path, self.backup_path)
        try:
            os.replace(tmp_path, self.path)
        except BaseException:
            # 还原：把 .bak 放回，尽力保持一致性。
            if self.backup_path.exists() and not self.path.exists():
                try:
                    os.replace(self.backup_path, self.path)
                except OSError:
                    pass
            raise
        _fsync_dir(self.config_dir)
        logger.info("settings.json saved (schemaVersion=%d) at %s", SCHEMA_VERSION, self.path)

    def update(self, patch: dict[str, Any], force: bool = False) -> dict[str, Any]:
        """读取-合并-保存的便捷入口。返回保存后的完整配置。"""
        current = self.require()
        _deep_merge(current, patch)
        self.save(current, force=force)
        return current

    def recover_from_backup(self) -> bool:
        """从 settings.json.bak 恢复（仅当主文件缺失或损坏时）。"""
        if not self.backup_path.exists():
            return False
        current = self.load()
        if current.ok:
            return False  # 主文件完好，不覆盖
        try:
            os.replace(self.backup_path, self.path)
        except OSError as exc:
            logger.error("恢复备份失败: %s", exc)
            return False
        logger.warning("已从 %s 恢复配置", self.backup_path)
        return True

    # ------------------------------------------------------------------ 视图
    def public_view(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        """脱敏视图：所有密钥形态叶子置为 None，认证引用原样保留（引用不是密钥）。"""
        if config is None:
            config = self.require()
        view = deepcopy(config)
        for path, value in list(_leaf_paths(view)):
            if _looks_like_secret(path, value):
                _set_at_path(view, path, None)
        return view

    # ------------------------------------------------------------------ 内部
    def _validate_shape(self, payload: dict[str, Any]) -> None:
        try:
            merged = _default_config()
            _coerce(merged, payload, DEFAULTS)
        except ConfigError as exc:
            raise ConfigError(f"配置校验失败: {exc}") from exc

    @property
    def schema_version(self) -> int:
        return SCHEMA_VERSION


# ---------------------------------------------------------------------- 工具
def _deep_merge(target: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = deepcopy(value)


def _set_at_path(root: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = root
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = value


def _fsync_dir(path: Path) -> None:
    """尽力 fsync 目录（POSIX）。Windows 跳过。"""
    if os.name == "nt":
        return
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def backup_settings_file(path: Path | str) -> Path | None:
    """把任意 settings.json 复制为 .bak（legacy_import 前留档用）。"""
    src = Path(path)
    if not src.exists():
        return None
    bak = src.with_name(src.name + ".bak")
    shutil.copy2(src, bak)
    return bak

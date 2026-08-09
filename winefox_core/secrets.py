"""WineFox Desktop 密钥存储。

- Windows 使用 Credential Manager（通过 keyring 的标准 Windows 后端）。
- 服务名固定为 ``WineFoxDesktop``，条目名即 SecretStore 的 name 参数。
- 没有任何明文 fallback：keyring 不可用或后端失败时抛
  SecretStoreError(SECRET_STORE_FAILED)，绝不落盘明文。
- 测试注入 MemorySecretBackend，绝不触碰真实凭据库。
- 日志永不包含值；本模块不记录任何 get/set 的内容。
"""

from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger("winefox_core.secrets")

SERVICE_NAME = "WineFoxDesktop"
SECRET_STORE_FAILED = "SECRET_STORE_FAILED"


class SecretStoreError(Exception):
    """密钥存储错误。code 取值见各常量。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    def __str__(self) -> str:
        return f"[{self.code}] {super().__str__()}"


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise SecretStoreError(SECRET_STORE_FAILED, "密钥名不能为空")
    name = name.strip()
    if len(name) > 256 or "\x00" in name:
        raise SecretStoreError(SECRET_STORE_FAILED, f"非法密钥名: {name!r}")
    return name


def _validate_value(value: str) -> str:
    if not isinstance(value, str):
        raise SecretStoreError(SECRET_STORE_FAILED, "密钥值必须是字符串")
    if "\x00" in value:
        raise SecretStoreError(SECRET_STORE_FAILED, "密钥值包含 NUL 字节")
    return value


class SecretBackend(Protocol):
    def set(self, name: str, value: str) -> None: ...
    def get(self, name: str) -> str | None: ...
    def delete(self, name: str) -> bool: ...
    def has(self, name: str) -> bool: ...


class MemorySecretBackend:
    """内存后端（测试/无持久化场景）。绝不落盘。"""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def set(self, name: str, value: str) -> None:
        self._data[name] = value

    def get(self, name: str) -> str | None:
        return self._data.get(name)

    def delete(self, name: str) -> bool:
        return self._data.pop(name, None) is not None

    def has(self, name: str) -> bool:
        return name in self._data

    def __len__(self) -> int:
        return len(self._data)


class KeyringBackend:
    """通过 keyring 使用平台凭据库（Windows 上为 Credential Manager）。

    服务名固定 SERVICE_NAME；条目名为 name。删除用 keyring.delete_password。
    """

    def __init__(self, service: str = SERVICE_NAME):
        try:
            import keyring  # noqa: PLC0415
        except ImportError as exc:
            raise SecretStoreError(
                SECRET_STORE_FAILED, "keyring 不可用，无法访问系统凭据库（禁止明文 fallback）"
            ) from exc
        self._keyring = keyring
        self._service = service
        # 预检：确认确实存在可用后端，而不是 keyring 的空壳。
        try:
            backend = keyring.get_keyring()
        except Exception as exc:  # noqa: BLE001 - 映射为统一错误
            raise SecretStoreError(
                SECRET_STORE_FAILED, f"系统凭据库不可用: {type(exc).__name__}"
            ) from exc
        if backend is None or "fail" in type(backend).__name__.lower():
            raise SecretStoreError(
                SECRET_STORE_FAILED, "没有可用的系统凭据后端"
            )
        self._backend_name = type(backend).__name__

    @property
    def backend_name(self) -> str:
        return self._backend_name

    def _wrap(self, exc: BaseException, action: str) -> SecretStoreError:
        return SecretStoreError(
            SECRET_STORE_FAILED, f"凭据库{action}失败: {type(exc).__name__}"
        )

    def set(self, name: str, value: str) -> None:
        try:
            self._keyring.set_password(self._service, name, value)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "写入") from exc

    def get(self, name: str) -> str | None:
        try:
            return self._keyring.get_password(self._service, name)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "读取") from exc

    def delete(self, name: str) -> bool:
        try:
            self._keyring.delete_password(self._service, name)
            return True
        except self._keyring.errors.PasswordDeleteError:
            return False
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "删除") from exc

    def has(self, name: str) -> bool:
        return self.get(name) is not None


class SecretStore:
    """应用密钥门面。

    >>> store = SecretStore(backend=MemorySecretBackend())  # 测试
    >>> store.set("comfyui_api", "abc")
    >>> store.get("comfyui_api")
    'abc'
    """

    def __init__(self, backend: SecretBackend | None = None):
        if backend is not None:
            self._backend: SecretBackend = backend
        else:
            self._backend = KeyringBackend()  # 可能抛 SECRET_STORE_FAILED

    @property
    def backend(self) -> SecretBackend:
        return self._backend

    def set(self, name: str, value: str) -> None:
        name = _validate_name(name)
        value = _validate_value(value)
        try:
            self._backend.set(name, value)
        except SecretStoreError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一映射
            raise SecretStoreError(
                SECRET_STORE_FAILED, f"密钥写入失败: {type(exc).__name__}"
            ) from exc
        logger.debug("secret set: %s", _redact(name))

    def get(self, name: str) -> str | None:
        name = _validate_name(name)
        try:
            return self._backend.get(name)
        except SecretStoreError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SecretStoreError(
                SECRET_STORE_FAILED, f"密钥读取失败: {type(exc).__name__}"
            ) from exc
        finally:
            logger.debug("secret get: %s", _redact(name))

    def delete(self, name: str) -> bool:
        name = _validate_name(name)
        try:
            return self._backend.delete(name)
        except SecretStoreError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SecretStoreError(
                SECRET_STORE_FAILED, f"密钥删除失败: {type(exc).__name__}"
            ) from exc

    def has(self, name: str) -> bool:
        name = _validate_name(name)
        try:
            return self._backend.has(name)
        except SecretStoreError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SecretStoreError(
                SECRET_STORE_FAILED, f"密钥查询失败: {type(exc).__name__}"
            ) from exc


def _redact(name: str) -> str:
    """日志只允许出现条目名（名称本身不算密钥值）；值永不进入日志。"""
    return f"{name} (value redacted)"

"""配置脱敏：GET /api/v1/settings 只返回「已配置」项，且敏感键值一律打码。

实现方（Agent 03）只需返回明文配置；后端统一在此做掩码，保证任何响应路径
（GET/PATCH settings）都不会泄露密钥。
"""

from __future__ import annotations

import re
from typing import Any

# 键路径段命中即视为敏感（整段匹配，避免误伤 keywords 之类的键）
_SENSITIVE_KEY_RE = re.compile(
    r"(^|[^a-z0-9])("
    r"token|secret|password|passwd|apikey|api_key|api-key|"
    r"access_key|access-key|private_key|private-key|credential|"
    r"authorization|auth_token|refresh_token|access_token|session_key|"
    r"cookie|signature|salt"
    r")([^a-z0-9]|$)",
    re.IGNORECASE,
)

# 值本身含内嵌凭据（URL userinfo / git 凭据）
_URL_CREDENTIAL_RE = re.compile(r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<user>[^/@\s]+)@")
_GIT_CREDENTIAL_RE = re.compile(r"^(?P<scheme>https?://)(?P<user>[^/@\s]+):[^/@\s]+@")

MASK = "********"


def is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY_RE.search(str(key)))


def _mask_value(value: str) -> str:
    # URL 内嵌凭据：只打码 userinfo，保留可读地址
    m = _URL_CREDENTIAL_RE.match(value)
    if m:
        return f"{m.group('scheme')}{MASK}@{value[m.end():]}"
    m = _GIT_CREDENTIAL_RE.match(value)
    if m:
        return f"{m.group('scheme')}{MASK}:{MASK}@{value[m.end():]}"
    if len(value) <= 4:
        return MASK
    return MASK


def mask_settings(data: Any, key_path: str = "") -> Any:
    """递归掩码。dict 键敏感 -> 值打码；list 递归每个元素。"""
    if isinstance(data, dict):
        out: dict = {}
        for k, v in data.items():
            skey = str(k)
            path_key = f"{key_path}.{skey}" if key_path else skey
            if is_sensitive_key(skey):
                out[k] = MASK if not isinstance(v, (dict, list)) else mask_settings(v, path_key)
            else:
                out[k] = mask_settings(v, path_key)
        return out
    if isinstance(data, list):
        return [mask_settings(v, key_path) for v in data]
    if isinstance(data, str) and ("://" in data or "git@" in data):
        return _mask_value(data)
    return data


def validate_settings_patch(patch: Any) -> dict:
    """PATCH /settings 基本校验：必须是 JSON 对象，且不允许直接写敏感键。"""
    from .errors import ValidationError

    if not isinstance(patch, dict):
        raise ValidationError("PATCH /settings 需要 JSON 对象")
    bad = [k for k in patch if is_sensitive_key(k)]
    if bad:
        raise ValidationError(
            "敏感配置项请通过 PUT /api/v1/secrets/{name} 写入",
            details={"keys": bad},
        )
    return patch

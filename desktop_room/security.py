"""房间安全：Argon2id 密码哈希、成员名称校验、会话令牌。

规范：
- Argon2id：salt 16 字节、输出 32 字节、内存 64MB（65536 KiB）、time cost 3、并行 1。
- 密码绝不保存明文；数据库中只保存 algorithm / parameters / salt / hash。
- 会话令牌至少 256 位随机，只保存 SHA-256 哈希；Cookie 使用 Secure/HttpOnly/
  SameSite=Lax。
- 成员名称：1–24 个 Unicode 字符、去除首尾空白、NFC 规范化、禁止控制字符、
  房间内大小写不敏感唯一、禁止冒充保留名。
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import unicodedata
from typing import Any

import argon2.low_level
from argon2.low_level import Type

PASSWORD_ALGORITHM = "argon2id"
ARGON2_PARAMS = {
    "time_cost": 3,
    "memory_cost": 65536,  # KiB = 64 MiB
    "parallelism": 1,
    "hash_len": 32,
}
_SALT_BYTES = 16
_TOKEN_BYTES = 32  # 256 位会话令牌

# 保留名（房间内禁止冒充），大小写不敏感。
RESERVED_NAMES = {"系统", "管理员", "winefox", "房主"}
_CONTROL_REMOVED = str.maketrans("", "", "\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\x0c\r\x0e\x0f"
                                 "\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1a\x1b\x1c\x1d\x1e\x1f\x7f")


class NameErrorDetail(Exception):
    """名称校验失败。message 为面向用户的明确提示。"""


class PasswordErrorDetail(Exception):
    """密码校验失败（登录失败 / 参数错误）。"""


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text)


def hash_password(password: str) -> tuple[str, str, str, str]:
    """返回 (algorithm, parameters_json, salt_b64, hash_b64)。"""
    salt = secrets.token_bytes(_SALT_BYTES)
    raw = argon2.low_level.hash_secret_raw(
        secret=password.encode("utf-8"),
        hash_len=ARGON2_PARAMS["hash_len"],
        salt=salt,
        time_cost=ARGON2_PARAMS["time_cost"],
        memory_cost=ARGON2_PARAMS["memory_cost"],
        parallelism=ARGON2_PARAMS["parallelism"],
        type=Type.ID,
    )
    return (
        PASSWORD_ALGORITHM,
        __import__("json").dumps(ARGON2_PARAMS, separators=(",", ":")),
        _b64encode(salt),
        _b64encode(raw),
    )


def verify_password(password: str, salt_b64: str, hash_b64: str, params_json: str | None = None) -> bool:
    """常量时间校验密码。参数从数据库读取；缺省时使用当前 ARGON2_PARAMS。"""
    try:
        salt = _b64decode(salt_b64)
        expected = _b64decode(hash_b64)
        params = ARGON2_PARAMS
        if params_json:
            imported = __import__("json").loads(params_json)
            if isinstance(imported, dict):
                params = ARGON2_PARAMS | imported
        raw = argon2.low_level.hash_secret_raw(
            secret=password.encode("utf-8"),
            hash_len=params["hash_len"],
            salt=salt,
            time_cost=params["time_cost"],
            memory_cost=params["memory_cost"],
            parallelism=params["parallelism"],
            type=Type.ID,
        )
        return secrets.compare_digest(raw, expected)
    except Exception:
        return False


def normalize_name(raw: str) -> str:
    """NFC 规范化并去除首尾空白。"""
    return unicodedata.normalize("NFC", raw or "").strip()


def validate_name(raw: str) -> str:
    """校验并规范化成员名称；失败抛出 NameErrorDetail（提示面向用户）。"""
    name = normalize_name(raw)
    if not name:
        raise NameErrorDetail("显示名称不能为空")
    if len(name) > 24:
        raise NameErrorDetail("显示名称不能超过 24 个字符")
    # 控制字符（含 \n \t 等）一律禁止
    if any(unicodedata.category(ch).startswith("C") for ch in name):
        raise NameErrorDetail("显示名称不能包含控制字符")
    if normalize_lookup(name) in RESERVED_NAMES:
        raise NameErrorDetail("该名称不可使用，请换一个")
    return name


def normalize_lookup(name: str) -> str:
    """用于房间内唯一性比较的键（NFC + 去除所有空白 + 小写）。

    显示名保留内部空格，但唯一性键去掉全部空白，使"小 明"与"小明"同键。
    """
    return "".join(normalize_name(name).split()).lower()


def new_session_token() -> tuple[str, str]:
    """生成 (token, token_hash)。token 为 256 位随机（64 位十六进制，无 Cookie 转义问题），只保存 hash。"""
    token = secrets.token_hex(_TOKEN_BYTES)
    return token, hashlib.sha256(bytes.fromhex(token)).hexdigest()


def new_host_token() -> str:
    """房主管理令牌：256 位随机（64 位十六进制）。

    该令牌只保存在 Rust 内存与 sidecar 内存，绝不写入数据库、日志或 URL。
    """
    return secrets.token_hex(_TOKEN_BYTES)


def hash_session_token(token: str) -> str:
    """对传入的令牌原文计算 SHA-256 十六进制（用于匹配存储的 session_hash）。"""
    return hashlib.sha256(bytes.fromhex(token)).hexdigest()


def constant_time_equal(a: str, b: str) -> bool:
    return secrets.compare_digest(a, b)

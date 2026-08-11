# -*- coding: utf-8 -*-
"""生成 WineFox 更新签名密钥对（发布机一次性操作）。

产物：
  * ~/.winefox/update.key   — Ed25519 私钥（OpenSSH 格式，带口令加密）
  * ~/.winefox/update.pass  — 口令（与私钥同目录，随私钥一起保护）
  * packaging/updater/pubkey.b64 — 32 字节原始公钥的 base64（提交进仓库）

安全约定：私钥与口令只在发布机本地，绝不进入代码仓库；仓库只提交公钥。
已有私钥时直接退出，绝不覆盖，避免损坏真实签名密钥。

用法：
  python packaging/updater/gen_key.py
"""

import base64
import os
import pathlib
import secrets

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


def gen(force: bool = False) -> None:
    keydir = pathlib.Path.home() / ".winefox"
    key_path = keydir / "update.key"
    if key_path.is_file() and not force:
        raise SystemExit(f"已存在私钥 {key_path}，拒绝覆盖（如需重新生成请显式删除后重试）")

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    keydir.mkdir(parents=True, exist_ok=True)

    key = ed25519.Ed25519PrivateKey.generate()
    pub_raw = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    assert len(pub_raw) == 32

    passphrase = secrets.token_urlsafe(18)
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.BestAvailableEncryption(passphrase.encode()),
    )

    key_path.write_bytes(priv)
    (keydir / "update.pass").write_text(passphrase, encoding="utf-8")

    pubkey_path = repo_root / "packaging" / "updater" / "pubkey.b64"
    pubkey_path.write_text(base64.b64encode(pub_raw).decode("ascii"), encoding="ascii")

    print(f"私钥写入：{key_path}")
    print(f"口令写入：{keydir / 'update.pass'}")
    print(f"公钥写入：{pubkey_path}")


if __name__ == "__main__":
    import sys

    sys.exit(gen() or 0)

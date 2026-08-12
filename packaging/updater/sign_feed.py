# -*- coding: utf-8 -*-
"""WineFox 更新清单签名工具。

对 NSIS 安装包计算 Ed25519 签名（与 desktop_shell/src-tauri/src/updater.rs
的 verify_installer_signature 完全对应：签名对象是安装包原始字节，签名与公钥均为
base64），并写出 latest.json。latest.json 随安装包一起上传到 GitHub Release，
更新器通过 `.../releases/latest/download/latest.json` 获取。

用法（由 build-feed.ps1 调用，也可直接运行）：
    python sign_feed.py --version 8.6.0 --url <下载URL> --installer <exe路径>
        --key <私钥路径> [--notes <更新说明>] [--out <输出路径>]

私钥口令：优先环境变量 WINEFOX_UPDATE_KEY_PASSPHRASE，未设置时回退到
`~/.winefox/update.pass`。私钥与口令都必须在发布机本地，绝不进入代码仓库。
"""

import argparse
import base64
import json
import os
import pathlib
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


def _passphrase() -> bytes | None:
    env = os.environ.get("WINEFOX_UPDATE_KEY_PASSPHRASE", "").strip()
    if env:
        return env.encode()
    pass_file = pathlib.Path.home() / ".winefox" / "update.pass"
    if pass_file.is_file():
        return pass_file.read_text(encoding="utf-8").strip().encode()
    return None


def load_private_key(path: pathlib.Path):
    data = path.read_bytes()
    passphrase = _passphrase()
    # 兼容 PEM/OpenSSH 两种私钥封装；无口令密钥也支持。
    for loader in (serialization.load_pem_private_key, serialization.load_ssh_private_key):
        try:
            return loader(data, password=passphrase)
        except (ValueError, TypeError):
            pass
    raise SystemExit(f"无法读取私钥（口令缺失或不正确）：{path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True, help="本次版本号，如 8.6.0")
    parser.add_argument("--url", required=True, help="安装包下载地址")
    parser.add_argument("--installer", required=True, help="本地安装包路径")
    parser.add_argument("--key", required=True, help="Ed25519 私钥路径")
    parser.add_argument("--notes", default="", help="更新说明")
    parser.add_argument("--out", default="", help="latest.json 输出路径")
    args = parser.parse_args()

    installer = pathlib.Path(args.installer)
    if not installer.is_file():
        raise SystemExit(f"未找到安装包：{installer}")

    body = installer.read_bytes()
    key = load_private_key(pathlib.Path(args.key))
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise SystemExit("私钥必须是 Ed25519 密钥")

    signature = base64.b64encode(key.sign(body)).decode("ascii")

    # 发布当前 UTC 时间（更新器以字符串展示 pub_date）。
    from datetime import datetime, timezone

    feed = {
        "version": args.version,
        "notes": args.notes,
        "pub_date": datetime.now(timezone.utc).isoformat(),
        "platforms": {
            "windows-x86_64": {
                "url": args.url,
                "signature": signature,
                "size": len(body),
            }
        },
    }

    out = pathlib.Path(args.out) if args.out else pathlib.Path(installer).parent / "latest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(feed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"signed {installer.name} ({len(body)} bytes) -> {out}")
    print(f"version={args.version} url={args.url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

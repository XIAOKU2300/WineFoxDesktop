# WineFox Updater — 签名与更新清单

本目录承载 WineFox 桌面端自动更新的签名材料与工具。

## 安全边界

- **私钥只在发布机**：位于 `~/.winefox/update.key`（Ed25519，OpenSSH 格式，
  带口令加密），口令保存在 `~/.winefox/update.pass` 或环境变量
  `WINEFOX_UPDATE_KEY_PASSPHRASE`。两者都**绝不进入代码仓库**。
- **仓库只提交公钥**：`pubkey.b64`（32 字节原始 Ed25519 公钥的 base64），
  由 Rust 编译期内嵌（`desktop_shell/src-tauri/src/updater.rs` 的
  `resolve_pubkey_b64()`，可被环境变量 `WINE_FOX_UPDATE_PUBKEY` 覆盖）。
- 更新流程：下载 `latest.json` → 下载安装包 → 用内嵌公钥验证安装包
  Ed25519 签名 → 通过后关闭 sidecar、以 `/S` 静默启动安装器 → 退出。
  签名失败立即终止，绝不提供“强制安装”。

## 签名与发布清单（发布机操作）

1. 生成密钥对（只需一次，已有则跳过）：
   ```
   python -c "from packaging.updater import gen_key; gen_key.gen()"
   ```
   或直接运行 `packaging/updater/gen_key.py`。生成后：
   - `~/.winefox/update.key`：私钥（带口令）
   - `~/.winefox/update.pass`：口令
   - `packaging/updater/pubkey.b64`：公钥（提交进仓库）

2. 构建安装包后（`packaging\nsis\WineFox-Setup-<版本>-x64.exe`），签名并生成清单：
   ```
   rtk powershell -NoProfile -ExecutionPolicy Bypass -File packaging\updater\build-feed.ps1 -Notes "本次更新说明"
   ```
   产物为 `packaging\nsis\latest.json`。

3. 把 `WineFox-Setup-<版本>-x64.exe` 与 `latest.json` 一起上传到 GitHub
   Release（Tag 例如 `v8.6.0`）。更新器从
   `https://github.com/XIAOKU2300/WineFoxDesktop/releases/latest/download/latest.json`
   拉取清单。

## 文件

| 文件 | 说明 |
|---|---|
| `pubkey.b64` | 真实发布公钥（32 字节 base64），Rust 编译期内嵌 |
| `sign_feed.py` | 对安装包计算 Ed25519 签名并写出 `latest.json` |
| `build-feed.ps1` | 发布机一键入口：取版本、定位安装包、调 `sign_feed.py` |
| `README.md` | 本文件 |

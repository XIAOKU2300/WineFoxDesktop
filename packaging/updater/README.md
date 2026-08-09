# WineFox Updater — 公钥占位说明

本目录为 WineFox 桌面端自动更新机制预留。

## 状态（Agent 09）

- **没有写入任何真实签名密钥。** 签名密钥必须在受控构建机（或 CI secrets）中生成，绝不进代码仓库。
- 更新流程本身由 Tauri updater 插件（或等价的 manifest 下载器）实现，**仅校验公钥**：
  1. 下载 `latest.json`（版本、安装包 URL、签名、SHA-256）
  2. 用内嵌**公钥**验证安装包签名
  3. 通过后下载安装包并调用安装器（`/S` 静默、`/D=` 覆盖安装）
  4. 安装失败时由 NSIS 回滚机制保留旧版本（见 `packaging/nsis/installer.nsi` 的 `$INSTDIR.old` 备份/恢复）

## 接入步骤（真实发布前）

1. 在构建机生成 Ed25519 密钥对：
   ```
   npx tauri signer generate -w ~/.winefox/update.key -p <passphrase>
   ```
2. 将**公钥**（`~/.winefox/update.key.pub`）放入本目录并重命名为 `pubkey.pem`，由构建脚本嵌入应用。
3. 私钥只存在于构建机 / CI secrets；`pubkey.example.pem` 为格式示例，非真实密钥。
4. `latest.json` 由发布流水线生成，签名使用 `tauri signer sign`。

## 文件

| 文件 | 说明 |
|---|---|
| `pubkey.example.pem` | 公钥格式示例（占位，非真实密钥） |
| `README.md` | 本文件 |

# WineFox Packaging（Agent 09）

版本单一来源：`packaging/version.json`（当前 `app` = 8.6.7，`protocol` = 1）。
完整的 Windows 安装包构建、验证与桌面交付流程见
[`WINDOWS_BUILD_RUNBOOK.md`](WINDOWS_BUILD_RUNBOOK.md)。发布前需按手册同步 shell
版本；`build.ps1` 会继续生成 sidecar 的 `_version.py`、VSVersionInfo 与
`generated_version.nsh`，并在构建期和安装期双重校验一致。

## 目录

| 路径 | 说明 |
|---|---|
| `version.json` | 版本/协议唯一来源 |
| `WINDOWS_BUILD_RUNBOOK.md` | 给后续 AI/维护者的完整 Windows 打包流程 |
| `build.ps1` | 总入口：venv → PyInstaller → 打包产物自检 → 体积策略检查 → 暂存 externalBin → tauri build → NSIS |
| `pyinstaller/winefox-sidecar.spec` | onedir、windowed、收集 workflow/templates/assets/desktop_ui |
| `pyinstaller/requirements-sidecar.txt` | sidecar 构建依赖（独立于插件根 requirements.txt） |
| `nsis/installer.nsi` | NSIS3 x64 安装器（回滚、同版本校验、卸载保留用户数据） |
| `nsis/build-nsis.ps1` | 组装 payload（含 onedir `_internal`）+ makensis |
| `updater/` | 更新机制公钥占位（无真实密钥） |
| `tests/` | sidecar 协议/生命周期测试 + shell 端到端包装脚本 |
| `scripts/generate-icons.py` | 生成 src-tauri/icons（stdlib） |

## 体积策略（基础包）

PyInstaller spec 的 excludes 保证基础包**不含**：TensorFlow / Keras / torch /
opennsfw2 / Playwright（及其 Chromium）/ ComfyUI / 模型 / CUDA。
实测 onedir ≈ 22 MB。需要图像审核（opennsfw2）等重特性时，在 spec 的
excludes 中移除对应项并添加依赖到 `requirements-sidecar.txt`（不改插件根
requirements.txt）。

## 测试

```powershell
python packaging\tests\test_sidecar_protocol.py   # 22 项：握手/超时/崩溃/退出/路径/脱敏
cd desktop_shell\src-tauri; cargo test            # 17 项：握手解析/脱敏/Job Object/HTTP 客户端
```

shell 端到端（需要 GUI 会话）：
`packaging\tests\run-shell-e2e.ps1` —— 分别验证 ready/超时/版本不匹配/崩溃
重启/启动失败/单实例/安装卸载保留用户数据。

## 已知边界

- `tauri build` 的 externalBin 只接受单个 exe；onedir 的 `_internal` 目录由
  自定义 NSIS（build-nsis.ps1）随 payload 一起安装，因此**正式安装包必须走
  自定义 NSIS 产物**（`WineFox-Setup-<v>-x64.exe`）。
- NSIS 脚本必须保持 UTF-8 **带 BOM**（中文文本要求）。
- 更新失败保留旧版：安装器先把现有目录 Rename 为 `$INSTDIR.old`，新版本
  提取失败时自动还原。

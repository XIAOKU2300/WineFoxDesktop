# WineFox Windows 安装包构建手册

这份手册是给后续 AI/维护者直接执行的标准流程。目标是在不启动可见软件、
不覆盖用户数据的前提下，产出包含完整 Python sidecar、桌面 UI、内置工作流
和自定义 NSIS 文案的 Windows x64 安装包。

## 1. 最终产物

正式交付物只有这个文件：

```text
packaging\nsis\WineFox-Setup-<版本>-x64.exe
```

不要把 Tauri 自动生成的 NSIS 包当成正式产物。WineFox 的 Python sidecar 是
PyInstaller `onedir` 结构，必须同时安装 `_internal`；自定义安装提示、升级回滚、
快捷方式和卸载保留数据逻辑也只存在于 `packaging/nsis/installer.nsi`。

## 2. 执行原则

1. 从仓库根目录执行全部命令。
2. 先运行 `git status --short --branch`，保留所有已有改动，不得 reset、checkout
   或覆盖用户尚未提交的代码。
3. 构建过程不得启动 WineFox 可见窗口，也不得替用户点击安装器。
4. 不安装 ComfyUI，不下载模型，不把 CUDA、模型、TensorFlow、Playwright Chromium
   等内容塞进基础安装包。
5. 不修改全局 Python、Node、Rust 或 Git 配置。
6. 没有签名证书时不得伪造签名；最终报告必须说明安装器未签名，SmartScreen
   可能显示“未知发布者”。
7. 除非用户明确要求，不创建 GitHub Release。
8. 当前 Codex 环境的命令以 `rtk` 开头；普通 PowerShell 没有 `rtk` 时，删除命令
   开头的 `rtk ` 即可。

## 3. 构建依赖

- Windows 10/11 x64
- PowerShell 7 或 Windows PowerShell 5.1
- Python x64（项目已验证 Python 3.14）
- Node.js（用于 JavaScript 语法检查与 Tauri CLI 依赖）
- Rust stable MSVC toolchain，命令行可用 `cargo`
- NSIS 3.x，命令行可用 `makensis.exe`

NSIS 可放入 PATH，也可以解压到下面的目录，构建脚本会自动寻找：

```text
packaging\.build\nsis\nsis-<版本>\makensis.exe
```

例如：

```text
packaging\.build\nsis\nsis-3.10\makensis.exe
```

必须使用完整 NSIS 发行包，不能只复制一个 `makensis.exe`，因为编译还需要
`Include`、`Contrib`、`Plugins` 和 `Stubs`。

## 4. 版本同步

`packaging/version.json` 是打包版本的主来源。发布新版本时，先确定 `<版本>`，
例如 `8.5.12`，然后同步以下位置：

- `packaging/version.json` 的 `app`
- `desktop_shell/VERSION.txt`
- `desktop_shell/package.json`
- `desktop_shell/package-lock.json` 顶层及根 package 的两个 `version`
- `desktop_shell/src-tauri/Cargo.toml`
- `desktop_shell/src-tauri/tauri.conf.json`
- `desktop_ui/js/app.js` 中界面显示版本
- `metadata.yaml`
- `CHANGELOG.md` 新增对应版本记录

下面两个文件由构建流程自动生成，不要只改它们：

- `desktop_shell/sidecar/winefox_sidecar/_version.py`
- `packaging/nsis/generated_version.nsh`

`Cargo.lock` 会由 Cargo 根据 `Cargo.toml` 自动更新。版本同步后先搜索旧版本，确认
剩余命中只是历史 Changelog、测试样例或注释：

```powershell
rtk rg -n "旧版本号" --glob '!desktop_shell/src-tauri/target/**' --glob '!packaging/dist/**' --glob '!packaging/.build/**'
```

## 5. 构建前检查

```powershell
rtk git status --short --branch
rtk git diff --check
```

检查安装器脚本仍是 UTF-8 BOM；中文 NSIS 文案不能丢失 BOM：

```powershell
rtk powershell -NoProfile -Command '$b=[IO.File]::ReadAllBytes("packaging\nsis\installer.nsi"); if(($b[0..2] -join ",") -ne "239,187,191"){throw "installer.nsi 必须是 UTF-8 BOM"}'
```

检查 NSIS：

```powershell
rtk powershell -NoProfile -Command 'Get-Command makensis.exe -ErrorAction SilentlyContinue; Get-ChildItem "packaging\.build\nsis" -Directory -Filter "nsis-*" -ErrorAction SilentlyContinue'
```

## 6. 回归测试

先检查全部前端 JavaScript：

```powershell
rtk powershell -NoProfile -Command '$files=Get-ChildItem "desktop_ui\js" -Recurse -Filter "*.js"; foreach($file in $files){& node --check $file.FullName; if($LASTEXITCODE -ne 0){exit $LASTEXITCODE}}'
```

再运行桌面、原插件、sidecar 和 Rust 测试：

```powershell
rtk python -m unittest discover -s tests_desktop -v
rtk python -m unittest discover -s tests -v
rtk python packaging\tests\test_sidecar_protocol.py -v
rtk cargo test --manifest-path desktop_shell\src-tauri\Cargo.toml
```

Python 3.14 可能在测试子进程退出时打印 `ResourceWarning`。只要测试最终为 `OK`
且退出码为 0，可记录为非阻断警告；真正的 `FAIL`、`ERROR` 或非零退出码必须修复。

## 7. 标准构建顺序

### 7.1 构建 Python sidecar

```powershell
rtk powershell -NoProfile -ExecutionPolicy Bypass -File packaging\build.ps1 -SkipTauri -SkipNsis
```

这个步骤会：

- 在 `packaging/.build/venv` 使用隔离环境安装打包依赖
- 写入 sidecar 版本
- 用 PyInstaller `onedir` 构建 sidecar
- 运行打包后 `--version` 与 `--selftest`
- 检查基础包体积和禁用依赖
- 将 external binary 暂存到 Tauri 的 `binaries` 目录

成功后应存在：

```text
packaging\dist\winefox-sidecar\winefox-sidecar.exe
packaging\dist\winefox-sidecar\_internal\
```

### 7.2 构建 Rust/Tauri 主程序

```powershell
rtk cargo build --release --manifest-path desktop_shell\src-tauri\Cargo.toml
```

成功后应存在：

```text
desktop_shell\src-tauri\target\release\winefox-desktop.exe
```

### 7.3 暂存便携版并验证文件布局

```powershell
rtk powershell -NoProfile -ExecutionPolicy Bypass -File packaging\stage-portable.ps1
```

成功后应存在：

```text
packaging\dist\WineFoxDesktop\WineFox.exe
packaging\dist\WineFoxDesktop\winefox-sidecar.exe
packaging\dist\WineFoxDesktop\_internal\
packaging\dist\WineFoxDesktop\VERSION.txt
```

### 7.4 构建正式 NSIS 安装包

```powershell
rtk powershell -NoProfile -ExecutionPolicy Bypass -File packaging\nsis\build-nsis.ps1
```

此脚本会重新暂存安装 payload，核对 `WineFox.exe`、`winefox-sidecar.exe` 与
`packaging/version.json` 三者版本，生成 `generated_version.nsh`，最后调用
`makensis.exe`。任何版本不一致都必须中止，不能跳过检查。

成功输出：

```text
packaging\nsis\WineFox-Setup-<版本>-x64.exe
```

## 8. 产物验证

将 `<版本>` 替换为本次版本：

```powershell
rtk powershell -NoProfile -Command '$v=(Get-Content "packaging\version.json" | ConvertFrom-Json).app; $setup=Get-Item "packaging\nsis\WineFox-Setup-$v-x64.exe"; $app=Get-Item "packaging\dist\WineFoxDesktop\WineFox.exe"; $sidecar=Get-Item "packaging\dist\WineFoxDesktop\winefox-sidecar.exe"; [pscustomobject]@{SetupVersion=$setup.VersionInfo.FileVersion;AppVersion=$app.VersionInfo.FileVersion;SidecarVersion=$sidecar.VersionInfo.FileVersion;SetupMB=[math]::Round($setup.Length/1MB,2)} | Format-List'
```

三处版本必须完全一致。检查内置工作流数量：

```powershell
rtk powershell -NoProfile -Command '(Get-ChildItem "packaging\dist\WineFoxDesktop\_internal\plugin_data\workflow" -Filter "*.json" -File).Count'
```

当前基线应为 16 个；未来如果工作流清单有意变化，以仓库源码数量为准，但源码
和安装包内数量必须一致。

检查禁用的大型依赖和模型文件：

```powershell
rtk powershell -NoProfile -Command '$bad=Get-ChildItem "packaging\dist\WineFoxDesktop" -Recurse -File | Where-Object {$_.Name -match "(?i)tensorflow|playwright|chromium|\.safetensors$|\.ckpt$|\.gguf$|cuda"}; if($bad){$bad.FullName; throw "安装包混入禁用内容"}'
```

计算 SHA-256：

```powershell
rtk certutil -hashfile packaging\nsis\WineFox-Setup-<版本>-x64.exe SHA256
```

## 9. 复制到桌面

确认前面所有检查通过后再复制，不要启动安装器：

```powershell
rtk powershell -NoProfile -Command '$v=(Get-Content "packaging\version.json" | ConvertFrom-Json).app; $source=(Resolve-Path "packaging\nsis\WineFox-Setup-$v-x64.exe").Path; $target=Join-Path ([Environment]::GetFolderPath("Desktop")) "WineFox-Setup-$v-x64.exe"; Copy-Item -LiteralPath $source -Destination $target -Force; Get-Item -LiteralPath $target | Select-Object FullName,Length,@{N="FileVersion";E={$_.VersionInfo.FileVersion}}'
```

再次对桌面文件计算 SHA-256，并与源码目录中的安装包比较：

```powershell
rtk certutil -hashfile C:\Users\Administrator\Desktop\WineFox-Setup-<版本>-x64.exe SHA256
```

## 10. 提交与交付报告

提交前：

```powershell
rtk git diff --check
rtk git status --short
```

不要提交这些构建目录或二进制：

- `packaging/.build/`
- `packaging/dist/`
- `packaging/nsis/payload/`
- `desktop_shell/src-tauri/target/`
- `WineFox-Setup-*.exe`

若用户此前要求每次修改自动提交，则提交源码和文档后推送当前分支；不要创建
GitHub Release。最终报告至少包含：

- 安装包桌面绝对路径
- 版本、大小和 SHA-256
- 测试数量与结果
- 包内工作流数量
- Git 提交哈希
- 未签名/SmartScreen 提示

## 11. 常见失败

### `makensis.exe not found`

安装或解压完整 NSIS 3.x，把 `makensis.exe` 放入 PATH，或放入：

```text
packaging\.build\nsis\nsis-<版本>\makensis.exe
```

### `version mismatch`

不要改安装脚本绕过检查。重新完成“版本同步”，再从 7.1 开始构建。特别检查
`Cargo.toml`、`tauri.conf.json`、`package.json`、`VERSION.txt` 与
`packaging/version.json`。

### `WineFox.exe not found`

说明 7.2 的 Rust release 构建没有成功。确认下面的文件存在后重跑 7.3：

```text
desktop_shell\src-tauri\target\release\winefox-desktop.exe
```

### sidecar 启动后卡住或缺 `_internal`

PyInstaller 使用的是 `onedir`，不能只复制 `winefox-sidecar.exe`。必须重新运行
`stage-portable.ps1` 与 `build-nsis.ps1`，让 `_internal` 一起进入安装包。

### 安装器中文乱码

检查 `packaging/nsis/installer.nsi` 的前三个字节必须为 `EF BB BF`，并使用
支持 Unicode 的 NSIS 3.x 重新编译。

### 安装器还是旧 UI

不要复用旧 `packaging/dist`。从 7.1 开始完整重建 sidecar，再构建 Rust 主程序、
便携版和 NSIS。

## 12. 交给后续 AI 的一句话指令

可以直接把下面这段发给后续 AI：

```text
严格阅读并执行 packaging/WINDOWS_BUILD_RUNBOOK.md。保留工作区已有修改，从源码完整重建 WineFox Windows x64 自定义 NSIS 安装包；不要启动或控制可见 GUI，不要创建 GitHub Release。完成全部测试、版本/工作流/禁用依赖/SHA-256 校验后，把 WineFox-Setup-<版本>-x64.exe 复制到桌面，并报告路径、版本、大小、哈希、测试结果和提交哈希。
```

# WineFox Desktop

WineFox 是一款 Windows 桌面端 ComfyUI 客户端，提供文生图、图生图、LoRA 管理、历史收藏、模型检测和多人房间。

## 下载

前往 [Releases](https://github.com/XIAOKU2300/WineFoxDesktop/releases/latest) 下载 `WineFox-Setup-*-x64.exe`。

要求：Windows 10/11 x64、WebView2，以及可访问的 ComfyUI。应用不捆绑 ComfyUI、模型或 CUDA。

安装后在「设置」中填写 ComfyUI 地址，默认为 `http://127.0.0.1:8188`。后续版本可在「设置 → 关于与更新」中自动更新。

## 主要功能

- 文生图、图生图与内置工作流
- Checkpoint、LoRA、中文别名和兼容性检测
- 本地任务队列、历史记录与收藏
- AI 文本增强和图片分析
- 局域网/公网多人房间
- 签名校验的自动更新

## 从源码运行

需要 Python 3.11+、Rust 1.77+、Node.js 20+。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r packaging\pyinstaller\requirements-sidecar.txt
cd desktop_shell
npm install
$env:WINE_FOX_PYTHON = "..\.venv\Scripts\python.exe"
npm run tauri dev
```

## 测试

```powershell
python -m pytest tests tests_desktop -q
python -m pytest desktop_integration_tests -c desktop_integration_tests\pytest.ini -q
cargo test --manifest-path desktop_shell\src-tauri\Cargo.toml
```

用户数据默认保存在 `%LOCALAPPDATA%\WineFox`。API 密钥由系统凭据存储管理，不写入普通设置文件。

项目使用 [MIT License](LICENSE)。

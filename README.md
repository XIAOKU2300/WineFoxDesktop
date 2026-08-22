<p align="center"><img src="assets/logo-banner.png" width="360" alt="WineFox Desktop"></p>

<h1 align="center">WineFox Desktop</h1>

<p align="center">Windows 桌面端 ComfyUI 客户端 · 文生图 / 图生图 / LoRA 工作台 / 多人房间</p>

<p align="center">by <b>SakuraFan</b></p>

---

WineFox（酒狐）把 ComfyUI 的生成力装进一个安静克制的桌面应用：左侧管理模型与工作流，中间是画布，右侧是参数。生成、历史、收藏、LoRA、诊断，一屏各就其位。

## 主要功能

### 🎨 生成
- **文生图 / 图生图**：内置多套 Illustrious / Anima 工作流，支持参考图拖放上传。
- **实时队列**：排队位置、生成进度百分比、任务取消一目了然；Ctrl+Enter 快速开画。
- **画布交互**：滚轮缩放、拖拽平移、双击复位，结果可放大预览、保存、收藏、一键复制。
- **参数覆盖**：默认沿用工作流调好的采样参数，需要时再按任务覆盖步数 / CFG / 采样器 / 调度器。
- **AI 文本增强**：提示词智能润色，图片分析反推提示词。

### 🦊 LoRA 工作台
- **在线发现**：跨 Civitai / Hugging Face / Civitai Red 搜索，中文别名、英文角色名、原名都能搜；分页浏览，点卡片看大图详情。
- **一键安装**：装完自动提取触发词、更新档案；进度实时可见，安装通道自动探测（本机目录或 Bridge 远程写入）。
- **本地档案**：别名 / 触发词 / 角色编辑，文件隔离与恢复（不提供永久删除，安全可回退）。
- **自动匹配**：输入提示词即可预览会命中哪些 LoRA，提交前可逐条移除；生成与预览使用同一证据链。

### 🗂 历史 · 收藏
- **历史**：cursor 分页、提示词 / 种子搜索、状态筛选；预览大图带完整参数，可回填重生成。
- **收藏**：分类归档、备注编辑、关键词检索；回收站软删除，可恢复或彻底清除。
- 点击卡片即预览，悬浮只留收藏 / 回收等必要操作。

### 🧩 模型与环境
- Checkpoint / 自定义节点检测，兼容性（ready / missing / incompatible）标注。
- 环境中心支持 Bridge 安装与远程 ComfyUI 接入。

### 👥 多人房间
- 局域网 / 公网房间，内置隧道（cloudflared）自动开通，和朋友共用一个画布。

### 🔄 安全自动更新
- 应用内检查 / 下载 / 安装一条龙；Ed25519 签名硬校验，绝不执行被篡改的安装包；自动跟随系统代理。

## 下载

前往 [Releases](https://github.com/XIAOKU2300/WineFoxDesktop/releases/latest) 下载 `WineFox-Setup-*-x64.exe`。

要求：Windows 10/11 x64、WebView2，以及可访问的 ComfyUI。应用不捆绑 ComfyUI、模型或 CUDA。

安装后在「设置」中填写 ComfyUI 地址，默认 `http://127.0.0.1:8188`。

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

前端亦可直接静态调试：`python -m http.server -d desktop_ui 8000` 后打开
`http://127.0.0.1:8000/index.html?mock=1` 即可离线体验全部界面。

## 测试

```powershell
python -m pytest tests tests_desktop -q
python -m pytest desktop_integration_tests -c desktop_integration_tests\pytest.ini -q
cargo test --manifest-path desktop_shell\src-tauri\Cargo.toml
```

## 安全与隐私

- 用户数据保存在 `%LOCALAPPDATA%\WineFox`；API 密钥由系统凭据存储管理，不写入普通文件。
- 界面不渲染绝对路径；诊断导出经过脱敏。
- 安装包更新强制 Ed25519 签名校验，失败即终止。

## License

[MIT](LICENSE)

## 社区

本项目已在 LinuxDO 社区进行宣传：https://linux.do/

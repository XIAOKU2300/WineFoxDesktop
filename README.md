<p align="center"><img src="assets/logo-banner.png" width="360" alt="WineFox Desktop"></p>

<h1 align="center">WineFox Desktop</h1>

<p align="center">把 ComfyUI 的图像生成、LoRA 和作品管理放进一个 Windows 桌面应用。</p>
<p align="center">by <b>SakuraFan</b> · 免费开源 · MIT</p>
<p align="center">
  <a href="https://www.bilibili.com/video/BV1FnhR6TE37/">▶ 项目介绍视频</a> ·
  <a href="https://github.com/XIAOKU2300/WineFoxDesktop/releases/latest">下载最新版</a> ·
  <a href="#快速上手">快速上手</a> ·
  <a href="#常见问题">常见问题</a>
</p>

WineFox（酒狐）是 ComfyUI 的桌面客户端。选好模型与工作流后，可以在同一个界面里输入提示词、上传参考图、选择 LoRA、查看生成进度，以及整理历史记录和收藏。

**WineFox 需要连接可用的 ComfyUI，安装包不包含 ComfyUI、模型或 CUDA。** 已有环境可以直接接入，也可以在「模型与环境」中配置本机环境。中文提示词转译和参考图理解需要另行配置兼容的 AI 接口。

## 项目介绍

[在 B 站观看：我给 ComfyUI 做了个桌面端：不连节点，一句中文就能出图](https://www.bilibili.com/video/BV1FnhR6TE37/)

约 2 分钟了解生成界面、LoRA 工作台和作品管理。实际生成能力取决于连接的 ComfyUI、模型、工作流及 AI 配置。

## 下载与安装

前往 [GitHub Releases](https://github.com/XIAOKU2300/WineFoxDesktop/releases/latest)，在最新版本的 **Assets** 中选择：

| 文件 | 适合的使用方式 | 如何启动 |
| --- | --- | --- |
| `WineFox-Setup-版本号-x64.exe` | 常规安装与手动升级 | 退出旧版后运行安装器 |
| `WineFox-Portable-版本号-x64.zip` | 解压使用、独立测试 | 解压后运行 `WineFox.exe` |
| `SHA256SUMS.txt` | 核对下载文件完整性 | 对照安装包或 ZIP 的 SHA-256 |

- **系统要求**：Windows 10/11 x64、Microsoft Edge WebView2 Runtime，以及可访问的 ComfyUI。
- **普通用户无需安装 Python、Node.js 或 Rust**；它们只在源码开发时需要。
- 便携版运行 `WineFox.exe` 会使用已有默认数据；运行 `Start-WineFox.cmd` 会使用便携目录下的独立 `data`，首次需要重新填写设置。移动便携版时请保留 `winefox-sidecar.exe` 和 `_internal`。

> **8.6.18 更新说明**：此版本需要从 Releases 手动下载安装。原自动更新签名私钥已丢失，目前保留的兼容清单仍对应 8.6.14，应用内检查更新不会提供 8.6.18。安装器未进行 Windows 代码签名，系统可能显示「未知发布者」。

## 快速上手

### 1. 准备并连接 ComfyUI

先启动 ComfyUI，确认浏览器能打开它的界面。本机常见地址是 `http://127.0.0.1:8188`。

打开 WineFox，进入 **设置 → ComfyUI 连接**，填写服务地址，点击「测试连接」并保存。远程 ComfyUI 请填写实际可访问的地址；如果服务有认证，在「认证与密钥」中配置对应凭据。

没有本机环境时，可进入 **模型与环境 → 环境配置**：已有 ComfyUI 就选择其根目录；新环境可使用「从零装机」，查看安装计划后再开始。模型与依赖会另外下载，请按磁盘空间和网络情况选择安装档案。

### 2. 检查模型与工作流

在「模型与环境」中检查 Checkpoint、自定义节点和环境状态，按提示补齐缺失项。模型通常放在 ComfyUI 的 `models/checkpoints` 中，添加后重新扫描；新增自定义节点后通常需要重启 ComfyUI。

回到「生成」，选择与模型族匹配的工作流。第一次建议使用 **Illustrious 快速 · 20 步 · 单段**，前提是已有对应模型和节点。原高分辨率工作流仍可选，但步骤更多、生成时间也更长。Anima、Illustrious 等模型族的模型与 LoRA 需要分别匹配。

### 3. 配置提示词 AI（按需）

想用中文描述画面、让 AI 整理提示词，进入 **设置 → AI 提供商 → 提示词 AI**：

1. 填写 OpenAI 兼容接口的 Base URL（包含接口要求的 `/v1`）、模型名称与 API Key。
2. 点击「获取模型列表」或「模型测试」确认连通性。
3. 开启「AI 提示词优化」并保存。

参考图理解可以跟随提示词 AI，也可以单独配置视觉接口；使用的模型需要支持图像输入。AI 调用由你选择的提供商处理，可能产生相应费用。

暂时不使用提示词 AI 时，可以先用英文标签验证模型与生成流程。

### 4. 生成第一张图

1. 进入「生成」，选择文生图、模型族和工作流。
2. 输入提示词。例如：`1girl, green hair, white shirt, standing in a garden, soft daylight`。
3. 检查尺寸和参数；先沿用工作流默认采样设置，再按需调整。
4. 点击「开始生成」，或按 `Ctrl+Enter` 提交，查看排队与生成进度。
5. 完成后预览、保存或收藏；也可以在「历史」中查找图片并回填参数重新生成。

图生图时切换到相应模式，上传或拖入参考图，再描述希望修改的内容。不同工作流支持的参数和参考图处理方式可能不同。

### 5. 使用 LoRA

进入左侧 **LoRA** 工作台，搜索角色或模型，查看详情后安装。已有文件可在 **设置 → LoRA** 中指定本地目录并点击「扫描本机 LoRA」登记。

- **本机 ComfyUI**：本地目录应指向该 ComfyUI 实际读取的 `models/loras`。
- **远程 ComfyUI**：使用部署在远端的 LoraBridge 处理安装，并按提示配置连接与鉴权。参见 [LoraBridge 说明](comfyui_custom_nodes/ComfyUI-AstrBot-LoraBridge/README.md)。
- **角色档案**：中文别名用于查找和匹配；模型触发词用于激活 LoRA，两者含义不同。安装后建议检查别名、模型族和触发词。
- **生成前确认**：可手动选择 LoRA，或查看提示词自动匹配结果，移除不需要的条目。LoRA 应与当前底模及工作流兼容。

8.6.18 会按本次请求筛选 LoRA 注入词，减少默认服装与用户换装要求的冲突。模型训练偏好仍会影响画面，建议固定种子进行对照，再调整描述和权重。

## 还有哪些功能

| 功能 | 用途 |
| --- | --- |
| 生成画布 | 文生图、图生图、队列与取消；缩放、平移和大图预览 |
| 历史与收藏 | 按提示词或种子查找、回填参数、分类收藏、备注与回收站 |
| LoRA 工作台 | 在线发现、下载进度、本地档案、别名与触发词、自动匹配预览 |
| 模型与环境 | 模型和节点检查、本机环境扫描、安装计划及 Bridge 配置 |
| 诊断 | 查看连接与运行状态，导出经过脱敏的诊断信息 |
| 主题 | 浅色、深色和猛男粉；按 `T` 切换 |

### 多人房间（实验功能）

多人房间位于调试模式的「实验室」中。连续点击左上角 WineFox Logo 10 次解锁，再进入实验室创建房间、设置昵称与访问方式，分享软件生成的邀请地址。

局域网模式需要设备能互相访问；公网模式需要可用的隧道或反向代理。自动隧道是否可用取决于网络和 cloudflared 环境。建议先确认单人生成正常，再测试房间功能。

## 常见问题

| 问题 | 先检查什么 |
| --- | --- |
| 无法连接 ComfyUI | 服务是否启动、浏览器是否可访问、地址和端口是否正确；远程环境再检查认证与网络 |
| 工作流提示缺少模型或节点 | 在「模型与环境」检查缺失项；确认模型族，安装节点后重启 ComfyUI |
| 中文提示词转译失败 | 提示词 AI 的地址、密钥、模型与连通性；可先用英文标签排查生成链路 |
| LoRA 已下载但无法使用 | 文件是否在 ComfyUI 实际读取的目录、是否重新扫描，以及底模与 LoRA 是否同族 |
| 搜索或下载失败 | 来源站点是否可访问；在 LoRA 设置中检查代理；受限内容可能需要对应账号凭据 |
| 下载后仍显示旧版 | 先完全退出旧版，包括托盘进程，再运行新安装包或新解压目录中的程序 |
| 应用内没有提示 8.6.18 | 当前需要手动升级，请直接使用本仓库 Releases 的安装包 |

仍有问题时，请在 [Issues](https://github.com/XIAOKU2300/WineFoxDesktop/issues) 中提供版本、复现步骤和界面报错。诊断信息可先从「诊断」页导出，提交前确认不含密钥或个人信息。

## 数据与隐私

- 默认配置、历史和输出保存在 `%LOCALAPPDATA%\WineFox`；便携独立启动脚本使用所在目录的 `data`。
- API 密钥使用系统凭据存储。更换电脑后通常需要重新填写；独立数据目录不等于独立的 Windows 凭据项。
- 使用提示词 AI、视觉 AI 或在线 LoRA 来源时，相关内容会发送到你配置或使用的服务；请根据需要选择。
- 自动安装更新会校验 Ed25519 签名，验证失败时终止。当前 8.6.18 的手动升级限制见上方说明。

## 源码开发

建议使用 Python 3.12、Node.js 20+ 与当前稳定版 Rust MSVC 工具链；Windows 还需具备相应的 C++ 构建工具。完整打包流程见 [Windows 构建手册](packaging/WINDOWS_BUILD_RUNBOOK.md)。

在仓库根目录执行以下 PowerShell 命令。首次运行前确认 `.venv` 为本项目使用的环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r packaging\pyinstaller\requirements-sidecar.txt
$env:WINE_FOX_PYTHON = (Resolve-Path .\.venv\Scripts\python.exe).Path
Set-Location desktop_shell
npm ci
npm run tauri dev
```

只查看前端演示时，在仓库根目录运行：

```powershell
python -m http.server -d desktop_ui 8000
```

打开 `http://127.0.0.1:8000/index.html?mock=1`。这是模拟数据演示，不会执行真实生成。

开发测试需在自己的虚拟环境中安装测试依赖，再从仓库根目录运行：

```powershell
.\.venv\Scripts\python.exe -m pip install pytest pytest-subtests pyyaml
.\.venv\Scripts\python.exe tools\run_desktop_review_tests.py tests tests_desktop -q
.\.venv\Scripts\python.exe tools\run_desktop_review_tests.py desktop_integration_tests -c desktop_integration_tests\pytest.ini -q
node --experimental-vm-modules --test tests_ui\review-regressions.test.mjs
cargo test --manifest-path desktop_shell\src-tauri\Cargo.toml --locked
```

## 社区与许可

- [B 站项目介绍](https://www.bilibili.com/video/BV1FnhR6TE37/)
- [GitHub Issues：问题与建议](https://github.com/XIAOKU2300/WineFoxDesktop/issues)
- [LinuxDO 社区](https://linux.do/)
- [MIT License](LICENSE)

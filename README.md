# WineFox Desktop

WineFox Desktop 是将 `astrbot_plugin_comfyui_pro_UltraPlusMax` 的 ComfyUI 绘图能力迁移到 Windows 桌面的本地应用。它采用 Tauri 2 桌面外壳、Python 本地 sidecar 和原生 HTML/CSS/ES Modules 界面，保留文生图、图生图、多工作流、LoRA 与中文别名、历史、收藏、模型检测、诊断和旧数据导入等核心能力。

> 当前状态：开发预览。仓库暂不发布 Release，也不提供自动更新包。应用不会捆绑 ComfyUI、模型、CUDA 或用户密钥。

## 桌面版能力

- 连接本机或受信任网络中的 ComfyUI，检测节点、checkpoint、LoRA 与工作流兼容性。
- 文生图和图生图使用内置工作流参数；高级参数只有显式开启后才覆盖工作流值。
- LoRA 列表、中文别名检索、角色匹配、模型族筛选与多 LoRA 注入。
- 本地任务队列、结果历史、收藏、软删除与崩溃恢复。
- 独立 AI 文本/视觉提供商配置，密钥通过系统凭据存储，不写入普通配置。
- 仅监听 `127.0.0.1`，使用一次性 bootstrap 会话、Host/Origin/CSRF 校验和默认 TLS 验证。

## 直接运行源码

环境要求：Windows 10/11、Python 3.11+、Rust 1.77+、Node.js 20+、WebView2。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r packaging\pyinstaller\requirements-sidecar.txt
cd desktop_shell
npm install
$env:WINE_FOX_PYTHON = "..\.venv\Scripts\python.exe"
npm run tauri dev
```

启动后应用会先在本地加载配置、工作流和目录数据，完成后直接进入生成页。ComfyUI 默认地址为 `http://127.0.0.1:8188`，可在设置中修改。

## 构建便携版

```powershell
powershell -ExecutionPolicy Bypass -File .\packaging\build.ps1 -SkipNsis
powershell -ExecutionPolicy Bypass -File .\packaging\stage-portable.ps1
```

便携版输出到 `packaging\dist\WineFoxDesktop`。正式安装包还需要 NSIS；签名和更新公钥配置完成之前不得发布。

## 验证

```powershell
python -m unittest discover -s tests -v
python -m unittest discover -s tests_desktop -v
python packaging\tests\test_sidecar_protocol.py
cd desktop_shell\src-tauri
cargo test
```

仓库还包含 `tools\desktop_verify.py`，用于检查 LAN 监听、固定口令、query token、密钥泄漏、TLS 默认值和生产包内容。

## 数据与隐私

运行数据默认位于 `%LOCALAPPDATA%\WineFox`。设置、上传、输出、日志和 SQLite 数据均不应提交到 Git；密钥不写入 `settings.json`、数据库或日志。卸载默认保留用户数据，删除前请自行备份。

## 目录

| 路径 | 用途 |
|---|---|
| `desktop_ui/` | 桌面界面与设计系统 |
| `desktop_backend/` | 本地 `/api/v1` HTTP API |
| `desktop_runtime/` | Core、存储、工作流与后端装配 |
| `winefox_core/` | 平台无关领域逻辑与 ComfyUI 传输 |
| `desktop_shell/` | Tauri 外壳和 sidecar 入口 |
| `packaging/` | PyInstaller、便携版和 NSIS 构建 |
| `workflow/` | 内置 ComfyUI 工作流 |

## 项目来源与许可

桌面版基于 WineFox AstrBot 插件迁移。根目录保留原插件源码与下方原始说明，便于核对行为和继续兼容。仓库使用根目录 `LICENSE`；发布前仍需完成第三方 Python 包、Rust crates、前端依赖、工作流和 ComfyUI 自定义节点的 Notice 审核。

---

# 原 WineFox AstrBot 插件说明

AstrBot の究极绘图插件～ 这不过是本喵随手捏的玩具啦，笨蛋杂鱼主人能玩明白吗喵～(≧∀≦)ゞ 支持 ComfyUI 文生图、引用/附带图片自动图生图、角色 LoRA 自动匹配、Civitai LoRA 搜索导入、GPT 魔法生图、中文快捷指令、多工作流切换和生图结果自动撤回～ 功能多到你这只杂鱼脑容量要爆炸了捏 (●ˇ∀ˇ●)

- 插件名：`astrbot_plugin_comfyui_pro_UltraPlusMax` （名字长才够高贵喵呜～）
- 作者：`XIAOKU2300` （快跪下膜拜本喵！(◣ω◢)）
- 版本：`8.5.6` （持续进化喵，杂鱼主人可别掉队啦～）
- 仓库：`https://github.com/XIAOKU2300/astrbot_plugin_comfyui_pro_UltraPlusMax.git` (⸝⸝•̀֊•́⸝⸝)

## 🐾 安装更新

在 AstrBot 插件管理里用这个仓库地址安装喵～ 复制粘贴要是还出错的话本喵就挠你哦 (╬ Ò﹏Ó)
    https://github.com/XIAOKU2300/astrbot_plugin_comfyui_pro_UltraPlusMax.git

插件的小窝窝在这里，可别手残删掉啦 (｡•́︿•̀｡)：
    data/plugin_data/astrbot_plugin_comfyui_pro_UltraPlusMax/

就算更新代码，你辛苦攒的 LoRA 缓存、角色档案、自定义工作流全都安然无恙喵～ 本喵做事超严谨的！(๑•̀ㅂ•́)و✧

## ⚙️ 基础配置

- 把 ComfyUI 叫醒起来，确认 API 地址能访问～ 默认就那个 127.0.0.1:8188，敢填错的话本喵就罚你学猫叫一百遍喵！(｀・ω・´)
- 在插件配置里乖乖填上 server_address～ 填些奇奇怪怪的东西的话……（亮爪子）
- 文生图默认推荐 finish_optimized_api.json～ 节点号自己用本喵的小本本记好喵：正面 11，负面 12，输出 66，角色 LoRA 99 (=｀ω´=)
- 图生图角色替换默认用 character_replace_anima_reference_api.json～ 这可是本喵用猫爪精心调配的喵！(ฅ´ω`ฅ)
- Civitai 下载目录必须写你 ComfyUI 的 LoRA 文件夹路径喵！比如 F:/AIdaw/ComfyUI-aki-v7/ComfyUI/models/loras，要是填错找不到文件，本喵可不帮你找 (눈‸눈)
- 如果 Civitai 需要代理，填 http://127.0.0.1:7899 就行啦～ 这都不会的话，你是笨蛋吗？(￣ω￣;)
- 图生图视觉反推可以单独填：llm_settings.img2img_vision_api_base、img2img_vision_api_key、img2img_vision_model～ 想玩高级功能就要好好设置喵！(o゜▽゜)o☆

### 🎬 Wan 图生视频模型清单（8.5.1 画质修复必备）

放到 ComfyUI 的 `models/` 对应目录（如 `F:/AIdaw/ComfyUI-aki-v7/ComfyUI/models/`）：

| 文件 | 目录 | 用途 | 必需 |
|------|------|------|------|
| `clip_vision_h.safetensors` | `clip_vision/` | CLIPVisionEncode 源图语义编码 | **两档都必需** |
| `wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors` | `unet/`（或 `diffusion_models/`） | 画质档-高噪模型（FP8 原生） | 仅画质档 |
| `wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors` | `unet/`（或 `diffusion_models/`） | 画质档-低噪模型（FP8 原生） | 仅画质档 |

> `umt5_xxl_fp8_e4m3fn_scaled.safetensors`（T5 文本编码）、`Wan2.1_VAE.safetensors`、Q4 GGUF 两个快速档模型通常已具备，无需重下喵～ 以上 3 个画质修复模型都来自 `Comfy-Org/Wan_2.2_ComfyUI_Repackaged` / `Comfy-Org/Wan_2.1_ComfyUI_repackaged`，可一键脚本 `-DownloadWanQuality` 直接拉取。


## 🖼️ 图生图说明

只要你引用图片或者附带图片，本插件就会自动切成图生图模式喵～ 超敏锐的猫猫感应！(✧ω✧)
一旦命中角色 LoRA，工作流会立刻换成 character_replace_anima_reference_api.json。这个工作流是按照你给的 Anima正式版-图生图工作流-反推提示词.json 重建的 API 版，完美保留了 Anima 基础模型、DepthAnythingV2、Anima LLLite、固定质量 LoRA 和角色 LoRA 注入链路～ 本喵可是用小鱼干换来这些代码的喵！(´・ω・`)ﾉ

你的 ComfyUI 那边得准备好这些宝贝 (ᗒᗨᗕ)：
  - anima_baseV10.safetensors
  - qwen_3_06b_base.safetensors
  - qwen_image_vae.safetensors
  - Anima-LLLite\anima-lllite-any-test-like-1-step2000.safetensors
  - anima-highres-aesthetic-boost.safetensors
  - anima-preview-3-masterpieces-v5.safetensors
  - 节点：AIO_Preprocessor、DepthAnythingV2Preprocessor、AnimaLLLiteApply、LayerUtility: ImageScaleByAspectRatio V2、LoraLoaderModelOnly

注意啦喵！你给的那个原始前端工作流里，RH_LLMAPI_Pro_Node 和 CR Prompt Text 在当前 ComfyUI 根本就没加载，JSON 字符串还坏掉了 (╯°□°）╯︵ ┻━┻ 所以本插件内置的是直接可喂给 API 的重建版，可别搞混了喵～

## ✨ 常用指令

背不下来这些指令的话，本喵就在你脸上按猫爪印喵！(≧∇≦)ﾉ

- `酒狐来点 <描述>`：唯一普通绘图入口，带图/引用图会自动走图生图～
- `引用/附带图片 + 酒狐来点视频 <动作/镜头>`：Wan 图生视频入口，默认 832×480 / 81 帧 / 16fps，分段续写拼 ~15-20s，结果发密码 `114514` 压缩包。**默认使用 `wan_i2v_api.json` 快速档（Q4 4 步，更稳、更显存友好）**，两档工作流均接入 CLIPVision 语义引导；需要更高画质时可在 `video_settings.workflow` 手动切换到 `wan_i2v_quality.json` 画质档(FP8 20 步)。
- `引用/附带图片 + 酒狐来点 达妮娅`：自动触发图生图角色替换，省得你多哔哔～
- `酒狐魔法 <描述> / 酒狐魔法+<描述>`：GPT 魔法生图，提示词原样发给 OpenAI 兼容图片接口，享受钞能力吧杂鱼！٩(●˙▿˙●)۶
- `引用/附带一张或多张图片 + 酒狐魔法 <要求>`：GPT 魔法图生图 / 多图编辑，源图和要求一起扔给编辑接口，本喵帮你搞定 (๑•̀ㅂ•́)و✧
- `引用/附带图片 + 酒狐魔法改图 <要求> / 酒狐魔法图生图 <要求>`：同上，写法更直白，照顾你这金鱼脑～
- `酒狐魔法重画 <描述>`：绕过缓存重新生成，免得你嘟囔“怎么又是这张” (￣へ￣)
- `酒狐魔法注册`：注册 GPT 魔法并领默认额度，白嫖党喵喵叫～
- `酒狐魔法余额`：查查你还有多少额度，花完别哭着找本喵 (｡•́︿•̀｡)
- `酒狐魔法兑换 <卡密>`：兑换管理员发的额度卡密，快说谢谢管理员喵！
- `/jh magic <prompt>`：GPT 魔法英文入口，给那些洋文场合备用 (。-`ω´-)
- `/jh help` 或 `/comfy帮助`：查看帮助，实在不行再看这个吧 (눈_눈)
- `/jh 模型`：查看当前模型族、checkpoint、工作流和 LoRA 搜索 BaseModel，让你心里有数～
- `/jh 模型 列表`：列出 Anima / Illustrious，二选一，选错本喵会嘲笑你哦 (ﾉ>ω<)ﾉ
- `/jh 模型 切换 <1|2>`：管理员用序号切模型族，闲杂人等退散喵！
- `/jh 切换画风 <序号|默认|超写实|pvc|newpvc|随机>`：切 Illustrious 内置画风，发 `/jh 切换画风` 可看列表～
- `/jh 角色列表`：查看当前模型族可以直接画的角色、叫法和触发词～
- `/jh lora 切换 <序号|名称> [model] [clip]`：换 LoRA 啦，想换哪个换哪个 (ﾉ´ヮ`)ﾉ*: ･ﾟ
- `/jh 找lora <关键词>`：按当前模型族搜 Civitai LoRA，返回前 6 个结果，带预览图和热度评分～ 本喵的筛选品味可是一流的喵！ฅ^•ﻌ•^ฅ
- `回复 1-6`：下载最近一次搜索结果，手速慢了可就没啦～
- `/jh 导入 <链接|ID> [别名]`：从 Civitai 链接或 ID 直接导入，懒人福利～
- `/jh 删除角色 <角色名|LoRA名>`：统一清理角色档案、别名、角色词和本地 LoRA 文件，删完重找就能重下，像猫砂一样干净 (´-ω-`)
- `/jh 状态`：查看当前工作流、LoRA、负面词，强迫症必备～
- `/jh 撤回 查看`：看看自动撤回状态喵～
- `/jh 撤回 开启` / `/jh 撤回 关闭`：控制生图结果自动撤回，免得留下黑历史 (〃ω〃)
- `/jh 撤回 设置 <秒数>`：设个倒计时，手慢无～
- `/jh 自审查 查看/开启/关闭`：管理 ComfyUI 一级自审查，没过审的会发加密 ZIP，想偷看可没那么容易！(｀ε´)
- `酒狐审核` / `酒狐鉴黄`：引用或附带图片后，进行独立五分类图片审核，涩涩退散喵！(╬☉д⊙)
- `酒狐更新画图`：管理员从当前 git 仓库更新插件代码，用 git fetch + git merge --ff-only，不动 AstrBot 配置和插件数据，稳得像猫爬架 (￣▽￣)~*
- `酒狐画图强制更新`：管理员强制从远端覆盖更新，执行前会备份当前插件源码～
- `酒狐强制更新 8.0.0`：管理员强制覆盖到指定版本/tag，会依次尝试 `v8.0.0` 和 `8.0.0`～
- `酒狐更新画图 状态` / `酒狐更新画图 记录`：查看自更新状态和图片版更新历史，跟上本喵的肉垫节奏～
  自更新默认复用 civitai_settings.proxy 或 model_download.proxy；你也可以在 self_update_settings.proxy 单独填～ Git 网络失败时会换 GitHub zip 下载兜底，双重保障喵！(๑¯◡¯๑)
- `/jh 工作流 列表`：看看有哪些工作流可以翻牌～
- `/jh 工作流 切换 <序号>`：换文生图工作流，换个口味喵～

## 🪄 GPT 魔法 WebUI

GPT 魔法是本喵额外塞进去的独立通道，用 OpenAI 兼容图片接口，跟 ComfyUI 井水不犯河水喵～ 首次启动会在插件数据目录建个小窝：
    data/plugin_data/astrbot_plugin_comfyui_pro_UltraPlusMax/gpt_magic/

默认 WebUI 入口，用你的猫爪点进去 (つ≧▽≦)つ：
    http://你的服务器IP:1144

令牌自动生成并写进 gpt_magic/db.json，启动日志里也会提示，眼睛瞪大了找哦！WebUI 分区如下：

- GPT 魔法配置：API Base/API URL/API Key、模型、尺寸、质量、流式、缓存、队列和默认额度，调教好才会乖乖干活喵～ (´･ω･`)
- 用户额度：给用户加减额度、拉黑、加备注，管理大权在握喵！
- 卡密管理：批量生成 JHM-XXXX-XXXX-XXXX 卡密，像撒猫粮一样～
- 队列状态：看当前任务和排队情况，别催，在画了在画了 ( ´Д`)y━･~~
- 历史图片：翻看 GPT 魔法生成的历史，回味一下喵～
- 收藏图库：在 WebUI 里直接浏览 `favorites` 目录，按角色分类看收藏图，支持搜索角色、文件名、QQ 和提示词预览。
- ComfyUI：集中查看/切换 Anima 与 Illustrious，检测并下载 Illustrious checkpoint，也能直接修改 ComfyUI 地址、默认工作流节点、Anima 图生图/角色替换参数和头像重绘提示词。

## 🔢 猫娘数学小课堂：OpenAI 兼容接口规则

看仔细了喵，别配错，不然本喵要念一百遍 ∞ 给你听！(>ω<)

- api_url 优先；如果留空，就用 base_url + /v1/images/generations。base_url 填到域名或者带 /v1 都行～ 数学课：base_url ∪ {/v1} 都是合法的喵～
- edit_api_url 用于图生图，可留空；留空时本喵会用同一个 base_url 拼出 /images/edits，或者把 api_url 里的 /images/generations 自动替换成 /images/edits，替换式：f(x) = replace(x, '/images/generations', '/images/edits') (≧∇≦)ﾉ
- 默认模型：gpt-image-2，别问，问就是本喵的直觉 (ΦωΦ)
- gpt-image* 默认尝试流式请求；要是中转不支持，会自动退回到非流式，绝不卡死喵～
- GPT 魔法尺寸新增 2K/4K/Max 预设：3840x2160、2160x3840、2880x2880 等会通过 size 字段透传给支持高分辨率的中转；官方不支持或参数名不同的，你可以在 额外请求 JSON 里塞 output_size、resolution 等私有字段，本喵兼容性拉满～ 数学上等价于 ∀ 参数 ∈ {所需高分辨率}，∃ 透传方式 (๑•̀ㅂ•́)و✧
- GPT 图生图自动读取引用图片和同消息附带的一张或多张图片，并把要求原样传给 /v1/images/edits；如果中转不认 input_fidelity、output_format、background 这些扩展参数，会自动降级成最小兼容参数重试，就像本喵踩奶一样坚持不懈 (´▽`ʃ♡ƪ)
- 默认缓存开启，相同 prompt/model/尺寸会命中缓存，想尝鲜请用 酒狐魔法重画 绕过～ 命中率 P(hit) ≈ 99.9%，除非你每次都换词 (¬‿¬)

## 📜 本版变化

- 2.2.0：光流图生图微调，长边 1024 -> 896、Depth 768 -> 640、采样 22 -> 18 步，ControlNet 提前收尾，给那 5 分钟超时一点颜色看看喵 ✨(≧∀≦)ゞ
- 2.1.9：光流文生图采样风格退回原来的 Euler ancestral 味儿，只降二采重绘免得融线；更新日志底部给你补上了插件署名，毕竟本喵的杰作 ✨(=^･ω･^=)
- 2.1.8：LoRA 导入不再把搜索词当角色档案，谁让你乱搜的！光流文生图换更稳的 DPM++ SDE Karras，高步数成型、轻二采收尾，融线和局部化开都给我退散喵 ✨(｀・ω・´)
- 2.1.7：LoRA 搜索结果合进一个聊天记录，整洁多啦；光流文生图补手部负面词，二段 refine 改低重绘 DPM++ 2M Karras，手指再也不画散，本喵的猫爪验证通过！✨(ฅ´ωฅ)
- 2.1.6：光流图生图换低显存单采版：生成前主动释放 ComfyUI 显存，OOM 后自动清理并用低显存参数重试一次；默认长边降到 1024，Depth 768，砍掉 latent 放大和二采，显存危机解除喵～ 像吃光猫粮后又续杯一样 ✨(๑¯∀¯๑)
- 2.1.5：修复引用消息里喊酒狐来点...不触发绘图的问题，现在会剥掉引用标记只看本条消息文本，别想用引用消息来欺负本插件喵 ✨(눈‸눈)
- 2.1.4：更新记录图继续用 Playwright Chromium 本地截图；Docker 里 Chromium 秒退的话会自动尝试补系统依赖，也可以用history_render_browser手动换内核，这都搞不定就是你自己的问题喵 ✨(╯°□°)╯︵ ┻━┻
- 2.1.3：更新记录图改成插件自己用本地 Playwright/Chromium 截图，不再优先走 AstrBot 远端html_render；背景去掉外链，缺浏览器会自动补装，失败了才回退 Pillow 兜底，已读不回？不存在的喵 ✨(≧ω≦)
- 2.1.2：参考 IL_Controlnet V2.6 重做 Illustrious/光流图生图主干：WAI + CLIP skip -2 + Depth ControlNet + 两段采样 + Tiled VAE；图生图 denoise 不再把二采一起玩坏，稳得一批喵 ✨(๑•̀ㅂ•́)و
- 2.1.1：更新记录图背景改成整张长图连续铺底，字号整体加大；HTML 截图失败会重试一次，少退回旧兜底图，美观度 UP 喵 ✨(｡•̀ᴗ-)✧
- 2.1.0：同步新的更新记录 HTML，长图背景和毛玻璃卡片样式再优化；后续版本按 2.1.0-2.1.9 满十进一，期待满满喵 (｡•̀ᴗ-)✧
- 2.0.39：更新记录图先本地渲染模板数据再交给截图服务，保住自定义 HTML UI；禁用无用高亮脚本注入，Pillow 兜底补中文字体扫描，全是细节喵 ✨(=｀ω´=)
- 2.0.38：HTML 记录图改请求 JPEG，并发送前重写成标准 RGB JPEG，修正渲染服务.jpg文件名里装 PNG 的可疑问题；失败才回退 Pillow，就是这么严谨喵 (｡•̀ᴗ-)✧
- 2.0.37：聊天里的更新记录图强制恢复旧版 Pillow PNG 渲染，避开html_render截图导致的 QQ 富媒体上传失败；HTML 模板继续保留，两手准备喵 (｡•̀ᴗ-)✧
- 2.0.36：自更新恢复旧版发送链路：文字先普通发，记录图再用Image.fromFileSystem原图单独发；保留新版记录图 UI，美观与稳定兼得喵 ✨(´･ω･)
- 2.0.35：更新记录图发送前自动压成小 JPEG，QQ 富媒体上传失败时会补发文字结果，不再整条静默失败，不许你说我坏话 (｡•́︿•̀｡)
- 2.0.34：自更新只发一条结果+一张记录图，不再单独刷热重载；无更新时直接提示“当前已经最新版本”，废话少说。记录图现在展示每版更新内容，少放 Git 噪音，看着清爽喵 ✨(￣▽￣)~*
- 2.0.33：更新记录图改为 HTML/CSS 模板渲染，模板文件在 templates/self_update_history.html；渲染失败时仍会回退 Pillow 兜底图，留好后路喵。
- 2.0.32：酒狐更新画图 成功后延迟约 1 秒调用 AstrBot 插件管理器热重载自己，新增 self_update_settings.auto_reload_after_update 开关。从旧版本升级这一次还得手动重载，之后全自动，算你走运喵 ✨(ΦωΦ)
- 2.0.31：精简 酒狐更新画图 聊天提示，更新后自动发重新设计的更新记录图；记录图变浅色毛玻璃卡片、细线装饰和短摘要，缺中文字体时自动用英文标签，拒绝方块字和长 Git 日志刷屏喵。
- 2.0.30：自更新识别 classic PAT（ghp_...）后优先走 git HTTPS clone/fetch + askpass，不再优先 GitHub API zip；fine-grained PAT（github_pat_...）仍保留 API zip 优先，省得你混淆喵 (。-ω´-)
- 2.0.29：HTTPS token 已配置且仓库是 GitHub 时，自更新的非 git 安装路径优先走 GitHub API zip 下载，避开git clone对 fine-grained PAT 的Write access误导；权限不够就只返回私库读取权限提示喵。
- 2.0.28：自更新私库认证提示改成人话；如果误把github_pat_.../ghp_...填进token_env，插件会自动当作 HTTPS token 使用，包容度拉满。GitHub zip 兜底遇到 401/403/404 会明确提示 Docker 环境变量缺失或 token 缺少仓库 Contents Read/repo 权限喵。
- 2.0.27：自更新网络层默认复用 Civitai/模型下载代理，也可用self_update_settings.proxy单独覆盖；git clone/fetch遇到 GitHub TLS/GnuTLS 等网络失败时，自动换 GitHub zip 下载并走同一个代理更新，Docker 里的 git 网络抽风也不怕喵 ✨(╯✧∇✧)╯
- 2.0.26：自更新支持 zip 安装兜底。当前插件目录不是 git 仓库时，只要配了repo_url，会临时 clone 仓库到插件数据目录，备份当前代码后复制新版覆盖；配置和插件 data 不会被复制/删除。还修正常见误填：把 SSH 私钥路径填到token_env时会自动按ssh_key_path使用，贴心吧喵～
- 2.0.25：修复酒狐更新画图普通消息不触发的问题。自更新命令现在和酒狐审核同套普通消息兜底检测，会从message_str、raw message 和消息组件文本里识别命令并拦截 LLM/其它插件，休想逃喵 ✨(｀ε´)
- 2.0.24：新增管理员命令酒狐更新画图，支持从插件 git 仓库安全快进更新；私库可用 SSH deploy key 或 HTTPS PAT 环境变量认证。新增酒狐更新画图 状态和酒狐更新画图 记录，更新记录会保存在插件数据目录并渲染成图片发送。更新只操作插件代码仓库，默认拒绝有未提交改动的工作区，不会删 AstrBot 配置、LoRA 缓存或插件 data，安全得像猫窝 (￣ω￣)
- 2.0.23：修复“找不到当前模型族 LoRA 时自动切到另一模型族”会持久改掉当前模型的问题。现在只对本次 ComfyUI 生图临时切换，成功、失败、冷却、缺 checkpoint 等路径结束后都会恢复原模型族，提示改为“本次临时用 ...”，再也不会偷偷改设置喵 (´-ω-)
- 2.0.22：精简 /jh help，只保留日常最优入口；新增 /jh 删除角色 <角色名|LoRA名> / jh删除角色 <角色名|LoRA名>，统一清理该角色在 Anima/Illustrious 下的角色档案、别名、角色词、本地 LoRA 文件和当前 LoRA 选择，避免手动分开删后仍命中旧档案，懒人救星喵～
- 2.0.21：收紧普通消息触发，裸 来点、裸 魔法、裸 审核 不再触发插件，保留完整 酒狐来点、酒狐魔法、酒狐审核 和 jh... 入口；/jh 找lora 不再因本地角色档案命中而阻断 Civitai 搜索，并新增 /jh lora 档案 列表/删除 <角色名|LoRA名> 清理本地角色档案。
- 2.0.20：修复 GantMan/nsfw_model 在 TensorFlow 2.21/Keras 3 下加载 KerasLayer 失败的问题；加载链路现在会依次尝试 nsfw_detector、TensorFlow+tensorflow_hub.KerasLayer、tf_keras 兼容加载，仍失败时自动用 opennsfw2 二分类兜底。新增 requirements.txt 安装 opennsfw2[keras3]。
- 2.0.19：修复酒狐审核普通消息触发。现在会按 message_str、raw message 和消息组件文本多路检测；唤醒词剥离后的 审核初始化 / 审核状态 只有在 AstrBot 明确标记为唤醒消息时才会生效，并加了命令边界，避免裸 审核 或无关词误触。
- 2.0.18：酒狐审核新增显式初始化/状态命令：酒狐审核初始化 立即启动模型下载部署，酒狐审核状态 显示模型路径、下载进度、代理和 nsfw_detector/tensorflow 依赖状态；本地审核新增 TensorFlow-only 兜底，没装 nsfw_detector 但已装 TensorFlow 时也能直接跑 GantMan 五分类模型。
- 2.0.17：酒狐审核新增本地模型自动部署开关；开启 image_audit_settings.auto_download_model 后，插件初始化时若未检测到本地模型，会用内置多线程下载器拉取 GantMan/nsfw_model zip，支持代理和分片数配置，下载后自动解压并识别模型路径。
- 2.0.16：修复 酒狐审核 在普通带图消息里可能不触发的问题；现在 酒狐审核、酒狐鉴黄、jh图片审核、jh鉴黄 都会被普通消息兜底入口捕获，休想躲开审核喵 (｀ω´)
- 2.0.15：新增独立图片审核命令 酒狐审核 / 酒狐鉴黄，支持引用图片和随消息发送图片，输出绘画/二次元色情/正常/色情/性感五分类概率和通过结果。后端优先支持本地 nsfw_detector + GantMan/nsfw_model，也可接自建 NSFWJS HTTP 服务。
- 2.0.14：生图时如果当前模型族没有命中 LoRA 角色档案、另一模型族显式命中该角色，会自动切换到对应模型族并发送简短提示；自动切换只看角色名/别名，不使用 LLM 猜测，避免误匹配喵～
- 2.0.13：修复模型族切换解析，/jh 模型 切换 会直接显示序号列表并支持 1/2 切换；无效文本不再兜底切回 Anima。Illustrious 图生图会在提交 ComfyUI 前修复旧工作流里的 VAE 接线。
- 2.0.12：修复 Civitai LoRA 搜索预览图直连超时导致整条搜索结果发送失败的问题。预览图会先按小图 URL 下载到本地缓存，下载失败只跳过预览，不影响文字结果和序号导入；同一用户连续搜索时旧搜索结果会被丢弃，避免“娜娜莉/达妮娅”这种串词覆盖；搜索前会先检查本地角色档案，当前模型族已有角色时直接提示本地已有，其他模型族已有时在无结果提示里给出切换建议。
- 2.0.11：新增 ComfyUI 一级自审查，默认开启且只作用于 ComfyUI，不影响 GPT 魔法。未通过时不再直发图片，而是提示“未通过酒狐一级自审查，已发送加密压缩包 密码默认114514”，并把单张结果图写入密码 ZIP；可用 /jh 自审查 查看/开启/关闭/密码 <新密码> 管理，也可在 comfy_nsfw_self_review 配置里调整是否拦截内衣/透视/湿衣、是否连泳装也拦截。
- 2.0.10：重做 Civitai LoRA 搜索排序和展示。搜索结果数量默认提升到 6 个；同一关键词会并行抓取当前模型族的 Most Downloaded / Highest Rated 候选，并在需要时并行补充 LLM 英文搜索词。结果按猫娘数学公式排序喵～ 看好了：
  综合分 = (65*log10(心+1)+35*log10(下载+1)) * (1+0.12*相关度) (≧ω≦)
  每个候选在转发消息里附一张安全预览图，NSFW/未审核/未成年人相关预览会过滤不发。搜索仍严格按当前 Anima / Illustrious 模型族隔离喵～
- 2.0.9：参考 光流文生图.json 重写 Illustrious 文生图为插件稳定版：WAI checkpoint、CLIP -2、角色 LoRA 节点 99、双段 latent refine、Tiled VAE 输出，并自动优先使用已加载的 waiIllustriousSDXL_v170.safetensors，找不到时仍回退官方 Illustrious checkpoint。Civitai 角色档案导入和自动拼接触发词时默认剥离衣服/鞋袜/制服标签，只保留角色身份与稳定外观；只有用户明确要求默认服装/原皮/官服/default outfit 时才追加默认衣服。
- 2.0.8：收紧 LoRA 角色自动匹配。只有用户明确提到角色名、别名或可验证音译时才会注入角色 LoRA；LLM fallback 必须给出原文证据且置信度足够高，否则拒绝。导入新 LoRA 时也不再把 1girl/solo/blue eyes 这类泛触发词当作角色别名。
- 2.0.7：修复 /jh lora 列表 显示 ComfyUI 全量物理 LoRA 的问题。默认列表、序号切换、别名和角色词设置都会按当前模型族过滤；ControlNet、OpenPose、Depth、YOLO、LLLite 和其他模型族 LoRA 会被隐藏，Civitai 导入结果会明确标注模型族和 BaseModel。
- 2.0.6：修复 2.0.5 Illustrious 双段 latent refine 过重导致 ComfyUI 崩溃的问题，文生图改回轻量单段工作流；新增独立 img2img_illustrious_api.json，切到 Illustrious 后引用/附带图片会走 Illustrious 图生图，不再回退 Anima。同步整理 IL_Controlnet V2.6 参考包的模型放置：checkpoint 到 models/checkpoints，controlnet 到 models/controlnet，YOLO 到 models/yolo。
- 2.0.4：修复 Illustrious 图生图临时回退 Anima 时可能覆盖管理员后续模型切换的问题，避免已经切回 Anima 后仍提示“Illustrious 暂只支持文生图”。GPT 魔法图生图新增多图输入支持，会优先收集引用图片，再收集同消息图片，并以 multipart 多个 image 字段传给 /v1/images/edits，历史和结果说明会记录源图数量。
- 2.0.3：优化 Civitai LoRA 导入流程。下载文件和 LLM 角色档案解析会并行执行，减少导入等待；导入后先做一次 LoRA 列表刷新并尝试同步到 ComfyUI 实际目录，跨机器/跨容器不可写时不再长时间反复轮询。LoRA 列表刷新会合并 ComfyUI /object_info 和 /models/loras 两个接口，减少“ComfyUI 已能看到但插件误判未识别”的情况。路径提示会明确指出插件下载目录是否不是 ComfyUI API 返回的实际 LoRA 目录。
- 2.0.2：重做 GPT 魔法 WebUI 历史图片区域，改成日式线条简约毛玻璃 UI，历史图点击后在页内弹窗预览，不再新建窗口。GPT 魔法新增 2K/4K/Max 尺寸预设，并支持提示词里出现“4K + 横图/竖图/方图”时自动映射到对应高分辨率尺寸；额外请求 JSON 继续用于透传中转私有 4K 参数。
- 2.0.1：优化 Illustrious checkpoint 下载目录探测。若 AstrBot 无法直接访问 ComfyUI 的 models/checkpoints，WebUI 会显示原因、ComfyUI 返回目录、LoRA 目录推断出的候选目录和挂载提示，避免只报“目录为空”。下载仍只会写入明确可访问的目录，不会在容器里创建 ComfyUI 读不到的假目录。缺少 Illustrious checkpoint 时会在 LLM 转译和 LoRA 匹配前直接拦截，避免无意义调用 LLM。
- 2.0.0：新增 Illustrious 模型族，保留现有 Anima 全部能力。新增 finish_illustrious_api.json 文生图工作流，默认使用 Illustrious-XL-v2.0.safetensors、1024x1024、Euler ancestral、28 steps、CFG 6.0；/jh 模型 支持查看和切换模型族；/jh 找lora 会按当前模型族分别搜索 Anima 或 Illustrious，并按模型族隔离 LoRA 别名和角色档案。GPT 魔法 WebUI 新增模型管理入口，可检测并一键下载 Illustrious checkpoint。2.0.0 暂不引入 Pony；2.0.6 已新增独立 Illustrious 图生图工作流，引用/附带图片时不再回退 Anima。
- 1.0.9：新增结果图片是否放入聊天记录/合并转发的独立开关：ComfyUI 用 output_settings.comfy_image_in_forward，GPT 魔法用 gpt_magic_settings.result_image_in_forward。关闭后会把结果说明放进聊天记录，图片单独发出。GPT 魔法新增“收到任务提示”和“开始生成/编辑提示”的可配置文案。
- 1.0.8：修复 Civitai LoRA “插件本地已下载但 ComfyUI 不加载”时只能提示不能自救的问题。导入后会读取 ComfyUI /experiment/models 暴露的实际 loras 目录，若该目录对 AstrBot 进程可见则自动复制 LoRA 并重新等待识别；不可见时会把插件下载目录和 ComfyUI 实际目录同时报出。新增 /comfy_lora diagnose <LoRA|别名> / /comfy_lora 诊断 <LoRA|别名> 排查路径和加载状态。
- 1.0.7：修复视觉反推把模型分析过程、markdown 标题和第一人称说明塞进最终 Prompt 的问题；不再读取 reasoning_content，并会截断污染段、保留前面的有效 tags，仍无效才重试/跳过反推。同步迁移旧版角色替换 prompt 模板和 0.9/0.92 等旧重绘幅度到 Anima 推荐默认。Civitai 导入后会等待 ComfyUI 识别新 LoRA，别名列表会标出“本地已下载，ComfyUI 未加载”的 LoRA。
- 1.0.6：围绕 Anima 模型重做图生图角色替换默认策略：参考图只作为 Depth/LLLite 结构控制，KSampler 保持高重绘，目标角色由 LoRA 和提示词完整重画；新增 Anima 推荐参数说明，增强视觉反推约束，修复 酒狐魔法生成... 无空格写法，LoRA 搜索结果改为单条合并转发记录，自动撤回发送通道改为普通消息以稳定拿到 message_id，并重排 GPT 魔法 WebUI。
- 1.0.5：修复唤醒词被 AstrBot 截断后 酒狐魔法 ... 变成 魔法 ... 导致不触发的问题。现在 魔法 <提示词>、魔法+<提示词>、魔法改图 <要求> 等唤醒词剥离后的形态也会进入 GPT 魔法，并继续阻止 AstrBot LLM 插嘴。
- 1.0.4：修复 GPT 魔法 WebUI 端口/Host 改了不生效的问题。现在 AstrBot 插件配置变更会同步覆盖到 gpt_magic/db.json；独立 WebUI 内保存的端口也会在重启后继续生效。
- 1.0.3：优化 酒狐魔法 图生图/图片编辑：支持 酒狐魔法改图、酒狐魔法图生图 中文写法；增强 OpenAI 兼容返回解析；edits 接口失败时会从标准参数自动降级到最小兼容参数，提升中转兼容性。
- 1.0.2：酒狐魔法 支持引用/附带图片自动切换 GPT 图生图，走 OpenAI 兼容 /v1/images/edits；新增 edit_api_url 与 input_fidelity 配置，编辑接口不支持保真参数时会自动移除重试。
- 1.0.1：修复上传 zip 在 Linux/AstrBot 容器里因 Windows 反斜杠路径导致找不到插件目录的问题；版本号切换到 1.x。
- 0.1.40：新增 GPT 魔法生图通道，融合 OpenAI 兼容图片接口、独立额度、卡密、队列、缓存、历史图片和独立 WebUI。新增 酒狐魔法 <提示词> / 酒狐魔法+<提示词>，不影响原有 ComfyUI 工作流。
- 0.1.39：修复图生图视觉模型空输出时被静默跳过的问题。现在会兼容更多 OpenAI/Gemini 返回格式；首轮空输出会自动用短提示词重试一次；最终结果里会显示“视觉反推：已生效”或具体失败原因，避免误以为视觉反推参与了生成。
- 0.1.38：接入图生图视觉 LLM 反推。引用/附带图片时，插件会先用视觉模型把源图反推为“姿势、表情、镜头、构图、场景、道具、风格”等可复用结构词，并剥离源图角色身份，再合并目标角色 LoRA 触发词进入 Anima 图生图工作流。配置示例：img2img_vision_api_base=https://newapi-hk.qianye.host/v1，img2img_vision_model=gemini-3.5-flash-low，Key 在 WebUI 自行填写。
- 0.1.37：自动迁移旧版图生图角色替换提示词，移除会强行保留源图裁切的 meme sticker layout、same action and silhouette 等旧模板词；新版模板把源图作为宽松姿势/表情参考，用户要求的取景优先于源图裁切。同步放松 Anima LLLite 结构控制，减少局部近景被深度图锁死成身体局部裁切。
- 0.1.36：图生图角色替换默认改为 character_replace_anima_reference_api.json，按你提供的 Anima 正式版图生图参考工作流重建 API 版；旧 character_replace_anima_lllite_api.json 和 character_replace_union_control_api.json 会自动迁到新默认工作流。同步修复 _conf_schema.json 编码损坏导致 WebUI 配置不可读的问题。

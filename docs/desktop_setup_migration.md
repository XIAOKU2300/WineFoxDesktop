# WineFox Desktop「ComfyUI 环境中心」迁移文档

> 本文件是旧 AstrBot 插件 ComfyUI 初始化/检测/修复/节点安装/模型下载/Resource Bridge
> 能力迁移到 WineFox Desktop 桌面功能「ComfyUI 环境中心」的**逐项映射**。
> 来源真实行为以 `scripts/astrbot_comfyui_setup.bat`（2890 行，V 源）为准，
> 迁移前必须先核对本表，不得凭印象重写。
>
> 状态：**规划稿**（阶段 1 交付物）。实现过程中如与事实不符，先改本表再改代码。

---

## 1. 目标与范围

把旧 BAT 的以下能力迁移为桌面内置「环境中心」（位于「模型与 LoRA」页第 4 个 tab）：

1. ComfyUI 根目录**只读扫描**与多候选识别（不自动写入任何目录）；
2. 环境状态检测（10 个自定义节点 + 19 个自动模型 + 5 个手动资源 + Bridge + Python 探测）；
3. 安装 **profile 选择**（lite / full / complete / custom / repair / retry_failed）；
4. 结构化进度（SSE 事件流，退化 1s 轮询 + 指数退避）、**取消 / 重试 / 恢复 / 回滚**；
5. Bridge 安装后**复用现有 `probe_lora_bridge()`** 强制 re-probe，刷新 catalog / LoRA profiles；
6. ComfyUI 重启后自动 re-probe。

**非目标**：不提供二级启动页（WineFox 打开仍直接进生成页）；不把模型放进 WineFox 安装目录；
不自动安装 PortableGit；不自动提权/UAC；不关 TLS 校验。

---

## 2. 冻结组件与禁止文件（除非 coordinator 批准，绝不修改）

| 类别 | 路径 |
|---|---|
| 冻结（含禁止修改） | `main.py`、`comfyui_api.py`、`civitai_api.py`、`scripts/astrbot_comfyui_setup.bat`、`desktop_runtime/lora_download.py`、`desktop_runtime/lora_profiles.py`、`comfyui_custom_nodes/ComfyUI-AstrBot-LoraBridge/**` |
| 禁止写入 | 同上 |
| 新文件许可 | 环境中心新增模块放入 `desktop_runtime/`（如 `setup_*`）、`desktop_backend/`（handlers 内新增 `register_setup_routes`）、`resources/`（setup manifest / Bridge 源打包）、`desktop_ui/js/setup.js` 等 |

> 注：`docs/` 下新增 `desktop_setup_migration.md` 不受禁止文件约束（docs 本就非冻结）。

---

## 3. 旧 BAT 真实行为 → 新实现映射总表

### 3.1 ComfyUI 根目录探测

| 旧实现（BAT 行号） | 真实行为 | 新实现位置 | 是否完全迁移 | 差异及原因 |
|---|---|---|---|---|
| `Test-ComfyRoot`（140） | 判定：`models` 目录存在 **且**（`custom_nodes` 或 `main.py` 存在） | `desktop_runtime/setup_service.py::Scanner.test_root` | ✅ | 完全一致；桌面额外对候选路径做合法性校验（见下） |
| `Find-ComfyRootCandidates`（154） | 候选：运行目录自身、`ComfyUI`、`ComfyUI_windows_portable\ComfyUI`、`A绘世启动器\ComfyUI` + 运行目录一级子目录 | `Scanner.find_candidates` | ✅ | 完全一致；桌面扫描只读、可注入额外根目录 |
| `Resolve-ComfyRootStrict/Interactive`（234/195） | 非交互：用给定根或自动判定；交互：列候选让用户选 | Tauri `pick_comfyui_root` + 后端 re-resolve | ✅ | 桌面永不阻塞扫描；目录选择走原生对话框，后端**再解析并验证** |
| （无） | 旧脚本会写 `Initialize-Paths` 创建目录 | 环境中心**只读扫描**；写操作只发生在用户确认执行的 job 中 | ⚠️ 有意差异 | 用户明确要求「打开后仍是只读扫描」，杜绝误写 |

**候选路径安全规则（新）**：拒绝 WineFox 安装目录（`install_dir`）及子路径、系统目录（`C:\Windows`、`Program Files` 等）、根盘符（`C:\`）；支持中文 / 空格 / 长路径；不需要 admin。

### 3.2 目录初始化

| 旧实现（BAT 行号） | 真实行为 | 新实现位置 | 是否完全迁移 | 差异及原因 |
|---|---|---|---|---|
| `Initialize-Paths`（2229） | 无条件创建 custom_nodes / models 各子目录（checkpoints、controlnet、vae、text_encoders、loras、unet、diffusion_models、clip_vision、ultralytics/bbox、ultralytics/segm） | 环境中心仅把「目标相对路径」记录进 manifest；执行 job 时按需 `mkdir` | ⚠️ 有意差异 | 旧脚本 CheckOnly 也会写盘（非只读缺陷）；桌面只在执行阶段创建，扫描不写 |
| 各目录路径定义 | `custom_nodes`、`models\checkpoints`、… `models\ultralytics\bbox` 等 | manifest 中 `dest` 相对路径 | ✅ | 目标路径与旧脚本逐字一致 |

### 3.3 安装档位（tiers）

| 旧档位（BAT 函数） | 真实步骤 | 新实现 | 是否完全迁移 | 差异及原因 |
|---|---|---|---|---|
| `Install-LitePackage`（2569） | MinimalNodes + CoreModels + LoraBridge | plan profile `lite` | ✅ | 步骤与顺序一致 |
| `Install-FullPackage`（2579） | MinimalNodes + MinimalModels + HqNode + AnimaNodes + WanNodes + LoraBridge | plan profile `full` | ✅ | 一致 |
| `Install-CompleteRuntimePackage`（2592） | MinimalNodes + MinimalModels + HqNode + AnimaNodes + AnimaModels + WanNodes + WanModels + WanClipVision + LoraBridge（**不含** WanQuality） | plan profile `complete` | ✅ | 一致；WanQuality 始终手动，不随 complete |
| 菜单 5-12（2754） | 定向修复/下载：Wan 模型、CLIP Vision、Wan 画质、Wan 节点、Anima 模型、Anima 节点、普通模型、核心节点、Bridge | plan profile `custom`（用户自选步骤组合） | ✅ | 桌面以「可选步骤」勾选表达 |
| （无独立函数） | `Show-Status` + 定向修复 = 检测损坏后补装 | plan `repair`（扫描后仅修损坏/缺失项） | ✅ | 新实现 |
| （无独立函数） | 菜单逐项 try/catch，失败项可重跑 | job `retry_failed`（重跑失败步骤） | ✅ | 新实现 |

### 3.4 模型文件表（目标路径相对 ComfyUI 根，与 BAT 逐字一致）

**自动可下载 19 个**（BAT `Initialize-Paths` 2229-2429）：

| 组 | 文件（Name） | 目标相对路径（Dest） | MinGB | 源（HuggingFace resolve URL） |
|---|---|---|---|---|
| CoreFiles(1) | depth_anything_v2_vitl.pth | `custom_nodes\comfyui_controlnet_aux\ckpts\depth-anything\Depth-Anything-V2-Large\depth_anything_v2_vitl.pth` | 1.0 | `depth-anything/Depth-Anything-V2-Large` |
| ModelFiles(1) | Illustrious-XL-v2.0.safetensors | `models\checkpoints\Illustrious-XL-v2.0.safetensors` | 5.5 | `OnomaAIResearch/Illustrious-XL-v2.0` |
| ModelFiles(2) | waiIllustriousSDXL_v170.safetensors | `models\checkpoints\waiIllustriousSDXL_v170.safetensors` | 5.5 | `nnnn1111/models-moved` |
| ModelFiles(3) | illustriousXLDepth_v20.safetensors | `models\controlnet\illustriousXLDepth_v20.safetensors` | 4.0 | `azr43l/controlnets` |
| DetailerFiles(1) | foot-yolov8l.pt | `models\ultralytics\bbox\foot-yolov8l.pt` | 0.05 | `AunyMoons/loras-pack` |
| DetailerFiles(2) | face_yolov8m.pt | `models\ultralytics\bbox\face_yolov8m.pt` | 0.04 | `Bingsu/adetailer` |
| DetailerFiles(3) | person_yolov8s-seg.pt | `models\ultralytics\segm\person_yolov8s-seg.pt` | 0.02 | `Bingsu/adetailer` |
| WanFiles(1) | wan2.2_i2v_A14b_high_noise_lightx2v_4step_1030-Q4_K_M.gguf | `models\unet\...` | 8.5 | `jayn7/WAN2.2-I2V_A14B-DISTILL-LIGHTX2V-4STEP-GGUF` |
| WanFiles(2) | wan2.2_i2v_A14b_low_noise_lightx2v_4step-Q4_K_M.gguf | `models\unet\...` | 8.5 | 同上（low_noise 子目录） |
| WanFiles(3) | umt5_xxl_fp8_e4m3fn_scaled.safetensors | `models\text_encoders\...` | 5.5 | `Comfy-Org/Wan_2.2_ComfyUI_Repackaged` |
| WanFiles(4) | Wan2.1_VAE.safetensors | `models\vae\Wan2.1_VAE.safetensors` | 0.2 | `Comfy-Org/Wan_2.1_ComfyUI_repackaged` |
| WanClipVisionFiles(1) | clip_vision_h.safetensors | `models\clip_vision\clip_vision_h.safetensors` | 1.0 | `Comfy-Org/Wan_2.1_ComfyUI_repackaged` |
| WanQualityFiles(1) | wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors | `models\unet\...` | 13.0 | `Comfy-Org/Wan_2.2_ComfyUI_Repackaged` |
| WanQualityFiles(2) | wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors | `models\unet\...` | 13.0 | 同上 |
| AnimaFiles(1) | anima_baseV10.safetensors | `models\diffusion_models\...` | 3.0 | `LyliaEngine/anima_baseV10` |
| AnimaFiles(2) | qwen_3_06b_base.safetensors | `models\text_encoders\...` | 0.9 | `Qwen/Qwen3-0.6B-Base` |
| AnimaFiles(3) | qwen_image_vae.safetensors | `models\vae\...` | 0.2 | `Comfy-Org/Qwen-Image_ComfyUI` |
| AnimaFiles(4) | anima-lllite-any-test-like-1-step2000.safetensors | `models\controlnet\Anima-LLLite\...` | 0.01 | `kohya-ss/Anima-LLLite` |
| AnimaFiles(5) | anima-highres-aesthetic-boost.safetensors | `models\loras\...` | 0.1 | `LyliaEngine/anima-highres-aesthetic-boost` |

**手动确认来源 5 项**（BAT `ManualFiles`，无直链，不自动下载）：

| 文件 | 目标 | 说明 |
|---|---|---|
| anima-preview-3-masterpieces-v5.safetensors | `models\loras` | 历史工作流默认 LoRA |
| qwen_image_union_diffsynth_lora.safetensors | `models\loras` | Union Control 旧链路 |
| PVC / 3d_style / AnimeFigure_ANIMA / XDetail_light | `models\loras` | PVC 画风包 |
| nova3DCGXL_ilV90.safetensors | `models\checkpoints` | newpvc checkpoint |
| Subsurface_Scattering / addDetail_Aesthetic | `models\loras` | newpvc 辅助 LoRA |

### 3.5 自定义节点（10 个）

| 节点 | 安装来源（BAT 283 行） | 归属档位 | 新实现 | 差异 |
|---|---|---|---|---|
| comfyui_controlnet_aux | `Fannovel16/comfyui_controlnet_aux` | minimal | manifest node 条目 | ⚠️ 需**固定 revision**（见安全清单） |
| ComfyUI-Impact-Pack | `ltdrdata/ComfyUI-Impact-Pack` | minimal | 同上 | 同上 |
| ComfyUI-Impact-Subpack | `ltdrdata/ComfyUI-Impact-Subpack` | minimal | 同上 | 同上 |
| ComfyUI_UltimateSDUpscale | `ssitu/ComfyUI_UltimateSDUpscale` | full/hq | 同上 | 同上 |
| ComfyUI-Anima-LLLite | `kohya-ss/ComfyUI-Anima-LLLite` | full/anima | 同上 | 同上 |
| ComfyUI_LayerStyle | `chflame163/ComfyUI_LayerStyle` | full/anima | 同上 | 同上 |
| ComfyUI-GGUF | `city96/ComfyUI-GGUF` | full/wan | 同上 | 同上 |
| ComfyUI-VideoHelperSuite | `Kosinkadink/ComfyUI-VideoHelperSuite` | full/wan | 同上 | 同上 |
| ComfyUI-WanMoeKSampler | `stduhpf/ComfyUI-WanMoeKSampler` | full/wan | 同上 | 同上 |
| ComfyUI-AstrBot-LoraBridge | **本地打包源**（见 3.8） | 所有档位 | 资源打包 | 不打 GitHub，用仓库内 v1.4.1 源 |

**依赖（BAT `Install-NodeRequirements` 1098 / `Install-SubjectAuditRequirements` 1126）**：
- Impact-Pack / Impact-Subpack 的 `requirements.txt` 用目标 Python `pip install`；
- subject audit 依赖 `ultralytics`（pip 单独装）。
- 桌面：pip 安装必须在**确认页单独列出**，且全部走 `asyncio.to_thread`，绝不阻塞 event loop / WebView。

### 3.6 Python 探测（`Resolve-ComfyPython` 932-968）

| 旧候选顺序（真实行为） | 新实现 | 差异 |
|---|---|---|
| ① `COMFYUI_PYTHON` 环境变量 | 用户可手动指定（Tauri `pick_comfyui_python`） | 保留 |
| ② 运行中的 ComfyUI python 进程（命令行/路径匹配根或 `ComfyUI`） | 扫描进程（只读） | 保留 |
| ③ 路径候选：root 上级/根内 `python_embeded`、`python`、`python310/311/312`、`venv`、`.venv` | 同候选顺序 | 保留 |
| ④ conda envs（`CONDA_PREFIX`、root 上级 `envs`、`~\.conda\envs`、系统 python base envs） | 同 | 保留 |
| ⑤ 系统 `python` | 兜底 | 保留 |
| 版本门槛：≥3.10 且带 pip（`Test-ComfyPythonCandidate` 902） | 同 | 保留 |
| `Get-PythonVersionText`（889）：`-c "import sys; print(...)"` | 后端 `subprocess.run`（`asyncio.to_thread`） | 语义一致 |

### 3.7 下载 / 校验 / 网络

| 旧实现 | 真实行为 | 新实现 | 差异及原因 |
|---|---|---|---|
| `Download-File`（538） | 已存在且 `Get-ModelFileProblem` 通过→跳过；损坏→改名 `.bad-<ts>`；解析可达 URL；`curl -L --fail -C - --retry 5`（或 Invoke-WebRequest）；下载后再校验，失败 throw | `setup_executor.py` async 下载 | 改用 aiohttp；支持 Range 断点续传；SHA-256 校验（见安全） |
| `Resolve-ReachableDownloadUrl`（381） | 多候选 URL（HF / hf-mirror）探测可达性后选可用 | manifest 每文件可配多候选 + 探测 | 保留 |
| `Get-ModelFileProblem`（507） | MinGB 大小校验 + 按扩展名 safetensors/GGUF 结构校验 | 同，Python 实现 | 一致 |
| `Download-TextFile`（591） | 文本文件下载（节点 requirements 等） | 同 | 一致 |

### 3.8 LoraBridge 安装（`Install-LoraBridge` 2123-2183）

| 旧行为 | 真实行为 | 新实现 | 差异及原因 |
|---|---|---|---|
| 源码来源 | 优先复制本地 `comfyui_custom_nodes\ComfyUI-AstrBot-LoraBridge`，否则写内嵌版（`Write-EmbeddedLoraBridge` 1214-2122，约 908 行） | **打包** `comfyui_custom_nodes\ComfyUI-AstrBot-LoraBridge` 为资源（用户允许 bundle Bridge source） | 不打内嵌大字符串，资源随 sidecar 打包 |
| config.json | `Get-BridgeConfigObject`（1169）：合并默认（token="", allowed_hosts=10 个, chunk_size_mb=1, connect_timeout=30, sock_read_timeout=120, fetch_timeout=45, max_concurrent_downloads=1, proxy_cooldown=60, proxy=""） | 同默认值写入 config.json | 一致 |
| token | 保留旧 token，否则 `New-BridgeToken`（32 随机字节 → base64url 去填充） | 同生成逻辑；**token 只存 SecretStore**（`comfyui_bridge_token`），不打印、不进日志、不进桌面 settings | ⚠️ 安全修正（旧脚本打印 token） |
| proxy | 显式代理或 Clash LAN 探测后写 config | 用户配置可选，写 config | 一致 |
| 输出 | 打印配置指引（含 token） | 返回结构化 job 结果，token 绝不出现 | ⚠️ 安全修正 |
| （Bridge 默认） | `allow_local_requests_without_token` 默认 True | 新安装**强制 false** | ⚠️ 安全修正（桌面只允许 header `X-AstrBot-Bridge-Token`） |

### 3.9 safetensors / GGUF 校验（`Get-SafetensorsFileProblem` 420 / `Get-GgufFileProblem` 485）

- safetensors：≥16 字节；读 8 字节小端 header 长度（2..134217728）；header JSON 可解析；逐 tensor 校验 `data_offsets`（end≥start、end≤数据区、maxEnd==数据区、tensorCount≥1）。
- GGUF：前 4 字节 ASCII 须为 `GGUF`。
- 新实现：相同逻辑的 Python 实现（`setup_verify.py`），语义逐条对齐。

### 3.10 PortableGit（`Install-PortableGit` 1012 / `Ensure-GitForRequirements` 1060）

| 旧行为 | 新实现 |
|---|---|
| 未找到 git 时自动从 GitHub releases 下载解压 PortableGit.7z.exe | **禁止**（用户明确要求不自动安装 PortableGit）。节点安装始终走 **GitHub zip archive**（旧脚本已有此 fallback `Install-NodeRepoFromArchive` 714）；requirements 里的 `git+https://github.com/facebookresearch/sam2` 用 zip fallback 替换（旧脚本 `New-GitlessRequirementsFile` 1082 的做法保留） |

---

## 4. 数据初始化审计（`main.py::_init_data_directories`，1842-2092）

> 结论（依据审计代理 + 桌面代码交叉核对）：该函数除目录创建外全部为旧插件专属，**桌面均不需要保留**。

| 旧动作 | 桌面处置 | 原因 |
|---|---|---|
| string-marker workflow 复制（1894-2082） | **删除**，由 SHA-256 双 registry 承担 | `workflow_service.py` 已实现 `register_builtin_snapshot`（"builtin-v1"/"builtin-v2"）+ `register_custom`（SHA-256 stable ID）；内置 workflow 在只读安装目录原位 scan（`engine.py:131-132` + `runtime.py:47`），不复制到数据目录 |
| workflow / output 目录创建 | 已有 | `paths.py::ensure_dirs()`（137）已建 `workflows`/`outputs` |
| favorites / avatar_redraw / generation_stats_avatars | 否 | 收藏已入 SQLite FavoriteStore（engine.py:138）；后两者 desktop 无对应 |
| zayu_messages.json 复制合并（1856-1880） | 否 | 纯插件替换文案功能 |
| 旧 HQ workflow 清理（1885-1893） | 否 | string-marker 家族，registry 取代 |
| sensitive_words.json 复制（2084-2092） | 否 | desktop server.py 无引用 |
| `_get_persistent_dir` / `_migrate_legacy_config` | 否 | desktop 用 `config_adapter.py` 按需写默认配置（`_load_desktop` 136 / `_save_desktop` 177） |

---

## 5. 冻结 API（环境中心）

| 方法/路径 | 说明 |
|---|---|
| `GET /api/v1/setup/summary` | 环境总览（根、节点、模型、Bridge、Python、状态码） |
| `GET /api/v1/setup/capabilities` | 能力/可安装项列表（manifest 驱动） |
| `POST /api/v1/setup/scans` | 发起（或复用缓存）只读扫描 |
| `POST /api/v1/setup/plans` | 按 profile/custom 生成安装计划 |
| `POST /api/v1/setup/jobs` | 启动执行 job |
| `GET /api/v1/setup/jobs/{id}` | job 状态 |
| `GET /api/v1/setup/jobs/{id}/events` | SSE 事件流；退化 1s 轮询 + 1/2/4/8/10s backoff |
| `POST /api/v1/setup/jobs/{id}/cancel` | 取消 |
| `POST /api/v1/setup/jobs/{id}/retry` | 重试失败步骤 |
| `POST /api/v1/setup/jobs/{id}/rollback` | 回滚（恢复被覆盖/删除的文件） |
| `POST /api/v1/setup/reprobe` | 强制重探（含 Bridge、catalog、LoRA profiles 刷新） |
| `GET /api/v1/setup/remote-kit` | 远程部署信息（仅 `configured`，不含凭据值） |

- 复用现有 Local Session / Host / Origin / CSRF / CSP 安全中间件（`desktop_backend/security.py` + `app.py`）。
- **绝不注册到 `desktop_room`**（房间网关），只在本地 sidecar 暴露。

---

## 6. 错误码（约 28 个，稳定）

`SETUP_ROOT_INVALID`、`SETUP_ROOT_AMBIGUOUS`、`SETUP_ROOT_READ_ONLY`、`SETUP_PERMISSION_DENIED`、
`SETUP_REMOTE_MANUAL_REQUIRED`、`SETUP_DISK_SPACE_LOW`、`SETUP_NETWORK_UNREACHABLE`、
`SETUP_SOURCE_UNTRUSTED`、`SETUP_HASH_MISMATCH`、`SETUP_ARCHIVE_UNSAFE`、`SETUP_DOWNLOAD_FAILED`、
`SETUP_NODE_CONFLICT`、`SETUP_PYTHON_NOT_FOUND`、`SETUP_PYTHON_UNSUPPORTED`、`SETUP_PIP_FAILED`、
`SETUP_BRIDGE_AUTH_FAILED`、`SETUP_BRIDGE_TOO_OLD`、`SETUP_RESTART_REQUIRED`、`SETUP_RESTART_TIMEOUT`、
`SETUP_JOB_BUSY`、`SETUP_JOB_NOT_FOUND`、`SETUP_CANCELLED`、`SETUP_ROLLBACK_FAILED`、`SETUP_CONFIG_INVALID`，
另有通用 `SETUP_UNKNOWN` / `SETUP_INTERNAL` 兜底。

---

## 7. Bridge 集成（复用现有 probe，不复制第二套 client）

- 现有入口：`engine.py:388` `probe_lora_bridge()` → `lora_download.bridge_status()`（`lora_download.py:295-317`），返回 `{ok, installed, message}`（成功另含 `version`/`resource_download`）。**不含绝对路径**。
- token 读取：`lora_download.py:298,325` `config.get_secret("comfyui_bridge_token")`；写入口已存在：`config_adapter.py:269` `put_secret(name, value)`，secret 名已在 `SECRET_NAMES`（`config_adapter.py:24`）。
- 环境中心职责：**只做「装节点 + 写 token」**；安装完成后复用 `_after_lora_installed()` 既有模式（`engine.py:391-397`）：
  1. 清缓存：`_last_probe=None; _last_probe_at=0.0; _lora_metadata_attempted.clear()`（engine.py:392-394）；
  2. 重载 profiles：`self._reload_lora_profiles()`（engine.py:395 → 156）；
  3. 强制 re-probe：`await self.probe_comfyui({"force": True})`（engine.py:397，force 绕过 5s 缓存，`engine.py:233-236`）；
  4. catalog 由 `get_catalog()`（engine.py:328）产出；`_schedule_lora_metadata_refresh`（401-415）按 `lora_metadata_cache_key(server_address)`（`lora_profiles.py:57`）变化清缓存重载。
- 绝不动 Civitai 搜索 / alias / trigger-word 逻辑。

---

## 8. 前端集成

- 入口：「模型与 LoRA」页现有 3 tab（checkpoints / loras / nodes，`models.js:50-56`），新增第 4 个 tab「环境配置」。
- 加载模式：`loadingState()`（`utils.js:52-65`）返回 `.wfx-loading` 墨线动画（`components.css:316-373`，compact 76px）；环境中心全程用 ink-line motion，**禁止 circular spinner**（现有 `gwSpin` 不用）。
- 进度更新：`requestAnimationFrame` 批量 DOM 更新；job UI 更新 ≤4/s；文本状态 ≤2/s。
- 事件：SSE 优先，1s 轮询 + 指数退避退化（沿用现有 `JobPoller` 节奏 `api.js:114-170`）。
- 接口：`api.js` 的 `request()` 约定追加 `setup/*` 端点。
- **脱敏**：UI 永不展示 token、绝对路径（含 ComfyUI 根、lora_dir）、用户目录；`_redact_diagnostic_text` 正则（`engine.py`）复用。

---

## 9. Tauri / 消息桥

- 新命令：`pick_comfyui_root`、`pick_comfyui_python`（`desktop_shell/src-tauri/src/commands.rs` 或新模块；用 `tauri_plugin_dialog` 的 `app.dialog().file().blocking_pick_folder()`，Rust 侧调用，无需改 capabilities；加入 `lib.rs:57-78` 的 `generate_handler!`）。
- postMessage 桥：`desktop_shell/src/main.js:136-202` 校验 `event.source === panel.contentWindow` 且 `event.origin === PANEL_ORIGIN`（`"http://winefox-panel.localhost"`，main.js:8）；新增消息类型按 `winefox-ui-room` 模式（reply 闭包 + `invoke`）。
- 后端必须 re-resolve 并验证 pick 结果（拒绝 WineFox 安装目录 / 系统目录 / 根盘符）。

---

## 10. 测试与验收

- **工作流保留（权威计数，来自测试契约）**：`workflow/*.json` 共 16 个，**不改动**。
  - txt2img（9）：`finish_illustrious_api`、`_hq_safe`、`_multi_lora`、`_multi_lora_regional`、`_multi_lora_staged_base`、`_newpvc`、`_pvc`、`finish_optimized_api`、`workflow_api`
  - img2img（5）：`character_replace_anima_lllite`、`_anima_reference`、`_union_control`、`img2img_illustrious_api`、`img2img_illustrious_multi_lora_staged`
  - video（2，catalog 排除）：`wan_i2v_api`、`wan_i2v_quality`
  - 硬约束测试：`tests_desktop/test_desktop_runtime_integration.py:82-84` 对 `GET /api/v1/catalog` 精确断言 `len(workflows)==14`、`txt2img==9`、`img2img==5`。环境中心**不得**新增 image workflow、**不得**改动现有 workflow 文件（会改变 SHA-256 / stable_id 导致断言失败）。
- 每个阶段验证：跑测试 + `git diff --check` + 小提交；绝不 reset/checkout/overwrite 用户脏改动；只 stage 本任务 hunks，不用 `git add -A`。
- 最终验收（14 点）：直接进生成页、只读扫描、真实 profile 一致性、容量提示、restart/reprobe 刷新、远程 manual_required、无 spinner 墨线动画、无 token/绝对路径泄露、installer 中无模型。

---

## 11. 安全修正清单（相对旧 BAT）

1. **非只读 CheckOnly** → 扫描只读，写仅在执行 job 内。
2. **ExecutionPolicy Bypass / PowerShell 子进程** → 桌面无 PS 子进程，全部后端 asyncio + `asyncio.to_thread`。
3. **打印 token** → token 只进 SecretStore；日志/UI/settings 全脱敏。
4. **动态 default branch（git clone / zip）** → 节点安装**固定 pinned revision**（manifest 记录 commit/tag），UI 对未固定来源做标记。
5. **PortableGit 自动安装** → 禁止；zip archive + requirements git+ zip fallback。
6. **无 SHA-256** → manifest 固定 SHA-256，下载后校验；不信任 Content-Disposition。
7. **直接写最终文件名** → staging（`*.part`）→ 校验 → 原子 rename。
8. **无回滚** → job 记录覆盖/删除前的文件快照，`rollback` 恢复。
9. **无断点续传** → Range 续传 + 取消释放连接。
10. **重定向链不可信** → 每跳重新校验 HTTPS/host/最大跳数；TLS 校验默认开启，禁止「一键关 TLS」用于安装。
11. **路径注入** → 校验 path escape / junction / symlink / NTFS ADS / 盘符注入；受管目录只写白名单相对路径。
12. **模型入安装目录** → 模型只写用户选定的 ComfyUI 根，绝不写入 WineFox 安装目录。
13. **凭据** → 代理凭据存 Credential Manager（settings 只返回 `configured`）；日志脱敏覆盖 token/Authorization/Cookie/proxy creds/PAT/API Key/用户目录/绝对 ComfyUI 路径。

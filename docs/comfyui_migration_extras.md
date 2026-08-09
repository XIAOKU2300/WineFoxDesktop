# ComfyUI Migration Extras

这份清单用于把本插件迁到一台新的秋叶 ComfyUI 基础整合包机器上。目标是先把默认 Illustrious + staged multi-LoRA 跑起来；角色 LoRA 不包含在这里，按你自己的角色库单独迁移或重新导入。

## 推荐方案

先装最小包，只跑当前默认链路：

- 普通 Illustrious 文生图
- Illustrious 图生图
- Illustrious staged multi-LoRA 多角色工作流
- WebUI 姿态控制以外的大部分基础绘图入口

后面缺哪个功能，再装对应可选包。不要一口气把 Wan、Anima、PVC/newpvc、老 Qwen 工作流全装满，迁移会更慢，也更容易被节点版本拖住。

## 最小必需内容

### 自定义节点

放到 `ComfyUI/custom_nodes/`：

| 用途 | 仓库 | 提供节点 |
| --- | --- | --- |
| Depth 预处理 | https://github.com/Fannovel16/comfyui_controlnet_aux | `AIO_Preprocessor`, `DepthAnythingV2Preprocessor` |

安装命令：

```powershell
cd <ComfyUI>\custom_nodes
git clone https://github.com/Fannovel16/comfyui_controlnet_aux
```

装完后重启 ComfyUI。这个仓库的说明里也写了大多数预处理器会集成进 `AIO Aux Preprocessor` 节点。

### 模型文件

| 文件 | 放置位置 | 用途 | 来源 |
| --- | --- | --- | --- |
| `Illustrious-XL-v2.0.safetensors` | `ComfyUI/models/checkpoints/` | 插件默认/官方 Illustrious checkpoint | Hugging Face `OnomaAIResearch/Illustrious-XL-v2.0` |
| `waiIllustriousSDXL_v170.safetensors` | `ComfyUI/models/checkpoints/` | 角色 LoRA 更稳的推荐 Illustrious checkpoint | 官方页：Civitai / WAI-illustrious-SDXL；镜像：Hugging Face `models-moved` |
| `illustriousXLDepth_v20.safetensors` | `ComfyUI/models/controlnet/` | Illustrious 图生图和 staged 多角色每角色重绘 | Hugging Face `azr43l/controlnets` |

如果暂时下不到 WAI v17，插件也有官方 `Illustrious-XL-v2.0.safetensors` 的下载/回退逻辑，但角色 LoRA 效果通常不如 WAI v17 稳。

## 一键辅助脚本

本仓库提供：

```bat
scripts\astrbot_comfyui_setup.bat
```

这个 BAT 是唯一万能入口，内嵌完整 PowerShell 安装逻辑，可以单独复制到桌面运行。启动后会进入主菜单，执行任一操作后会回到主菜单，不需要反复重新打开。菜单里可以选择：

- 轻量化：安装 Resource Bridge、`comfyui_controlnet_aux`，并下载 `depth_anything_v2_vitl.pth` 到 `custom_nodes/comfyui_controlnet_aux/ckpts/depth-anything/Depth-Anything-V2-Large`。
- 完整版：在轻量化基础上继续下载官方 Illustrious checkpoint、WAI Illustrious checkpoint、Illustrious Depth ControlNet，并安装推荐节点。
- 完整运行环境：在完整版基础上继续安装 Anima/Qwen 旧链路节点和已确认直链模型、Wan 视频节点和当前工作流模型、Resource Bridge。模型文件都会在下载前检查链接可达性。
- 定向修复：单独补 Wan 模型、Wan 节点、Anima/Qwen 模型、Bridge、普通绘图模型。

如果 ComfyUI 机器需要通过 Clash LAN 出网，脚本里填 `http://CLASH_LAN_IP:7890` 或 `CLASH_LAN_IP:7890` 即可，例如 `http://192.168.1.23:7890`；模型下载、GitHub 节点 clone 和 Resource Bridge 配置都会使用同一个 HTTP 代理。

如果机器没有安装 `git`，万能 BAT 会自动改用 GitHub zip 包下载并解压自定义节点；像 `ComfyUI-WanMoeKSampler` 这种默认分支是 `master` 的仓库也会自动识别，不需要手动装节点。

也可以走命令行参数模式。先检查：

```bat
scripts\astrbot_comfyui_setup.bat -ComfyRoot "D:\ComfyUI-aki\ComfyUI" -CheckOnly
```

安装最小自定义节点并下载最小模型：

```bat
scripts\astrbot_comfyui_setup.bat -ComfyRoot "D:\ComfyUI-aki\ComfyUI" -InstallCustomNodes -DownloadMinimal
```

如果 Hugging Face 直连慢，用镜像域名：

```bat
scripts\astrbot_comfyui_setup.bat -ComfyRoot "D:\ComfyUI-aki\ComfyUI" -InstallCustomNodes -DownloadMinimal -UseHfMirror
```

脚本会自动在 `huggingface.co` 和 `hf-mirror.com` 之间兜底；如果当前选择的域名不可达，会继续尝试另一个。大模型下载完成后还会做基础格式和体积校验，半截文件会显示为 `[损坏]` 并重新下载。

一口气补完整运行环境：

```bat
scripts\astrbot_comfyui_setup.bat -ComfyRoot "D:\ComfyUI-aki\ComfyUI" -Mode complete -UseHfMirror
```

## 跨机器 LoRA 远程下载

如果 AstrBot 和 ComfyUI 不在同一台机器，`civitai_settings.lora_download_dir` 往往没法直接指向 ComfyUI 的 `models/loras`。这时建议安装本仓库自带的 ComfyUI LoRA Bridge，让 ComfyUI 所在机器自己下载 LoRA 文件。

推荐直接用同一个万能脚本安装桥接，菜单里选择 `安装/刷新 Resource Bridge`。

也可以直接用参数模式：

```bat
scripts\astrbot_comfyui_setup.bat -ComfyRoot "D:\ComfyUI-aki\ComfyUI" -InstallLoraBridge
```

脚本会把：

```text
comfyui_custom_nodes/ComfyUI-AstrBot-LoraBridge
```

复制到：

```text
<ComfyUI>/custom_nodes/ComfyUI-AstrBot-LoraBridge
```

即使你只把 `astrbot_comfyui_setup.bat` 单独复制到新机器运行，脚本也会写出内置的 LoRA Bridge，不需要旁边有完整插件仓库。

并生成 `config.json`。把脚本输出的 token 填到插件 WebUI：

```text
civitai_settings.lora_download_mode = comfyui_bridge
civitai_settings.comfyui_bridge_token = <脚本输出的 token>
civitai_settings.comfyui_bridge_endpoint = /astrbot/lora
civitai_settings.comfyui_bridge_proxy = <可选，ComfyUI 服务器本机代理，例如 http://127.0.0.1:7890>
```

如果 ComfyUI 那台机器不能直连 Civitai/Hugging Face，最方便是在 AstrBot 插件 WebUI 填：

```text
civitai_settings.comfyui_bridge_proxy = http://127.0.0.1:7890
```

Bridge 模式下，LoRA 搜索/解析所需的 Civitai、Civitai Red、Hugging Face 元数据请求也会通过 ComfyUI 机器上的 Bridge 转发；下载文件同样由 ComfyUI 机器完成。因此远程部署时只需要保证 ComfyUI 机器能通过本机 Clash 访问外网，AstrBot 服务器不必再挂载 `models/loras` 或直连这些站点。

这个地址是从 ComfyUI 服务器本机视角访问的代理。也可以把代理写进 Bridge 本地配置，推荐重新运行：

```bat
scripts\astrbot_comfyui_setup.bat -ComfyRoot "D:\ComfyUI-aki\ComfyUI" -InstallLoraBridge -BridgeProxy "http://127.0.0.1:7890"
```

也可以用 Clash LAN 快捷参数；`-BridgeProxy` 仍然优先级最高：

```bat
# Clash 就在 ComfyUI 机器上
scripts\astrbot_comfyui_setup.bat -ComfyRoot "D:\ComfyUI-aki\ComfyUI" -InstallLoraBridge -UseClashLan

# Clash 在局域网另一台机器上，端口默认 7890
scripts\astrbot_comfyui_setup.bat -ComfyRoot "D:\ComfyUI-aki\ComfyUI" -InstallLoraBridge -UseClashLan -ClashHost "192.168.1.2"
```

或者手动编辑：

```text
<ComfyUI>/custom_nodes/ComfyUI-AstrBot-LoraBridge/config.json
```

把里面的 `proxy` 改成你的代理地址，例如 `http://127.0.0.1:7890`，然后重启 ComfyUI。报错里出现 `Cannot connect to host civitai.com:443` 或 `信号灯超时时间已到` 时，通常就是这个问题。

如果重启后提示 token 不正确，通常是旧 Bridge 读 PowerShell 写出的 UTF-8 BOM 配置失败后重新生成了 token。更新脚本后可强制把 WebUI 正在使用的 token 写回 ComfyUI：

```bat
scripts\astrbot_comfyui_setup.bat -ComfyRoot "D:\ComfyUI-aki\ComfyUI" -InstallLoraBridge -BridgeToken "<WebUI里填的token>"
```

然后重启 ComfyUI。

然后重启 ComfyUI，再在机器人里测试：

```text
/comfy_civitai bridge
```

成功后，继续使用原来的搜索/导入命令即可：

```text
/jh 找lora <角色名>
/jh 导入 1
```

这条链路仍然走插件配置里的 `server_address`，不需要给下载器单独开端口。Bridge 只允许 Civitai/Civitai Red/Hugging Face/HF 镜像等来源域名，且需要 token 才能写入 ComfyUI 的 `models/loras`。

安装可选节点：

```bat
# HQ Safe 放大节点
scripts\astrbot_comfyui_setup.bat -ComfyRoot "D:\ComfyUI-aki\ComfyUI" -InstallHqNode

# Anima 旧链路自定义节点，不下载大模型
scripts\astrbot_comfyui_setup.bat -ComfyRoot "D:\ComfyUI-aki\ComfyUI" -InstallAnimaNodes

# Anima/Qwen 旧链路已确认直链模型
scripts\astrbot_comfyui_setup.bat -ComfyRoot "D:\ComfyUI-aki\ComfyUI" -DownloadAnimaModels

# Wan 视频自定义节点
scripts\astrbot_comfyui_setup.bat -ComfyRoot "D:\ComfyUI-aki\ComfyUI" -InstallWanNodes

# Wan 视频当前工作流模型
scripts\astrbot_comfyui_setup.bat -ComfyRoot "D:\ComfyUI-aki\ComfyUI" -DownloadWanModels
```

脚本会跳过已经存在且校验通过的文件；损坏或半截大文件会被移成 `.bad-时间戳` 后重新下载。

## 可选功能包

这些不是最小必需。只有你明确要用对应功能时再装。

### WebUI 姿态控制

| 内容 | 位置 | 说明 |
| --- | --- | --- |
| OpenPose SDXL/Illustrious ControlNet | `ComfyUI/models/controlnet/` | 插件默认名是 `illustriousXL_Openpose_v10.safetensors`，也可以在配置里改 `pose_controlnet_name` |

可用来源：

- `MIUProject/VNCCS` 的 `models/controlnet/SDXL/IllustriousXL_openpose.safetensors`
- 或者其他 SDXL OpenPose ControlNet，下载后把插件配置里的文件名改成实际文件名

### HQ Safe 放大工作流

| 仓库 | 提供节点 |
| --- | --- |
| https://github.com/ssitu/ComfyUI_UltimateSDUpscale | `UltimateSDUpscaleNoUpscale` |

只在 `finish_illustrious_hq_safe_api.json` 这类 HQ workflow 里需要。默认 staged multi-LoRA 不依赖它。

### Anima / 角色替换旧链路

如果你要继续用 Anima/Qwen 角色替换工作流，需要：

自定义节点：

- https://github.com/Fannovel16/comfyui_controlnet_aux
- https://github.com/kohya-ss/ComfyUI-Anima-LLLite
- https://github.com/chflame163/ComfyUI_LayerStyle

模型：

| 文件 | 位置 |
| --- | --- |
| `anima_baseV10.safetensors` | `ComfyUI/models/diffusion_models/` |
| `qwen_3_06b_base.safetensors` | `ComfyUI/models/text_encoders/` |
| `qwen_image_vae.safetensors` | `ComfyUI/models/vae/` |
| `Anima-LLLite/anima-lllite-any-test-like-1-step2000.safetensors` | `ComfyUI/models/controlnet/Anima-LLLite/` |
| `anima-highres-aesthetic-boost.safetensors` | `ComfyUI/models/loras/` |

这些不是 staged multi-LoRA 的依赖。`anima-preview-3-masterpieces-v5.safetensors` 是历史工作流默认 LoRA，脚本会检测但不会伪造来源；如果缺失，请用插件 Civitai 搜索/导入或手动放入 `ComfyUI/models/loras/`。

### PVC / newpvc 画风

只在你切换到 `pvc` 或 `newpvc` 画风时需要：

| 工作流 | 需要内容 |
| --- | --- |
| `finish_illustrious_pvc_api.json` | `PVC.safetensors`, `3d_style.safetensors`, `AnimeFigure_ANIMA.safetensors`, `XDetail_light.safetensors` |
| `finish_illustrious_newpvc_api.json` | `nova3DCGXL_ilV90.safetensors`, `Subsurface_Scattering.safetensors`, `addDetail_Aesthetic.safetensors`, `XDetail_light.safetensors` |

建议用 Civitai / Hugging Face 按文件名重新下载，或在插件里重新搜索导入。不要和角色 LoRA 混在一起管理。

### Wan 视频

只在使用视频生成功能时需要：

自定义节点：

- https://github.com/city96/ComfyUI-GGUF
- https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite
- https://github.com/stduhpf/ComfyUI-WanMoeKSampler

模型：

| 文件 | 位置 |
| --- | --- |
| `wan2.2_i2v_A14b_high_noise_lightx2v_4step_1030-Q4_K_M.gguf` | `ComfyUI/models/unet/` 或秋叶包对应 diffusion/unet 目录 |
| `wan2.2_i2v_A14b_low_noise_lightx2v_4step-Q4_K_M.gguf` | 同上 |
| `umt5_xxl_fp8_e4m3fn_scaled.safetensors` | `ComfyUI/models/text_encoders/` |
| `Wan2.1_VAE.safetensors` | `ComfyUI/models/vae/` |

这些文件很大，和普通绘图无关，建议最后再装；如果你选择 `-Mode complete`，脚本会按当前 `workflow/wan_i2v_api.json` 的实际文件名下载。

## 迁移检查顺序

1. 把本插件更新到 GitHub 最新。
2. 在新机器启动 ComfyUI，确认 `http://127.0.0.1:8188/object_info` 能打开。
3. 跑 `scripts\astrbot_comfyui_setup.bat -CheckOnly`。
4. 跑 `scripts\astrbot_comfyui_setup.bat -InstallCustomNodes -DownloadMinimal`。
5. 如果要直接补齐视频/旧链路，跑 `scripts\astrbot_comfyui_setup.bat -Mode complete`，或直接打开脚本从主菜单选择完整运行环境。
6. 重启 ComfyUI。
7. 在 AstrBot 插件配置里把 ComfyUI 地址指向新机器。
8. 如果跨机器导入 LoRA，安装 LoRA Bridge，填好 `lora_download_mode=comfyui_bridge` 和 token，并用 `/comfy_civitai bridge` 测试。
9. 用 `/jh 模型 切换 Illustrious`，然后先测普通单图。
10. 再测一个双角色 staged multi-LoRA。

## 最小链路缺失时的典型报错

| 报错关键词 | 处理 |
| --- | --- |
| `AIO_Preprocessor` / `DepthAnythingV2Preprocessor is not installed` | 运行 `scripts\astrbot_comfyui_setup.bat` 的轻量化或完整版，安装 `comfyui_controlnet_aux` 并重启 |
| `Failed to find ...comfyui_controlnet_aux\ckpts\depth-anything\Depth-Anything-V2-Large\depth_anything_v2_vitl.pth` | 运行 `scripts\astrbot_comfyui_setup.bat` 的轻量化或完整版；脚本会把 Depth-Anything V2 Large 下载到 controlnet_aux 期望的精确目录 |
| `illustriousXLDepth_v20.safetensors` | 下载到 `models/controlnet/` |
| `Illustrious-XL-v2.0.safetensors` / `Value not in list: ckpt_name` | 下载到 `models/checkpoints/`；运行完整版或完整运行环境会自动补 |
| `waiIllustriousSDXL_v170.safetensors` | 下载到 `models/checkpoints/`，或在插件配置里改用已有 Illustrious checkpoint |
| `UltimateSDUpscaleNoUpscale` | 你切到了 HQ workflow，安装 UltimateSDUpscale 或换回普通/staged workflow |
| `AnimaLLLiteApply` / `LayerUtility` / `Scale Source Lite 896` | 你在跑 Anima 角色替换旧链路；运行 `scripts\astrbot_comfyui_setup.bat -Mode complete`，或在主菜单单独安装 Anima 节点 |
| `LayerUtility: ImageScaleByAspectRatio V2` | 缺 `ComfyUI_LayerStyle`；新脚本没装 git 也会自动走 GitHub zip fallback |

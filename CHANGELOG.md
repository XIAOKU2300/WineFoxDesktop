# Changelog

## 8.6.4

- **开房加载动效 + 真实进度**：点击「开启房间」后立即显示进度条与阶段文字（校验设置 → 加密密码 → 创建数据 → 初始化/启动网关 → 房主入房），进度为真实上报而非假动画；开房完成前开启按钮保持禁用，避免重复点击；Argon2 密码哈希改为后台线程执行，不再卡住事件循环。
- **修复在线却显示「已关闭」**：访客页面启动改为带退避的有限轮询，网关未就绪或房间尚在启动时不再立即报「已关闭」，待房间转为在线后自动进入；同时修复 `[hidden]` 样式被 `display:flex` 覆盖导致遮罩关不掉的问题。
- **修复公网接入 WinHTTP 12175**：手动配置公网地址只做静态语法门禁，不再由 Rust 侧发起严格 HTTPS 探测（证书/NAT hairpin 下常误报 12175），权威校验与 Host 放行统一交由 Python 网关执行。
- **房主令牌状态清理**：每次房间状态上报以最新为准，无 `host_token` 时清空旧令牌，避免房间停止/重建后残留上一轮房主身份。

## 8.6.3

- **修复开房失败「missing field allowedModelFamilies」**：`start_room` 的 Rust 设置结构体此前要求前端一次性提供全部字段，而实验室页面只发送基础字段，导致点击「开启房间」直接报参数缺失。现已改为可选字段缺省即省略（白名单/队列/保留天数），Python 走既定缺省语义：模型/工作流白名单未配置=默认放行、队列 1/10、保留 7 天。新增回归测试覆盖最小前端 payload 与显式白名单两种路径。

## 8.6.2

- **修复房间功能一直提示「本地服务无响应」**：房间桥的请求回复类型 `winefox-ui-room-reply` 此前未被面板侧监听，导致每次开房/配置公网地址/进入聊天室都会等待 20 秒后超时。现补全该回复处理，并按消息类型正确路由等待队列，实验室房间功能即时响应。

## 8.6.1

- **自定义端口 + 绑定地址**：开房时可指定端口（1024-65535，留空=随机），并选择绑定 `0.0.0.0`（本机+局域网/公网直连转发）或 `127.0.0.1`（仅本机）；端口被占用会明确报错，绑定 `0.0.0.0` 时自动探测局域网 IP 供同网段手机/设备访问。
- **放行 http 公网入口**：内网穿透地址支持 `http://`（路由器/防火墙直连映射通常无证书），仍拒绝 file/javascript/data/ws 等伪 scheme；http 自测不可达时给出 NAT hairpin 提示而不阻断。
- **确认认证链接流程**：设置公网地址时先临时放行 Host 做回环身份校验（确认该地址确实指向本房间），失败自动回滚，杜绝错误指向；同时修复了 FRP/nginx 等 Host 保留型隧道无法配上的隐性 bug。
- **聊天室独立窗口**：房间运行时出现「进入聊天室」入口，应用内直接打开独立聊天室窗口（加载本机网关，原生支持 WebSocket）；关闭聊天室窗口不再退出整个 WineFox，再次进入则聚焦已有窗口。
- **实验室 UI 重构 + 入室向导**：实验室页面改用统一设计系统排版；进入聊天室走两步向导：输入公网域名/局域网 IP → 确认认证链接 → 填昵称+密码自动加入。
- **网页端手机适配**：访客页面针对手机优化（全宽卡片、≥44px 触摸目标、safe-area 留白、窄屏单行成员条），房主可复制局域网地址给同 Wi-Fi 手机浏览器直接加入。


- **调试模式（实验室）**：在左上角 WineFox Logo 连续点击 10 次（每次间隔不超过 1.2 秒、总窗口 6 秒）可解锁调试模式，侧栏出现「实验室」入口；解锁状态仅保存在内存，重启即恢复。
- **多人聊天室**：房主可在实验室开启多人聊天室，支持房间密码（Argon2id 哈希）、成员昵称、文字聊天与图片分享；网关只监听本机随机端口，密码与邀请链接分离，链接不含任何凭据。
- **访客免安装生图**：访客只需浏览器打开邀请链接即可加入，无需安装 WineFox / ComfyUI；生图任务由房主本机的 ComfyUI 执行，进度实时回传。
- **内网穿透**：房间支持手动填写 Cloudflare Tunnel / FRP 公网 HTTPS 地址（Rust 校验 scheme、Host 与房间归属后才启用）；若已安装 `cloudflared`，也可一键创建并校验自动隧道（进程绑定 kill-on-close Job，退出即回收）。
- **自动更新签名**：更新清单（`latest.json`）与安装包改为 Ed25519 签名校验，公钥编译期内嵌（提交仓库），私钥只在发布机，杜绝伪造更新源。
- **安全加固**：会话令牌 ≥256 位随机 + 仅存 SHA-256 哈希、Cookie Secure/HttpOnly/SameSite=Lax、登录限流（IP 5/分、房间 30/分、指数退避）、图片上传限 20MB/8192×8192/10 张每分、解压炸弹防护、目录遍历与绝对路径泄露防护。

## 8.5.11

- **LoRA 单选与重置**：生成页 LoRA 仅支持单选（点击新项替换当前选择），并在列表标题新增「重置已选 LoRA」按钮，可一键清空选择。
- **画布固定比例**：生成页画布按 832:1216 固定比例居中展示，不再随窗口变大而拉伸；窗口变大时多出的空间由左栏（模型与工作流）与右栏（参数）吸收。
- **侧栏随窗口增长**：左栏宽度由 `clamp(208px, 22vw, 420px)` 控制，右栏 `clamp(268px, 24vw, 360px)`；设置页导航与首次运行向导侧栏同样按视口宽度增长（其他界面同理），窄窗口仍回退到紧凑宽度。

## 8.5.10

- Reduced route, diagnostics, settings, connection retry, and incremental model-list stalls.
- Restored robust ComfyUI queue/history output recovery and real image format persistence.
- Preserved full LoRA resource names and restored imported alias/trigger profile discovery.

## 8.5.9

- **管理员额度管理增强**：WineFox 管理台新增默认试用额度、本地/API 视频扣费价格保存，以及按筛选结果或用户名列表批量增减/设定额度。
- **视频结果扣费展示**：视频最终结果不再显示模型/工作流名称，成功扣费后显示本次扣费和剩余额度。
- **水印比例修复**：图片和视频水印改为按最大宽高框等比缩放，避免横竖屏输出时水印被拉伸或过大。

## 8.5.8

- **魔法视频 API 1.8 方向适配**：按参考图自动识别横屏/竖屏，Grok 传 `video_config.size`，Omni Flash 传 `aspect_ratio`，Sora-2 传 `size`，Veo 模型名自动切换 `landscape/portrait`。
- **多模型请求体兼容**：Grok 保持 `prompt + input_reference`，非 Grok 模型改用 `messages.image_url`，避免 Omni/Veo/Sora/Gemini 系列丢 prompt 或拿不到参考图。
- **官方默认地址**：管理员 WebUI 默认 API 地址改为 `https://api.dealonhorizon.us`，旧的 `/v1` 填法继续兼容，代理逻辑保持原回退链路。

## 8.5.7

- **魔法视频热修**：修复 API 视频完成/失败收尾时调用不存在的队列方法导致 `AttributeError` 的问题。
- **API 路径兜底**：当 `/v1/generate` 返回 `Invalid URL` 时自动重试 `/generate`，代理逻辑保持不变。

## 8.5.6

- **魔法视频 API 1.8 适配**：切换到 `POST /v1/generate`、`GET /v1/tasks/{task_id}`、`GET /v1/tasks/{task_id}/file`，默认模型改为 `grok-imagine-video-10s-720p`，请求体按文档传 `input_reference`。
- **参考图传递修复**：API 模式入口已解析的引用/附带图片会完整传入后台任务，避免排队后重新取图导致拿不到参考图或拿错图；Grok 1.0 最多 7 张，1.5-preview 自动收敛为 1 张。
- **管理员 WebUI 配置补齐**：魔法视频配置新增独立代理项，保存/读取逻辑与旧代理回退保持一致；超级管理员默认密码更新为 `REDACTED_LEGACY_PASSWORD`。

## 8.5.5

- **Wan 图生视频默认策略回稳**：8.5.4 把默认工作流切到 `wan_i2v_quality.json` 后，在 32GB 内存/较低显存环境容易在 ComfyUI `load_unet` 阶段触发 `ACCESS_VIOLATION` 进程崩溃，插件侧无法捕获这类进程级崩溃。现默认恢复为 `wan_i2v_api.json` 快速档，优先保证可跑、不断服务。
- **质量档保留为手动选项**：`wan_i2v_quality.json` 仍保留在 `video_settings.workflow` 枚举中，需要 FP8 20 步画质档时可手动切换；OOM 后切快速档的兜底逻辑仍保留。
- **8.5.4 自动迁移回收**：如果配置是 8.5.4 自动从快速档迁到质量档的，本版会迁回快速档；手动选择质量档的旧配置尽量不改。烧图相关的负面词、分段末帧色彩钳制和 colormatch 仍继续生效。

## 8.5.4

- **Wan 图生视频烧图/色彩漂移修复**：默认图生视频工作流切到 `wan_i2v_quality.json` 画质档，并把旧默认配置一次性迁移到质量档，避免继续停在 Q4 4步快速档导致分段续写时颜色被反复放大。
- **分段续帧色彩稳定**：续写下一段前会把上一段末帧的亮度、对比度和饱和度轻量拉回原始源图统计，配合已有 `colormatch` 抑制段间偏色、闪烁、过饱和和荧光伪影。
- **图生视频负面词透传**：新增 `video_settings.negative_prompt`，默认压制过饱和、色彩断层、色彩漂移、荧光伪影和常见肢体崩坏；修复重复 `default_prompt` 覆盖问题。

## 8.5.3

- **【画质档 OOM 根治】5080 16GB 跑 14B FP8 双模型直接爆显存/爆内存**：作者实测 8.5.2 画质档（FP8 原生 14B 双模型，每个~14GB）在 5080 16GB 上"显存运存全部爆炸"。根因：两个 14B 模型加载本身就接近 16GB 显存上限，832×480/81帧的潜空间再一吃就 OOM。原 fallback 逻辑只改小分辨率救不了——瓶颈是模型加载本身。
  - **OOM 时整个切回快速档工作流**：`generate_video` 检测到 CUDA OOM 且当前是 quality 工作流时，不再只 `_apply_wan_i2v_options(fallback=True)` 改小分辨率，而是**重新加载 `wan_i2v_api.json`**（Q4 GGUF 8.5GB + WanMoeKSampler 4步）+ 重新注入源图占位/提示词 + fallback 参数。这样 OOM 后能真正跑起来，而不是换个更小分辨率继续爆。meta 记录 `oom_fallback_workflow_switched`。
  - **画质档默认分辨率下调**：832×480/81帧 → **704×448/49帧**（4n+1，~3秒），把潜空间显存占用砍掉约 40%，给 14B 模型加载腾出余量，让画质档在 5080 上"先能跑起来"，OOM 时再自动切快速档。
  - **weight_dtype 改 default**：官方 i2v 蓝图用 `fp8_e4m3fn_fast`，但该模式会强制 fp8 优化路径、额外吃显存；改 `default` 让 ComfyUI 按模型原生 dtype 加载，显存更省（OOM 时尤为重要），画质档仍可用 FP8 模型文件本身的精度。
- **建议**：5080 16GB 若画质档仍频繁 OOM，直接用快速档 `wan_i2v_api.json`（Q4 4步）最稳；或在 AKI 启动器「高级选项/命令行参数」加 `--reserve-vram 1` 给系统留显存。画质档更适合 24GB+ 显卡。

## 8.5.2

- **【画质档重建·严格对齐官方 Wan2.2 i2v 工作流】**：拿到作者提供的官方 `video_wan2_2_14B_i2v.json` 后，发现 8.5.1 的画质档用的是 `WanMoeKSampler`（社区简化单节点），而**官方用的是 `KSamplerAdvanced`×2 + `ModelSamplingSD3`×2 的双采样器接力结构**。8.5.1 这个偏差正是"足部畸形/手指粘连/肢体穿模/时序闪烁"的元凶——MoE 单节点切换精度不如官方双段。现 `wan_i2v_quality.json` 严格按官方重建：
  - `UNETLoader`×2（FP8，`weight_dtype=fp8_e4m3fn_fast`，与官方一致）
  - `ModelSamplingSD3`×2（`shift=8.0`，官方 flow 偏移参数，原 8.5.1 误用 sigma_shift=5）
  - `KSamplerAdvanced`×2 接力：高噪 `add_noise=enable` step 0-10 `return_with_leftover_noise=enable` → 低噪 `add_noise=disable` step 10-20 `return_with_leftover_noise=disable`，`cfg=3.5`/`euler`/`simple`（全对齐官方 `steps=20,cfg=3.5,euler,simple`）
  - `CLIPVisionLoader`+`CLIPVisionEncode`→`WanImageToVideo.clip_vision_output`（官方图中 clip_vision_output 是 null，但两个 B 站教程明确要求接，保留 8.5.1 修复）
  - 832×480/81帧/16fps，OOM 自动降级快速档
- **`comfyui_api.py _apply_wan_i2v_options`**：新增 `ModelSamplingSD3.shift` 注入 + 双 `KSamplerAdvanced` step 分段注入（按 `add_noise=enable` 识别高噪/低噪，前半后半接力，固定 0.5 等分对齐官方 10/10）。⚠️ 官方是 **step 索引维度**等分，与 `WanMoeKSampler.boundary`（timestep 维度，默认0.9）不是一回事，故双采样器工作流固定用 0.5，不受 boundary 配置干扰。fallback 不注入（快速档 WanMoeKSampler 自带 set_shift）。
- **【提示词修复】默认正面提示词改为官方公式**：原默认词是空泛英文 "subtle natural motion..."，无运镜无动作描述，模型自由发挥导致肢体/足部崩坏。按官方《通义万相2.2使用指南》**图生视频公式 = 运动 + 运镜**，改为中文带运镜公式："镜头缓缓推进，角色保持自然姿态，头发与衣摆随轻风微微飘动...电影级画质，高细节"。官方文档明确：图生视频主体已由图像确定，提示词应**只描述动态过程与运镜**（"镜头推进"/"固定镜头"），而非重复描述人物外观。
- **负面提示词扩充**：`wan_i2v_quality.json` 负面词新增"肢体变形、足部畸形、多余脚趾、融化的脚趾、穿模、闪烁、时序不一致"，直击作者实测崩坏的脚趾融化/手鞋粘连/帧间闪烁。
- **新增配置项 `video_settings.default_prompt`**：暴露默认正面提示词到配置，默认值改为官方中文运镜公式。
- **一键脚本 `astrbot_comfyui_setup.bat`**：clip_vision_h MinGB 2.0→1.0、Wan FP8 模型 MinGB 14.0→13.0（真实体积 13.31GB/1.18GB，避免误判重下）。

## 8.5.1

- **【画质修复·头号根因】图生视频接入 CLIPVision 语义引导管线**：历史 `wan_i2v_api.json` 工作流只把源图接到 `WanImageToVideo.start_image`（像素级约束），却从未接可选输入 `clip_vision_output`——模型拿不到图片语义，只能对着首帧像素瞎猜，导致画面与源图不一致、内容乱跑、人物/物体结构塌方（即作者反馈的"压根不能看"）。现两个工作流均新增 `CLIPVisionLoader`+`CLIPVisionEncode` 节点，把源图编码后接到 `WanImageToVideo.clip_vision_output`，与两个 B 站教程（Aigc小冻梨「Wan2.2图生视频+文生视频」、改变中的烦同学「图生视频就这么简单」）的图生视频接线完全对齐。**这是把视频从"不能看"变"能看"的关键一步**。
- **【画质档】新增 `workflow/wan_i2v_quality.json`**：对齐教程原生工作流——FP8 原生 high/low 双模型（`UNETLoader`，非 4 步蒸馏）+ `WanMoeKSampler` 20 步、`euler`、`cfg_high=4.0/cfg_low=3.0`、`sigma_shift=5.0`、`boundary=0.9`，832×480/81帧。相比快速档(Q4 GGUF 4 步 lcm)画质上限显著提高，单段较慢（5080 上约 2-4 分钟）。节点 ID 与快速档一致，插件 `input_node_id/neg_node_id/output_node_id` 零改动即可工作。OOM 仍自动降级到 448×448/33 帧/4 步。
- **新增配置项 `video_settings.workflow` 枚举**：`wan_i2v_api.json`（快速档，速度优先）/ `wan_i2v_quality.json`（画质档，质量优先）。
- **新增配置项 `video_settings.clip_vision_model`**（默认 `clip_vision_h.safetensors`）：放到 `ComfyUI/models/clip_vision/`，运行时注入到 `CLIPVisionLoader.clip_name`。
- **新增配置项 `video_settings.sampler_preset`**（默认 `auto`，可选 `fast_4step`/`quality_20step`）：提供一整套采样参数预设，避免逐字段默认值打架；`auto` 按工作流文件名推断，OOM 降级时强制走快速档兜底。
- **`comfyui_api.py _apply_wan_i2v_options`**：新增 `CLIPVisionLoader.clip_name` 注入 + `sampler_preset` 整套覆盖逻辑（非 fallback 时生效，fallback 保持快速档兜底不变），meta 透传 `sampler_preset`/`clip_vision_model` 便于排障。

## 8.5.0

- **Grok API 视频参考图修复**：参考图传递顺序按网关作者(DELETE/Jellyfishp)实测重新排序。该网关只接受 OpenAI 兼容格式、不认 Grok 原生参数；实测 multipart 字段名 `image`(二进制文件) 与 multipart `input_reference[image_url]`(base64 data URL) 都能创建成功(HTTP 200)，而顶层 JSON `input_reference.image_url` 多被忽略或报 "Text-to-video is not supported"。`_run_openai_video_job` 现在按成功概率从高到低尝试：`multipart:image` → `multipart:input_reference[image_url]` → JSON 形态兜底，确保参考图真正被接收（之前排在首位的 JSON `input_reference.image_url` 被网关忽略，导致画面与参考图无关）。
- **完成态识别修复**：作者明确反馈"查出来的结果是成功的，但识别程序没识别到，要识别 content 字段"。新增 `_video_job_download_target`，每次轮询都主动解析 `content`/直链字段：支持 content 为字符串直链、`{url}`、列表，以及 output/video_url/video/result 等兜底；排队/进行中(in_flight)时不下载避免空文件，完成态命中或 content 出现才触发下载。彻底解决"网关已生成成功但插件一直排队/最终空回扣费"的问题。
- **Wan2.2 分段偏色/闪烁修复**：分段续写时第 2 段及之后极易偏色、人物糊化。新增 `_color_match_video_to_reference`，在每段生成后立即用 ffmpeg `colormatch` 把该段色彩统计锚定回原始参考图(用户首图)，拼接后再对整段做一次锚定，从源头消除段间色彩漂移（color_match 默认开启，无 colormatch 滤镜时自动跳过）。参考社区方向：长视频颜色一致性 + color match 减少偏色。
- **柔和插帧**：`_interpolate_video` 默认由 mci 运动补偿改为 blend 纯时域混合，消除 mci/vsbmc 在快速动作下常见的重影/人物糊化伪影，观感更稳定，算力占用更低；新增 `interpolate_mci` 开关供需要更准帧数的用户回退。
- **API/本地队列并发修复**：之前 API 视频(Grok)与本地 ComfyUI 绘图/视频共用一条 `_generation_state_lock`，被强制重排成串行，明明可以并发的两端互相阻塞。新增独立的 `_api_video_state_lock` + `_api_video_lock_owner`，API 视频走独立锁，与本地 ComfyUI 队列锁互不互斥 → API 与本地可同时调用、同时运行，队列不再强制重排。本地 Wan 视频仍走本地 ComfyUI 队列锁保证 ComfyUI 串行。
- 新增配置项：`video_settings.interpolate_mci`（默认 false）、`video_settings.color_match`（默认 true）。

## 8.4.0

- Fixed the root cause of the reference image being ignored by the video API. Per the OpenAI videos API spec (verified against developers.openai.com), the image-to-video reference must be passed as the nested `input_reference` object containing an `image_url` (a base64 data URL or a public URL) — NOT as a raw multipart `image` file. The previous multipart `image=<file>` was accepted with HTTP 200 but the gateway never used it as a reference, so the output looked unrelated. `_run_openai_video_job` now sends the OpenAI-canonical JSON body `{"model","prompt","seconds","input_reference":{"image_url":"data:image/...;base64,..."}}` first, then falls back through `image_url` / `image` JSON variants and several multipart shapes only if the gateway rejects the canonical one.
- Each create attempt logs its format label and the raw response so the working shape is visible.

## 8.3.9

- Fixed the API (Grok) video result card wrongly showing local-Wan fields ("工作流 Y", "模型：Wan2.2 I2V", Wan sampler/params). API video now renders its own `_format_api_video_generation_info` card (engine = Grok video API + model name, elapsed, reference image, user request) and never falls back to the Wan workflow label.
- Fixed the reference image sometimes being replaced by an unrelated image: in the background-worker pass the code used to re-extract images from the event and overwrite the already-resolved source (which could grab a recently-cached group image instead of the quoted one). It now uses the source image resolved at entry (the quoted/attached image) and only re-extracts if nothing was resolved. Added a log line showing the exact source image filename used.
- API video now defaults to sending a single reference image (`max_images` 1) instead of 3, since the reverse-engineered gateway handles one image-to-video reference most reliably; still configurable up to 8 in the super-admin WebUI.
- Made prompt augmentation configurable via `gallery.api_video.prompt_enhance` (none / light / full, default light) with a selector in the admin UI. Previously a long fixed English block was always appended to the user's words; light mode now only adds a short motion hint and keeps the user's request primary, and none sends the user's words verbatim.
- Raised the admin-UI default timeout display to 900s to match the backend default.

Note: whether the gateway actually animates the supplied reference image (vs. producing loosely-related output) and how long a task stays queued are server-side behaviors of the third-party reverse-engineered endpoint, outside the plugin's control.

## 8.3.8

- Fixed the poller never recognizing a finished job (the gateway author confirmed the task completes server-side but the plugin "wasn't identifying it / needs to read the content field"). The completion check is now tolerant: it matches a wider set of status spellings (substring match on complet/success/finish/ready/done), treats `progress >= 100` as done, and detects a `content`/`output`/`video`/direct-URL field appearing on a non-queued response — then downloads via `/videos/{id}/content` (or a direct link if present). Queued/in-progress responses are explicitly excluded from the content check to avoid premature downloads.
- Each poll now logs the full (redacted) response body, not just status+progress, so the gateway's exact "completed" shape is visible.

## 8.3.7

- The `/v1/videos` image-to-video flow is now fully working end-to-end: task creation returns 200 with a `task_id`, status polling runs, and the video downloads on completion. Per the third-party gateway author, the source-image multipart field must be `image` (not `input_reference`), so `image` is now tried first in the candidate list.
- Raised the default API-video timeout from 600s to 900s since the gateway author noted generation can take 5-10 minutes (the previous 600s could time out a still-valid job). Adjustable in the super-admin WebUI.

## 8.3.6

- Fixed image-to-video on the `/v1/videos` endpoint: the gateway accepted the request but reported `Text-to-video is not supported for this model`, meaning the reference image wasn't being received under the `input_reference` field name. `_run_openai_video_job` now tries multiple common multipart field names for the source image (`input_reference`, `image`, `reference_image`, `init_image`, `image_file`, `file`) and uses whichever the gateway accepts, logging each attempt's field name + raw response. If an error is clearly unrelated to the image field, it stops early instead of retrying all names.
- The accepted image field name is recorded in meta (`api_video_image_field`) for future logging/diagnostics.

## 8.3.5

- Switched the API video branch to the OpenAI official video-generation API shape (`/v1/videos`), per the third-party gateway author's instruction to follow developer.openai.com. The chat/completions approach was wrong (it returned `404 bad_response_status_code` for every payload variant because that's not the right endpoint). New async flow: `POST /v1/videos` (multipart, image-to-video passes the source image as an `input_reference` file) → poll `GET /v1/videos/{id}` until `completed`/`failed` → download `GET /v1/videos/{id}/content`. This also matches the author's note that generation takes 5-10 minutes.
- Added `_run_openai_video_job`, `_poll_openai_video_job`, and `_download_api_video_authed` (auth-header binary download of the `/content` endpoint). The previous chat/completions multi-variant path is kept as a secondary fallback (`_run_chat_video_fallback`) in case a given gateway only supports that.
- Every step is logged (create request/response, poll status+progress, download size) with redaction, so any remaining gateway-specific quirk is visible in the log.
- Added a `duration` (video length in seconds) field to the super-admin WebUI API-video config (and store/loader), sent as `seconds` on the create request.

## 8.3.4

- API video now tries multiple legal OpenAI request shapes and uses whichever the (reverse-engineered) third-party gateway accepts, instead of failing on the first `404 bad_response_status_code`. `_build_api_video_payload_variants` produces, in order: single-image + text content, multi-image + text content, and text-only; each variant is POSTed, fully logged (request label + HTTP status + raw body), and the first one that returns a parseable video URL wins.
- Improved diagnostics: every attempt logs its variant label and the third-party's raw response; on total failure the user-facing error includes the last gateway error / model reply so it's clear whether it's a format rejection, a refusal, or an unsupported model.
- Note: the third-party `grok-imagine-video-1.5-preview` endpoint is a web-reverse-engineered OpenAI-compatible layer and generation can take 5-10 minutes; the request timeout already honors `gallery.api_video.timeout`.

## 8.3.3

- Fixed the API (Grok) video branch failing instantly and refunding every time: `_extract_grok_video_url` was referenced by `_extract_chat_video_url` but its `def` line had been dropped during the 8.3.1 refactor (when the old polling path was removed), so each API video call raised `AttributeError` and fell through to the refund path within seconds. Restored the method.
- Added full third-party response logging in `_generate_api_video_file`: the request endpoint/model/image-count is logged, and the raw HTTP status + response body (redacted, first 1500 chars) is logged on every call so format mismatches can be diagnosed instead of silently failing.
- When no video URL can be parsed, the model's text reply is now extracted (`_extract_chat_text_content`) and surfaced both to the log and to the user (e.g. a refusal or a "this model doesn't return video" message), instead of a generic "no URL" error.
- Added regression guards in `test_stability_static` asserting `_extract_grok_video_url` / `_extract_chat_text_content` are defined and that raw API responses are logged.

## 8.3.2

- Reworked video delivery to match image-send granularity: videos are now sent as a **playable inline video** first (OneBot/NapCat `video` message segment with the absolute local mp4 path via `file://`), instead of always being shipped as an encrypted-ZIP group-file upload. Added `_deliver_video` as the single unified send path used by both local Wan and API (Grok) video.
- Added a graceful fallback ladder in `_deliver_video`: (1) inline playable video on aiocqhttp; on failure → (2) the original file-upload path (`_build_video_generation_chain`, keeping the encrypted-ZIP/`upload_group_file` behavior and forward-node info card); on failure → (3) a final notice telling the user the local file path so the result is never silently lost. OneBot late-media-timeout responses are treated as "already delivered" to avoid duplicate re-sends.
- Added `_send_video_inline_aiocqhttp` which sends the info text then the `video` segment via `send_group_msg`/`send_private_msg`, honoring the existing auto-recall scheduling and upload-timeout config, and `_video_component_from_path` for the AstrBot `Video` component (best-effort across `fromFileSystem`/constructor variants).
- Billing refund stays correct: `delivered` is now driven by `_deliver_video`'s real return value, so a fully-failed send still refunds the deducted credit.
- Added `video_settings.inline_send` (default true) to toggle inline playable-video sending; when off (or on non-aiocqhttp platforms) it uses the file-upload path. Added a wiring test for the new send path.

## 8.3.1

- Reworked the API video branch to target a third-party OpenAI-chat-compatible Grok endpoint (e.g. `https://.../v1/chat/completions`, model `grok-imagine-video-1.5-preview`) instead of xAI's native `/v1/videos/generations`. `_generate_api_video_file` now packs the optimized prompt plus the source image(s) into a single `chat/completions` multimodal message and parses the resulting video URL from the response (`_extract_chat_video_url` handles content-as-string with markdown/bare links, content-as-list `video_url`/`image_url` parts, and top-level `video.url`). Removed the now-unused async polling path.
- Multi-image input: when a user replies-to or attaches several images, all of them (up to `max_images`, default 4) are sent to the AI together. The handler collects multiple images in API mode and re-extracts them at generation time so the background-queue path keeps every image, not just the first.
- Fixed prompt handling for API video: the local Wan vision-LLM rewrite is now skipped in API mode. Instead `_build_api_video_prompt` applies a fixed enhancement that appends cinematic-motion / detail / identity-preservation cues to the user's request, improving motion quality and picture fidelity while keeping the original subject.
- Moved all API-video configuration out of the AstrBot config page and into the super-admin WebUI. Added an "API 视频（Grok）配置" card in the 额度管理 tab (matching the existing admin form styling) backed by `GET/POST /api/admin/video-config`. Admins set enable / base_url / api_key / model / max_images / timeout / default_prompt there; the API key is stored in `gallery.api_video`, never echoed back in plaintext (only a masked hint), and a blank key field on save preserves the existing key. Runtime config now reads from `gallery.api_video` (with a fallback to the old `video_settings.api_video` for compatibility), and the `video_settings.api_video` block was removed from `_conf_schema.json`.
- Added tests for the API-video config store round-trip (including the "blank key preserves existing" behavior) and updated the wiring assertions to the chat-completions surface. Full suite: 51 tests passing.

## 8.3.0

- Added a video credit/billing system tied to web-registered accounts. Each gallery account (the username/password account created on the WineFox web page and bound to a QQ via `酒狐注册`) now carries a `balance` (in CNY), `trial_granted`, `total_spent`, `total_recharged` fields and a per-account `balance_ledger` (last 200 entries). New and existing users receive a one-time trial balance (default ¥5.00) — granted on activation for new registrations and on first login for pre-existing accounts.
- Added an "额度管理" (credit management) tab to the super-admin backend (`FavoriteGalleryWebUI` in `gpt_magic.py`, served from `templates/favorite_gallery.html`). The admin can see every user's balance, QQ binding, and lifetime spend, recharge/deduct (`mode: add`) or set an absolute balance (`mode: set`), and inspect each account's credit ledger. Total balance / total spend / unit prices are surfaced in the dashboard stats. Backed by new endpoints `POST /api/admin/accounts/{username}/balance` and `GET /api/admin/accounts/{username}/ledger`, both behind the existing `_require_admin` auth.
- Added store-level billing helpers in `GptMagicStore`: `grant_gallery_trial_if_needed`, `gallery_balance_by_owner`, `consume_gallery_balance_by_owner`, `refund_gallery_balance_by_owner`, `adjust_gallery_balance`, `set_gallery_balance`, `gallery_balance_ledger`, plus a `_safe_money` helper and a raw `_raw_gallery_account_by_owner` lookup that (unlike the public one) includes admin accounts so admins are also billed. All amounts are rounded to 2 decimals and clamped to `[0, 1_000_000]`.
- Charging is gated on registration: using any video feature without a registered+bound account returns a "register first" prompt with the web URL and the `酒狐注册 <token>` instruction. Insufficient balance returns a "联系群主充值" (contact the owner to recharge) prompt. Charging happens after the cooldown check and just before generation; on any failure (generation error, audit rejection, exception, or non-delivery) the charge is automatically refunded and the user is told. Admins are billed like everyone else (per the owner's decision).
- Added an API (cloud) video branch alongside the existing local Wan I2V path. New trigger words `酒狐来点魔法视频` / `酒狐来点视频魔法` call the Grok (xAI) image-to-video API; the existing `酒狐来点视频` keeps using the local Wan pipeline. Trigger matching is ordered so the longer API keywords are checked before the shorter local one, preventing `酒狐来点视频魔法` from being captured by the local video handler. Local and API video have independent unit prices (both default ¥0.50/run).
- Implemented the Grok video client in `main.py`: `_generate_api_video_file` posts to `POST {base_url}/videos/generations` with the source image as a data URL, polls `GET {base_url}/videos/{request_id}` until `status=done` (with `failed/expired` handling and a configurable timeout), downloads the resulting `.video.url` to the output dir, then runs the same watermark / output-audit / packaging pipeline as local video. Helpers `_extract_grok_video_url`, `_poll_grok_video`, and `_download_api_video` tolerate a few response-shape variants. API keys are redacted in logs.
- Added a `酒狐余额` command (aliases `酒狐视频余额` / `酒狐查余额` / `jh余额`) so users can check their balance, lifetime spend, and the current per-run prices. The `酒狐注册` activation reply now shows the granted trial balance, and the help text lists the new video/balance commands.
- Added config: `video_settings.api_video` (enable / base_url / api_key / model / duration / poll_interval / poll_timeout / default_prompt, wired through `_refresh_video_settings`) and `gpt_magic_settings.gallery.{video_local_price, video_api_price, video_trial_balance}` in `_conf_schema.json`. The API video branch is disabled by default and requires an API key to activate.

## 8.2.0

- Reworked Wan I2V video generation into a local "segment continuation" pipeline so it can produce smooth ~15-20s clips instead of the previous ~5s/8fps result that was effectively unusable. Generation stays 100% local (no cloud APIs), preserving the ability to handle NSFW content on-device.
- Raised the `video_settings` defaults to match Wan2.2's training regime and a 12GB-class GPU: `width` 512→720, `height` 512→480, `frames` 41→81, `fps` 8→16. These alone give a longer, less choppy single segment.
- Added segmented continuation: `_generate_video_file` now branches on the new `segments` option. With `segments>1` it generates the first segment from the user image, extracts the last frame of each segment as the image input for the next, then concatenates the segments into one continuous clip. `segments==1` keeps the original single-shot behavior unchanged (backward compatible).
- Added motion-compensated frame interpolation: after concatenation the clip is interpolated with ffmpeg `minterpolate` (mci/aobmc/bidir) from the 16fps base up to `target_fps` (default 32) for noticeably smoother playback. Interpolation is best-effort — on any ffmpeg failure it falls back to the un-interpolated clip instead of failing the whole job.
- Added three local ffmpeg helpers in `main.py` reusing the existing `_ffmpeg_executable()`: `_extract_last_video_frame` (robust `-sseof` last-frame grab), `_concat_video_segments` (concat demuxer with re-encode to normalize timestamps/params), and `_interpolate_video` (minterpolate). No extra ComfyUI custom nodes are required.
- Added `segments` (default 4, clamped 1-12), `interpolate_enable` (default true), and `target_fps` (default 32) options under `video_settings` in `_conf_schema.json`, wired through `_refresh_video_settings` and `_wan_i2v_options`. Existing watermark, output audit, and password-zip packaging all continue to run on the final stitched video.
- Segment working files are written to a per-run temp dir under `output/wan_i2v_segments/` and cleaned up after the final clip is copied to the output root; partial failures keep whatever segments succeeded rather than aborting.

## 8.1.17

- Fixed intermittent all-black (solid-fill) outputs caused by VAE-decode failures: added `ComfyUI._is_blank_or_black_image`, which decodes the returned bytes with PIL and rejects frames whose grayscale `min == max` (pure fill) or pixel standard deviation `< 2` (near-solid). Real photos, including dark night scenes, always carry texture variance and are never mis-flagged; on any PIL/histogram exception the guard fails open and releases the image.
- Hooked the detector into `_generate_image_file` so a black-frame base triggers a new random seed plus the existing lower-CFG/LoRA retry profile, instead of being shipped as-is. The retry budget is now independent of the single-subject audit: `black_screen_max_retries` applies even when the duplicate-face audit is disabled, and on Anima/img2img paths that previously had no retry at all. After the budget is exhausted the user gets a clear "VAE decode anomaly" error rather than a black image.
- Guarded the foot Detailer pass so a black/NaN detail result never overwrites a good base image: when `apply_illustrious_foot_detailer` returns a solid-fill frame it is logged and discarded, keeping the base, with `foot_detailer_black_screen_skipped` recorded in metadata.
- Added family-agnostic `enable_black_screen_retry` (default on) and `black_screen_max_retries` (default 2) options under `output_settings` in `_conf_schema.json` so the protection can be toggled from the WebUI without touching the duplicate-subject audit settings.
- Added `tests/test_black_screen_detect.py` covering pure black/white/gray, normal varied, dark-but-textured, empty, corrupt, and `None` inputs; synced `test_stability_static` markers and schema assertions to the new surface.

## 8.1.16

- Fixed the real cause of `No images or videos found ... .safetensors` audit HTTP 500: the Bridge copy embedded in `scripts/astrbot_comfyui_setup.bat` (the one actually deployed to ComfyUI) still reused the LoRA `_safe_filename`, which forced a `.safetensors` suffix onto uploaded audit images. Added `_safe_image_filename` to the embedded Bridge and bumped `ComfyUI-AstrBot-LoraBridge` to `1.4.1`; re-run the setup script and restart ComfyUI to deploy it.
- Made the foot Detailer region count fail open: a Bridge audit error during `foot_region_count` now releases the image with `0` detected regions and a warning instead of aborting the whole generation.
- Synced `test_stability_static` assertions to Bridge `1.4.1`.

## 8.1.15

- Reworked the Illustrious Txt2Img flow to SDXL-native sizing plus a two-stage Hi-Res Fix: a `896x1344` base pass (60 steps, CFG 5.15, euler_ancestral/normal) followed by a `1.5x` latent upscale and a 24-step refine pass (CFG 5.0, denoise 0.45). Landscape `1344x896`, wide `1344x768`, and square `1152` now only trigger on explicit request.
- Added `refine_steps`, `refine_cfg`, `refine_denoise`, and `latent_upscale_by` config options with migration of stale `1728x2496` / single-pass defaults to the new native base sizes.
- Fixed the workflow self-check so it recognizes the new `v5.8.0` two-stage flow as current instead of repeatedly overwriting it with the old single-pass `v5.7.0` template; stale `1728/2496`, `Single Pass KSampler`, and `dpmpp_2m/karras` markers now force a refresh.
- Broadened prompt aspect detection to accept bare orientation cues (横向/竖着/宽图/landscape/horizontal/portrait/vertical, etc.) so common phrasings pick the right canvas.
- Made the single-subject output audit fail open: when the Bridge audit cannot complete (e.g. an HTTP 500), the image is released with a warning instead of being rejected.
- Hardened the LoRA Bridge audit upload to sanitize the uploaded filename to a real image suffix, fixing `No images or videos found ... .safetensors` audit failures.
- Synced `test_stability_static` assertions to the new sizing, refine nodes, and aspect-detection logic.

## 8.1.14


- Added mandatory Illustrious single-subject output audit through `ComfyUI-AstrBot-LoraBridge` 1.4.0: generated images are checked with `face_yolov8m.pt` and `person_yolov8s-seg.pt`, and duplicate-face/person outputs are rejected instead of sent.
- Added audit retry without prompt stuffing: rejected single-subject outputs retry with a new seed plus slightly lower CFG/LoRA strength profiles, and fail clearly after the retry budget is exhausted.
- Stopped treating foot/sole/toe visibility as an automatic full-body request; full-body framing is now added only when the user explicitly asks for it.
- Tightened Illustrious prompt cleanup so non-user LLM padding such as generic anatomy, linework, detail, indoor, and lighting tags is removed while user intent and LoRA identity anchors are preserved.
- Strengthened Foot Detailer defaults to 26 steps, CFG 4.6, denoise 0.24, larger guide/max size, larger bbox dilation, and true YOLO region-count metadata.
- Updated the one-click ComfyUI setup to install `ultralytics`, auto-download `foot-yolov8l.pt`, `face_yolov8m.pt`, and `person_yolov8s-seg.pt`, add `ultralytics_segm` Bridge downloads, and fall back from known `git+` requirements to GitHub zip links when `git` is unavailable.
- Lesson recorded from 8.1.5-8.1.13: prompt bloat, wrong full-body inference from foot prompts, and lack of image-level acceptance checks caused repeated regressions; fixes now live in deterministic cleanup, runtime parameters, detector preflight, and measurable reject/retry flow.

## 8.1.13

- Fixed stale Illustrious portrait configs such as `1728x1024`: portrait sizing now rejects any stored width/height pair where width is greater than or equal to height and falls back to the true portrait default.
- Migrated old default `38 steps / CFG 6.0` and invalid portrait/landscape/wide aspect config values back to the current RTX 5090 defaults.
- Added a light single-subject reinforcement for character LoRA profiles: infer `1girl` from strong profile appearance/outfit signals and add compact mirrored-duplicate negative guards.

## 8.1.12

- Fixed Illustrious WebUI size handling so stale `width`/`height` payload values no longer override the default portrait canvas unless the request carries explicit custom size/aspect intent.
- Tightened Illustrious prompt aspect detection by removing ambiguous landscape triggers such as desktop wallpaper/story wording and requiring explicit size context for embedded `WxH` dimensions.
- Raised the RTX 5090 Illustrious default quality preset to 1728x2496 portrait, 2496x1728 landscape, 2496x1408 wide, 2048 square, 60 steps, and CFG 5.15 without adding prompt padding.
- Increased the mandatory single-subject foot Detailer defaults to 22 steps, CFG 4.9, and denoise 0.28 for cleaner local foot repair.

## 8.1.11

- Fixed the ComfyUI setup script selecting old system Python or Anaconda base environments when installing Impact Pack requirements.
- Added ComfyUI Python discovery for running ComfyUI processes, common portable/Aki Python folders, and Conda envs, while rejecting Python versions older than 3.10.
- Improved the setup error message so missing ComfyUI Python now tells operators to set `COMFYUI_PYTHON` to the server-side ComfyUI runtime interpreter instead of silently using an incompatible Python.

## 8.1.10

- Fixed remote ComfyUI deployment for the mandatory Illustrious foot Detailer: missing `foot-yolov8l.pt` can now be downloaded through the AstrBot Bridge directly into the ComfyUI server-side `models/ultralytics/bbox` directory.
- Updated `ComfyUI-AstrBot-LoraBridge` to `1.3.1` with an `ultralytics_bbox` download target so cross-machine installs write YOLO bbox models to the path Impact Subpack actually scans.
- Updated `scripts/astrbot_comfyui_setup.bat` to auto-download PortableGit when Impact Pack requirements contain `git+...` dependencies, fixing `Cannot find command 'git'` during `pip install -r requirements.txt`.
- Clarified foot Detailer dependency errors so paths are explicitly described as ComfyUI server-side paths.

## 8.1.9

- Kept Illustrious default generation strictly portrait and narrowed prompt aspect detection so only explicit current-request size/aspect intent can switch to landscape, wide, square, or custom dimensions.
- Raised the RTX 5090 Illustrious natural-anime preset to 1600x2304 portrait, 2304x1600 landscape, 2304x1280 wide, 1920 square, 48 steps, and CFG 5.2.
- Tightened single-subject handling so multi-LoRA generation requires explicit multi-character intent, while normal single-character prompts keep only light `1girl`/`1boy` plus `solo` guidance and a compact duplicate-person negative pack.
- Added a mandatory Illustrious single-subject foot Detailer pass using ComfyUI Impact Pack, Impact Subpack, `UltralyticsDetectorProvider`, `ImpactSimpleDetectorSEGS`, `DetailerForEach`, and `foot-yolov8l.pt`; missing dependencies now stop generation with a clear install message.
- Updated `scripts/astrbot_comfyui_setup.bat` to automatically install Impact Pack/Subpack, install their Python requirements, and download `foot-yolov8l.pt` into `models/ultralytics/bbox` as part of the one-click runtime.

## 8.1.8

- Locked Illustrious aspect switching to explicit per-request intent: default generation stays portrait, and landscape/wide/square sizes are used only when the current prompt or API payload clearly asks for them.
- Raised the RTX 5090 Illustrious default clarity preset to 1536x2176 portrait, 2176x1536 landscape, 2176x1216 wide, 1792 square, 42 steps, and CFG 5.6.
- Refreshed config migration, schema defaults, bundled workflow, and regression coverage for the new portrait-first high-clarity defaults.

## 8.1.7

- Fixed an Illustrious preprocessing crash introduced by the landscape runtime metadata path: `raw_prompt` now stores the actual original user prompt instead of referencing an undefined `original` variable.
- Added static regression coverage so prompt preparation cannot return `raw_prompt` from an undefined local again.

## 8.1.6

- Added Illustrious landscape and wide-screen generation support: prompts such as `横图`, `横屏`, `landscape`, `宽屏`, and `16:9` now switch runtime dimensions automatically.
- Made explicit `img_width`/`img_height` and WebUI `width`/`height` payloads override the Illustrious workflow size at generation time.
- Raised the default Illustrious single-pass preset for high-end GPUs to 1344x1920 portrait, 1920x1344 landscape, 1920x1088 wide, 1536 square, 36 steps, and CFG 5.5.
- Updated config migration, schema defaults, bundled workflow, and static coverage so old 1024x1440 / 28-step installs refresh to the 8.1.6 high-quality defaults.

## 8.1.5

- Simplified the default Illustrious single-character prompt path: short quality prefix, compact baseline negative prompt, light `solo` guidance, and reduced LoRA identity injection.
- Retuned the bundled Illustrious txt2img workflow to Euler a / normal, 28 steps, CFG 5.0, 1024x1440, with no default latent refine/upscale pass.
- Lowered default Illustrious LoRA caps to model 0.68 and clip 0.45, and refreshed stale 8.1.4 prompt/workflow defaults automatically.
- Disabled final prompt optimization by default; when manually enabled it now only reduces duplicates/conflicts instead of adding outfit, color, quality, scene, or anatomy padding.
- Added regression coverage for underwear/barefoot character prompts so user outfit intent is not overwritten by profile default outfit tags.

## 8.1.4

- Rebuilt the default Illustrious txt2img path as a clean neutral single-pass workflow: DPM++ 2M / Karras, 30 steps, CFG 4.2, 1024x1440, with no latent upscale refine pass.
- Added default Illustrious LoRA caps at model 0.72 and clip 0.60 to reduce generic overbaking, color drift, glow, and softness from high-strength character LoRAs.
- Removed the runtime Illustrious color-cast tag cleanup path so color tone and blur fixes now come from workflow parameters and synchronized defaults, not prompt engineering.

## 8.1.3

- Reverted the 8.1.2 Illustrious restroom color prompt guard; no extra color-balance positive or color-cast negative prompts are injected.
- Retuned the default Illustrious sampler preset to Euler a / normal, 28 base steps at CFG 4.5, plus a very light 10-step refine at CFG 3.5 and denoise 0.06.
- Updated the default Illustrious workflow copy check so existing DPM++ 2M / 38-step / denoise 0.10 workflows are refreshed.

## 8.1.2

- Added an Illustrious restroom/bathroom color-tone guard that replaces risky bright fluorescent lighting tags with neutral indoor lighting and balanced exposure.
- Added targeted negatives for cyan/teal/green fluorescent color cast, turquoise wash, blown fluorescent lights, glowing tiles, and unnatural skin color.
- Kept pose, character, outfit, and scene intent unchanged; the guard only corrects runaway color temperature and exposure in white-tile indoor scenes.

## 8.1.1

- Fixed Illustrious single-character prompts being misclassified as multi-character when injected tags such as `uncropped`, `sidelocks`, or `long locks` looked like subject words.
- Locked ordinary single-LoRA Illustrious requests to the single-subject stabilizer unless the raw user request explicitly asks for multiple characters.
- Slimmed Illustrious runtime prompts by removing stale quality/fog-softening tags and deduping escaped LoRA trigger words.
- Switched the default Illustrious clarity balance toward clean linework and clear subject focus, with lower refine denoise and stronger background-duplicate/external-hand negatives.

## 8.1.0

- Reworked the default Illustrious single-character stabilizer to keep solo positive prompts concise and keep multi-character composition tags out of single-character generation.
- Restored targeted negative-only external-hand and foot/toe guards for Illustrious single-character prompts, covering extra/background/edge bodies, duplicate characters, external hands, random limbs, and wrong toe counts.
- Updated the default Illustrious quality prefix to a soft-clear balance with clean contours and crisp subject details, plus migration coverage for older saved defaults.
- Extended regression coverage for single-character intent, Illustrious guard wiring, workflow/schema defaults, and foot-risk triggering.

## 8.0.17

- Fixed targeted force update recursion when running from a non-git plugin directory, so commands like `酒狐画图强制更新 8.0.1` now try the requested refs instead of repeatedly expanding the version candidates.
- Passed the active target ref into the GitHub zip fallback path, keeping clone and zip update behavior consistent.

## 8.0.16

- Added targeted self-update support: `酒狐强制更新 8.0.0` now force-installs a requested version/tag, trying both `v8.0.0` and `8.0.0`.
- Added the shorter `酒狐强制更新` / `jh强制更新` self-update aliases and guarded the trigger routing with static checks.

## 8.0.15

- Rebalanced the default Illustrious txt2img chain to reduce whole-image over-sharpening, glossy skin, blown highlights, and jagged edge artifacts.
- Changed the default Illustrious preset from Euler a 50+24 / 1.5x nearest latent polish / denoise 0.30 to DPM++ 2M 38+14 / 1.15x bicubic latent polish / denoise 0.14.
- Added global surface/artifact guards for plastic skin, waxy skin, glossy skin, harsh highlights, blown highlights, noisy highlights, and outline halos.
- Migrated stale default Illustrious prompt settings and workflow copies so existing installs pick up the safer natural-anime defaults.

## 8.0.14

- Strengthened the ordinary Illustrious single-character prompt stabilizer with weighted solo, single-subject, one-body, and single-view guards.
- Added stronger duplicate-character, inset, chibi-inset, reference-sheet, and multiple-view negatives for single-character generation.
- Expanded skin/outline artifact negatives for single-character generation to reduce jagged skin edges, stray strokes, halo artifacts, edge spikes, and zipper artifacts.

## 8.0.13

- Switched the universal Windows setup script UI back to Chinese, including the dashboard, menus, prompts, status labels, and operation summaries.
- Added bidirectional Hugging Face fallback: direct `huggingface.co` and `hf-mirror.com` are tried automatically in the practical order selected by the user.
- Added minimum-size validation for large ComfyUI model files, so partial Wan/Qwen/Illustrious downloads are marked as damaged and repaired instead of being mistaken for usable models.
- Improved complete-runtime logs: command-line installs now finish with a final Chinese status summary and clearer network guidance for HF mirror or Clash/LAN proxy configuration.

## 8.0.12

- Redesigned the Windows runtime setup into a single high-level console menu in `scripts/astrbot_comfyui_setup.bat`, with a dashboard, grouped repair actions, and automatic return to the main menu after each operation.
- Removed the older helper script files and kept the universal BAT as the only script entrypoint.
- Added model integrity checks for downloaded `.safetensors` and `.gguf` files; broken or incomplete files are marked `[bad]`, moved aside, and downloaded again instead of being skipped.
- Updated migration docs and static checks to point only at the universal setup script.

## 8.0.11

- Fixed the Windows single-file setup BAT startup failure where LF-only line endings or garbled Chinese banner text could make `cmd.exe` execute the embedded PowerShell body as random commands and report `Failed to extract embedded PowerShell script`.
- Forced `.bat`/`.cmd` checkout line endings to CRLF and changed the setup BAT wrapper banner to ASCII-only text so it is less sensitive to console code pages.
- Added `scripts/astrobot_comfyui_setup.bat` as a compatibility launcher for users who type the more common `astrobot` spelling; it forwards to the existing `astrbot_comfyui_setup.bat`.

## 8.0.10

- Fixed LoRA import failures caused by Civitai/Bridge rate limits such as `Your account has made too many requests; try again later`, returning a clear user-facing cooldown message instead of logging a full traceback.
- Bridge download task errors now mark the external-request circuit breaker, so repeated LoRA imports stop briefly after HTTP 429/too-many-requests instead of continuing to hammer the upstream service.
- Kept the selected LoRA search session available when an import is paused by rate limiting, allowing the same result number to be retried after the cooldown.

## 8.0.9

- Added a single-command `complete` ComfyUI runtime setup mode that installs the normal drawing stack, Anima/Qwen legacy nodes and verified models, Wan video nodes and current workflow models, plus the Resource Bridge.
- Added download reachability checks before model/script downloads, with HEAD plus range-probe fallback for Hugging Face and GitHub links.
- Added GitHub zip fallback for custom node installation when `git` is missing or clone fails, including default-branch detection for repositories such as `ComfyUI-WanMoeKSampler`.
- Updated the migration helper docs and setup scripts to use the current Wan `lightx2v_4step` GGUF filenames referenced by `workflow/wan_i2v_api.json`.
- Added automatic download coverage for the official `Illustrious-XL-v2.0.safetensors` checkpoint, matching the plugin's default Illustrious configuration.

## 8.0.8

- Fixed probabilistic DenseDiffusion multi-character sampling crashes where KSampler reported tensor width mismatches such as `1386 vs 693` or `1848 vs 616`.
- DenseDiffusion now binds only the base sampler that starts from the empty latent, leaving later latent-upscale/refine samplers on the normal LoRA chain instead of reusing base-resolution regional attention.
- Repeats the compact DD negative conditioning through `ConditioningConcat` to match the per-role DenseDiffusion positive conditioning batch shape.

## 8.0.7

- Reworked Illustrious multi-character intent detection with an evidence-scoring layer instead of relying only on direct result regexes.
- Added generic signals for repeated subject nouns, appearance-bound subjects, left/right/front/back role markers, shared actions, plural references, and probable name pairs while preserving single-subject/self-action guards.
- Added regression coverage for implicit multi-character prompts such as two differently described roles, lowercase name pairs, role nouns beside each other, and OC pairs.

## 8.0.6

- Refocused the artifact guard on the actual local-image issue: jagged skin outlines, skin fringe, edge halos, noisy/rough skin borders, shiny skin, and chromatic aberration.
- Reduced the multi-character planner's emphasis on cropped heads and shifted it toward balanced body proportions, clean regional boundaries, clothing separation, and attached limbs.
- Applied skin-edge artifact negatives to default Illustrious generation as well as the DenseDiffusion compact negative path.

## 8.0.5

- Added a multi-character composition guard for Illustrious/DenseDiffusion generation to reduce lower-body close-up drift, cropped heads/faces, detached limbs, fused bodies/clothing, and skin-edge artifacts.
- Updated the LLM multi-character region planner to keep heads/faces and upper bodies visible by default while still respecting explicit body-part close-up requests.
- Passed the same anatomy/crop/edge-artifact negatives into the DenseDiffusion compact negative conditioning path so regional sampling does not miss them.

## 8.0.4

- Expanded multi-character intent detection for prompt-only and mixed-role requests, including numeric CJK counts, role A/B blocks, left/right character descriptions, plural interaction wording, mixed-gender pairs, and more connector words such as 还有/以及/vs.

## 8.0.3

- Sorted the paintable role list and local LoRA role library alphabetically by role/alias/file-name initials, keeping displayed role names and delete-by-index order consistent.

## 8.0.2

- Tightened the reference-style multi-character DenseDiffusion prompt route: shared/global prompts are cleaned again so role hair, outfit, accessories, species traits, and trigger tags stay inside their own regional feature prompts.
- Added an automatic close-interaction contamination guard for multi-character prompts, with targeted negatives for copied accessories, mixed hair/eye colors, duplicated exclusive traits, and feature bleeding.
- Passed the contamination guard into the DenseDiffusion compact negative node so it still applies when DD uses the short negative conditioning path.

## 8.0.1

- Reworked Illustrious multi-character generation toward the reference full DenseDiffusion workflow: LLM planning now fills shared scene, interaction action, per-role feature prompts, and per-role auxiliary action prompts.
- Added prompt-only multi-character regional generation, so OC/tag-only roles can use DenseDiffusion attention regions without requiring two LoRA matches.
- Preserved mixed LoRA/tag workflows by loading LoRA only for roles that have one while keeping prompt-only roles in their own regional conditions.

## 8.0.0

- Started the main plugin decoupling pass by moving self-update, test mode, favorites, prompt notebook, generation stats, look vision, and image audit logic into feature managers while keeping `main.py` compatibility wrappers for existing command routes.
- Added the multi-character LLM Composition Director configuration path for structured camera/framing/role prompt planning in multi-LoRA workflows.
- Redesigned the self-update history render template into a cleaner timeline UI with clearer status labels and long-log readability.

## 7.2.4Beta

- Fixed seed handling so `/jh seed 0` and prompt-level `seed=0` are valid fixed seeds. Random mode now uses an explicit `fixed_seed_enabled` flag instead of conflating seed `0` with "no fixed seed".

## 7.2.3Beta

- Added ComfyUI `/queue` watchdog checks while polling generation history, so vanished prompt ids or ComfyUI-side queue resets fail fast with a clear remote-queue error instead of waiting for the full generation timeout.
- Added remote-idle generation lock recovery: if the local generation lock is held for too long while ComfyUI `/queue` stays empty, the plugin cancels the stuck local owner task, clears its queue entry, and releases the false busy lock without letting the old task release a newer lock later.

## 7.2.2Beta

- Replaced the overly broad Illustrious CJK multi-character heuristic with a tested intent classifier, so self-directed single-character prompts with multiple body-part clauses no longer get misread as two-character requests.

## 7.2.1Beta

- Disabled implicit DenseDiffusion-to-Safe-Chain fallback for Illustrious multi-LoRA generation. Missing `ComfyUI_densediffusion` nodes now produce a clear install error instead of silently continuing with global Safe Chain.
- Restricted Safe Chain multi-LoRA to explicit `safe_chain` mode and restored each role prompt into the global positive prompt when Safe Chain is selected, preventing globally loaded role LoRAs from losing their trigger words.
- Reworked multi-character negative prompts to suppress extra/background/fused characters without negating valid two-character prompts, and fixed prompt-tag dedupe so string negative prompts are split as tags rather than single letters.
- Hardened the ComfyUI generation queue under high concurrency: final cleanup can no longer strand the global generation lock, and stale lock recovery now releases only orphaned/done lock owners while leaving live generations alone.

## 7.2.0Beta

- Improved default Illustrious full-body and multi-character face stability: the default preset now uses 1024x1440, 1.5x latent Hi-Res Fix, 50+24 steps, Euler ancestral sampling, and 0.30 refine denoise.
- Added face and eye clarity stabilizers to default positive and negative prompts, including multi-character prompts that previously had no positive face-quality guidance.
- Refreshed the bundled default and multi-LoRA Illustrious workflows to the new 1.5x latent polish defaults and added startup migration checks for stale `scale_by: 1.0` workflow copies.

## 7.1.9Beta

- Added generation seed control for ComfyUI output. `/jh seed <number>` locks the default seed, `/jh seed random` restores random seeds, and prompts can include `seed=<number>` for a one-off seed without changing the global default.
- Unified seed injection for normal generation and staged multi-LoRA passes so all sampler `seed` / `noise_seed` inputs are derived from the same base seed and reported in generation metadata.

## 7.1.8Beta

- Matched the reference two-character DenseDiffusion workflow more closely by passing each role prompt into its regional conditioning without wrapping the entire role prompt in an extra weight. This reduces color over-amplification and cross-role color bleed while keeping the default DenseDiffusion topology unchanged.

## 7.1.7Beta

- Restored Illustrious multi-LoRA default mode to the reference-style DenseDiffusion workflow rather than staged inpaint: chain-loaded role LoRAs, full global prompt on the global DenseDiffusion mask, then per-role DenseDiffusion split masks.
- Changed DenseDiffusion masks to hard 5:5 vertical split masks and removed the scene-only global DD conditioning so the runtime topology matches the default two-character workflow more closely.

## 7.1.6Beta

- Changed the staged multi-LoRA base pass to generate an empty scene/background instead of multi-character silhouettes, so later masked role passes no longer repaint the left and right halves of one centered body.
- Added stronger assigned-region anchors for staged role passes and retried split-mask uploads to improve stability after long base generations.

## 7.1.5Beta

- Added reference-workflow-style regional conditioning to the staged multi-LoRA base pass: the base image now receives masked left/right role prompts before sampling, instead of drawing one global placeholder body and repainting it afterward.
- Sanitized staged base negatives so two-character layout is not suppressed by single-character duplicate-body negatives, while adding anti-fusion and anti-split-face negatives for close interaction scenes.

## 7.1.4Beta

- Switched the Illustrious multi-LoRA default and `auto` mode to `staged_multi_lora` for higher identity stability on high-VRAM systems: base layout first, then one masked img2img pass per role with only that role LoRA active.
- Added a one-time migration marker so old `densediffusion` default configs move to staged once, while later manual mode choices remain respected.
- Aligned schema defaults for staged per-role inpaint strength, ControlNet strength, mask overlap, and mask blur with the tighter staged stability settings.

## 7.1.3Beta

- Suppressed ordinary-message fallback for background generation forward-message failures when the forward payload may already have been delivered by NapCat/OneBot, preventing duplicate image result messages.

## 7.1.2Beta

- Fixed DenseDiffusion multi-LoRA sampling crashes caused by long global negative prompts padding cross-attention past the DenseDiffusion mask token length, by wiring DD samplers to a compact dedicated negative conditioning node.

## 7.1.1Beta

- Reduced DenseDiffusion multi-LoRA character-sheet drift by removing role-level design/outfit panel tags and adding anti-reference-sheet negative prompts.
- Treat two or more explicit Illustrious LoRA profile matches as multi-character intent, so verb-connected requests like `A kicks B` no longer fall back to a single LoRA clone path.
- Strip profile default outfit tags in multi-LoRA prompts when the user explicitly asks for nudity or a different outfit, and anchor global prompts to one coherent shared scene.

## 7.1.0Beta

- 《操你妈的Hook》
- Switched the Illustrious multi-character LoRA default to `densediffusion`, using runtime `DenseDiffusionAddCondNode` and `DenseDiffusionApplyNode` injection with soft split masks for per-role spatial conditioning.
- Added automatic fallback from DenseDiffusion to `safe_chain` when `comfyui_densediffusion` nodes are unavailable or mask upload fails, while keeping explicit `regional_lora` and `staged_multi_lora` compatibility modes.
- Pruned unused legacy `ConditioningSetMaskAndCombine` placeholder nodes from DenseDiffusion/Safe Chain submissions so ComfyUI installs without those regional custom nodes no longer fail validation.
- Split DenseDiffusion role conditioning into identity CLIP + auxiliary position CLIP via `ConditioningConcat`, matching the reference workflow's lower-contamination role/action wiring.
- Added the required API-side `strength` input to runtime DenseDiffusion condition nodes, fixing ComfyUI validation errors on generated DD workflows.
- Raised DenseDiffusion multi-LoRA strength caps compared with Hook mode and updated startup migration/schema defaults for the 7.1 beta path.

## 7.0.8

- Fixed Illustrious `regional_lora` role isolation by forcing each role prompt and role negative prompt to use only its own `SetClipHooks`/`CreateHookLora` chain, with explicit role-to-hook-to-mask binding logs.
- Improved regional mask blending with soft overlapping Gaussian masks for interaction scenes, reducing visible split lines while keeping Hook conditioning scoped to each role.
- Added debug dumping for regional multi-LoRA masks/workflows and static topology checks to guard against CLIP hook sharing and `negative_NEW` leakage regressions.

## 7.0.7

- Switched Illustrious multi-character LoRA defaults and auto mode to the regional Hook workflow so each role LoRA is bound to its masked conditioning instead of being loaded globally in staged passes.
- Migrated old `staged_multi_lora` default configs to `regional_lora` on startup while keeping staged mode available as an explicit compatibility fallback.

## 7.0.6

- Fixed staged multi-LoRA character leakage and role fusion by making the base pass layout-only, raising role inpaint freedom, lowering Depth ControlNet strength, tightening mask overlap/blur defaults, and migrating stale staged defaults from previous releases.
- Added explicit solo/single-character role prompts with stronger role trigger weighting so each staged inpaint pass redraws only its assigned character region.

## 7.0.5

- Fixed multi-character LoRA matching for short CJK role names after connectors such as `和`, `跟`, `与`, `與`, `及`, and `同`, so prompts like `爱弥斯和圣娅相互拥抱` can match both role profiles and enter staged multi-LoRA.

## 7.0.4

- Isolated staged multi-LoRA role passes with pixel-level mask compositing: each inpaint pass now contributes only its masked region to the accumulated clean image, preventing one role LoRA's global color/style leakage from contaminating later roles.

## 7.0.3

- Fixed Illustrious generation validation when ComfyUI only has `waiIllustriousSDXL_v170.safetensors`: the plugin now auto-selects an actually available ComfyUI checkpoint before submitting the workflow, instead of leaving `Illustrious-XL-v2.0.safetensors` in `CheckpointLoaderSimple`.

## 7.0.2

- Improved Illustrious staged multi-LoRA composition: base passes now include weak per-role identity hints and role inpaint prompts retain user action/scene tags.
- Raised staged inpaint defaults to `denoise=0.70`, `control=0.42`, `overlap=0.10`, and `blur=14` to reduce role blending, stale LoRA leakage, and mask overlap artifacts.
- Bumped staged workflow markers to v1.3.0 and migrated older persisted staged defaults, including the previous `0.52/0.22/26` set, to the new values.
- Clarified ComfyUI Bridge proxy diagnostics by replacing angle-bracket LAN proxy placeholders with concrete `http://CLASH_LAN_IP:7890` examples, avoiding logs/messages that render the placeholder as `http:// :7890`.

## 7.0.1

- Added a unified Windows setup entrypoint `scripts/astrbot_comfyui_setup.bat` with lightweight and full modes, Clash LAN HTTP proxy prompting, and embedded PowerShell so the BAT can be run by itself.
- Updated the ComfyUI extras installer to download the missing Depth-Anything V2 Large preprocessor checkpoint into `custom_nodes/comfyui_controlnet_aux/ckpts/depth-anything/Depth-Anything-V2-Large`, preventing `comfyui_controlnet_aux` from falling back to direct Hugging Face downloads during generation.
- Routed setup downloads and `git clone` operations through the same Clash LAN HTTP proxy used by the Resource Bridge when configured.

## 7.0.0

- Fixed Illustrious multi-character LoRA triggering across non-default style presets such as PVC, newpvc, and Perfect Realism by removing the default-style gate from multi-LoRA matching and related single-character stabilizers.
- Improved multi-character intent detection for mixed Chinese/Latin prompts such as `Denia和Verina` and broader together/meal/interaction wording.
- Changed partial multi-character matches to keep the one matched character LoRA active with a warning instead of disabling LoRA entirely.
- Avoided single-character translation profile hints for multi-character requests, while preserving multi-profile hints when multiple roles are already matched before translation.
- Tightened connector validation so multi-LoRA connector checks verify that matched role terms are actually separated by a connector.

## 6.2.21

- Fixed standalone Bridge installer usage: `install_comfyui_lora_bridge.ps1` now embeds the ComfyUI AstrBot Resource Bridge and writes it directly when the local repository source is not available, so the `.bat/.ps1` pair can be copied to Desktop without depending on GitHub raw downloads.

## 6.2.20

- Fixed Civitai/Hugging Face metadata imports through ComfyUI Bridge when large JSON responses are truncated: Bridge JSON fetches now request a larger response body, detect truncated payloads, retry JSON parse failures, and return clearer corruption/network guidance.
- Upgraded ComfyUI-AstrBot-LoraBridge to v1.3.0 as a generic resource bridge: downloads can now target safe ComfyUI folders such as `loras`, `checkpoints`, `controlnet`, and `controlnet_aux_ckpts`, while old LoRA download behavior remains compatible.
- Illustrious checkpoint downloads now prefer ComfyUI Bridge when Bridge mode is configured, so remote ComfyUI machines can download checkpoints through their own proxy without mounting `models/checkpoints` into AstrBot.
- Added Windows `.bat` launchers for the Bridge installer and ComfyUI extras helper: `scripts/install_comfyui_lora_bridge.bat` and `scripts/prepare_comfyui_extras.bat`.

## 6.2.19

- Raised staged multi-LoRA workflow quality defaults for high-end GPUs: base pass now uses 44 + 18 sampler steps and each role inpaint pass uses 34 steps.
- Bumped staged multi-LoRA workflow markers to v1.2.0 so existing data-dir copies are refreshed automatically on plugin startup.

## 6.2.18

- Fixed staged multi-LoRA base pass validation on Illustrious-only ComfyUI installs: the no-character-LoRA pass now bypasses and removes the placeholder LoRA node instead of leaving the old Anima placeholder filename for ComfyUI to validate.

## 6.2.17

- Rebuilt staged multi-LoRA generation into explicit stateless passes: base pass now runs without character LoRA, each role inpaint pass injects only that role's LoRA plus its uploaded region mask, and watermarking happens only on the final output.
- Added a dedicated `generate_staged_pass()` ComfyUI API path that skips runtime config reloads, automatic LoRA matching, style LoRA injection, and silent LoRA neutralization retries for staged passes.
- Fixed staged prompt leakage by separating scene/composition tags from per-character trigger prompts so one role's trigger words no longer bleed into another role's inpaint pass.
- Fixed multi-character LoRA profile matching so high-confidence fuzzy matches can participate in explicit multi-LoRA selection, profile key/character names are not blocked as ambiguous markers, CJK `A和B` prompts count as multi-character intent, and Chinese import search terms are retained in runtime match terms.

## 6.2.16

- LoRA admin now shows physical LoRA files that do not yet have role profiles, marked as unrecognized so they can be manually organized later.
- Saving an unrecognized LoRA from the WebUI creates a real role profile instead of failing with "profile not found".
- Added LoRA admin refresh, visible-scope multi-select, and backend batch update/delete support for selected LoRA rows.
- Fixed LoRA admin row layout/click handling after the gallery batch-selection grid change.

## 6.2.15

- Fixed WineFox Gallery admin batch edits keeping hidden selected items after a filter/category change.
- Batch edit/delete now only submits currently visible checked items and clears selection after the operation refreshes.

## 6.2.14

- Added batch selection to WineFox Gallery admin favorite management: admins can select visible results, invert selection, clear selection, and keep selections synchronized across search/filter refreshes.
- Added batch favorite edit support for character/category, maker, prompt, and design summary; empty batch fields are ignored so existing metadata is not accidentally overwritten.
- Added a backend `/api/admin/gallery/batch` endpoint plus store-level batch update/delete helpers that deduplicate ids, cap batch size, and return updated/deleted/not-found/failed counts.

## 6.2.13

- Fixed WineFox Web Draw history privacy: normal gallery users now only see their own draw history, while gallery admin accounts can still view all users.
- Applied the same ownership check to draw history images, downloads, favorites, live job status, and temporary job image URLs so item ids cannot be used to access another user's results.
- Stored account ownership metadata on new Web Draw jobs/history records and kept username-based compatibility for older records.

## 6.2.12

- Fixed generation queue stalls where a dead or unbound ready queue entry could remain at the head and make later prepared tasks loop forever with `ready_ahead`.
- Bound paint, video, LLM draw, and Web Draw background tasks to their queue entries; finished/cancelled task callbacks now clear leftover queue placeholders.
- Added a ready/preparing orphan watchdog and richer queue wait logs with head entry age and generation lock owner details.
- Narrowed the generation lock to ComfyUI runtime mutation and the actual generate call; audit, gallery bookkeeping, Web response work, and message sending no longer block the next ComfyUI submission.

## 6.2.11

- Reduced the default Illustrious stabilizer to a minimal prompt budget: it no longer appends bulky hand, foot, phone, chain, armpit, camera, or duplicate-body guard tag lists.
- Kept only `solo` as the default single-character positive stabilizer and a five-line core negative set to avoid pulling CLIP attention back onto stray hands/limbs.
- Added a static regression test that fails if `_apply_default_illustrious_prompt_stabilizers()` grows `positive.extend(...)` or `negative.extend(...)` calls again.

## 6.2.10

- Reworked the generation queue into a true two-stage ready queue: jobs now enter `preparing` first, then move to `queued` only after prompt translation, LoRA/profile matching, and workflow prep are complete, so a slow or stuck preparation no longer blocks later ready jobs from submitting to ComfyUI.
- Added production-grade background task tracking for paint, LLM paint, LoRA, and video jobs; task callbacks now surface unexpected exceptions instead of silently discarding finished tasks.
- Improved Web Draw queue status reporting so the browser can distinguish preparing, queued, running, done, and error states instead of showing every started job as already running.
- Hardened ComfyUI LoRA Bridge failures: Bridge 4xx/5xx JSON errors are parsed into actionable messages, localhost proxy hints now explicitly explain the ComfyUI-host perspective, and repeated Bridge/Civitai proxy failures trigger a short circuit breaker to avoid concurrent search stalls.
- Reduced retry storms for Civitai Bridge HTTP 408 overload responses so LoRA searches fail fast with a useful partial-source warning instead of piling up repeated requests.

## 6.2.9

- Added Clash LAN-friendly ComfyUI LoRA Bridge setup: the bridge installer scripts now support `-UseClashLan`, `-ClashHost`, and `-ClashPort 7890`, while explicit `-BridgeProxy` still wins.
- Hardened Bridge proxy handling and diagnostics: `host:port` proxy values are normalized to HTTP URLs, Bridge fetch requests carry their own timeout, and `/comfy_civitai bridge` now separately reports Bridge reachability and Civitai proxy egress.
- Fixed intermittent Illustrious checkpoint false negatives when remote ComfyUI `/object_info` reads time out: the plugin now falls back to the last successful checkpoint cache and avoids blocking generation when the checkpoint list cannot be verified.

## 6.2.8

- Improved NapCat/OneBot merged-forward sending by trying NapCat's `send_forward_msg` path before falling back to standard group/private forward APIs.
- Hardened merged-forward fallback handling for NapCat `NodeIKernelMsgService/sendMsg` retcode 1200 timeouts, treating likely delayed sends as non-fatal to avoid duplicate spam and noisy error logs.
- Fixed LoRA selection imports so `MessageEventResult(...)` objects are no longer stringified into chat output; the selection summary is now prepended into the forward message with a clean text fallback.

## 6.2.7

- Added Web Draw favorite support: generated results, preview images, and recent history cards can now be collected directly into the WineFox Gallery.
- Kept Web Draw sensitive-result masks in place for favorite actions; masked images must still be revealed before preview, download, or favorite controls become active.
- Made Web Draw favorites idempotent by matching image SHA-256 hashes, so repeated favorites reuse the existing Gallery item and merge the current web account as a collector instead of copying duplicate files.

## 6.2.6

- Fixed Web Draw result and preview image layout for tall portrait outputs: generated images now keep their natural aspect ratio, stay centered, and fit within the viewport instead of being squeezed or visually offset.
- Improved Web Draw preview dialog sizing and long prompt wrapping so the title and metadata no longer force the preview into a narrow single-line card.

## 6.2.5

- Disabled Web Draw input prompt sensitive-word blocking while keeping generated-result audit masks and click-to-reveal behavior for sensitive outputs.
- Fixed WineFox Gallery registration so bare `WFREG-...` tokens in normal group messages are recognized from raw and component text, and raised the plain-message handler priority so registration is not missed.
- Reworked ComfyUI generation queue handling for high-latency remote servers: prompt translation, LoRA/profile matching, img2img vision reverse prompts, and final LLM cleanup now run before the generation lock; queued jobs keep FIFO order and only apply runtime state when they are ready to submit to ComfyUI.
- Added queue capacity limits, stale queued/running entry cleanup, Web Draw HTTP 429 responses when the queue is full, and background task cleanup for cancelled or failed Web Draw jobs.

## 6.2.4

- Fixed false sensitive-word blocking from the ambiguous English terms `cut`, `cuts`, and `cutting`; normal prompts such as `hair cut`, `cut out`, `cinematic cut`, and `cute smile` no longer fail the pre-generation text check.

## 6.2.3

- Moved the Web Draw entry into the main Gallery navigation next to 作品墙 / 排行榜 / 管理台, with a matching mobile bottom-nav entry, and removed the separate tiny action icon.
- Added the 18+ confirmation gate and disclaimer to the whole Gallery web entry flow instead of only gating `/draw.html`.
- Tightened `/draw.html` visual consistency with the Gallery glass-panel system and replaced the per-letter Thinking animation with a slower whole-phrase generation status.

## 6.2.2

- Changed Web Draw audit-blocked results to return the watermarked image as a sensitive result with a Telegram-style click-to-reveal mask instead of showing the robot/VIP blocked-send text as a plain failure.
- Enhanced `/draw.html` generation feedback with rotating Thinking words, per-letter transition animation, and the running message “酒狐正在疯狂画图中”.
- Added a Web Draw LoRA sidebar backed by a new authenticated `/api/draw/loras` endpoint that lists currently usable character LoRA entries for the active model family.
- Added an 18+ confirmation gate with a disclaimer, recent draw counters, and in-page image preview modals so history viewing no longer opens a new browser tab.

## 6.2.1

- Added Web Draw history caching with a default 24-hour retention window, persistent image/history endpoints, and a download endpoint for generated watermarked images.
- Reworked `/draw.html` to match the WineFox Gallery visual language with dark glass panels, animated background, Thinking-style generation animation, result metadata, download action, and recent generation history.

## 6.2.0

- Added a minimal WineFox Web Draw page at `/draw.html`, backed by authenticated `/api/draw` job APIs that return an estimated wait time immediately and expose the finished watermarked image when generation completes.
- Reused the ComfyUI robot drawing pipeline for Web Draw: model-family auto switch, LoRA profile matching, prompt translation/finalization, Illustrious guards, staged multi-LoRA generation, output audit, and watermarking all run through the same plugin helpers before the image is served to the page.

## 6.1.9

- Fixed LLM-triggered `jiuhu_draw_image` calls timing out after AstrBot's 60-second local tool limit while ComfyUI was still generating. The tool now validates the request, starts the ComfyUI generation in a tracked background task, and returns immediately so the runner cannot cancel the image job.
- Kept the LLM drawing output behavior from 6.1.7: the final generated image message still includes the LLM's short user-facing text, without a separate prompt echo or verbose queue/status block.

## 6.1.8

- Fixed `酒狐画图更新` / `酒狐更新画图` producing an empty response when the LLM request hook saw the command before the plain-message update handler. The request hook now directly runs the self-update command and sends its normal feedback instead of only stopping propagation.
- Added a per-event self-update handled flag so the plain-message path and LLM-request fallback cannot run the updater twice.

## 6.1.7

- Changed `jiuhu_draw_image` results to send only the LLM's user-facing message plus the generated image in one plain message chain, avoiding the merged-forward/chat-record "ComfyUI drawing result" block, prompt echo, elapsed time, and workflow details on LLM-triggered drawings.
- Folded any assistant text produced alongside a tool call into `jiuhu_draw_image.message` and cleared the normal LLM response text so the bot no longer sends a separate pre-drawing sentence before the image.
- Replaced the verbose generation queue status table with short natural queue notices and added zayu-mode ids for ready/waiting queue replies.

## 6.1.6

- Fixed LLM drawing tool calls being ignored when a persona/session uses a restricted tool list: `jiuhu_draw_image` is now injected into the current LLM request's `func_tool` set, so the runner can find the handler after the model calls it.
- Added a defensive runtime registration path for `jiuhu_draw_image` if AstrBot did not keep the decorator-registered tool in the global tool table during reload.

## 6.1.5

- Added a single active LLM drawing tool, `jiuhu_draw_image`, and activate it during plugin initialization while disabling the legacy `comfyui_txt2img` tool name if it is still registered.
- Reworked the main LLM drawing instructions so models call the tool instead of emitting `<pic prompt="...">` tags, while keeping the old tag parser only as a compatibility fallback.
- Let the LLM provide a short `message` argument that is sent together with the generated image through the existing ComfyUI result chain.

## 6.1.4

- Fixed cross-device LoRA profile handling: `jh 角色列表` now keeps all current-model role profiles visible and only reports live ComfyUI LoRA mismatches instead of shrinking the list to the currently mounted device subset.
- Stopped automatic cleanup from deleting LoRA aliases, character prompts, or role profiles just because the current ComfyUI device does not list the referenced LoRA file. This prevents device switching or temporary mount issues from making role libraries disappear.
- Kept indexed role deletion based on the full role-profile list so delete numbers remain stable across ComfyUI device switches.

## 6.1.3

- Fixed WineFox favorites image pickup on NapCat/OneBot by normalizing `file_id`, `path`, `sourcePath`, `fileUuid`, and `originImageUrl` image fields from current and replied messages.
- Added a safer image refresh fallback for `酒狐收藏`: when direct local/URL conversion fails, the plugin now asks NapCat/OneBot `get_image` / `get_file` for a fresh path or URL before telling the user no image was found.
- Reused the same OneBot image resolver for direct images, replied images, and private history image pickup, improving `酒狐看看` and related image commands that share the extraction pipeline.

## 6.1.2

- Rebuilt Illustrious staged multi-LoRA as the default multi-character path: a no-LoRA base composition pass followed by one masked img2img pass per character LoRA.
- Updated staged multi-LoRA workflows to v1.1.0 with Euler A sampling, multi-character base prompts, Depth ControlNet inpaint passes, and softer split masks.
- Fixed staged img2img mask injection for prewired workflows by avoiding the undefined latent reference path that could crash the first role pass.
- Fixed staged multi-LoRA prompt assembly so base composition prompts merge through the existing two-argument tag merger without raising `multiple values for argument 'extra_first'`.
- Added a ComfyUI-only `server_ssl_verify` switch, defaulting off, so self-signed HTTPS/FRP ComfyUI endpoints can be reached without weakening LLM, Civitai, GitHub, or model download certificate checks.
- Added a ComfyUI LoRA Bridge custom node plus installer script so cross-machine deployments can let the ComfyUI host download imported LoRA files directly into its own `models/loras` folder through the existing ComfyUI address.
- Merged LoRA Bridge installation into the existing ComfyUI migration helper menu and added an embedded Bridge fallback when the helper script is run without a full plugin checkout.
- Added Bridge proxy setup support and configurable AstrBot-to-Bridge connection/read timeouts, with clearer diagnostics for FRP timeouts and ComfyUI-host Civitai/Hugging Face download failures.
- Added a WebUI-level `comfyui_bridge_proxy` override and made the Bridge read PowerShell-written UTF-8 BOM config files without regenerating tokens after restart.
- Routed Civitai/Civitai Red/Hugging Face metadata fetches through the ComfyUI LoRA Bridge in Bridge mode, so remote deployments can use the ComfyUI machine's Clash/proxy for search, resolve, and download.
- Added Bridge-side download queueing plus proxy circuit breaking, and added AstrBot-side Civitai/HF retry limits and proxy circuit breaking to avoid long stalls when a proxy is down.
- Added local LoRA directory health caching so broken mounts such as `/mnt/comfyui_loras` with `Host is down` are bypassed instead of showing stale local roles or blocking imports.
- Improved high-latency ComfyUI generation handling with longer submit/read defaults, adaptive history polling, retried `/prompt` submission, and retried `/view` downloads.
- Added generation queue notices for background `酒狐来点` jobs, including the number of jobs ahead, queue position, and estimated wait time based on recent generation durations.
- Changed LoRA import completion output to a merged-forward/chat-record style result with separate download, role profile, trigger-word, and availability nodes.
- Added a single-character external-hand guard for Illustrious prompts, especially armpit/hand interaction close-ups, to rewrite risky hand tags as the character's own attached hands and suppress offscreen, third-party, foreground, and frame-edge hands.
- Anchored hands away from feet/ankles in single-character foot or barefoot compositions, with phone prompts explicitly keeping both hands on the smartphone near the upper body and dynamic negatives such as `hands near feet`, `hands near ankles`, and `fingers near toes`.
- Clarified zero-strength placeholder LoRA diagnostics so skipped LoRAs are no longer mistaken for applied model files.
- Filtered the paintable role index by the current ComfyUI live LoRA list so migrated stale role profiles are no longer shown as drawable roles.
- Added automatic plain-message fallback when OneBot rejects merged-forward LoRA/background result messages.
- Migrated old staged multi-LoRA defaults to stronger role identity and smoother seams (`denoise=0.52`, `overlap=0.22`, `blur=26`) while preserving user-customized values.
- Added an interactive ComfyUI migration helper script that prompts for the new ComfyUI folder, detects missing plugin extras, and lets admins choose minimal or optional dependency installs.
- Reworked role list output into paged compact HTML tables with numeric deletion indexes.
- Added indexed role deletion while preserving existing name-based cleanup.
- Optimized built-in LoRA range downloads and allowed Civitai/Civitai Red R18 previews.
- Added admin WebUI LoRA profile management for names, aliases, trigger prompts, disable state, and deletion.

## 6.1.1

- Fixed background `酒狐来点` / video intake sometimes producing an empty AstrBot send by yielding the short waiting notice from the command entry instead of the background worker.
- Restored zayu-mode waiting lines for generation intake without sending long prompt echoes or duplicate background notices.
- Added fail-fast behavior for prompt translation/final optimization LLM timeouts: slow upstream calls now fall back immediately instead of retrying another full timeout cycle before generation.

## 6.1.0

- Restored the configured short waiting message for background image/video generation, so zayu-mode custom waiting lines work again.
- Kept background intake free of long prompt echoes and “moved to background” explanations; only the configured short waiting line, errors, and final results are sent.

## 6.0.30

- Made background image/video generation silent on successful intake: `酒狐来点` no longer sends a long “moved to background” prompt echo and background workers suppress the extra waiting/status message.
- Kept validation failures and final generation results unchanged, so users only see errors or the finished output.

## 6.0.29

- Fixed WineFox favorites creating duplicate gallery entries for the same image: `酒狐收藏` now deduplicates by image SHA-256 and merges collectors into one sidecar record instead of copying the image again.
- Existing duplicate favorite files are hidden at gallery/API listing time by SHA-256 so the public gallery shows one artwork entry per identical image.
- Added safe `collector_count` metadata for gallery clients without exposing collector QQ details in the public gallery payload.

## 6.0.28

- Added an Illustrious restraint/chain scene guard for single-character bound poses: chains are now stabilized as fixed to the bed, wall, or environment instead of being interpreted as held by off-screen hands.
- Strengthened dynamic negatives for chain restraint prompts with `hands holding chains`, `offscreen hands`, `third-party hands`, and detached pulling arms suppression.
- Expanded arm/hand intent detection to cover wrists, forearms, elbows, limbs, shackles, and handcuffs without treating those as hand-detail close-up requests.

## 6.0.27

- Split Illustrious leg-pose stabilization from explicit foot-detail stabilization so prompts that only mention legs or spread-leg poses no longer auto-add toe/sole detail tags such as `five toes on each foot` or `clear sole shape`.
- Strengthened the automatic negative prompt for high-risk leg/foot compositions with `third foot`, `random foot`, `foot from nowhere`, extra lower-leg, and limb-between-legs suppression.
- Mark older Illustrious default negative prompts as stale when they lack the new foot artifact guards, allowing existing configs to migrate safely.

## 6.0.26

- Moved admin LoRA search/import commands into background tasks so `/jh 找lora ...`, `/jh 导入 ...`, numeric LoRA selection replies, and `/comfy_civitai search|auto|import ...` no longer keep the command handler occupied while Civitai/Hugging Face search, preview fetching, LLM profile refinement, or large LoRA downloads are running.
- Added a per-chat/user LoRA background task guard: one active LoRA search/import per session, while other plugin commands such as drawing, rank, gallery, status, and GPT Magic remain usable.
- Moved explicit image/video generation commands into background tasks after fast validation, so long ComfyUI jobs no longer block later LoRA search/import or other bot commands at the event-handler layer.
- Cancel LoRA/image/video background tasks during plugin teardown to avoid stale tasks surviving a reload.

## 6.0.25

- Reworked the GPT Magic draw queue from a single pipeline into a configurable parallel worker scheduler. `max_concurrent_tasks` defaults to 2, keeps the existing per-user single-task guard, and dispatches queued jobs as soon as a worker slot is available.
- Updated the GPT Magic WebUI queue panel and config form to show running workers, waiting jobs, active users, and the editable concurrency limit.

## 6.0.24

- Fixed low-frequency gallery roles being effectively hidden at the far end of the role chip strip by allowing role chips to wrap and scroll vertically instead of only scrolling sideways with no visible affordance.
- Added a dedicated role filter search on the public gallery toolbar and mobile filter drawer; normal gallery search now also boosts matching role chips to the front so newly generated one-off roles such as Cartethyia can be found quickly.

## 6.0.23

- Split the WineFox gallery authentication UI into separate login and registration pages so mobile users no longer have a single overlong card that cannot scroll cleanly.
- Improved mobile registration ergonomics with scrollable auth screens, compact spacing, larger mobile input text to avoid browser zoom, and a dedicated return-to-login path after generating the bot verification command.

## 6.0.22

- Changed WineFox gallery registration to a Web-first activation flow: users create a pending account on the gallery page, receive a one-time `WFREG-...` verification string, then send `酒狐注册 <字符串>` to bind and activate the account through the bot.
- Updated the gallery public URL default and bot replies to `https://frp-own.com:53414/`.
- Made the privacy watermark less intrusive by reducing size, density, and opacity while keeping account/time traceability.

## 6.0.21

- Removed the WineFox gallery guest password path entirely: `/api/login` now requires a registered username and password, and old guest cookies no longer authenticate.
- Hardened gallery account security with PBKDF2-HMAC-SHA256 password storage, automatic upgrade for legacy SHA-256 hashes after a successful login, 16-character generated passwords, 24-hour sessions, no-store security headers, and per-user/IP login lockouts after repeated failures.
- Added best-effort anti-screenshot protection to the standalone gallery: account/time watermarks, disabled long-press/right-click/drag/copy/print shortcuts, and automatic privacy shielding when the page loses focus or goes to the background.

## 6.0.20

- Added WineFox gallery accounts: users can send `酒狐注册 <用户名>` in chat to create a gallery login with a generated password, while the original gallery password still works for guest viewing.
- Added a built-in gallery administrator account (`admin` / `114514`) with a protected management console for accounts, access logs, favorites metadata, deletion, and URL/data-URL imports.
- Extended the standalone gallery UI with an admin-only page for editing role/category, maker, prompt, and design summary without exposing internal local paths in the public gallery API.

## 6.0.19

- Added visible loading states to the public WineFox gallery: page-level refresh/rank loaders, per-card image spinners, showcase image spinners, and lightbox image spinners.
- Fixed slow image loads collapsing gallery cards into text-only rows by giving every card image area a stable aspect ratio before the image arrives.
- Added image-load failure placeholders so broken or slow image URLs no longer look like frozen blank cards.

## 6.0.18

- Fixed the public WineFox gallery startup gate: unauthenticated visitors now stay on the password page instead of entering the album shell and spamming 401 loading errors.
- Added frontend handling for expired gallery sessions so any 401 from gallery or rank APIs immediately returns to the login page.

## 6.0.17

- Extracted the public WineFox favorites gallery into `templates/favorite_gallery.html` so the user-facing album UI can be edited as a standalone single-file HTML page.
- Added a small gallery API manifest and extension-friendly display-safe endpoints: `/api/meta`, `/api/roles`, `/api/makers`, and `/api/gallery/{item_id}`.
- Extended `/api/gallery` with optional `role`, `maker`, `q`, `sort`, and `limit` query parameters while keeping private IDs, local paths, workflow names, and model metadata out of the public payload.

## 6.0.16

- Added a targeted Illustrious foot/leg perspective guard for high-risk sole, low-angle, upside-down, raised-leg, and bent-knee prompts; it appends only small anatomy anchors and focused foot/ankle/knee negatives instead of changing normal generations.
- Explicit barefoot/no-shoes requests now remove conflicting LoRA/default outfit footwear tags such as sandals, heels, boots, shoes, socks, stockings, and pantyhose before generation, preventing mixed shoe-foot anatomy.
- Wired the same guard into both normal bot generation and WebUI pose generation metadata so future local inpaint repair can reuse the high-risk detection without touching unrelated workflows.

## 6.0.15

- Made `酒狐看看` image pickup simpler and more stable: it now reads direct raw OneBot/NapCat image segments from the current message in addition to AstrBot `Image` components.
- Added a short-lived per-session recent-image cache for `酒狐看看`; in group chats users can send an image and then simply send `酒狐看看`, or reply to an image even when the platform reply card fails to expose the original image.
- Kept the private-chat follow-up image flow, but it now runs after direct/reply/recent-session pickup so normal group usage no longer depends on the fragile private-image fallback.

## 6.0.14

- Added a standalone WineFox favorites gallery on `0.0.0.0:6666`, protected by the default password `114514`, so收藏图 can be shown through intranet tunneling without opening the admin WebUI.
- The gallery API now exposes only display-safe fields: image, role/category, prompt preview, and maker name. QQ IDs, group IDs, local paths, filenames, workflows, models, and other internal metadata stay out of the public showcase.
- Added a polished gallery UI with role filters, maker filter, search, lightbox preview, and a dedicated drawing leaderboard tab backed by the existing `daily_generation_stats.json` rank data.

## 6.0.13

- Hardened WebUI pose generation against duplicate/body-part pollution: full-body pose mode now injects single-subject positive tags and strong duplicate/reference-sheet/body-part negative tags.
- Removed `pose reference` wording from generated pose prompts and sanitizes optional pose prompt tags before merging, preventing reference-sheet style layouts from being sent to ComfyUI.
- Fixed the pose generation size rounding bug that turned 1024x1440 into 1024x1408; pose dimensions now stay aligned to the OpenPose control canvas using 8-pixel rounding.

## 6.0.12

- WebUI pose generation now treats camera/framing as a hard generation input: yaw, pitch, roll, shot size, facing, and composition are rebuilt from pose state on the backend and injected into the final prompt even when full pose prompt tags are disabled.
- Added automatic pose generation decisions so users no longer need to tune ControlNet strength/end-percent for normal use; full body, upper body, and close-up modes choose their own strength, end percent, canvas size, and output aspect ratio.
- Full-body pose mode now forces a vertical 1024x1440 generation preset, matching the OpenPose control canvas and adding anti-crop positive/negative tags to keep the whole character in frame.
- Moved ControlNet strength/end-percent into a collapsed advanced area. Manual overrides only apply when explicitly enabled, keeping the main workflow focused on character, image prompt, and pose.

## 6.0.11

- WebUI pose generation now runs the existing Illustrious LoRA role matching pipeline before submitting the ControlNet workflow.
- The pose endpoint returns matched role and LoRA metadata, and the WebUI result panel shows the role, LoRA, workflow, ControlNet, strength, output path, and LoRA warnings.
- The OpenPose preview image was simplified to a conservative COCO-18 body-only skeleton: no hand/finger lines, no foot detail lines, and no head circle that could pollute the ControlNet map.
- Pose prompt tags are no longer included by default in WebUI pose generation; ControlNet carries the pose while LoRA and the user prompt carry character and scene details.

## 6.0.10

- WebUI `姿态控制` 接入 ComfyUI 直出图：新增画面提示词、OpenPose ControlNet、姿态强度、结束比例、控制图预览和结果图回显。
- 姿态控制器现在会把 3D rig 状态导出为黑底 OpenPose 控制图，而不是只复制姿态提示词；避免纯 prompt 无法约束动作的问题。
- ComfyUI API 新增运行时 OpenPose ControlNet 注入：上传 WebUI 控制图，动态插入 `LoadImage`、`ControlNetLoader`、`ControlNetApplyAdvanced` 并重连采样器 conditioning。
- WebUI 生图接口只用于后台测试，不走机器人命令、不消耗聊天入口逻辑；生成时临时切到 Illustrious 配置，结束后恢复原模型族状态。

## 6.0.9

- 重写 WebUI `姿态控制` 前端控制器：从粗 COCO 点位升级为扩展人体 rig，加入骨盆、脊柱、胸腔、头脸、手掌/手指、脚尖/脚跟等细节控制点。
- 新增手臂/腿部两段 IK：拖动手腕、手掌、脚踝时会自动带动肘部/膝盖，骨盆可移动全身，胸腔/肩/髋会带动对应身体段落，减少关节断裂和不符合人体规则的姿态。
- 新增姿态层级显示、人体约束、骨长锁定、控制点大小、左右镜像和常用动作模板（站立、举手、抱臂、奔跑、坐姿、单膝跪）。
- 姿态提示词生成改为读取细节 rig 状态，可识别手部靠脸/抱臂/扶髋、手指张开、腿部弯曲、单膝跪、坐姿、躯干倾斜、头部朝向、脚部姿态和镜头构图。

## 6.0.8

- WebUI 新增 `姿态控制` 页：提供单人 COCO-18 轻量 3D 骨架、关节点拖拽、XYZ TransformControls、Orbit 相机和实时姿态提示词输出。
- 姿态提示词只生成姿态与镜头相关英文 tags，不写角色身份、服装、场景或 LoRA 触发词；本版不接 ComfyUI 出图和 OpenPose ControlNet。
- 新增姿态预设 API 和 `pose_presets.json` 独立存储，支持保存、覆盖、加载和删除本地姿态预设，并限制单个预设大小。
- Three.js、OrbitControls、TransformControls 改为插件本地静态资源加载，保留 MIT license，不依赖 CDN，也不修改本机 ComfyUI `custom_nodes`。

## 6.0.7

- 酒狐魔法 WebUI 整体重构：改成更清爽的后台工作台样式，减少横向卡片堆叠和拥挤表单。
- 收藏图库改成真正的角色目录：左侧按角色分组，右侧只显示当前角色收藏；缩略图固定比例裁切，避免卡片被提示词和元数据撑成长条。
- ComfyUI 页面改成分区导航：概览、基础连接、模型族、图生图、头像重绘分开编辑，状态信息改成键值行展示。
- WebUI 帮助面板补上 `酒狐来点`、`酒狐来点 @群友`、`酒狐收藏`、`酒狐记下来` / `酒狐看本本`、`酒狐排行` 和 `酒狐看看` 等常用入口。

## 6.0.6

- 酒狐魔法独立 WebUI 重整：侧边栏新增统一 `ComfyUI` 页，原来的模型管理、Anima 图生图推荐和 ComfyUI 只读状态合并到一处，不再显示“只读”入口。
- `ComfyUI` 页新增可编辑配置：可在 WebUI 里修改 ComfyUI 地址、默认工作流节点、Anima 角色替换参数和 `酒狐来点 @群友` 头像重绘提示词/头像 URL 模板；保存后写回插件配置并刷新当前运行实例。
- 新增 WebUI `收藏图库`：读取 `favorites` 目录，按角色目录展示缩略图，支持搜索角色、文件名、QQ 和提示词预览，并提供鉴权图片预览接口。
- `酒狐收藏` 从本版开始会给收藏图片写轻量 `.favorite.json` sidecar，记录角色、收藏人、来源、模型/工作流/LoRA 和提示词预览；旧收藏仍按目录名正常显示。

## 6.0.5

- `酒狐记下来` 升级为本本模板：保存时会优先用 utility LLM 把原始提示词整理成 `draw_prompt` 纯文本模板，同时保留 `raw_prompt`，不会写入或修改 ComfyUI 工作流节点。
- 新增 `酒狐看本本 [标题/触发词]`：不带参数查看当前用户在当前群/私聊作用域里的本本；带标题或触发词会把保存的 `draw_prompt` 直接送入现有 `酒狐来点` 绘图链路，后面追加的文字会合并为本次额外提示词。
- 本本记录继续按 `群/私聊 + 用户` 隔离，命名阶段保留用户锁，使用时会更新 `use_count` / `last_used_at`；旧版只含 `prompt` 的记录读取时自动兜底。
- 后台新增 `prompt_note_settings.template_llm_enabled` 开关；LLM 未配置或整理失败时自动保存原文模板，帮助面板补上 `酒狐看本本` 用法。

## 6.0.4

- 帮助菜单补上普通用户常用入口：`酒狐看看` 和 `酒狐排行 [今日|昨日|总榜]`，其中排行榜明确标注为渲染图片排行榜。
- 排行榜渲染图增加头像：渲染前会优先通过 NapCat/OneBot 用户信息接口获取头像 URL，下载到 `generation_stats_avatars` 缓存目录；接口拿不到时回退 QQ qlogo。
- HTML 渲染和 Pillow 兜底渲染都会显示头像，浏览器渲染失败时头像仍能在兜底图里出现。

## 6.0.3

- 新增 `酒狐记下来` 提示词收藏流程：用户可引用一段文字提示词后发送 `酒狐记下来`，也可直接发送 `酒狐记下来 <提示词>`；插件读取到文字后会要求同一用户继续发送标题/标签再保存。
- 新增用户会话锁：命名阶段按 `群/私聊 + 用户` 隔离，只有发起收藏的用户在同一会话里的下一条命名消息会被接收，其他群友不会掺进来；发送 `取消` 可放弃。
- 新增 `saved_prompts.json` 独立存储和 `prompt_note_settings` 配置项，记录按用户、群/私聊作用域和标题 key 分类；同一用户同一会话同名标题会更新旧提示词，后续扩展查找/调用更干净。

## 6.0.2

- 新增 `酒狐来点 @群友` 头像重绘入口：会从 OneBot/NapCat 的 at 消息段中识别被 @ 的 QQ，获取头像作为图生图源图，并复用现有 `comfyui_txt2img` / img2img 生成链路。
- 新增 `avatar_redraw_settings` 后台配置：默认开启，`prompt` 默认 `猪猪`，并支持自定义 QQ 头像 URL 模板和下载超时。
- 头像获取优先尝试平台接口返回的头像 URL，失败时回落到 qlogo 模板；只写入插件数据目录下的 `avatar_redraw` 缓存目录，不改 ComfyUI 插件目录。

## 6.0.1

- Illustrious 多人 LoRA 默认策略升级为 `auto`：2-3 个显式角色命中时优先使用新的 `finish_illustrious_multi_lora_regional_api.json` 区域 Hook LoRA 工作流，缺节点、mask 上传失败或 Hook/Pair conditioning 执行异常时自动回退旧 `finish_illustrious_multi_lora_api.json` Safe Chain。
- 新增 `multi_lora_mode`、`multi_lora_optional_refine`、`multi_lora_install_policy` 配置项；可选 refine 默认关闭，安装策略只保留 optional-only，不删除、不清理现有 ComfyUI `custom_nodes`。
- 根据 B 站多角色 LoRA/分区控制/提示词污染教程重新整理链路：全局 prompt 只放人数、场景、构图和互动；角色触发词、外观和服装进入各自 Hook CLIP + soft mask 区域，2 人互动使用较宽交叠，3 人使用更窄区域降低串色和融合成第三人的概率。
- 新 regional workflow 使用 `CreateHookLora`、`SetClipHooks`、`PairConditioningSetPropertiesAndCombine` 和运行时上传软遮罩，不依赖新增第三方节点；本机已有 Impact Pack/SEGS 等增强后续可选接入但不默认启用。

## 6.0.0

- `酒狐排行` / `jh排行` 改成渲染图片版：优先用本地 HTML/CSS 截图生成排行榜卡片，浏览器渲染失败时自动走 Pillow 兜底，最后才退回纯文本。
- 稳定性收束：版本号同步到 `metadata.yaml`，JSON 读取和 checkpoint 列表增加轻量缓存，少一点反复读文件/反复探测的笨蛋开销。
- 常用提示入口收口：缺 prompt、冷却、ComfyUI 离线、酒狐看看无图/失败开始走统一 `_notice`，杂鱼模式更容易接管。
- Illustrious checkpoint 检测逻辑从主生图流程里抽出一小段，行为不变，但以后不容易一改就串到主流程。
- 给第四画风 `PerfectDeliberate` 超写实预设加了专用肢体前景保险：抬腿/腿脚前景风险姿势会压掉脚腿特写和强透视，避免腿脚把脸和身体挤没。
- 没有动 GPT 魔法请求参数，也没有改工作流采样、denoise、steps。

## 5.2.12

- Illustrious 绘图前检查 checkpoint 时，如果其实是 ComfyUI 主机没开/连不上，不再把“没检测到 checkpoint + 下载说明 + 连接异常”整段发到群里。
- 这类情况现在走统一 `comfyui_offline_short` / `comfyui_connect_timeout_short` 短提示，杂鱼模式会自动套对应语录；完整连接异常只保留在日志里。

## 5.2.11

- 修复 `酒狐看看` 引用 QQ/NT 图片卡片却提示“请引用/附带图片”的问题：现在会从 AstrBot Reply、OneBot reply 段和 NapCat `raw.elements.replyElement` 三条链路提取引用图。
- 引用卡片里如果已经带 `picElement/sourcePath` 会直接用；否则会尝试用引用消息 ID 调 `get_msg` 拉原图。

## 5.2.10

- `酒狐看看` 私聊补图兼容 NapCat 历史消息里的 CQ 字符串、OneBot 图片段和 `raw.elements.picElement`，不再“历史查到了但图片解析不到”。
- 私聊历史取图继续收紧：有 `time` 必须晚于命令时间，有 `message_seq` 也必须大于触发时基线，避免误吃旧图。
- 视觉模型返回 `prohibited_content` 空输出时，会尝试 SFW 构图反推重试；仍被拦才发短提示，不再把一串报错甩出来。
- `酒狐看看` 转发聊天记录节点名带上 `昵称/QQ`，多人同时用的时候不容易串。

## 5.2.9

- 修复 `酒狐看看` 私聊补图只有部分用户生效的问题：私聊图片事件如果被 AstrBot 白名单阶段截断，现在会主动通过 OneBot 私聊历史查询最近 10 秒图片。
- `酒狐看看` 等图期间同时支持事件缓存和历史轮询，同一个 QQ 发来的图才会被使用，避免别人串图。

## 5.2.8

- `酒狐看看` 缺图时会按用户 QQ 检查私聊发给 bot 的图片；没有引用、没有随消息图片时，会等 10 秒接收私聊补图。
- 私聊图片只缓存给发送它的那个人用，拿来识图后就消费掉，避免串到别人或下一次命令。

## 5.2.7

- `酒狐看看` 不再把参考图的遮头/裁头照搬成 `headless crop`、`neck down`，默认会补成完整可见头部和可读脸。
- `酒狐看看` 把 `camera angle` 改成 view/perspective 说法，并清掉实体相机相关词，少把“视角”画成桌子底下冒出来的摄像头。
- 简笔画/线稿参考继续只当动作和构图参考：缺头、断手、乱线会被当成草稿噪声，不再直接塞进最终 prompt。

## 5.2.6

- `酒狐看看` 现在会把线稿、简笔画、草图当成动作/构图参考，只输出合理人体姿势，不再把歪线条和断手断脚也照抄进去。
- 真人动作示意图会自动二次元化：保留动作、镜头、光影和场景布局，输出 anime illustration / clean anime linework / stylized proportions 这类锚点，不走照片写实那条路。

## 5.2.5

- `酒狐看看` 反推改成“只要构图不要身份”：不再输出角色名、作品名、LoRA 触发词、发色眼色和标志性角色特征，复制去画图也不容易把旧角色召回来。
- `酒狐看看` 细节更啰嗦一点：镜头、姿势、构图、场景、光影、材质这些会写得更满；旧版已持久化的识图规则会自动迁移。

## 5.2.4

- 新增每日出图计数器：ComfyUI 单图、多图和 GPT 魔法成功出图都会记账，失败不算。
- 新增 `酒狐排行` / `jh排行` / `jh统计`：可以看今日、昨日、指定日期和总榜，看看今天谁又在疯狂压榨酒狐啦。

## 5.2.3

- LLM 注入别再装死啦：主聊天 `system_prompt` 补进 WebUI，插件内部 LLM 请求也支持单独的 `llm_request_injection_prompt`，还能镜像到 user 消息，给那些不吃 system 的中转一点颜色看看。
- 新增 `酒狐看看` / `jh看看`：引用或附带多张图，让单独配置的视觉 LLM 反推一条超详细英文生图 Prompt，只发一条聊天记录，不刷屏。
- `酒狐看看` 视觉配置可单独填；懒得填就复用图生图视觉模型，杂鱼也能少填几格。

## 5.2.2

- 收掉默认 Illustrious 文生图里最容易召唤神秘手的正向词：全身/远景不再无脑追加 `natural hands`、`natural hand pose`。
- `抱`、`手套` 这类动作/服装不再等同于“手部特写”；只有明确要手指、露手、手势、手部清晰时才补手部正向约束。
- 默认负面词和三个 Illustrious 工作流都补了 `hand from nowhere`、`hand not attached to body`、`random hands`、`stray hand` 等约束，启动会自动覆盖旧工作流。

## 5.2.1

- 杂鱼模式加随机语录池啦，同一句提示不会老是复读，笨蛋看着也没那么无聊了。
- ComfyUI 主机没开、连接超时、工作流节点炸掉的时候，群里只发短提示，不再把一大坨报错糊你脸上，哼。
- GPT 魔法生图那边带上 2K/4K 模型、默认分辨率、质量、格式这些参数显示；这部分按你的改动保留，本小姐只是帮你检查一眼。

## 5.1.9fix16

- 改善 Illustrious 多人 LoRA 全局 Safe Chain 的角色特征平均化：两角色时 LoRA 上限从 `0.55/0.55` 调整为 `model 0.65 / clip 0.72`，三角色仍保持较低上限。
- 多人全局 prompt 增加不同发型、发长、头部轮廓、脸型和面部特征的区分标签；多人工作流负面词补充 `same hairstyle / same head shape / same face` 等克隆特征抑制词。

## 5.1.9fix15

- 多人 LoRA 默认停用 masked regional conditioning，回到全局 Safe Chain：链式 LoRA 的 model/clip patch 本来就是全图生效，mask 只能限制文本 conditioning，继续组合会导致分屏、拼贴和额外角色。
- 多人工作流负面词补充 `split screen / multiple panels / collage / picture-in-picture` 等拼贴抑制词，并更新数据目录 workflow 覆盖检测。

## 5.1.9fix14

- 修复 Illustrious 链式多人 LoRA 外观退化：全局 prompt 重新加入每个角色的裸触发词/外观标签，让共享 CLIP 编码能看到 LoRA 激活信号。
- 位置词、`:1.05` 加权和 `distinct outfit` 仍只保留在 regional conditioning 节点中，避免回到 fix9 那种全局位置词额外召唤第三人的路径。

## 5.1.9fix13

- 修复 Illustrious 多人 LoRA regional conditioning 覆盖 node 11 的问题：不再用早期 `entries.global_prompt` 覆写已经过 quality prefix / stabilizer 处理的全局 prompt，避免质量、色彩和稳定标签丢失。
- 下调多人区域 mask 重叠比例：互动场景 `0.46 -> 0.25`，普通场景 `0.16 -> 0.10`，减少双 LoRA 在中间区域混合成第三人的概率。

## 5.1.9fix12

- 修复 Illustrious 多人 LoRA 的 final prompt 回流污染：`grouped_parts` 不再追加到返回的 `final_prompt`，角色触发词/外观/服装只通过 regional conditioning 节点进入采样器。
- 避免 node 11 全局 conditioning 因 `left character/right character` 分组词额外召唤第三个角色；分组内容仅保存在 meta 里用于调试记录。

## 5.1.9fix11

- 修复 Illustrious 多人 LoRA 的全局 prompt 污染：多人工作流会从全局 prompt 中移除已匹配角色的触发词、别名、外观和默认服装标签，只把这些信息留在对应角色分区里。
- 避免类似“达妮娅 + 爱弥斯睡一起”时，全局 `Denia (wuthering waves)` 额外召唤第三个达妮娅前景大脸的问题；全局 prompt 继续保留动作、场景、构图和互动关系。

## 5.1.9fix10

- 修复 Illustrious 默认画风的单 LoRA 多人回退问题：当用户明确要求多人，但只匹配到 1 个角色 LoRA 时，本次会跳过单角色 LoRA 注入，避免把另一个未匹配角色画成同一个角色的复制体或前景大脸分身。
- 该临时跳过会在下一次请求开始时自动恢复原 LoRA 覆盖状态，不会永久关闭 WebUI/命令设置。

## 5.1.9fix9

- 修正多人 Regional Safe Chain 漏掉全局动作/构图的问题：采样器现在使用 `ConditioningCombine` 把全局 prompt（动作、场景、互动关系）与分区角色 prompt 合并，避免“相互抱着”等互动词被区域输出旁路掉。
- 对拥抱、牵手、贴贴、亲吻、对视等近距离互动自动使用更宽的软重叠 mask，并给每个角色区域补充 shared interaction pose / close to the other character，减少区域太硬导致的站桩分离。
- 更新 Hook 工作流拦截提示：当前 Aki DynamicVRAM / `ModelPatcherDynamic` 不支持 `CreateHookLora`，默认仍保留 Safe Chain，避免再次把 ComfyUI 带崩。

## 5.1.9fix8

- 根据 B 站多角色 LoRA/提示词污染教程和 ComfyUI 本地节点实现，默认多人 Illustrious 工作流升级为 Regional Safe Chain：保留链式 LoRA 的稳定性，同时新增 `ConditioningSetMaskAndCombine`/`ConditioningSetMaskAndCombine3` 区域 conditioning，运行时给每个角色上传左右/三分区软 mask，把角色触发词、外观和默认服装限制到对应区域，减少发色、衣服和饰品互相串。
- 暂不默认启用 native `CreateHookLora` 权重遮罩，避免再次触发此前 `patch_hook_weight_to_device` 的 Windows access violation；本版先做低风险的区域 prompt 隔离。

## 5.1.9fix7

- 根据 B 站放大/分块教程字幕和 ComfyUI 本地节点实现，默认 Illustrious 与多人 Safe Chain 不再强制 `VAEDecodeTiled`，改用普通 `VAEDecode`：显存足够时整图解码，显存不够时由 ComfyUI 自己回退 tiled，降低最终解码阶段的分块接缝/飞边风险。
- 保留 `nearest-exact` latent 放大、二采 `denoise 0.20`、LoRA 和 Prompt 逻辑不动；本版只收敛输出链路里的 tiled 解码问题。

## 5.1.9fix6

- 收敛默认 Illustrious 与多人 Safe Chain 的二采边缘毛刺：latent 放大从 `1.15x` 降到 `1.10x`，refine denoise 从 `0.18` 小幅提高到 `0.20`，降低丝袜、鞋跟、白底高反差边缘的拖丝/飞边。
- 两个 Illustrious 工作流负面词补充 `edge artifacts`、`edge spikes`、`stray lines`、`broken outline`、`torn edges`、`contour artifacts` 等边缘伪影约束；启动迁移会自动覆盖仍停留在旧参数的数据目录工作流。

## 5.1.9fix5

- 收紧默认 Illustrious 多人 LoRA 自动匹配：多人角色匹配只使用用户原始请求文本，不再把 LLM 翻译后的英文外观词纳入匹配，避免 `yellow eyes` 等外观标签误触第三个角色档案。
- Safe Chain 多人 LoRA 自动把每个角色 LoRA 强度压到 `0.55` 上限，并把角色触发/外观/服装按 left/right/center 分组写回 prompt，降低链式 LoRA 全图互相污染和全员同服概率。
- 多人 Safe Chain 跳过最终 Prompt LLM 二次整理，保留分角色分组；多人负面词补充 `same dress`、`same costume`、`uniform outfit`、`matching uniforms`。

## 5.1.9fix4

- 紧急禁用默认 Illustrious 多人 LoRA 的 Hook/Mask 路线：当前 ComfyUI/PyTorch 环境会在 `patch_hook_weight_to_device` 触发 Windows fatal access violation，已回退到稳定的普通 `LoraLoader` 链式多人工作流。
- 启动迁移会自动覆盖残留的 Hook 多人工作流；运行时如果仍检测到 `CreateHookLora`，会在提交到 ComfyUI 前拦截，避免再次进入会崩溃的采样路径。

## 5.1.9fix3

- 默认 Illustrious 多人 LoRA 实验工作流在 Hooks 绑定基础上增加运行时左右/三分区 mask：每个角色的角色提示词和 LoRA hook 只作用于对应空间区域，降低发色、服装和脸型互相污染。
- mask 只在独立 `finish_illustrious_multi_lora_api.json` 工作流且成功命中 2 个以上角色 LoRA 时动态上传并接入；普通单角色、图生图、PVC/newpvc 和非 Hook 工作流保持原链路。

## 5.1.9fix2

- 将默认 Illustrious 多人 LoRA 实验工作流切换为 ComfyUI Hooks 绑定方案：每个角色 LoRA 只绑定到对应角色提示词 conditioning，减少发色、服装和角色特征互相污染。
- 多人 LoRA runtime 注入优先使用 `CreateHookLora` 与 `ConditioningSetPropertiesAndCombine`；旧链式 `LoraLoader` 注入保留为非 Hook 工作流兜底。

## 5.1.9fix1

- 优化默认 Illustrious 多人 LoRA 实验链路：角色提示词按组加权，降低多 LoRA 叠加强度，并为独立多人工作流增加反角色渗透负面词。

## 5.1.9

- 新增默认 Illustrious 多人 LoRA 首版实验入口：用户明确提出多人且显式点名 2 个以上本地角色档案时，自动切换到独立 `finish_illustrious_multi_lora_api.json` 工作流。
- 多人 LoRA 采用运行时链式注入，最多 3 个角色 LoRA；普通单人、图生图、PVC/newpvc 和未匹配到 2 个角色的请求继续走原链路。
- 多人匹配只使用本地档案的精确显式命中，不使用 LLM 猜测，避免干扰现有可用功能。

## 5.1.8

- 修复默认 Illustrious 单人 LoRA 链路容易出现背景剪影/侧影分身的问题：移除默认正面词中的 `clean character silhouette`，改为 `clean character shape`。
- 启动迁移会识别并替换已持久化到 AstrBot 配置里的 5.1.7 旧前缀，避免旧配置继续把 `silhouette` 带回最终 Prompt。
- 默认 Illustrious 单人 LoRA 生成时，在用户没有明确要求剪影/影子的情况下追加反设定图剪影负面约束，如 `character sheet`、`side silhouette`、`background character silhouette`、`shadow clone`。

## 5.1.7

- 继续收敛默认 Illustrious 文生图的手脚/五官问题：全身/远景默认只加入 `natural hands`、`natural feet` 这类弱约束，避免模型在用户没要求时主动强化手、脚、脚趾等小结构。
- 默认工作流二采改为 `16` 步、`denoise 0.18`，减少二采把已经错掉的手脚和近景五官继续描实。
- 手/脚稳定器改为分级触发：默认压自然形态，用户明确提到手、手指、脚、足底、袜鞋腿部时才追加 `readable/well-formed/clear toes` 等更强约束；否则优先压制手脚特写、复杂手势、脚趾焦点和随机前景手脚。
- Illustrious 转译提示词不再要求全身/远景默认补手脚脚趾细节，只保留脸和眼睛可读，避免 LLM 主动生成多余手脚标签。

## 5.1.6

- 优化默认 Illustrious 文生图质量：`finish_illustrious_api.json` 提升到 `960x1344`，基础采样 `40` 步、二采 `18` 步，二采 `denoise` 调为 `0.22`，用更长生成时间换取全身/远景细节。
- 默认 Illustrious 全身/远景稳定器补充 `readable face / clear eyes / readable hands / readable feet` 及对应负面词，降低角色占比小时眼睛、手脚和脚趾糊掉的概率。
- 启动时会自动替换旧版默认 Illustrious 文生图工作流；`pvc`、`newpvc` 和图生图工作流不受影响。

## 5.1.5

- 修复 `酒狐传授魔法 @用户` 在普通群消息里不触发的问题；现在和 `酒狐收藏` 一样走普通消息扫描。
- 兼容 `酒狐传授魔法 [At:QQ]` 与 `[At:QQ] 酒狐传授魔法` 两种顺序。

## 5.1.4

- `酒狐收藏` 改为按角色名自动分类保存图片：`favorites/角色名/角色名_日期时间.后缀`；优先使用角色档案里的中文名/中文别名，无法识别时归入 `未分类`。
- 收藏不再生成收藏 JSON，也不再把 Prompt/相关消息落盘；生成时仅保留内存里的轻量角色索引，旧 `.favorite.json` 仍可作为兼容回查。
- 新增 `酒狐传授魔法`：超级管理员可授予/收回普通管理员；普通管理员可使用 LoRA 搜索导入、删除角色、角色列表和酒狐收藏，但不能切换画风、模型、工作流、锁定或自更新。

## 5.1.3

- 杂鱼模式文案表从全量静态扫描结果收敛为常用提示表，只覆盖绘图、权限、冷却、审核、图片/视频、收藏、LoRA 和管理命令的常见用户提示。
- 内置常用表已填入杂鱼模式文案；WebUI 开关关闭时仍使用原文，开启时按小表替换。
- 启动时若检测到旧的大型 `zayu_messages.json` 且没有自定义 replacement，会自动替换为新的常用小表。

## 5.1.2

- `酒狐收藏` 群内确认收敛为“已收藏”；给主人私聊只发送收藏人、来源群/私聊和图片，完整 Prompt/相关消息继续保存在 JSON 记录中。
- 新增 WebUI 底部 `zayu_mode_settings`：默认关闭；开启后按 `zayu_messages.json` 的 exact/contains/regex 覆盖表统一替换插件提示文案。
- 新增 `zayu_messages_template.json` 和 `docs/language_prompt_catalog.md`，用于统计和改写插件内提示文案。

## 5.1.1

- 调整 `酒狐收藏` 提示文案：给主人私聊和原群确认都改为更短的雌小鬼风格摘要，不再把 `CQ:image` 原文整段刷出来；完整相关消息仍保存在收藏 JSON 中。

## 5.1.0

- 新增“酒狐收藏”：超级管理员引用或附带图片发送 `酒狐收藏` 时，会把图片复制到 `favorites` 目录，并保存 Prompt、引用消息、来源群/私聊、收藏人等 JSON 元数据。
- 新增 `favorite_settings` WebUI 配置：默认关闭；`owner_user_id` 留空时只本地保存，填写主人 QQ 后会把收藏记录和图片私聊发送给主人。
- 开启收藏功能后，ComfyUI/GPT 魔法生成图片会额外写入轻量 `.favorite.json` 索引；收藏时如果引用消息本身没有 Prompt，会尝试按图片 SHA-256 回查生成记录。

## 5.0.1fix6

- OneBot `retcode=1200` 富媒体超时不再打 ERROR 堆栈；自动撤回发送通道遇到该超时时会停止回退二次发送，避免普通群出现重复结果。
- 绘图入口增加同事件去重保护，避免普通消息入口和命令入口同时命中同一条 `酒狐来点` 时重复生成。
- 默认 Illustrious 在用户未明确要求手/手臂/手部动作时，会移除正面里的手部强化词，并加入 `extra/floating/disembodied hand` 等轻量负面约束，减少莫名其妙多出来的手。

## 5.0.1fix5

- OneBot `retcode=1200` 富媒体超时现在只记日志，不再补发本地路径和完整 Prompt，避免图片实际送达后又多刷一条文字结果。
- 非超时发送失败的文字兜底改为短提示，只保留本地保存路径和 Seed。
- 全身构图清理增加身体部位前景冲突处理：用户未明确要求前景/特写/局部时，`full body` 会移除 LLM 自行生成的 `forced perspective`、`pussy/genitals in foreground`、`lower body focus` 等裁切诱因，并轻量压制下半身裁切。

## 5.0.1fix4

- 回退默认 Illustrious 稳定器里的全局强色偏负面词和反打码权重：移除额外注入的 `(green/cyan/magenta/neon color cast:1.5)`、`(rgb split/chromatic aberration:1.6)`、`acid colors`、`uncensored/clean skin`、加权打码负面等，恢复接近 5.0.6 的轻量默认链路，避免简单角色图被推成绿色底/青色轮廓。
- 内置 `finish_illustrious_api.json` / `img2img_illustrious_api.json` 强制使用正确节点：正面 `11`、负面 `12`、输出 `66`，防止旧配置把正负提示词节点带偏。

## 5.0.1fix3

- 修正默认 Illustrious 的“机舱”语义链路：`机舱/飞机舱/客舱` 现在按普通客舱处理，不再被 LLM 误扩展为 `cockpit`、控制面板、发光屏幕、赛博或霓虹驾驶舱；只有用户明确写 `驾驶舱/cockpit/控制台/霓虹/赛博` 时才保留这些方向。
- 出图发送失败兜底补充本地图片路径：如果 OneBot/QQ 富媒体消息超时，文字兜底会提示图片已经生成并给出保存位置，避免误判为 ComfyUI 生成失败。

## 5.0.1fix2

- 撤回默认 Illustrious 单采直出改动：`finish_illustrious_api.json` 恢复 5.0.6 时的二采 refine 链路，node17 继续解码 node16，保留 `nearest-exact` latent upscale、refine `denoise 0.24`、VAE `overlap 96`。
- 移除运行时/启动时强制把默认 Illustrious 改成 node14 单采的兜底逻辑；启动时如果发现 data 目录里还残留 single-pass 工作流，会重新复制插件自带的二采默认工作流。
- 保留已有 Illustrious 质量前缀模糊迁移和单角色稳定器修复，不再额外乱改 prompt 链路。
- Illustrious 旧质量前缀迁移现在会在启动时保存到 AstrBot 配置；如果运行时仍读到 `soft lighting` / `natural shadows` 且缺少 `vibrant but stable colors`，会临时改用当前色彩稳定前缀。
- 默认 Illustrious 稳定器会移除 LLM 翻译自行生成的旧版光照/质量词，如 `soft lighting`、`natural shadows`、`detailed shading`，避免暗场景被拉成绿/青色霓虹色偏。
- 默认 Illustrious 进一步清理 LLM 自行加入的色差后处理词，如 `chromatic aberration`、`rgb split`、`neon/rainbow color cast`，并加权压制绿/青/品红色偏、过曝和过饱和。

## 5.1.0fix

- 质量前缀模糊迁移继续补强：除 `natural body proportions`、`soft lighting`、`natural shadows` 等旧标志外，也会识别被运行时清理后残留的 `clean lineart`、`soft focus` 变体；如果缺少 `full color`、`vibrant but stable colors`、`consistent character colors` 等新版色彩稳定词，会启动时迁移到当前默认质量前缀。
- 默认 Illustrious 稳定器重新加入加权单人数量锚定：非多人请求会补 `(1girl:1.2)` 或 `(1boy:1.2)`、`(solo:1.3)`，并用更强的 `(2girls:1.5)`、`(multiple characters:1.4)`、`(multiple views:1.3)` 等负面词压制角色复制。
- 增加反自动打码和皮肤伪影约束：正面补 `uncensored`、`clean skin`，负面补加权 `censored/mosaic/censor bar` 以及 `skin artifacts`、`white spots on skin`、`noise on skin` 等。
- 额外负面词去重改为保留权重，避免 `(2girls:1.5)`、`(censored:1.4)` 被旧分词逻辑清洗成普通标签。

## 5.1.0

- 回滚 5.0.7、5.0.8、5.0.9 对默认 Illustrious 文生图链路的全部改动，恢复 5.0.6 时的 `finish_illustrious_api.json`：`nearest-exact` latent upscale、refine `denoise 0.24`、VAE `overlap 96`、节点 17 解码节点 16。
- 回滚 5.0.7 追加的默认负面词、加权 solo/反多人负面词和全局反打码负面词，避免默认画风继续被过强的颜色/边缘/负面约束影响。
- 保留 5.0.6 的单角色变体规范化、用户指定发色优先级和猫爪袜纠偏修复。
- Illustrious 质量前缀迁移新增模糊兜底：如果配置里仍保留 `natural body proportions`、`soft lighting`、`natural shadows` 等旧版标志词，且缺少 `full color`、`vibrant but stable colors`、`consistent character colors` 等新版色彩稳定词，会在启动时强制迁移到当前默认质量前缀。

## 5.0.6

- 修复已匹配 LoRA 角色时，`cat girl` / `bunny girl` / `maid girl` / `schoolgirl` 等变体词容易被模型理解成第二个泛角色的问题；默认 Illustrious 单人链路会把这些词规范为同一角色的耳朵、尾巴或服装属性，并补充 `single subject` / `solo focus` / `single character only`。
- 非多人请求的负面约束增加 `second person`、`another girl`、`two faces`、`side-by-side characters`、`mirrored duplicate` 等标签，压低同款角色复制、对坐、镜像双人等失败形态。
- 用户明确指定发色、毛色或瞳色时，不再额外注入笼统的 `preserve character color scheme`，避免角色档案默认色把用户指定的 `pink hair` 等颜色拉回去；未被覆盖的档案外观仍会继续保留。
- 中文 `猫爪袜` 会规范为 `cat paw socks` / `paw print socks` / `paw print soles`，并在原文没有手套需求时移除误翻译出的 `cat paw gloves`。

## 5.0.5

- 修复「展示完整足底/脚底/脚趾/赤脚」等身体部位可见性需求没有触发全身构图的问题；这些请求现在会按 `full_body` 处理并清理冲突的 `upper body`、`portrait`、`close-up` 标签。
- Illustrious 翻译提示词和最终 Prompt 整理提示词补充「正面位=missionary」，避免把体位俚语误翻成正面镜头或上半身构图。

## 5.0.4

- 修复默认 Illustrious 多人意图漏判：中文「俩人/俩个人/仨人/仨个人」现在会被识别为多人请求，不再被稳定器强制改成 `solo`。
- 最终 Prompt LLM payload 新增 `multi_character_intent` 字段，规则会按该字段决定保留多人 count tags 还是强制 solo。
- 强化非多人请求的单人稳定性：正面补 `only one character`，负面补 `2girls`、`2boys`、`multiple people`、`split screen`、`two bodies` 等更有效的反多人标签。

## 5.0.3

- 强化 Illustrious 默认画风的 Prompt 管线：翻译阶段和最终整理阶段都会识别中文网络隐语/俚语，避免把「火车」「快餐」「打桩机」等词按字面直译。
- 最终 Prompt LLM 现在会主动保留 LoRA 档案的 `default_outfit`，除非用户明确要求裸体或指定另一套服装；同时要求非多人请求必须保留 `solo` 且不输出多人标签。
- 新增轻量服装安全网：角色档案有默认服装但最终 prompt 被 LLM 删空时，会在用户未要求裸体/换装的前提下注回默认服装。
- 精简默认 Illustrious 稳定器的正面标签，减少同义词堆叠导致的 prompt 过长、solo/服装/动作权重被稀释。

## 5.0.2

- Wan 图生视频结果默认改发密码压缩包，密码 `114514`，避免 OneBot/QQ 直发 mp4 导致发送失败或生成锁长时间卡住。
- 视频压缩包上传增加超时保护；平台文件上传卡住时会释放生成锁，并提示压缩包在本机的保存路径。
- 视频提示词改为两段式 AI 链路：先由识图模型只识别源图内容，再把“源图描述 + 用户动作/镜头需求”交给文生图提示词 LLM 整理成最终 Wan prompt。
- 视频结果消息会显示用户需求、源图识别摘要和 AI 视频 Prompt，方便排查提示词到底怎么被整理。

## 5.0.1

- Wan 图生视频默认档从 `512x512 / 49帧 / 16fps / 6步` 调整为 `512x512 / 41帧 / 8fps / 4步`：输出时长从约 `3.1s` 提到约 `5.1s`，同时减少采样计算量。
- 视频生成结束后默认主动释放 ComfyUI 显存，降低连续跑 Wan 后下一次加载文本编码器/模型时 CUDA 状态异常的概率。
- `CUDA error: invalid argument`、illegal memory access、device-side assert 等 Wan CUDA 运行时异常现在会按可重试故障处理：先释放显存，再降级到 `448x448 / 33帧 / 4步` 重试一次。
- 旧版已持久化的 `49帧 / 16fps / 6步` 视频配置和旧 Wan 工作流会在启动时自动迁移到新版默认参数。

## 5.0.0

- 新增 Wan 图生视频入口：引用或附带一张图片后发送 `酒狐来点视频 <动作/镜头描述>`，首版按低显存稳跑默认使用 `512x512 / 49帧 / 16fps / 6步`。
- 新增 `workflow/wan_i2v_api.json` 最小 Wan2.2 I2V 工作流，接入 high/low noise Q4 GGUF、Wan T5、Wan VAE、WanImageToVideo、WanMoeKSampler 和 VHS h264 mp4 输出。
- 插件新增视频生成通道：从 ComfyUI history 的 VHS `gifs` 输出下载 mp4 到本地，生成前释放显存，OOM 后自动降级到 `448x448 / 33帧 / 4步` 重试一次。
- 视频结果接入酒狐水印、普通群抽帧审核和特殊白名单绕过；普通群审核失败时不发视频，并转发来源、请求记录和视频文件到特殊白名单群。
- 版本号进入 `5.0.x` 序列；后续按 `5.0.0` 到 `5.0.9` 后进位到 `5.1.0`。

## 4.0.23

- 默认 Illustrious 画风新增全彩防单色兜底：用户未明确要求黑白/线稿时，会移除 `clean lineart`、`lineart`、`soft focus`、`monochrome`、`grayscale` 等容易把结果带成单色线稿的标签。
- 默认正负提示词同步改为 `full color`、`colored anime illustration`、`vibrant but stable colors`，并压制 `monochrome/grayscale/lineart only/sketch/colorless/desaturated`。
- 兼容旧 WebUI 默认质量前缀迁移，包括 `natural body proportions... clean lineart... soft lighting...` 那套旧参数，避免升级后仍跑旧单色倾向配置。

## 4.0.22

- 默认 Illustrious 画风加入角色原始发色/毛色保护：自动匹配到角色档案后，会把 `identity_appearance` 里的发色、毛色、瞳色和稳定外观在最终 prompt 后兜底补回。
- 只有用户原文明确要求改发色/毛色/瞳色时，才允许对应外观颜色被覆盖；普通姿势、场景、镜头、服装变化不会再把角色颜色带跑。
- 默认负面词补充 `wrong hair color`、`wrong fur color`、`changed hair color`、`inconsistent hair color`，并同步 schema、工作流和旧默认配置迁移。

## 4.0.21

- 优化默认 Illustrious 画风工作流：改用更稳的 DPM++ 2M、降低二采 `denoise` 到 `0.24`、放大改为 `1.15x`，减少颜色漂移、复制角色和二采重绘导致的局部崩坏。
- 默认画风新增按原文生效的动态稳定器：未要求多角色时补单角色约束，远景/全身时补脸、眼、手、脚细节约束，指定前后左右上下镜头时补精确 camera/view tags。
- 默认画风补充戒指数量控制和颜色稳定负面词；旧默认 quality/negative/system prompt 会在启动时自动迁移，新自定义内容不会被覆盖。

## 4.0.20

- 重新校准 `newpvc` 的风格方向：弱化 SSS LoRA 到 `0.18/0.12`，避免皮肤被拉成写实毛孔质感。
- `newpvc` 固定正面词和转译 prompt 改为偏 3D 动漫 CG：强化 anime face、大眼、虹彩瞳孔、小鼻子和柔和五官，移除 realistic skin / pores / film grain / lens flare。
- `newpvc` 负面词加入 photorealistic、photo、real skin、skin pores、too realistic 等反写实约束，并把 CFG 提到 `5.0` 增强提示词控制。

## 4.0.19

- 微调 `newpvc` 画风的手脚稳定性：降低 latent 二段放大到 `1.24x`，refine 改为 `14` 步、`denoise 0.25`，减少近景手指和画面边缘脚部被二次重绘拉歪。
- `newpvc` 正负提示词新增手指长度、五指分离、脚趾结构等约束，针对轻微长短不一、粘连、扭曲做兜底。
- 下调 `newpvc` 工作流里的 RIN Add Detail 和 XDetail 辅助强度，保留质感的同时避免近景局部过度锐化导致结构变形。

## 4.0.18

- 新增 Illustrious 第四个内置画风 `newpvc`：使用 Nova 3DCG XL、`finish_illustrious_newpvc_api.json` 新工作流、Subsurface Scattering + RIN Add Detail + 微量 XDetail 的 3D CG 电影级动漫路线。
- `/jh 切换画风 4` 现在切到 `newpvc`，原随机画风顺延为 `/jh 切换画风 5`；帮助、schema 和 WebUI 画风选项同步加入 `newpvc`。
- 已按本地资源名接入 `nova3DCGXL_ilV90.safetensors`、`Subsurface_Scattering.safetensors`、`addDetail_Aesthetic.safetensors`，并保留节点 99 作为用户角色 LoRA 注入位。

## 4.0.17

- 按 DeepSeek 官方 OpenAI 兼容文档优化：DeepSeek 请求默认传 `thinking={"type":"disabled"}`，日志会显示 `thinking=disabled`。
- DeepSeek 工具 JSON 调用启用官方 JSON Output：`response_format={"type":"json_object"}`，并提高 JSON 最小 `max_tokens`，避免 LoRA 匹配 JSON 因 `finish_reason=length` 被截断。
- 修复空 `refusal=` 让 `finish_reason=length` 被误判成安全拦截的问题；length 现在会走正常重试/回退流程。
- 如果第三方网关不兼容 `thinking` / `response_format`，会自动去掉这些可选参数再重试一次。

## 4.0.16

- 默认关闭 Gemini LLM thinking：Prompt 转译、最终 Prompt 整理、LoRA/搜索 JSON 工具调用和图生图视觉反推都会对 Gemini OpenAI 兼容请求显式加入 `reasoning_effort=none`。
- LLM 空回诊断日志新增 `reasoning_effort` 字段，方便确认本次请求是否真的带了关闭思考参数。
- 新增配置项 `llm_settings.disable_llm_thinking`，默认开启；如果某个网关不兼容，可手动关闭。

## 4.0.15

- 修复 LLM 非空拒绝话术被误当作 Prompt tags 的问题：转译和最终整理现在会识别 can't assist、policy/guidelines、or sexual acts 等拒绝残片，命中后回退本地结果。
- 明确本次日志中的 custom_prompt_in_request=yes 代表自定义注入已进入请求；后续 prohibited_content / choices=0 属于上游安全层或服务商空响应。

## 4.0.14

- 增强 LLM 空回诊断日志：空内容、content filter、prohibited/refusal 会记录模型、消息数、system/user 字符数、system prompt 指纹、是否实际带入自定义 prompt、当前模型族和画风。
- 当日志显示 custom_prompt_in_request=yes 但仍返回 prohibited_content 时，可确认是上游服务商安全层拦截，而不是插件没有注入提示词。

## 4.0.13

- 修复 Illustrious/画风家族 prompt 覆盖全局 `prompt_translate_system_prompt` 的问题：现在家族 prompt 会附加你的全局自定义注入提示词，而不是二选一。
- 最终 Prompt 整理会继承同一套注入上下文；但当本地自审查词已命中时会跳过最终 LLM 整理，避免把敏感 prompt 再送给上游导致 `prohibited_content` 空回。
- LLM 响应若明确是 content filter / prohibited / refusal，会直接回退本地结果，不再重复请求；同时清理 `<!--?-->` 这类 HTML 注释污染 token。

## 4.0.12

- LLM Prompt 转译、最终 Prompt 整理和工具 LLM JSON 调用现在遇到首轮空响应、HTTP/网络异常或空 `choices` 时会自动重试一次，第二次仍失败才回退原文/本地结果。
- 修复 LoRA LLM 角色匹配在上游返回 `choices: []` 时抛出 `list index out of range` 的问题，改为安全跳过并记录失败原因。
- 转译日志会明确显示首轮失败后的重试状态，方便判断是“没触发转译”还是“转译接口空返回后回退”。

## 4.0.11

- 出图审核拦截后会把被拦截图片、来源群/私聊、发出指令者和原生成请求合并转发到特殊白名单群，便于 VIP 群侧查看完整上下文。
- 新增 `image_audit_settings.generated_output_audit_forward_to_special_whitelist`，默认开启；合并转发失败时会降级为普通消息发送文字和图片。
- 普通来源收到审核拦截/审核失败提示时，会附带简短特殊 VIP 群入口提示。

## 4.0.10

- 新增独立的生图特殊白名单群配置：`control.special_whitelist_enabled` 和 `control.special_whitelist_group_ids`，只对生图链路绕过普通白名单、锁定、敏感词、冷却、文本自审查、出图审核和自动撤回，不放开更新、删角色、LoRA 管理等管理员命令。
- 新增生成图片发送前审核：非特殊来源的 ComfyUI 单图、多图、`/jh` 主流程和 GPT 魔法本地结果都会复用图片审核模型，综合风险达到 `image_audit_settings.generated_output_audit_threshold` 时拦截发送，审核异常默认 fail closed。
- 启动日志和配置 schema 补充普通白名单数量、特殊白名单数量、出图审核开关与阈值，方便后续按群分开调试。

## 4.0.9

- LoRA 搜索默认来源改为 `civitai,huggingface,civitai_red`，普通搜索会同时包含 Civitai Red（R18+）结果；red 来源条目继续显示 `Civitai Red (R18+)` 和 `Tag: R18+`。
- 修复短中文角色别名误匹配：2 字 CJK 触发词需要安全边界，避免 `眼神里` 误命中 `神里` 这类普通描述文本。

## 4.0.8

- 新增 `civitai.red` / Civitai Red（R18+）LoRA 来源，支持 `/jh 找lora red <关键词>`、`/jh 找lora r18 <关键词>` 和 `civitai.red` 链接导入/预览。
- `civitai_red` 搜索结果、选择导入和来源展示会标出 `Civitai Red (R18+)`，单条搜索结果额外显示 `Tag: R18+`。
- Civitai 客户端改为站点可配置，`.red` 来源会使用自己的 API、搜索页、下载兜底 URL 和 Referer，不再绕回 `civitai.com`。

## 4.0.7

- 替换内置酒狐水印资产为新的透明扣图版本。
- 新增输出配置 `watermark_size`（水印大小），默认 `0.24`，适配新的方形酒狐水印；旧的 `watermark_width_ratio` 配置继续兼容。

## 4.0.6

- 新增统一输出水印：ComfyUI 绘图和 GPT 魔法生图在保存/发送前都会叠加“酒狐绘制”透明水印，默认使用左下角白金样式。
- 新增内置水印资产 `assets/jiuhu_watermark_huizhi.png`，并支持通过 `output_settings.watermark` 调整路径、位置、大小和透明度。
- GPT 魔法缓存命中会识别已加水印的缓存图，避免重复叠加；旧缓存命中会给本次发送的 history 副本补水印。

## 4.0.5

- 修复 PVC 画风工作流默认 LoRA 被 Illustrious 家族守卫误判为不兼容的问题；PVC、3d_style、AnimeFigure_ANIMA、XDetail_light 会作为 PVC 预设的受信任工作流 LoRA 保留。
- 修复已导入角色 LoRA 在前置检查放行后，又被 `set_lora()` 的运行时配置刷新二次拦截的问题，避免角色 LoRA 显示匹配但实际未启用。
- LoRA 文件名匹配只剥真实模型后缀，不再把 `v1.0-xl` 这类版本号里的点误当成扩展名截断。

## 4.0.4

- 修复 PVC 画风 4.0.3 过度平滑导致的糊图、重影、光晕和发丝融边：采样回到 Euler a / normal，latent 放大改为 `bicubic`，精修 denoise 降到 0.16。
- PVC 默认 prompt 移除容易制造虚化的景深/体积光词，改为干净棚拍、清晰轮廓和均衡高光；负面词补充 motion blur、ghosting、bloom、duplicate edges 等糊化信号。
- 启动复制逻辑会覆盖仍使用 `bislerp` 或 `dpmpp_2m_sde` 的旧 PVC 工作流，避免 data 目录继续跑 4.0.3 参数。

## 4.0.3

- 优化 PVC 画风稳定性：latent 放大从 `nearest-exact` 改为 `bislerp`，减少长发边缘锯齿、三角缺口和断裂毛刺。
- PVC 工作流改为 960x1440、DPM++ 2M SDE Karras、46+24 步双段精修，并降低过锐化的 3D/XDetail 叠加强度，增强角色保真和头发轮廓稳定性。
- 启动时会自动覆盖旧版 `finish_illustrious_pvc_api.json`，避免 data 目录继续沿用 4.0.2 的旧参数。

## 4.0.2

- 新增 Illustrious 第三个内置画风 `pvc` / `PVC画风`：使用 WAI Illustrious、专用 `finish_illustrious_pvc_api.json` 工作流、PVC/3D/AnimeFigure/XDetail 画风 LoRA 链和双段 latent 精修，角色 LoRA 仍由节点 99 独立注入。
- `/jh 切换画风 3` 现在切到 PVC 画风，`/jh 切换画风 4` 为随机画风；帮助文案同步加入 `pvc`。

## 4.0.1

- 版本号切换到 `4.0.X` 线，后续按小版本递增。
- 新增管理员命令 `酒狐画图强制更新`：备份当前插件源码后从远端临时 clone/zip 覆盖更新，并跳过“目标版本号相同就不更新”的 noop 判断。

## 3.2.20

- 彻底关闭 ComfyUI 文生图 LLM tool 注册和启动激活，避免普通聊天被 LLM 工具自动触发绘图；ComfyUI 绘图入口限定为 `酒狐画图 <提示词>`。

## 3.2.19

- `/jh 切换画风 2` 会正确进入 Illustrious 画风预设切换，不再被未知 `/jh` 兜底当作绘图提示词。
- 普通绘图入口收敛为 `酒狐画图 <提示词>`；移除 `jh画图`、`jh绘图`、`酒狐来点`、`/画图`、`画图no` 等旧生图触发。
- `/jh help` 改为展示 `/jh 角色列表`，角色列表会按当前模型族列出可直接画的角色、可叫别名和触发词，并隐藏禁用/身份冲突档案。

## 3.2.18

- 修复 LoRA 档案身份冲突误判：中文角色名与罗马音/英文触发词证据不再因为别名里夹带来源英文而被误判为不一致，`activation_tags` 能与 LoRA 标题/文件证据对上时会直接放行。
- 自动身份冲突禁用的档案会重新走当前冲突逻辑复核，不再永久沿用旧的 `disabled_reason`；导入时也改为使用真实 profile key 做冲突检查。
- `/jh 重置lora 全部` 现在会同步清理 LoRA 档案运行时缓存、重复词缓存、冲突提示缓存并刷新 API 家族 guard；没有可移动项目时不再创建空备份目录。
- Civitai 导入建档保留“搜索词和角色名不同”的用户叫法，避免用户用原搜索词再次绘图时匹配不到。

## 3.2.17

- Civitai CJK LoRA 搜索改为调用网页前端实际使用的公开 Meilisearch 后端，不再只解析空的 `__NEXT_DATA__` 页面数据。
- 网页搜索会动态发现当前 Civitai 前端包里的 `NEXT_PUBLIC_SEARCH_HOST` / client key，并保留公开端点兜底；中文搜索使用 `matchingStrategy=all`，减少单字拆分导致的泛热门和跨作品误命中。
- Meilisearch 命中后会按模型 ID 补拉 REST 详情，继续输出原有搜索结果结构、预览图、版本号、触发词和热度数据，页面数据解析仅作为最后兜底。

## 3.2.16

- Civitai CJK LoRA 搜索新增网页全文搜索优先路径：中文/日文/韩文关键词会先请求公开搜索页面并解析页面数据，解决 REST API 对 CJK 单字拆分导致的低相关结果问题。
- 网页搜索结果会归一化为原有 `search_loras` 返回格式，继续复用现有 base model 过滤、候选排序和导入流程。
- 网页搜索请求失败或页面结构解析不到结果时自动回退 REST API，不影响英文/罗马音搜索和现有稳定性。

## 3.2.15

- LoRA 来源/IP 映射派生结构改为统一签名缓存：来源 key、标题 marker、匹配 entries 和预编译正则只在 JSON 文件变化时重建。
- 高频 normalize 方法加入 LRU 缓存，并在插件卸载时清理，降低巨量映射搜索时的重复正则开销。
- 候选来源校验复用同一份 haystack，保留“同源加分、冲突跳过、未知降权”的准确性策略，同时减少候选热路径重复计算。
- 测试模式日志去重表增加过期清理，避免长期运行时无限增长；Civitai 导入侧的来源 marker set 也加入签名缓存。

## 3.2.14

- LoRA 搜索来源/IP 映射读取支持重复 key 合并，避免巨量手工映射中后出现的同名来源覆盖前面的别名。
- 来源识别改为主名和别名双向匹配，并对 `RE` / `BA` 这类 1-2 位英文短别名做严格降噪，避免短词误伤。
- 用户指定来源时会生成来源约束搜索词，并在候选排序中优先同来源结果、跳过明确冲突来源结果，减少跨作品同名角色/标题被顶上来。
- LLM 搜索规划和最终 rescue 只接收当前 query 命中的来源别名上下文，不再把整张巨量映射表塞进提示词。
- 本地档案的来源上下文识别也复用 `lora_source_aliases.json`，新增 IP 后搜索、标题解析和档案补全会走同一份表。

## 3.2.13

- 将 LoRA 搜索的作品/IP 来源别名拆到 `lora_source_aliases.json`，支持插件默认表和数据目录同名文件追加维护；导入建档标题解析也复用同一份来源表。
- LoRA 搜索新增 `异环 / NTE / Neverness to Everness` 来源映射，并避免把 `Duet Night Abyss` 错绑到异环。
- 用户原始 CJK 搜索词直接命中候选标题时会保留更高相关度并参与排序；最终 relevance 重算改为取最大值，避免确定的中文标题命中被 LLM 英文扩展覆盖。
- 常规搜索、兜底搜索都无强相关结果时，会调用 LLM 生成最后一轮保守补救关键词，并把补救关键词发到测试模式日志。

## 3.2.12

- 修复 `卡提西亚` / `卡忒希亚` 这类中文音译差异导致的 LoRA 档案匹配失败：LLM 高置信命中且命中文本确实出现在用户原文时，会用 CJK 首尾字一致的音译兜底做二次放行。
- LoRA 档案 LLM 导入提示现在会保留常见中文音译变体，并刷新档案解析缓存版本，减少同一角色不同译名漏匹配。
- LoRA 搜索规划不再鼓励不确定时瞎猜拼音；新增 `弗洛洛/弗诺诺 -> Phrolova` 搜索兜底，并强化搜索无结果时的英文名/链接导入提示。

## 3.2.11

- 修复 LoRA 已按当前模型族导入建档后，生成前仍可能被 API 文件名 guard 误判为不兼容并跳过的问题；当前族档案现在作为运行时兼容判断的信任锚点。
- 导入 LoRA 保存角色档案后立即刷新 API 模型族 guard 数据，避免刚导入马上画时仍使用旧的 `profile_keys`。
- 自动匹配 LoRA 时会在前置检查前刷新 guard，并在 API guard 拒绝后用本地当前族档案做二次校验，保留不兼容 LoRA 的拦截能力。

## 3.2.10

- 移除翻译后 `_remove_translated_profile_identity_conflicts` 正则清理链路，不再在扁平 Prompt 上猜测哪些 tag 是角色身份；改为收紧 LoRA 翻译保护提示，让翻译阶段避免替换/新增角色身份。
- 移除画图阶段 `_backfill_search_alias_from_usage` 别名反补，避免把用户画面描述误写进 `search_aliases`；别名仍只在搜索/导入阶段沉淀。
- LoRA 自动匹配在调用 `api.set_lora()` 前增加当前模型族兼容检查，不兼容时主动跳过角色 LoRA 和角色触发词，避免交给 ComfyUI API 事后拒绝。

## 3.2.9

- LoRA 搜索增加非标准中文译名兜底：`爱弥斯/艾弥斯` 会直接扩展到 `Aemeath Wuthering Waves` / `Aemeath`，并刷新 LLM 搜索规划缓存版本，减少拼音乱猜。
- `/jh 找lora` 会利用本地角色档案的 `search_aliases` 反推出官方/英文角色搜索词；画图成功命中角色后，也会从用户中文原话里保守抽取角色名并补写进档案搜索别名。
- 翻译 Prompt 的身份冲突清理增加原文意图追溯，用户明确说的全裸、半身、动作、丝袜、泳装等细节不再因为“看起来像身份词”被误删。

## 3.2.8

- 优化 LoRA 多角色合集判断：单角色的多套服装/外观词不再轻易误判成多角色包，角色路由只参考用户原始命中文字，避免 LLM 翻译出的服装词污染触发词选择。
- 结构化 LoRA 档案在用户明确要求换装、裸露或身体状态时，不再强行注入默认服装，减少“面向结果化”覆盖用户需求。
- 生图结果合并转发记录会先追加发起者的生成请求节点，并使用发出指令者 QQ 作为节点 uin，同时在内容里标注 QQ 号，方便聊天记录对应真人。

## 3.2.7

- 新增群测试模式：管理员在群内发送 `jh测试模式 开` / `jh debug on` 后，会把绘图、LoRA 搜索/导入、自更新等关键流程节点发送回当前群。
- 测试模式会转发插件关键日志，并对 API key、token、cookie、Authorization、GitHub/HF/OpenAI 样式密钥和带认证信息的 URL 做脱敏。
- 支持 `jh测试模式 状态/关`、`jh debug status/off` 和 `酒狐测试模式 开/关/状态`，状态会持久化到插件 data 目录；AstrBot 核心白名单仍需先放行该群。

## 3.2.6

- 修复群聊白名单默认空列表会拦截所有群的问题；现在留空表示不限制群聊，配置了群号时才拦截未授权群。
- 白名单群号和群策略 key 增加统一规范化，兼容逗号/空白分隔、全角数字、事件对象不同字段返回的群号。
- LoRA 档案导入改为 `activation_tags`、`identity_appearance`、`default_outfit` 三类结构化标签，生成时把冲突处理交给最终 LLM，减少代码按结果硬判换装/外观。
- LoRA 搜索词扩展增强官方罗马音和作品来源识别，例如中文角色名会优先扩展成“角色罗马音 + 来源作品”的搜索短语。

## 3.2.5

- Civitai 导入建档时把标题解析出的角色名、最终角色名和可信触发词补进 `search_aliases`，避免用作品名搜索后选中角色却无法用角色名再次命中。
- 导入同一角色的新 LoRA 文件时自动清理同名旧档案，并同步删除旧文件对应的别名和角色词映射，减少死档案抢匹配。
- LoRA smart 角色词注入降噪：默认只补核心身份词和少量必要外观；用户明确要求换装或动作/姿势时不再追加档案外观/服装兜底，降低覆盖用户需求的概率。

## 3.2.4

- 收紧 Civitai `trainedWords` 写入 `search_aliases` 的条件，外貌词、服装词、画质/场景词和泛词不再作为角色搜索别名参与自动匹配。
- 收窄多角色合集判断里的 `collection` 关键词，只把明确的 character/char/LoRA collection 当作多角色包信号，减少单角色套装合集误判。

## 3.2.3

- 修复 LLM 翻译后的动作/物品短语被当成角色身份词误删的问题，保留用户明确要求的姿势、持物、场景细节。
- 多角色合集 LoRA 会根据本次命中的角色别名路由到对应触发词，避免画子角色时继续注入合集里的默认首个触发词。
- 重新导入已存在 LoRA 时合并旧档案的别名、搜索别名和触发词；运行时也不再覆盖已有 `search_aliases`。
- LoRA 搜索遇到 Civitai 408/429/overloaded 时返回可读的拥堵提示，不再把原始 JSON 错误直接丢给用户。

## 3.2.2

- 强化 LoRA 搜索稳定性：Civitai 请求改为 5 次指数退避和抖动重试，搜索并发加限流，降低 408/429 高峰失败率。
- 补强中文搜索兜底和相关度排序：增加赛博朋克/可塑性记忆等来源映射，本地 source+角色组合扩展，CJK 完整词匹配优先并提升标题精确命中权重。
- 收紧搜索污染和档案匹配：发现词只从标题候选提取，2 字 CJK search alias 可参与命中，多角色合集保留搜索别名，带来源括号的角色名不再误判身份冲突。
- LLM 搜索扩展 token 上限提高并加入缓存淘汰，网络异常包装为明确错误，避免缓存无界增长和字符串异常。

## 3.2.1

- 重写 LoRA 角色自动匹配算法：统一显式别名、官方别名、昵称别名和可信搜索别名的评分流程，强身份词优先，弱别名不再压过明确角色名。
- 新增适度模糊匹配：中文/日文允许少量字形或译名差异，英文和罗马音要求更高相似度，减少漏匹配但避免靠性别、画风、作品名、发色眼色等泛词乱猜。
- LLM LoRA 匹配降级为兜底校验：返回的 `matched_text` 必须出现在用户原文中，并通过同一套本地档案词评分后才会启用 LoRA。
- Civitai 导入优先使用官方 `trainedWords`/页面 Trigger Words：已有可靠触发词时不再让 LLM 或文件名兜底追加 `name (source)`，没有触发词时只从页面/简介提取触发关键词。
- CJK LoRA 标题解析补充 `／`、`/`、`⁄` 分隔符，档案 LLM 会接收预拆分标题变体，同时过滤低信息搜索扩展词，减少中文别名漏建和无关搜索污染。
- 补充常见作品来源映射，收紧 Hugging Face 搜索的 base model 标记兜底；LLM 超时/空结果会明确降级到本地数据，LoRA 列表和角色档案读取增加短缓存减轻重复请求。
- 保留现有跨模型族切换、Prompt 翻译、最终 Prompt 整理和 ComfyUI 注入流程，只替换角色档案命中决策，降低串线和误套 LoRA 的概率。

## 3.1.14

- 恢复 LoRA 搜索结果为原来的合并转发/聊天记录样式，继续带预览图；撤回 3.1.13 中把搜索结果改成纯文本主回复的展示改动。
- 保留 3.1.13 的二跳别名发现逻辑，只修搜索命中率，不再改搜索结果展示形态。

## 3.1.13

- LoRA 搜索结果改为可靠的纯文本主回复，避免合并转发/节点消息在部分 AstrBot/OneBot 链路里显示为空，导致用户侧像“没反应”。
- 搜索增加通用二跳别名发现：原词搜索拿到低相关或模型族不兼容候选时，会从标题、版本名、触发词里提取角色英文名和作品名，再追加搜索当前模型族可用 LoRA。

## 3.1.12

- LoRA 档案改成分层证据匹配：角色名/标题别名仍是强匹配，搜索词只作为经过过滤的弱别名，不再直接改写角色名。
- 修复“匹配和注入都成功但角色不对”的问题：导入时保留 Civitai API/页面原始触发词，LLM 档案解析不会再把 `角色名 (作品名)` 这类关键触发词压缩成裸角色名。
- Prompt 合成会在旧档案缺少可靠触发词时补充通用的作品限定触发词，例如 `name \(source\)`，避免只追加一个英文名导致 LoRA 不起角色特征。
- 标题解析支持并列中/日文角色名，中文搜索名会作为弱别名参与匹配；同时保留风格词、作品名、外貌词过滤，降低误匹配。

## 3.1.11

- 修复 LLM 已正确识别角色但 LoRA 仍未启用的问题：高置信 LLM 匹配现在会用用户原文命中片段和档案别名做近似校验，允许 `西格里卡` / `西格莉卡` 这类一字差异通过。
- 防乱猜逻辑仍保留：只有用户原文确实出现了 LLM 返回的 `matched_text`，且它和档案强匹配词足够接近时才会启用 LoRA；作品名、外貌词、风格词不会因此放行。

## 3.1.10

- LoRA 搜索顺序改为“先搜用户原词和本地拆词，再按需要调用轻量 LLM 扩展”，避免一上来就用半截/错误别名覆盖原始关键词。
- 轻量 LLM 搜索扩展增加日文取向：要求补日文官方标题、假名、罗马音和英文 localized title，并清理不完整括号，避免 `Frieren (Sousou no Frieren` 这类坏关键词。
- 搜索排序降低后续扩展词的过重惩罚，Civitai 结果不会因为是第二轮关键词就被 Hugging Face 零热度结果强压。
- 搜索候选过滤放宽为“强相关优先，完全没强相关时保留低置信候选”，减少短角色名搜不到任何东西的情况；导入和生成阶段仍按严格档案逻辑处理。

## 3.1.9

- 收紧 LoRA 导入建档清洗：Civitai 页面里的 `civitai:id@version`、问卷文案、作者名、`AIR`、版本/页面垃圾不再进入触发词。
- 外貌词和服装词会分别写入 `appearance_prompt` / `default_outfit_prompt`，不再把 `grey hair`、`crossed bangs`、`white camisole` 这类词当角色触发词或可触发称呼。
- 标题解析会剥离 `[4 outfits]`、`Illu v1.0`、作品/模型族后缀，优先建成角色本名，例如 `Elaina`，避免把作品名或版本说明写进档案。
- 新搜索开始时会清掉旧 LoRA 搜索会话，搜索结果构建完成后才开放序号选择，减少“结果还没发出来就回 1 选到旧结果/未展示结果”的情况。

## 3.1.8

- 新增最终 Prompt 整理链路：用户需求、LoRA 触发词、角色档案词、画风前缀全部拼接后，再调用主模型统一整理一次最终 tags。
- 最终整理会把用户原话作为最高优先级，并显式保护 LoRA 触发词；例如用户要求“全身像”时，会移除 `upper body`、`portrait`、`close-up`、`cropped` 等冲突构图词并保留 `full body`。
- 增加代码级构图互斥兜底，即使最终整理模型不可用或输出又带回冲突词，也会在提交 ComfyUI 前再清理一遍。
- WebUI 新增 `enable_final_prompt_optimize` 和 `final_prompt_optimize_max_tokens`，可按成本需要关闭或调低最终整理调用。

## 3.1.7

- LoRA 搜索扩展不再把短中文名只压成一个拼音词：轻量 LLM 需要给出 3-6 个候选，插件也会自动补 `Mo Ning` / `MoNing` / `Moning` 这类空格与紧凑写法。
- 中文/日文查询在 LLM 结果较少时会保留原始中文词作为兜底，不再出现“已尝试：Mo Ning”这种只搜一个词就结束的情况。
- 首轮搜索没有候选时会追加二阶段兜底扩搜，并把实际尝试过的关键词完整显示出来，方便判断是搜索词问题还是源站确实没有结果。

## 3.1.6

- LoRA 搜索词扩展增加“作品名不可单独搜索”的通用过滤：LLM 返回 `Wuthering Waves`、`Blue Archive` 这类来源词时，只会和角色名组合成 `Verina Wuthering Waves`，不再把作品名单独打到 Civitai/Hugging Face。
- 中文/日文角色查询在没有本地拆词兜底时，会优先等待轻量 LLM 产出官方名/罗马音后再搜索，减少直接拿中文自然语言请求 Civitai 导致的 408/过载。
- Civitai/Hugging Face API 对 408、429、5xx 和短暂网络抖动增加退避重试，避免服务临时过载或代理抖动时一次失败就中断。
- LoRA 档案 LLM 提取改成短 payload + 短 JSON，只提取角色名、中文/英文别名、触发词和稳定外观词，降低半截 JSON 导致角色名为空的概率。
- 修复身份冲突判断误伤：`roccia (wuthering waves)` 这类“角色名带作品后缀”的档案不再自己和自己判冲突；随机哈希文件名也不再作为强身份/触发兜底。
- 冲突档案 warning 加入短时间去重，避免一次生成链路里同一个坏档案刷多条日志。

## 3.1.5

- LoRA 建档标题解析更严格：`作品_角色｜中文作品_中文角色` 会优先抽取角色本名，并把中文角色名写入可匹配别名，避免把 `Wuthering Waves_Denia` 当成角色档案主名。
- LoRA 大白话匹配收紧：纯数字、版本号、模型族缩写、过短英文和泛词不再作为显式命中词；LLM 候选也只发送强匹配词，减少乱猜。
- `/jh 画风 1` 切回默认 Illustrious 时，如果 checkpoint 还停在 PerfectDeliberate，会同步切回默认 Illustrious checkpoint，避免下一张继续触发超写实预设。

## 3.1.4

- Civitai 内置多线程下载增加分片级重试：单个 Range 分片遇到 `ContentLengthError`、代理 TLS 重置、短暂断连等异常时会自动重试，避免一个分片抖动导致整次 LoRA 导入失败。
- 多线程下载最终失败时会自动回退到单线程下载，并清理遗留分片目录；401 登录态错误仍会直接提示配置 Civitai Cookie/Token。

## 3.1.3

- LoRA 搜索的轻量模型 JSON 调用增加自动重试：首次返回半截 JSON 或解析失败时，会用更严格的 minified JSON 提示和更高 token 上限重试，减少 `{"alias` 这类截断导致的空扩展。
- 搜索前新增本地关键词净化兜底，会把“游戏鸣潮里面的椿”这类自然语言拆成“椿 鸣潮 / 椿 Wuthering Waves / 椿 Wuwa”等搜索种子，LLM 偶发失败时也不会只搜“游戏”噪声。
- LoRA 搜索会并行搜索原始词、本地净化词和 LLM 扩展词，搜索会话也保留最多 5 个已搜关键词。

## 3.1.2

- 修复 LoRA 搜索词 LLM 扩展缓存会复用空结果的问题：空/无效扩展不再写入缓存，命中到旧空缓存会立即丢弃并重新调用轻量模型。
- LLM 返回的 `alias` 现在会纳入实际搜索词，避免只拿中文原词搜索；搜索日志会显示缓存扩展的具体词，方便确认是否真的生效。

## 3.1.1

- LLM 配置拆分为“主模型”和“轻量任务模型”：主模型只负责提示词生成/转译，LoRA 搜索别名转译、角色档案 JSON 提取、语义匹配等低成本任务改走轻量模型。
- LoRA 搜索现在会先用轻量模型把中文名、昵称、罗马音、官方英文名和作品名组合扩展成搜索词，再并行搜索 Civitai/Hugging Face，避免只拿中文关键词硬搜。
- 新增 LoRA 搜索转译缓存，和既有档案提取缓存、匹配缓存一起减少重复 token 消耗；轻量模型配置留空时会兼容复用主模型。

## 3.1.0

- LoRA 搜索新增 Hugging Face 来源：沿用 Civitai 的搜索词扩展、模型族过滤、综合评分、序号导入和角色档案建档逻辑；支持 `huggingface_api_token` 下载私有/gated 仓库。
- 新增 `civitai_settings.search_sources`，默认 `civitai,huggingface`；命令可用 `/jh 找lora hf <关键词>` 只搜 Hugging Face。
- 预留 Yodayo 来源和 `yodayo_cookie` 配置；由于 Yodayo 当前没有公开稳定 LoRA 下载 API 且网页有 Cloudflare 防护，默认不启用，启用后会给出明确原因而不是伪装可导入。

## 3.0.1

- 修复旧角色档案命中但 LoRA 已不可用时，结果里仍显示“角色档案：xxx”的误导问题；现在会明确提示已跳过失效档案，不再把它当成有效匹配展示。

## 3.0.0

- `/jh 自动优化` 改为 LoRA 重置链路，新增推荐入口 `/jh 重置lora`：默认只预览，`/jh 重置lora apply` 会清空角色档案、别名、角色词、Civitai/LLM LoRA 缓存、预览图缓存、最近搜索结果和默认角色/画风 LoRA 配置。
- `/jh 重置lora 全部` 或 `/jh 重置lora apply files` 会在清状态的同时把 `civitai_settings.lora_download_dir` 下的 LoRA 模型文件移入重置备份目录，方便彻底删掉重来；checkpoint、工作流和输出图片不会动。
- LoRA 导入建档不再把搜索词派生别名写入角色档案；搜索词只作为 `search_query/search_terms` 记录，角色身份优先来自 Civitai 标题、触发词、文件名和 LLM 页面证据。
- 导入时如果 LLM 抽到的角色名等于搜索词，会自动回退到页面标题/版本标题/触发词/文件名证据；触发词为空时会在导入阶段补一个保守触发词，减少“LoRA 对了但角色词没进 Prompt”。
- help 文案改为重置口径，保留 `/jh自动优化应用` 兼容入口但实际执行重置。

## 2.2.12

- 修复手动把 Illustrious checkpoint 选成 `perfectdeliberate_v90.safetensors` 时仍按默认插画预设出图的问题：现在 checkpoint 本身会自动识别并启用「超写实画风（PerfectDeliberate）」参数、负面词、尺寸和 LoRA 强度上限。
- LoRA 角色档案命中后，Prompt 转译会携带短 profile 保护提示，要求 LLM 保留档案触发词，避免把中文角色名翻成另一个作品角色。
- 生成前会按档案证据清理 LLM 翻译出来的可疑角色身份词，只保留 LoRA 文件名、Civitai 标题、触发词和档案别名能支撑的身份词，减少“LoRA 对了但角色名错了”的情况。
- 触发词缺失时的文件名兜底更保守，`角色-IL-v1` 这类文件名会优先取角色片段，不再优先把整串版本/模型族后缀写进 Prompt。
- 生成结束后清理 Illustrious 运行时 checkpoint 状态，避免上一次任务解析到的 checkpoint 影响下一次任务。

## 2.2.11

- 修复“匹配到 LoRA 但没有触发词进 Prompt”的问题：角色档案触发词缺失时，会用 LoRA 文件名和 Civitai 身份证据做保守兜底，例如 `VerinaWW.safetensors` 会补 `VerinaWW`。
- 增加 LoRA 档案身份冲突护栏：档案角色名、别名与 LoRA 文件名/Civitai 标题证据明显对不上时，生成时会跳过该档案，避免“伊蕾娜档案套到 Victorica LoRA”这类错配继续出图。
- `/jh 自动优化 apply` 会把身份冲突档案标记为禁用，并为触发词缺失档案补充保守触发词；重新导入时若证据冲突也会提示禁用。
- 啊😲？云朵☁，哒↘哒↗哒↘哒↗哒↘，好想玩原神😨，云☁原神😙，当当当当当😊，看精彩纷纷👍🎊😆，云☁原神😄，呜呜呜呜呜，好想玩原神😭😭😭云☁原神，朋友已就位😊😃😆，一起玩原神，云☁原神！啊啊啊啊啊😙，好想玩原神😙云☁原神，哈哈哈哈哈🤣🤣🤣，一起玩原神，云☁ 原神，好好好想，🤩想玩玩原神😋网页云端，低功耗不失真😌， WiFi网线🥰，都可以60帧😍，来来来来👏，进入云☁原神

## 2.2.10

- PerfectDeliberate 超写实预设改成更贴近官方样张的 Euler a / normal、34 steps、CFG 6.8，并把隐式 latent 放大固定为 1.0，避免最终图从 960x1440 又被放大到 1152x1728 后跑味。
- 超写实预设会限制角色 LoRA 注入强度上限，防止 Illustrious 角色 LoRA 把 PerfectDeliberate checkpoint 的半写实/3D 质感拽回扁平二次元。
- 结果文案区分真实生效 LoRA 和已旁路的占位 LoRA，避免“节点里残留 Anima LoRA 名字”被误读为正在混用。
- Prompt 清理扩大到 `anime`、`illustration`、`digital painting`、`lineart` 等冲突词，并强化高端 3D CG、皮肤质感、电影光和景深提示。

## 2.2.9

- 收紧 PerfectDeliberate 超写实预设：默认改为 960x1440、40 steps、CFG 7.0，并降低二采 denoise，减少画面被磨回普通二次元插画。
- PerfectDeliberate 预设使用独立半写实/3D 渲染 Prompt 转译提示词，不再沿用 Illustrious anime illustration 转译口径。
- 超写实预设生成前会清理 `anime illustration`、`cel shading`、`flat 2d anime` 等冲突风格词，同时强化皮肤、眼睛、头发高光、电影光和景深词。

## 2.2.8

- 新增 Illustrious 内置画风预设：`超写实画风（PerfectDeliberate）` 会自动切到 `perfectdeliberate_v90.safetensors`，并套用 DPM++ 2M Karras、832x1216、32 steps、CFG 6.5 的稳出图参数。
- 新增 `random` 随机画风预设；可用 `/jh 画风 <默认|超写实|随机>` 或 WebUI 的 `model_family_settings.illustrious.style_preset` 切换。
- 画风预设会同时覆盖正负面词前缀、checkpoint、采样器、scheduler、步数、CFG 和 latent 尺寸，不再只是单纯换模型文件。
- 生成结果会显示当前内置画风和关键参数，方便确认是否真的走了 PerfectDeliberate 预设。

## 2.2.7

- 当前模型族切换或插件启动时会自动清理跨模型族的默认角色/画风 LoRA，避免旧 WebUI 配置里的 Anima LoRA 继续进入 Illustrious 生成链路。
- ComfyUI API 生成前增加最终兜底：命令临时 LoRA 或 WebUI 默认 LoRA 与当前模型族不兼容时，本次直接按未启用处理。
- 中和 LoRA 节点时优先选当前模型族兼容的占位 LoRA，减少“强度为 0 但显示跨模型族 LoRA 名”的误导日志。
- LLM JSON 空响应或明显截断响应降级为静默无结果，避免可选 LoRA 匹配失败刷 warning。

## 2.2.6

- WebUI 为 Illustrious `checkpoint_name` 增加主模型 checkpoint 候选列表，优先读取 ComfyUI API 缓存和本地 checkpoints 目录；PerfectDeliberate 这类 checkpoint 不再被误导到 LoRA 设置里找。
- 配置说明明确区分 checkpoint、默认角色 LoRA、默认画风 LoRA，并在当前配置跨模型族或不像画风 LoRA 时给出提示。
- 默认画风 LoRA 下拉改为只显示未绑定角色档案、当前模型族兼容且文件名带风格/质量标记的 LoRA，减少角色 LoRA 混进画风设置。
- 运行时也会拒绝把已建档角色 LoRA 或明显不像风格/质量的 LoRA 当画风 LoRA 注入，避免旧配置残留继续串用。

## 2.2.5

- 酒狐审核多图结果改为聚合成一条回复，并按文件内容去重，避免同一张引用图被解析成两张时重复回复。
- 酒狐审核每次执行前实时刷新 `image_audit_settings`，阈值支持 `0.85`、`85`、`85%` 三种写法，状态页会显示当前阈值。
- OpenNSFW2 兜底改按二分类显示“正常 / NSFW”，不再伪装成五分类的“色情”，避免绘画、二次元色情、性感永远 0 造成误解。
- 五分类模型仍使用二次元色情、色情、性感、显性和综合阈值；二分类兜底单独使用 `fail_binary_nsfw`。

## 2.2.4

- 再收紧 Anima / Illustrious LoRA 串用护栏：工作流里残留的跨模型族 LoRA 只要强度生效，会在提交前自动中和并旁路。
- 角色 LoRA 注入只挑角色节点，避免 `apply_all_lora_nodes` 或空节点配置把角色 LoRA 写进固定画风、Control、LLLite、Union 等工具节点。
- 画风 LoRA 默认要求文件名能识别当前模型族；未标注模型族的画风 LoRA 需要手动开启 `allow_unmarked_style_lora` 才会显示和注入。
- 画风节点 ID 与角色节点 ID 相同时会忽略画风节点配置，避免把画风 LoRA 覆盖到角色节点 99。

## 2.2.3

- 修复 Illustrious/Anima 模型族下默认或临时 LoRA 混用：生成前按当前模型族护栏检查，不兼容的角色 LoRA 会被中和并旁路。
- 新增后台 WebUI「默认画风 LoRA」配置，和角色 LoRA 分开注入；Anima 可接已有固定画风节点，Illustrious 没有节点时会运行时动态接入。
- 生成结果会分别显示角色 LoRA 和画风 LoRA，配置页也把 `default_lora` 标成“默认角色 LoRA”，避免再把画风/质量 LoRA 塞进角色档案。
- 明确 PerfectDeliberate 是 Illustrious checkpoint，不按 LoRA 注入；要用它应放到 checkpoints 并作为 Illustrious 模型切换。

## 2.2.2

- 修复并发生成时 LoRA、模型族、负面词和工作流运行态互相串线的问题：生成、临时切换和删除角色现在共用生成状态锁排队。
- 修复 Civitai `trainedWords` 把多个触发词塞进一个逗号字符串时被整串长度过滤掉的问题；已存在文件重新导入也会刷新触发词档案。
- 多图链路改为每张图生成前再做 LoRA 档案匹配和触发词注入，避免提前改全局状态。
- `/jh 自动优化` 增加触发词缺失体检，能用档案证据或 LoRA 文件名给旧档案补一个保守触发词。

## 2.2.1

- 重做 LoRA 角色档案建档优先级：角色名来自 Civitai 标题、页面证据、触发词和 LLM 提取结果，搜索词只记录为查询来源，不再直接写成角色身份。
- LoRA 自动匹配改用 `match_terms`、官方别名和无冲突昵称，并给 LLM 匹配加缓存和短候选列表，减少误匹配和 token 浪费。
- 新增 `/jh 自动优化` 预览旧档案体检结果，`/jh自动优化应用` 会先备份再修复档案版本、泛别名、匹配词和可见性提示。
- 统一 `/jh` 紧凑指令解析，支持 `/jhlora切换2`、`/jh模型切换2`、`/jh找loraVerina` 等无空格写法。
- `/jh help` 改为聊天记录消息；富媒体发送失败时回退提示改为通用文案。

## 2.2.0

- 只改光流图生图：长边 1024 -> 896、Depth 768 -> 640、采样 22 -> 18 步。
- ControlNet 生效区间提前收尾，OOM 兜底再降到 768 / Depth 512 / 14 步，减少 5 分钟超时 ✨

## 2.1.9

- 光流文生图采样风格退回原来的 Euler ancestral 味道，只把 base 步数加到 32、二采重绘降到 0.32，避免换采样器导致画风跑偏。
- 更新日志底部补插件署名 ✨

## 2.1.8

- LoRA 导入把“搜索词”和“角色名”拆开：泛词如“赛博朋克”不会再写成角色档案，角色名优先取 Civitai 标题/页面/触发词/LLM 档案。
- 光流文生图改稳画质慢一点：base 改高步数 DPM++ 2M SDE Karras，latent 放大降到 1.15，refine 只做 0.24 轻收尾，减少融线和局部化开 ✨

## 2.1.7

- LoRA 搜索结果改成一个聊天记录：标题、本地命中、全部候选和预览图都放同一个转发节点里。
- 光流文生图加强手部和局部细节：补手部负面词，二段 refine 改低重绘 DPM++ 2M Karras，减少手指被二采画散 ✨

## 2.1.6

- 光流图生图改低显存单采版：默认长边 1024、Depth 768，去掉 latent 放大和二采。
- Illustrious 图生图生成前会请求 ComfyUI 释放显存；遇到 CUDA OOM 会清理显存并用更保守参数自动重试一次 ✨

## 2.1.5

- 修复引用消息里发送 `酒狐来点...` 不触发绘图的问题。
- 普通入口现在会剥掉 `[引用消息]` / CQ reply 标记，只读本条消息文本，不拿被引用原文乱触发 ✨

## 2.1.4

- 更新记录图继续用 Playwright Chromium 本地截图，Chromium 秒退时会自动尝试补系统依赖。
- 新增 `self_update_settings.history_render_browser`，需要时可手动选 `chromium/firefox/webkit` ✨

## 2.1.3

- 更新记录图改为插件本地 Playwright/Chromium 截图优先，不再把 AstrBot 远端 `html_render` 当主路。
- 移除记录图外链随机背景，改纯 CSS 铺底；本地缺浏览器会自动尝试安装，失败才回 Pillow 兜底 ✨

## 2.1.2

- 参考 IL_Controlnet V2.6 重做 Illustrious/光流图生图主干：WAI、CLIP skip -2、Depth ControlNet、两段采样、Tiled VAE。
- 图生图参数注入改为 Illustrious 专用处理，不再把基础采样和二采 denoise 全部刷成同一个值 ✨

## 2.1.1

- 更新记录图背景改成整张长图连续铺底，避免 fixed 背景只盖住首屏。
- 字号整体加大，HTML 截图失败会重试一次，少退回旧兜底图 ✨

## 2.1.0

- 同步新的更新记录 HTML，长图背景和毛玻璃卡片样式再优化 (｡•̀ᴗ-)✧
- 后续版本按 2.1.0-2.1.9，满十进一到 2.2.0。

## 2.0.39

- 更新记录图先在本地渲染模板数据，再交给截图服务，保住你改的 HTML UI ✨
- 禁掉无用代码高亮脚本注入，截图更轻；Pillow 兜底补中文字体扫描，少出方块字 (｡•̀ᴗ-)✧

## 2.0.38

- 找到 HTML 记录图发不出去的可疑点：渲染服务会把返回图存成 `.jpg`，但之前请求的是 PNG。
- 现在 HTML 渲染改请求 JPEG，并发送前重写成标准 RGB JPEG，再不行才回退 Pillow (｡•̀ᴗ-)✧

## 2.0.37

- 聊天里的更新记录图强制恢复旧版 Pillow PNG 渲染，避开 `html_render` 截图导致的 QQ 富媒体上传失败。
- HTML 模板继续保留，后面改样式不丢 (｡•̀ᴗ-)✧

## 2.0.36

- 自更新结果恢复旧版发送链路：文字先普通发，记录图再用 `Image.fromFileSystem` 原图单独发。
- 保留新版记录图 UI，但不再把文字和图片塞进同一条消息 ✨

## 2.0.35

- 更新记录图发送前自动压成小 JPEG，减少 QQ 富媒体上传失败 (｡•́︿•̀｡)
- 图片还是发不出去时，会自动补发文字结果，不再静默吞掉。

## 2.0.34

- 自更新只发一条结果+一张记录图，不再单独刷热重载提示 (｡•̀ᴗ-)✧
- 没有新版本时直接提示“当前已经最新版本”。
- 更新记录图改成展示每版更新内容，少放仓库路径和 Git 噪音 ✨

## 2.0.33

- 更新记录图改为 HTML/CSS 模板，想改样式直接看 `templates/self_update_history.html`。
- HTML 渲染不可用时继续用 Pillow 兜底。

## 2.0.32

- `酒狐更新画图` 成功后自动重载插件。
- 新增 `self_update_settings.auto_reload_after_update` 开关。

## 2.0.31

- 精简自更新聊天提示，更新后自动发记录图。
- 记录图改成浅色毛玻璃卡片和日式细线风。

## 2.0.30

- Classic GitHub PATs (`ghp_...`) now use the git HTTPS clone/fetch path first via askpass, matching the recommended classic-token workflow.
- Fine-grained PATs (`github_pat_...`) keep the GitHub API zip-first fallback added in 2.0.29.

## 2.0.29

- For HTTPS token GitHub repos in zip-install mode, self-update now tries GitHub API zip download before `git clone`, avoiding misleading `Write access` output from git with fine-grained PATs.
- Private-repo permission failures now stop on the clearer API auth hint instead of appending the git clone failure first.

## 2.0.28

- Added a guard for another common self-update config mistake: if a GitHub PAT is pasted into `token_env`, it is treated as the HTTPS token instead of an environment-variable name.
- Improved private-repo self-update failures: GitHub 401/403/404 zip fallback responses now explain whether the container is missing a token or the token lacks repository read permissions.

## 2.0.27

- Self-update now reuses the plugin proxy: `self_update_settings.proxy` takes priority, otherwise `civitai_settings.proxy` or `model_download.proxy` is applied to both git HTTPS and Python zip downloads.
- Added GitHub zip download fallback after `git clone` or `git fetch` network failures, so Docker containers with flaky git/GnuTLS can still update through the configured proxy.
- `酒狐更新画图 状态` now shows which proxy the updater will use.

## 2.0.26

- Added zip-install fallback for `酒狐更新画图`: if the current plugin directory is not a git repository but `repo_url` is configured, the plugin clones the repo into plugin data, backs up the current source, and copies updated source files over the installed plugin.
- Preserves AstrBot config and plugin data during fallback updates; no reset or data deletion is performed.
- Added a small config guard for the common mistake of putting an SSH private-key path into `token_env`; it is treated as `ssh_key_path` when the dedicated SSH field is empty.

## 2.0.25

- Fixed `酒狐更新画图` not being caught as a plain group message.
- Routed self-update through the same multi-source plain-message fallback as `酒狐审核`, including `message_str`, raw message, and text components, and blocked LLM propagation for matched update commands.

## 2.0.24

- Added admin self-update command `酒狐更新画图`, using `git fetch` plus `git merge --ff-only` from the current plugin git repository.
- Added private-repo friendly auth options: existing SSH/credential-helper setup, read-only deploy key via `ssh_key_path`, or HTTPS PAT via `self_update_settings.token_env`.
- Added `酒狐更新画图 状态` and `酒狐更新画图 记录`; update attempts are saved in plugin data and history can be rendered as an image.
- Self-update refuses dirty tracked code by default and only updates the plugin worktree, leaving AstrBot config and plugin data untouched.

## 2.0.23

- Fixed cross-family LoRA auto-switching so it is temporary for the current ComfyUI generation only.
- Auto-switch notices now say `本次临时用 ...`; the selected model family is restored after success, failure, cooldown, or checkpoint preflight exits.

## 2.0.22

- Simplified `/jh help` so it shows the normal drawing, model, LoRA, audit, recall, and status paths without exposing every legacy subcommand.
- Added unified role deletion via `/jh 删除角色 <角色名|LoRA名>` and `jh删除角色 <角色名|LoRA名>`.
- Unified role deletion removes matching role profiles, aliases, character prompts, local LoRA files, and current/default LoRA selection so the role can be searched and imported again cleanly.

## 2.0.21

- Tightened plain-message triggers. Bare `来点`, `魔法`, or `审核` no longer trigger the plugin; full `酒狐来点`, `酒狐魔法`, `酒狐审核`, and `jh...` entries remain available.
- Changed `/jh 找lora` so local role-profile hits are shown as a note but no longer block Civitai search results.
- Added local role-profile management under `/jh lora 档案 列表` and `/jh lora 档案 删除 <角色名|LoRA名>`.

## 2.0.20

- Fixed local image-audit loading for GantMan/nsfw_model on newer TensorFlow/Keras stacks by trying `tensorflow_hub.KerasLayer` custom objects and `tf_keras` compatibility loading after `nsfw_detector`.
- Added OpenNSFW2 fallback. If the GantMan five-class model still cannot load, the plugin can run `opennsfw2` binary classification and map it into the existing Chinese result format.
- Added `requirements.txt` with `opennsfw2[keras3]` and expanded `酒狐审核状态` dependency reporting.

## 2.0.19

- Fixed standalone image-audit plain-message routing when AstrBot strips the wake word. `酒狐审核初始化` can now be received as `审核初始化` and still trigger correctly only when AstrBot marks the event as a wake message.
- Added multi-source plain-text detection for image audit commands: `message_str`, raw message, and text components are all checked.
- Tightened audit command boundaries so bare `审核` or unrelated words that merely start with `审核` do not trigger the plugin.

## 2.0.18

- Added explicit standalone image-audit management commands. `酒狐审核初始化` starts model download/deploy immediately, and `酒狐审核状态` reports model path, download progress, proxy, and `nsfw_detector`/TensorFlow dependency status.
- Added a TensorFlow-only fallback for local image audit. If `nsfw_detector` is missing but TensorFlow is installed, the plugin can load the GantMan MobileNet model directly and run the same five-class audit.
- Improved no-image audit replies to point users to the initialization and status commands.

## 2.0.17

- Added optional local model auto-deploy for standalone image audit. When `image_audit_settings.auto_download_model` is enabled, plugin initialization downloads the GantMan/nsfw_model zip with the built-in multi-thread downloader, supports proxy/split settings, extracts it, and auto-detects `.h5`, `.keras`, or SavedModel paths.
- Model auto-deploy intentionally does not run `pip install`; `nsfw_detector`/TensorFlow must still be provided by the AstrBot environment or container image.

## 2.0.16

- Fixed standalone image audit triggers in plain image messages. `酒狐审核`, `酒狐鉴黄`, `jh图片审核`, and `jh鉴黄` are now caught by the plain-message fallback path even when the platform does not route them through command decorators.

## 2.0.15

- Added standalone image audit commands: `酒狐审核`, `酒狐鉴黄`, `/jh 图片审核`, and `/jh 鉴黄`.
- The audit command supports quoted images and images attached to the same message, then reports five Chinese-labeled scores: 绘画、二次元色情、正常、色情、性感, plus pass/fail.
- Added optional backends for local `nsfw_detector` with the open GantMan/nsfw_model five-class model, or a self-hosted NSFWJS-compatible HTTP classifier.

## 2.0.14

- Added automatic model-family switching during ComfyUI generation when the active family has no explicit LoRA role-profile match but another family does.
- The auto switch sends a short notice and only uses explicit role names or aliases, not LLM guesses, to avoid accidental cross-family switches.

## 2.0.13

- Fixed model-family switch parsing so invalid input such as `/jh 模型 切换 l` no longer falls back to Anima.
- `/jh 模型 切换` now shows the model list directly and supports numbered switching with `1` for Anima and `2` for Illustrious.
- Repaired Illustrious img2img workflow handling by replacing stale bundled workflow copies and fixing invalid VAE links before sending prompts to ComfyUI.

## 2.0.12

- Fixed Civitai preview images taking down the whole LoRA search reply when Civitai's image CDN times out. Preview images are now downloaded to a local cache first; failed previews are skipped while text results remain sendable.
- Added per-user Civitai search tokens so an older in-flight search cannot overwrite a newer search session or mix search terms.
- Added local role-profile detection before web search: existing current-family roles return a direct "already installed" note, while cross-family hits are included as switch-family guidance when no current-family result is found.
- Hardened plain-message send paths so a failed result send reports a concise error instead of bubbling into AstrBot's pipeline.

## 2.0.11

- Added ComfyUI-only level-1 NSFW self-review. GPT Magic is untouched.
- Blocked ComfyUI results are no longer sent as raw images; the plugin sends the review warning and a password-protected single-image ZIP instead. The default password is `114514`.
- Added `comfy_nsfw_self_review` settings and `/jh 自审查` admin commands for enabling/disabling the review and changing the ZIP password.
- The archive writer is pure Python, so Docker/Linux installs do not need external `7z`, `rar`, or WinRAR binaries.

## 2.0.10

- Reworked Civitai LoRA search ranking. Results are now sorted by a transparent popularity formula: `(65*log10(hearts+1)+35*log10(downloads+1)) * (1+0.12*relevance)`.
- Raised the default Civitai search result count from 3 to 6 and kept search sessions/import selection in sync with the larger list.
- Added one safe preview image per LoRA search result inside the forwarded search message; NSFW, review-risk, minor, and POI previews are filtered out.
- Parallelized Civitai candidate collection across current-family base models and API sorts (`Most Downloaded` / `Highest Rated`), with LLM keyword fallback running in parallel when needed.
- Tightened Civitai search filtering for non-character utility LoRAs such as ControlNet/OpenPose/Depth/SAM/YOLO/LLLite helpers while preserving Anima/Illustrious base-model isolation.

## 2.0.9

- Rebuilt `finish_illustrious_api.json` from the provided `光流文生图.json` reference as a plugin-safe Illustrious txt2img flow: WAI checkpoint, CLIP skip -2, node contract 11/12/66/99, two-stage latent refine, and tiled VAE output.
- Added Illustrious checkpoint candidate detection so an available `waiIllustriousSDXL_v170.safetensors` is preferred while the official `Illustrious-XL-v2.0.safetensors` remains a fallback.
- Cleaned Civitai role triggers at import and prompt-append time so default clothing/shoes/uniform tags are not injected unless the user explicitly asks for the canon/default outfit.
- Hid default outfit tags from the LoRA profile block shown to the LLM, reducing accidental outfit locking during Chinese prompt translation.

## 2.0.8

- Tightened automatic LoRA character matching so a role LoRA is injected only when the user explicitly names that character, an alias, or a verifiable romanization/translation.
- Raised the LLM LoRA-match threshold and added evidence validation; hallucinated matches without an explicit prompt mention are rejected.
- Filtered generic trigger words such as `1girl`, `solo`, and visual tags out of newly imported LoRA aliases.

## 2.0.7

- Filter `/jh lora 列表` by the active model family instead of showing ComfyUI's full physical LoRA list by default.
- Reject manual LoRA switching, alias creation, and role-prompt binding when the target LoRA is incompatible with the active model family.
- Hide non-character model files such as ControlNet, OpenPose, Depth, YOLO, SAM/SDPose, and LLLite from the LoRA cache/list surface.
- Mark Civitai import results with the plugin model family and Civitai BaseModel.

## 2.0.6

- Replaced the 2.0.5 Illustrious two-stage latent-refine workflow with a lighter single-stage txt2img workflow to avoid ComfyUI VRAM spikes and crashes.
- Added isolated Illustrious img2img support via `img2img_illustrious_api.json`; image-referenced requests no longer fall back to Anima when the active family is Illustrious.
- Added Illustrious runtime checkpoint override support so the configured checkpoint is applied to both Illustrious txt2img and img2img workflows.
- Added Illustrious img2img workflow/denoise settings and WebUI model-manager display for both txt2img and img2img workflows.
- Moved the provided IL_Controlnet V2.6 model assets into ComfyUI's expected folders: `models/checkpoints`, `models/controlnet`, and `models/yolo`.

## 2.0.5

- Rebuilt the Illustrious txt2img workflow as an SDXL two-stage latent refine workflow based on the provided ANIMA/SDXL reference while keeping the fixed node contract: positive `11`, negative `12`, output `66`, LoRA `99`.
- Added model family and checkpoint/model file display to ComfyUI generation result text.
- Improved ComfyUI execution failures by surfacing the failed node/type and exception message from history when ComfyUI reports an error.
- Added automatic replacement of stale `finish_illustrious_api.json` in the plugin data workflow folder.

## 2.0.4

- Fixed a model-family race where an old Illustrious img2img fallback task could restore Illustrious after an admin had switched back to Anima.
- Added GPT Magic multi-image edit support by collecting replied images plus same-message images and sending multiple multipart `image` fields.
- Recorded GPT Magic edit source image count in history, cache metadata, and result text.
- Updated GPT Magic help text for multi-image edits.

## 2.0.3

- Ran Civitai LoRA download and LLM profile extraction concurrently during import.
- Reduced wasted post-download polling when AstrBot cannot write ComfyUI's real LoRA folders.
- Merged ComfyUI `/object_info` and `/models/loras` results when refreshing LoRA lists.
- Improved import diagnostics for mismatched plugin download and ComfyUI runtime LoRA directories.

## 2.0.2

- Redesigned the GPT Magic WebUI history gallery with a light frosted-glass line-art style.
- Replaced history-image new-window behavior with an in-page preview modal.
- Added 2K/4K/Max GPT Magic size presets and prompt-based 4K aspect detection.
- Documented that high-resolution proxy-specific parameters can be passed through `extra_body`.

## 2.0.1

- Improved Illustrious checkpoint download directory detection and WebUI diagnostics.
- Added candidate directory suggestions from the configured LoRA download directory.
- Avoided creating guessed checkpoint folders that ComfyUI may not be able to read.
- Moved missing Illustrious checkpoint preflight before LLM prompt translation and LoRA matching.

## 2.0.0

- Added Illustrious model family while keeping all existing Anima behavior.
- Added `finish_illustrious_api.json` txt2img workflow with nodes 11/12/66/99.
- Added `/jh 模型` model-family commands and English `/jh model` alias flow.
- Scoped Civitai LoRA search/import, aliases, and character profiles by active model family.
- Added GPT Magic WebUI model manager for Illustrious checkpoint detection and download.
- Kept Illustrious img2img out of 2.0.0; image-referenced requests continue to use the Anima img2img route.

---

astrbot_plugin_comfyui_pro_UltraPlusMax + XIAOKU230

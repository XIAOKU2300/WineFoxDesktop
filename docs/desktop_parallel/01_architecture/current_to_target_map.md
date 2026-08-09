# Current → Target Module Map (WineFox Desktop)

Source baseline: `astrbot_plugin_comfyui_pro_UltraPlusMax-master` v8.5.9 (metadata.yaml), 49,081 LOC.
Mapping legend:
- **CORE** → `winefox_core`（sidecar，桌面 v1 必须实现，无 astrbot/aiohttp/Tauri 依赖）
- **ADAPTER** → `astrbot_adapter`（AstrBot 插件薄壳，仅消息事件 ↔ CoreEngine 的翻译层）
- **V1-NO** → 桌面 v1 不实现（保留在 AstrBot 插件内，或明确裁剪）

文件行号为 baseline 快照（git commit 0c89f41）时点。

## 1. 顶层类映射

| 现有符号 | 文件:行 | 职责 | 目标模块 | 说明 |
| --- | --- | --- | --- | --- |
| `ComfyUIPlugin(Star)` | main.py:882 | 插件总装：配置、生命周期、全部 `/jh` 命令、LLM 工具 | **ADAPTER + CORE 拆分点** | `Star`/`filter`/`Context`/`AstrMessageEvent` 全部隔离到 Adapter；绘图管线整体下沉 CORE。941 个定义中约 70% 是无 astrbot 依赖的纯逻辑 |
| `ComfyUI` | comfyui_api.py:83 | ComfyUI HTTP 传输 + 工作流注入 | **CORE: `comfy_transport`** | 6371 行中仅 `from astrbot.api import logger`（:17）与 `aiohttp`（:16）两个外部耦合；logger 换标准库即可，aiohttp 换 httpx 或保留（见 dependency_boundaries.md） |
| `GptMagicStore` | gpt_magic.py:640 | 单文件 JSON 存储 + 用户/额度/卡密/历史/收藏/画廊账户 | **CORE: `storage`** | 数据目录布局见 §4 |
| `GptMagicService` | gpt_magic.py:2749 | 任务队列（pending/active）、并发调度、WebUI 生命周期 | **CORE: `job_manager`（队列）+ `web_draw`（绘图管线宿主）** | `_dispatch_locked`/`_run_task` 与 CORE 的 JobManager 一一对应 |
| `FavoriteGalleryWebUI` | gpt_magic.py:3193 | 画廊 + Web 绘图 HTTP 服务（aiohttp web） | **CORE: `desktop_api`（/api/v1 参考实现）** | 现 `/api/draw*`、`/api/gallery*`、`/api/admin/*` 路由可直接迁移 |
| `GptMagicWebUI` | gpt_magic.py:5427 | 后台管理 WebUI（token 认证） | **CORE: `desktop_api` admin 部分（v1 可延后）** | `/api/state` 的 `public_state()` 输出即 v1 状态模型雏形 |
| `OpenAIImageProvider` | gpt_magic.py:2366 | OpenAI 兼容绘图 API 客户端 | **V1-NO** | 桌面 v1 主链是 ComfyUI 本地生成；作为可选 Provider 接口保留 |
| `CivitaiClient` | civitai_api.py:192 | Civitai 搜索/解析/下载 | **V1-NO**（LoRA 源客户端） | 2639 行；搜索、触发词提取、多线程下载全部 AstrBot 指令驱动 |
| `CivitaiRedClient` | civitai_api.py:1944 | Civitai Red 镜像 | **V1-NO** | |
| `HuggingFaceClient` | civitai_api.py:1961 | HF 搜索/下载 | **V1-NO** | |
| `YodayoClient` | civitai_api.py:2279 | Yodayo 搜索 | **V1-NO** | |
| `LoraProfileStore` | civitai_api.py:2292 | LoRA 档案 upsert/match | **CORE: `prompt_lora`** | `upsert_from_civitai`/`match` 纯逻辑，仅 Path 依赖 |
| `intent_utils.*` | intent_utils.py:210-415 | 多角色意图识别 | **CORE: `prompt_lora`** | 仅 `re` 依赖，零耦合，可直接复制 |
| `watermark.apply_watermark_to_image_file` | watermark.py:32 | 水印叠加 | **CORE: `storage/output_utils`** | 仅 Pillow+Path |
| `PluginFeature` | features/common.py:103 | 特性管理器基类（反射到插件） | **V1-NO** | `__getattr__` 反射绑定 `ComfyUIPlugin`，桌面版不需要 |
| `FavoritesFeature` 等 7 个 feature | features/*.py | 收藏/排行/审核/识图/笔记本/自更新/测试模式 | **V1-NO**（image_audit 视 v1 安全需求可移植为独立审核服务） | 全部依赖 astrbot 事件或 `PluginFeature` |

## 2. 关键方法映射（按领域）

### 2.1 生成管线（main.py → CORE `pipeline` / `comfy_transport`）

| 现有方法 | 行 | 目标模块 | 备注 |
| --- | --- | --- | --- |
| `comfyui_txt2img`（生成器） | 29803 | ADAPTER → CORE `pipeline.generate()` | 权限/冷却/敏感词留在 Adapter（消息域）；其余下沉 |
| `_handle_paint_prompt` | 12820 | ADAPTER 编排 + CORE 调用 | avatar 重绘、源图提取是消息域 |
| `_run_paint_background_task` | 12789 | CORE `job_manager`（等同 Web draw 路径） | 消息与 Web 共用队列的设计直接迁移 |
| `_handle_webui_draw_generate` | 13804 | **CORE `web_draw.generate()`（v1 主入口）** | 返回 `{ok,image(data_url),image_path,prompt,raw_prompt,elapsed_seconds,meta}` —— 就是 v1 的 `GenerationResult` 原型 |
| `_handle_webui_pose_generate` | 14055 | CORE `pipeline.pose_generate()` | OpenPose 控制图 data_url → 上传 → ControlNet 注入 |
| `_generate_image_file` | 26250 | CORE `pipeline` | 黑屏重试、foot detailer、subject audit、OOM 降级 |
| `_generate_staged_multi_lora_image_file` | 26771 | CORE `pipeline`（v1 可延后） | 多角色 staged 工作流 |
| `_generate_video_file` / `_generate_api_video_file` | 27604/27782 | CORE `video`（v1 可延后） | Wan i2v + 云端 API 视频 |
| `_prepare_draw_prompt_before_generation_lock` | 13432 | CORE `prompt_pipeline.prepare()` | LLM 转译 + LoRA 匹配 + 稳定器 |
| `_apply_prepared_generation_runtime` / `_apply_prepared_prompt_runtime` | 13733/13724 | CORE `pipeline` | 模型族切换、checkpoint 守护、多 LoRA 运行时 |
| `_acquire_generation_state_lock` 等队列锁 | 12381-12188 | CORE `job_manager` | 生成互斥锁 + 远端队列核对 |

### 2.2 ComfyUI 传输（comfyui_api.py → CORE `comfy_transport`）

| 现有方法 | 行 | 目标 | 备注 |
| --- | --- | --- | --- |
| `fetch_lora_list` | — | `comfy_transport` | `/object_info` + `/models/loras` + 磁盘缓存 `lora_cache.json` |
| `_inject_params` / `generate` / `generate_staged_pass` / `generate_video` | 3946+ | `comfy_transport` | 核心三态：`_queue_workflow`（POST /prompt）→ `_fetch_prompt_history`（GET /history/{id}）→ `_download_image_bytes`（GET /view） |
| `_queue_workflow` | 4545+ | `comfy_transport` | 校验失败自动中和 LoRA 重试、缺模型文件短路 |
| `_check_prompt_disappeared_from_queue` | 4800+ | `comfy_transport` | 结果恢复关键：grace 45s + 3 次确认 |
| `_apply_lora_override` / `_apply_style_lora_override` / multi-LoRA 系列 | 1080-3130 | `comfy_transport` | LoRA 节点注入、旁路、regional/hooks/densediffusion |
| `_inject_img2img_params` / `_inject_pose_control_params` / `_apply_wan_i2v_options` | 4985/3259/5966 | `comfy_transport` | img2img/pose/wan 注入 |
| `check_illustrious_foot_detailer_ready` / `apply_illustrious_foot_detailer` | 3618+ | `comfy_transport` | 动态 Impact-Pack 工作流 |
| `_is_blank_or_black_image` | 5890 | `comfy_transport` | VAE 黑屏检测（有测试覆盖） |

### 2.3 存储（gpt_magic.py → CORE `storage`）

| 现有方法 | 行 | 目标 | 备注 |
| --- | --- | --- | --- |
| `_load/save`、`config()/users()/keys()/history()` | 659-786 | `storage: Store` | `db.json` 单文件；桌面版可保留 JSON 或换 SQLite，契约不变 |
| 配额：`register_user/consume_quota/refund_quota/redeem_key` | 1574-1715 | `storage: QuotaStore` | v1 无卡密/群场景可裁剪 `keys` |
| 收藏：`list_favorites/import_favorite_image/update_favorite_metadata` | 1912-2220 | `storage: FavoriteStore` | sidecar `.favorite.json` + sha256 去重 |
| Web 绘图历史：`add_web_draw_history/list_web_draw_history_for_context` | 1788-1855 | `storage: DrawHistoryStore` | 24h TTL、按账户隔离 —— v1 结果恢复直接复用 |
| 画廊账户/余额：`gallery_accounts/verify_gallery_account/consume_gallery_balance` | 940-1375 | `storage: AccountStore` | v1 可简化，但会话/登录锁逻辑可复用 |
| 姿态预设：`pose_presets` | 1408-1468 | `storage` | `pose_presets.json` |

### 2.4 桌面 API（gpt_magic.py → CORE `desktop_api`）

| 现有路由 | 处理器 | 目标 | 备注 |
| --- | --- | --- | --- |
| `POST /api/draw` | `FavoriteGalleryWebUI.draw` | v1 `POST /api/v1/generate` | 队列登记→后台任务→job 轮询 |
| `GET /api/draw/{job_id}` | `draw_status` | v1 `GET /api/v1/jobs/{id}` | 状态：preparing/queued/running/done/error + wait_text |
| `GET /api/draw/history` + `/{item_id}/image` + `/favorite` | `draw_history*` | v1 `GET /api/v1/history` | 24h TTL 结果恢复 |
| `GET /api/draw/loras` | `draw_loras` | v1 `GET /api/v1/loras` | 可绘制角色 + live 可用性 |
| `POST /api/pose/generate` | `comfy_action_handler("pose_generate")` | v1 `POST /api/v1/pose/generate` | 走 `_handle_webui_pose_generate` |
| `POST /api/comfy/model/{action}` | `GptMagicWebUI.comfy_model_action` | v1 `POST /api/v1/comfy/{action}` | status/switch/download_illustrious |
| `GET /api/state` | `GptMagicWebUI.state` | v1 `GET /api/v1/state` | `public_state()`：config+queue+comfy+analytics |
| 画廊全套 `/api/gallery*`、`/api/admin/*`、`/api/register|login` | `FavoriteGalleryWebUI` | **V1-NO**（画廊社交功能 v1 不实现） | 但 `_draw_job_payload`/`_draw_safety_payload` 的敏感图遮罩逻辑应迁入 v1 UI |

## 3. AstrBot Adapter 职责（薄壳清单）

| 现有代码 | 目标 Adapter 方法 | 说明 |
| --- | --- | --- |
| `@register(...)` + `Star.__init__`（main.py:875-1400） | `WineFoxAstrBotPlugin(Star)` | 配置加载 + 数据目录迁移 + feature 装配，全部保留在插件 |
| `@filter.command("酒狐来点")` 等 ~40 个命令（main.py:18998-20161） | `filter.command` 处理器 | 每个命令体只做：参数解析 → `core.request(...)` → 把结果渲染成 `event.plain_result/chain_result` |
| `@filter.on_llm_request(priority=100)`（main.py:15997） | `on_llm_request` | 系统提示注入 + `jiuhu_draw_image` 工具注册（`context.get_llm_tool_manager`） |
| `initialize()`（main.py:18392） | `initialize()` | 调 `core.start()`（sidecar 由 Adapter 拉起或连接） |
| `terminate()`（main.py:18420） | `terminate()` | 调 `core.stop()`，取消后台任务 |
| `_handle_gpt_magic_model_action`（main.py:13153） | Adapter 转发到 `core.comfy_action(...)` | AstrBot 管理页 WebUI 仍由 Adapter 侧提供（端口 1144/6666 不迁移给 v1 UI） |

## 4. 数据目录映射（v1 必须兼容）

| 现有路径（data_dir 下） | 用途 | v1 处置 |
| --- | --- | --- |
| `workflow/*.json`（19 个） | API 工作流 | 原样复制，路径契约保留 |
| `lora_cache.json` | ComfyUI LoRA 缓存 | 原样 |
| `lora_profiles.json` / `lora_aliases.json` / `lora_character_prompts.json` | LoRA 档案 | 原样（Prompt/LoRA 模块读写） |
| `model_family_state.json` | 当前模型族 | 原样 |
| `gpt_magic/db.json` | 用户/额度/历史/画廊 | 迁移或并行（v1 只读 `web_draw_history` 时需注意 TTL 清理） |
| `gpt_magic/history/`、`cache/` | GPT 魔法图片 | V1-NO（ComfyUI 链路不用） |
| `output/` | 生成结果 | v1 沿用 |
| `favorites/` + `*.favorite.json` sidecar | 收藏图库 | v1 沿用 |
| `sensitive_words.json` | 敏感词 | v1 可选复用 |
| `daily_generation_stats.json` 等 | 统计 | V1-NO |

## 5. 明确 V1-NO 清单（桌面 v1 不实现）

- AstrBot 消息适配（全部 `filter.command`/`@filter` 装饰器、`event.*`、`MessageChain`、OneBot 头像下载、转发组件）
- LLM 系统提示注入与 `on_llm_request` 钩子（`_inject_main_llm_messages` 等）
- GPT 魔法 OpenAI 兼容生图（`OpenAIImageProvider` 整类 + 配额/卡密）
- 画廊社交：注册/登录/账户/余额/钱包/管理员计费
- Civitai/HF/Yodayo 搜索导入、comfyui_bridge LoRA 下载、checkpoint 一键下载
- 自更新（`SelfUpdateFeature`，git 拉取）
- 测试模式、杂鱼模式（zayu）、提示笔记本、收藏指令、酒狐排行、酒狐看看/审核指令
- Wan 视频 + API 视频（v1 若含视频需单独立项）

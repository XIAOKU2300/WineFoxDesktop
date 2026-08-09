# Dependency Boundaries (WineFox Desktop)

## 1. 目标单向依赖图

```
┌────────────┐      ┌──────────────────────┐      ┌──────────────────────┐
│ Tauri 壳    │ ───▶ │ UI (webview 前端)     │ ───▶ │ Desktop API          │
│ (Rust)     │      │ (winefox_draw.html    │      │ (desktop_api:        │
│ 托盘/窗口   │      │  迁移/重构)            │      │  /api/v1, WS)        │
└────────────┘      └──────────────────────┘      └──────────┬───────────┘
                                                             │ IPC（进程内函数调用或
                                                             │ JSON-RPC over stdio/WebSocket）
                                                             ▼
┌───────────────────────────────────────────────────────────────────────────┐
│                      winefox_core（sidecar 进程，Python）                  │
│                                                                           │
│  ┌─────────────┐    ┌──────────────┐    ┌──────────────────────────────┐  │
│  │ CoreEngine  │───▶│ JobManager   │───▶│ ComfyUI Transport            │  │
│  │ (编排/状态机)│    │ (队列/锁/恢复) │    │ (comfy_transport)            │  │
│  └──────┬──────┘    └──────────────┘    └──────────────────────────────┘  │
│         │                    ▲                       ▲                    │
│         ▼                    │                       │                    │
│  ┌─────────────┐    ┌────────┴──────┐    ┌────────────┴──────────────┐   │
│  │ Prompt/LoRA │    │ Storage       │    │ Provider 接口（可插拔）      │   │
│  │ (prompt_lora)│   │ (JSON/SQLite) │    │ (LLM 转译/审核/水印)         │   │
│  └─────────────┘    └───────────────┘    └───────────────────────────┘   │
└───────────────────────────────────────────────────────────────────────────┘
          ▲                          ▲                          ▲
          │                          │                          │
   AstrBot Adapter（消息域）      ┌────┴────┐             ComfyUI (HTTP)
   filter.command / event        │ 外部世界 │             127.0.0.1:8188
                                └─────────┘             （唯一强制外部依赖）
```

## 2. 硬性规则（违反即架构错误）

### R1：Core 禁止导入 astrbot
- **证据**：`comfyui_api.py:16-17` `import aiohttp` / `from astrbot.api import logger`；`main.py:882` 整个类继承 `Star`；`gpt_magic.py:14` `from astrbot.api import logger`；`features/common.py:18-21` `from astrbot.api...`。
- **迁移要求**：Core 内所有模块只能使用标准库 + 白名单第三方库（见 §4）。`astrbot.api.logger` 一律替换为 `logging.getLogger("winefox_core")`。
- **例外**：`astrbot_adapter` 可以（且应该）导入 Core 与 astrbot，方向唯一。

### R2：Core 禁止导入 aiohttp
- **证据**：`comfyui_api.py:16`、`civitai_api.py:14-15`、`gpt_magic.py:12-13`、`features/common.py:24` 均 `import aiohttp`。
- **理由**：aiohttp 在 Core 侧承担两个角色：① ComfyUI HTTP 客户端（`ComfyUI._client_session`，comfyui_api.py:296）；② HTTP 服务端（`GptMagicWebUI`/`FavoriteGalleryWebUI` 的 `aiohttp.web`）。桌面 v1 客户端统一走 httpx（ComfyUI 传输），服务端统一走 `Desktop API` 内置实现（FastAPI 或 asyncio 原生），两者都不依赖 aiohttp。
- **迁移次序**：先替换 logger（机械替换），再抽 `HttpClient` 薄接口（`_client_session` 的 4 个超时参数族 `_generation_client_timeout/_history_request_timeout/_queue_request_timeout` 已在 comfyui_api.py:4505-4550 集中，接口抽取成本低），最后替换 web 服务。

### R3：Core 禁止导入 Tauri / 任何 Rust 侧类型
- Core 与 Tauri 之间只存在 **进程边界协议**（sidecar ready JSON / /api/v1 JSON / 退出信号）。Core 不知道自己被 Tauri 托管。
- sidecar 的发现方式（环境变量 `WINEFOX_SIDECAR_PORT` 或 stdio JSON-RPC）由 Desktop API 层实现，CoreEngine 不感知。

### R4：依赖方向单向
- `UI → Desktop API → CoreEngine → JobManager/Storage/ComfyUI Transport/Prompt-LoRA`
- `JobManager → ComfyUI Transport`（提交/轮询/结果下载）、`JobManager → Storage`（历史/恢复）、`JobManager → Prompt/LoRA`（生成前预处理）
- `Storage` 不允许导入 `JobManager`（证据：现 `GptMagicStore` 是纯数据类，只被 Service/WebUI 调用，方向已正确，保持即可）
- `ComfyUI Transport` 不允许导入 `JobManager` 或 `Storage`（现状：`ComfyUI` 类只依赖 config+data_dir，方向已正确）
- `Prompt/LoRA` 只依赖纯函数库（`intent_utils.py` 仅 `re`；`LoraProfileStore` 仅 `Path`），可被 CoreEngine、JobManager、Desktop API 三方调用

### R5：AstrBot Adapter 必须是唯一了解 astrbot 的层
- Adapter 持有 `Star`/`filter`/`Context`/`AstrMessageEvent`（main.py:887-891 等），把消息事件翻译成 `core.request(GenerationRequest)`，把 `GenerationResult` 渲染成消息组件。
- Adapter 不允许调用 `ComfyUI Transport` 或 `Storage` 的内部方法；只许调用 CoreEngine 的公开门面（`generate/job_status/history/cancel/state`）。

## 3. 各模块边界定义

### 3.1 CoreEngine（编排）
- 职责：启动/停止 sidecar、加载配置（`_conf_schema.json` 结构保留：23 个顶级键）、装配子模块、暴露门面 API、状态广播（sidecar ready JSON + 状态变更事件）。
- 迁移来源：`ComfyUIPlugin.__init__`（main.py:883-1400）中的装配顺序；`initialize()`（main.py:18392）。
- 不允许：直接发起 HTTP、直接读写存储文件（必须经 Storage 接口）、import astrbot。

### 3.2 JobManager（队列/锁/恢复）
- 职责：生成互斥锁、队列登记/状态推进、并发上限、远端队列核对、崩溃恢复。
- 迁移来源：`_generation_queue_entries` 结构（main.py:12441-12536，字段 `id/kind/status/created_at/ready_at/sender_id/prompt_preview/ahead/position/estimated_wait_seconds`）、`_generation_state_lock` 家族（main.py:12113-12157）、`GptMagicService.pending/active_tasks`（gpt_magic.py:2882-2955）。
- 状态机：`preparing → queued → running → done|error`（与现有 `_mark_generation_queue_entry_ready/_mark_running/_finish` 一一对应）。

### 3.3 ComfyUI Transport
- 职责：ComfyUI HTTP 客户端（/prompt、/history/{id}、/view、/upload/image、/queue、/object_info、/free）、工作流注入、LoRA 注入/中和、结果下载、错误归类。
- 迁移来源：`ComfyUI` 类全部（comfyui_api.py:83-6371）。
- 边界：`generate()` 返回 `(bytes|None, error_str|None, meta_dict)`；`generate_video()` 返回 `(Path|None, error_str|None, meta_dict)` —— 保持此元组契约，v1 封装成 `GenerationResult` 时不改 Transport。

### 3.4 Prompt/LoRA
- 职责：提示词转译（LLM Provider 接口）、LoRA 档案匹配、多角色意图识别、触发词组装、稳定器/负向防护。
- 迁移来源：`intent_utils.py` 全量、`LoraProfileStore`（civitai_api.py:2292）、`main.py` 中 `_match_lora_profile*`/`_build_lora_smart_prompt`/`_apply_default_illustrious_prompt_stabilizers`（约 4800-8400 行的纯函数区）。
- 边界：不感知 JobManager/Transport；LLM 通过 `Provider` 接口注入（现 `_call_plugin_llm_json` main.py:9713 等 8 个 LLM 调用点收敛为一个接口）。

### 3.5 Storage
- 职责：`db.json`（用户/历史/web_draw_history/收藏/画廊账户/姿态预设）、`lora_profiles.json`、`model_family_state.json`、`output/`、`favorites/` + sidecar。
- 迁移来源：`GptMagicStore`（gpt_magic.py:640-2364）、main.py 的 `_load_json_map_file/_save_json_map_file`（main.py:2225-2235）。
- 边界：纯同步文件操作；写操作必须持 `asyncio.Lock` 语义（现有 `GptMagicStore.save()` 每次全量写盘，v1 可保留或换 SQLite，但对外契约不变）。

### 3.6 Desktop API
- 职责：`/api/v1/*` HTTP 路由、WebSocket 状态推送、sidecar ready JSON 输出、静态资源（迁移自 `templates/winefox_draw.html`）。
- 迁移来源：`FavoriteGalleryWebUI`（gpt_magic.py:3193-5426，尤其中 `draw/draw_status/draw_history/draw_loras/draw_history_favorite`）与 `GptMagicWebUI.state`（gpt_magic.py:5538）。
- 边界：认证（现有 token 对比 `secrets.compare_digest`）保留；`/api/draw*` 路由改名 `/api/v1/*` 时由 contract_review.md 统一规定。

### 3.7 UI
- 迁移来源：`templates/winefox_draw.html`（约 4000 行内联 HTML/JS/CSS，含 three.js 姿态编辑器）。v1 直接复用该单文件结构，仅把 fetch 路径从 `/api/draw/*` 换为 `/api/v1/*`。

### 3.8 AstrBot Adapter
- 迁移来源：main.py 中所有 `@filter.command` 处理器 + `@filter.on_llm_request` + `initialize/terminate`。约 40 个命令；每条命令翻译为 `core.request(...)`。

## 4. Core 依赖白名单

| 库 | 用途 | 现状证据 |
| --- | --- | --- |
| 标准库（asyncio/json/re/pathlib/logging/…） | 全部核心逻辑 | 全部文件 |
| httpx（或等价的纯异步 HTTP 客户端） | ComfyUI Transport | 替代 aiohttp：comfyui_api.py:16 |
| Pillow | 水印、黑屏检测、图片元数据 | watermark.py:36，comfyui_api.py:5890 |
| （可选）uvicorn+fastapi 或 asyncio 原生 HTTP | Desktop API | 替代 aiohttp.web：gpt_magic.py:12 |

禁止：astrbot\*、aiohttp、playwright、tensorflow/opennsfw2（审核是 v1 延后项，走独立进程或 Provider）、jinja2（自更新渲染用，V1-NO）。

## 5. 耦合证据速查（供代码审查用）

| 违规模式 | 证据 | 处置 |
| --- | --- | --- |
| Core import astrbot | `gpt_magic.py:14`、`comfyui_api.py:17`、`civitai_api.py:15`、`features/common.py:18-21`、`features/image_audit.py:11` | 全部替换为 `logging`；`AstrMessageEvent` 引用（gpt_magic.py:1552 `user_name_from_event`）移到 Adapter |
| Core import aiohttp | `comfyui_api.py:16`、`civitai_api.py:14`、`gpt_magic.py:12`、`features/common.py:24` | 客户端→httpx；服务端→Desktop API 实现 |
| Core 里藏 astrbot 类型 | `main.py:15997` `@filter.on_llm_request`、`main.py:29803` 生成器签名 `MessageEventResult` | 整段留在 Adapter |
| Storage 反向依赖 | 现状无（GptMagicStore 无导入外部域） | 保持，加 CI 检查 |
| Transport 反向依赖 | 现状无（ComfyUI 类仅 config/data_dir） | 保持，加 CI 检查 |

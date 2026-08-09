# Lifecycle：桌面启动 → sidecar → ComfyUI → 任务 → 恢复 → 退出

本文件把现有 AstrBot 插件生命周期（证据：main.py `ComfyUIPlugin.__init__`/`initialize()`/`terminate()`、comfyui_api.py 队列核对逻辑、gpt_magic.py WebUI 生命周期）映射为 WineFox Desktop 的生命周期。**每个阶段都给出源码证据与 v1 对应实现点。**

## 1. 阶段总览

```
 A. 桌面启动          B. sidecar ready    C. 连接 ComfyUI      D. 提交任务
 用户双击 → Tauri      sidecar 完成装配      health check         POST /api/v1/generate
 → 拉起 sidecar 进程   → 输出 ready JSON     /object_info          → job 登记
 → 显示启动窗          → UI 收到后进主界面    /queue 快照          → 后台任务排队
                                                    │
                                                    ▼
 E. 结果恢复          F. 运行中状态          G. 托盘关闭          H. 显式退出
 轮询 /history/{id}    queue 状态推送         hide → sidecar 驻留   Quit → sidecar 优雅停止
 队列消失核对          WebSocket /api/v1      锁文件/任务保留      取消后台任务、落盘、退出
```

## 2. 阶段 A：桌面启动（Tauri → sidecar）

| 步骤 | 现有行为证据 | v1 实现 |
| --- | --- | --- |
| 1. Tauri 拉起 Python sidecar 子进程（`winefox_core` 入口 `core/main.py`） | AstrBot 插件由宿主加载；无等价物 | Tauri `Command::new(python) .arg(core/main.py)`，stdout 解析 ready JSON |
| 2. sidecar 读取配置 | `ComfyUIPlugin.__init__`（main.py:883-1400）读取 AstrBot config；`_conf_schema.json` 23 个顶级键 | 读 `%APPDATA%/WineFox/config.json`（结构沿用 `_conf_schema.json`），`_auto_update_schema()`（main.py:2094）迁移逻辑保留 |
| 3. 数据目录解析与迁移 | `_get_persistent_dir()`（main.py:1405）：`StarTools.get_data_dir` 优先，否则 `data/plugin_data/<id>`；`_migrate_legacy_data_dir`（main.py:1432） | `%APPDATA%/WineFox/data`；若检测到旧插件 data 目录（`astrbot_plugin_comfyui_pro_UltraPlusMax` 或旧名），复制缺失文件（复用 `_copy_missing_tree` main.py:1479） |
| 4. 装配子模块 | `__init__` 末尾：`ComfyUI(config, data_dir)`（main.py:1393-1399）+ `GptMagicService(config, data_dir, owner=self)`（main.py:1401-1407）；feature 管理器 7 个 | CoreEngine 装配 `comfy_transport` + `job_manager` + `storage` + `prompt_lora` + `desktop_api`。feature 全部 V1-NO，不装配 |
| 5. 异步初始化 | `initialize()`（main.py:18392）：LLM 工具注册（V1-NO）、`gpt_magic.start_webui(...)`（main.py:18406-18414）、`api.fetch_lora_list(force=True)`（main.py:18422）、`_fetch_comfyui_checkpoint_names()`（main.py:18437） | `core.start()`：仅保留 LoRA 列表初始化与 checkpoint 初始化，作为阶段 C 的一部分 |

## 3. 阶段 B：sidecar ready JSON

v1 契约（字段名与其他 Agent 协商后冻结，见 contract_review.md §6）：

```json
{
  "event": "sidecar_ready",
  "version": "0.1.0",
  "pid": 1234,
  "api_base": "http://127.0.0.1:18780",
  "api_token": "…",                // 随机生成，UI 首连用
  "data_dir": "C:/Users/x/AppData/Roaming/WineFox/data",
  "comfy": {
    "server_address": "127.0.0.1:8188",
    "connected": false,            // 阶段 C 完成后推送 true
    "workflow": "finish_illustrious_api.json",
    "model_family": "illustrious",
    "lora_count": 0,
    "checkpoint": ""
  },
  "config_loaded": true,
  "migrations": {"files_copied": 0, "legacy_config": false}
}
```

- 现有证据：`_build_gpt_magic_comfy_status()`（main.py:13071）已产出等价状态 dict（插件版本/地址/工作流/节点/LoRA/模型族/checkpoint），v1 把它拆成 ready JSON 的 `comfy` 字段 + 后续状态推送。
- UI 收到 ready JSON 之前显示“正在启动 sidecar…”，之后调用 `POST /api/v1/state` 拉全量（对应 `public_state()` gpt_magic.py:1499：config+users+keys+history+favorites+queue+comfy+analytics；v1 裁剪 users/keys/analytics 之外的字段）。
- ready 超时（默认 30s）→ UI 报错并提供日志目录入口。

## 4. 阶段 C：连接 ComfyUI

| 步骤 | 现有行为证据 | v1 实现 |
| --- | --- | --- |
| 1. 地址规范化 | `_normalize_server_address`（comfyui_api.py:34）：裸 `127.0.0.1:8188` 补 `http://` | 相同 |
| 2. 连接探测 | `fetch_lora_list(force=True, allow_cache_fallback=False)`（comfyui_api.py:472）：GET `/object_info`，失败走 `/models/loras`，再失败报 warn | 新 `comfy_transport.ping()`：GET `/system_stats` + `/object_info`，返回 `(connected, latency_ms, warn)` |
| 3. 工作流可用性检查 | `_fallback_unsupported_workflow`（comfyui_api.py:3882）：`_workflow_missing_node_classes/_workflow_missing_required_inputs/_workflow_invalid_combo_inputs`，缺节点自动回退 `runtime_fallback_wf_filename` | 启动时全量校验 19 个 `workflow/*.json`，结果进 `comfy.workflows` 状态 |
| 4. Checkpoint 探测 | `_fetch_comfyui_checkpoint_names`（main.py:5269）+ `_illustrious_checkpoint_status`（main.py:5556） | 启动时异步执行；缺失时 UI 显示“一键下载”（V1-NO，只提示手工放置） |
| 5. 失败策略 | 现插件：`initialize()` 里 warn 后继续运行，生成时再报错（comfyui_api.py:4505-4512 超时族） | v1：`comfy.connected=false` + 重试后台每 30s 重连；不阻塞 UI |
| 6. 连接后的状态推送 | `_build_gpt_magic_comfy_status` 由 `/api/state` 拉取 | v1 增加 WebSocket `event: comfy_status`（connected/queue/lora_count 变更） |

## 5. 阶段 D：提交任务

| 步骤 | 现有行为证据 | v1 实现 |
| --- | --- | --- |
| 1. 请求校验 | `_handle_webui_draw_generate`（main.py:13804）：prompt 非空、≤4000 字、seed 解析（`_extract_prompt_seed_override` main.py:12687） | `POST /api/v1/generate` → `GenerationRequest`（contract_review.md §2） |
| 2. 队列登记 | `_register_webui_draw_queue_entry`（main.py:12478）：容量快照 `_generation_queue_capacity_snapshot`（main.py:12311）、`estimated_wait_seconds = ahead × average_seconds`（`_generation_queue_average_seconds` main.py:11873） | `JobManager.register()`，JobSnapshot 同字段 |
| 3. 后台任务 + 锁 | `FavoriteGalleryWebUI.draw`（gpt_magic.py:3975）：创建 `asyncio.Task(_run_draw_job)`；`_acquire_generation_state_lock`（main.py:12381）互斥锁 | `JobManager.submit()`；单机串行锁（`_generation_lock_owner` main.py:11937）保留 |
| 4. 生成前预处理 | `_prepare_draw_prompt_before_generation_lock`（main.py:13432）：LLM 转译 + LoRA 匹配 + 模型族自动切换（`_auto_switch_model_family_for_lora` main.py:5097） | `prompt_lora.prepare()`；LLM 走 Provider 接口 |
| 5. 提交 ComfyUI | `ComfyUI.generate`（comfyui_api.py:5830+）：`_load_runtime_config` → `_inject_params`（提示词/负面/LoRA/画风 LoRA/步数覆盖/seed，comfyui_api.py:4045）→ `_queue_workflow`（POST /prompt，comfyui_api.py:4713） | `comfy_transport.generate()`，契约不变 |
| 6. 轮询 | `_fetch_prompt_history`（comfyui_api.py:4526）：GET `/history/{prompt_id}`；`_generation_poll_delay`（comfyui_api.py:4517）指数退避 1s→3s | 相同；超时 = `poll_attempts`(300) |
| 7. 结果下载 | `_select_image_output`（comfyui_api.py:5878）+ `_download_image_bytes`（comfyui_api.py:4611，重试 3 次） | 相同 |
| 8. 收尾 | `_save_generated_image_bytes`（main.py:26465，水印+输出目录）+ `_finalize_generation_queue_and_lock`（main.py:12188）+ `_audit_generated_output_before_send`（审核） | `storage.save_output()` + `JobManager.finish()` + audit Provider（V1-NO 时默认放行） |

## 6. 阶段 E：结果恢复（崩溃/断连）

| 场景 | 现有行为证据 | v1 策略 |
| --- | --- | --- |
| 轮询中 history 暂时 404 | `_record_history_poll_failure`（comfyui_api.py:4526）记录，继续轮询 | 相同 |
| prompt_id 从远端消失 | `_check_prompt_disappeared_from_queue`（comfyui_api.py:4800）：grace 45s 内不判断；之后 GET `/queue`，连续 3 次（`queue_missing_confirmations`）找不到 → 判定任务丢失 | 相同；JobSnapshot 置 `status=error, code=queue_prompt_missing` |
| sidecar 自身崩溃后重启 | 无等价物（AstrBot 不重启） | v1：`web_draw_history`（gpt_magic.py:1788，TTL 24h，`_web_draw_history_ttl_ms`）作为恢复源；启动时扫描 `history_dir` 未完成的 job 记录，标记 `status=interrupted` |
| 任务结果留存 | `add_web_draw_history`（gpt_magic.py:1788）存 `{id, image_path, meta, expires_at}`，`cleanup_web_draw_history`（gpt_magic.py:1766）按 TTL 清理 | 相同；`/api/v1/history` 返回同结构（contract_review.md §4） |
| 图片损坏/黑屏 | `_is_blank_or_black_image`（comfyui_api.py:5890）+ `_black_screen_retry_settings`（main.py:25920） | 相同，重试逻辑进 pipeline |

## 7. 阶段 G：托盘关闭（隐藏）vs 阶段 H：显式退出

| 行为 | 现有证据 | v1 实现 |
| --- | --- | --- |
| 关闭窗口 → 最小化到托盘 | 无（插件无窗口） | Tauri 拦截 `WindowEvent::CloseRequested` → `hide()`；sidecar 不停止，任务继续跑，`comfy_transport` 轮询不中断 |
| 托盘图标点击 → 恢复窗口 | 无 | Tauri `show()` + UI 重新拉 `/api/v1/state`（幂等） |
| 显式退出（Quit） | `terminate()`（main.py:18420）：取消 `_lora_background_tasks/_paint_background_tasks/_video_background_tasks` 全部 task、`gpt_magic.stop_webui()`/`stop_gallery_webui()` | Tauri 发 `sidecar_shutdown`（stdin EOF 或命名管道信号）→ CoreEngine：① 取消后台任务（asyncio task.cancel）② Storage 落盘（`GptMagicStore.save()` 语义）③ 关闭 Desktop API 监听 ④ 进程退出码 0 |
| 退出超时强杀 | 无 | Tauri 等待 5s 后 `kill()`；下次启动走阶段 E 恢复 |
| 退出时正在生成 | 现插件 task.cancel 直接丢 | v1：记录 `interrupted` job（带 prompt_id 与 ComfyUI 端 prompt_id 存根），重启后若 `/history/{prompt_id}` 仍有结果则继续下载并补写历史 |
| 多实例防护 | 无 | sidecar 启动时独占锁文件（data_dir/.winefox.lock），失败则提示“已在运行”并退出 |

## 8. 状态机（JobSnapshot.status 合法迁移）

```
preparing ──预处理完成──▶ queued ──拿到锁──▶ running ──成功──▶ done
   │                       │                  │
   │ 校验失败               │ 队列满/取消        ├──失败──▶ error
   └────▶ error ◀───────────┴──────────────────┘
   running ──远端丢失(3次确认)──▶ error(code=queue_prompt_missing)
   running ──sidecar 崩溃重启──▶ interrupted ──恢复成功──▶ done
   interrupted ──历史已过期──▶ (清理, 记录丢失)
```
证据：`_mark_generation_queue_entry_ready`（main.py:12515）、`_mark_generation_queue_entry_running`（main.py:12530）、`_finish_generation_queue_entry`（main.py:12536）、`_draw_job_task_done`（gpt_magic.py:3980）。

# Contract Review：现有 Web 绘图链路 vs 桌面 v1 契约

审核对象：`GenerationRequest`、`GenerationResult`、`JobSnapshot`、`AppError`、`/api/v1`、`sidecar ready JSON` 是否覆盖现有 Web 绘图链路。
结论先行：**核心链路已覆盖，但存在 6 个必须的兼容扩展**（全部为「追加可选字段/新增端点」，不修改其他 Agent 已冻结的契约字段）。

## 1. 现有 Web 绘图链路的真实契约（证据）

来源：`FavoriteGalleryWebUI`（gpt_magic.py:3193-5426）+ `_handle_webui_draw_generate`（main.py:13804）。

```
POST /api/draw {prompt}                          → 202 {job_id, status:"preparing", wait_seconds, wait_text, queue_info}
GET  /api/draw/{job_id}                          → 200 {job_id, status, queue_status, error, ...,
                                                          history{id,prompt,image_url,elapsed_seconds,
                                                                  sensitive,reveal_required,safety,
                                                                  info{seed,workflow,model_family,lora,size,watermark},
                                                                  meta{...}}, image_url, info, meta}
GET  /api/draw/history?limit=24                  → {ok, history:[{id,prompt,image_url,expires_at,...}], ttl_hours}
GET  /api/draw/history/{item_id}/image           → 图片字节（FileResponse）
POST /api/draw/history/{item_id}/favorite        → {ok,item,duplicate}
GET  /api/draw/loras?limit=80&family=            → {ok,family,family_label,live_checked,live_warning,loras[],stats}
POST /api/comfy/model/{action}                   → {ok, model_manager:{...}}   （action: status|switch|download_illustrious|save_comfy_settings|draw_generate|pose_generate）
GET  /api/state                                  → public_state(): {config,users,keys,history,favorites,queue,comfy,analytics}
```
- 认证：`x-gpt-magic-token` / `x-qp-token` / `?token=`（GptMagicWebUI，gpt_magic.py:5437-5450）；画廊侧 `x-winefox-gallery-token`/cookie/`?gallery_token=`（gpt_magic.py:3220）。
- 生成请求可携带的**可选输入**（`_handle_webui_draw_generate` main.py:13804-13838 + `_handle_webui_pose_generate` main.py:14055）：
  - `prompt`（≤4000 字）、`seed`、`queue_entry_id`、`username`、`width/height`（`_webui_explicit_illustrious_payload_dimensions` main.py:13755）
  - 姿态：`pose_image`（data_url ≤8MB）、`pose_prompt`、`include_pose_prompt`、`state{camera,options}`、`controlnet_name`、`strength`、`start_percent`、`end_percent`、`free_memory_before`、`manual_control`
- **敏感图遮罩**：meta 中 `generated_output_audit*` / `webui_draw_sensitive` / `webui_draw_reveal_required` → 响应 `sensitive/reveal_required/safety`（`_draw_safety_payload` gpt_magic.py:3786）。

## 2. GenerationRequest 覆盖核对

| 现有链路输入 | 契约字段 | 状态 |
| --- | --- | --- |
| prompt（≤4000） | `prompt: str` | ✅ 覆盖 |
| seed（提示词内 `seed=123` 或显式） | `seed: int \| null` | ✅ 覆盖 |
| 尺寸（WebUI 显式宽高） | `width/height: int \| null` | ✅ 覆盖（需注明仅 Illustrious 显式尺寸链路） |
| 图生图源图 | `source_images: [data_url] \| [path]` | ⚠️ **扩展1**：现 Web 链路无源图上传端点（img2img 仅消息域 `_extract_source_image_path`）。桌面 v1 必须新增 `source_images` 字段或 `POST /api/v1/upload` |
| 多角色（multi-LoRA） | `loras: [{lora_name,strength_model,strength_clip,char_prompt,role_identity_prompt,role_aux_prompt,global_prompt,interaction_prompt,contamination_negative_prompt,prompt_only,interaction_intent}]` | ⚠️ **扩展2**：现 `set_multi_loras`（comfyui_api.py:359）已定义完整条目结构，v1 契约需显式声明（字段已存在，属契约冻结） |
| 模型族 / checkpoint / 画风预设 | `model_family: "anima"\|"illustrious"\|null`、`style_preset: str\|null`、`checkpoint: str\|null` | ⚠️ **扩展3**：现链路是隐式状态（`active_model_family`/`runtime_generation_preset`），v1 需把「每次请求可覆盖」声明为可选字段 |
| 姿态控制 | `pose: {image_data_url, controlnet_name, strength, start_percent, end_percent, include_pose_prompt, free_memory_before}` | ✅ 覆盖（`_handle_webui_pose_generate` 入参即此形状） |
| 用户名/审计上下文 | `username: str` | ✅ 覆盖（现 `webui_user` meta） |
| 队列条目关联 | `job_id` 由响应返回 | ✅ 覆盖 |

## 3. GenerationResult 覆盖核对

| 现有链路输出 | 契约字段 | 状态 |
| --- | --- | --- |
| 图片 | `image: data_url`、`image_path: str` | ✅ 覆盖（现 `_handle_webui_draw_generate` 返回两者） |
| 提示词 | `prompt`（最终）、`raw_prompt` | ✅ 覆盖 |
| 耗时 | `elapsed_seconds` | ✅ 覆盖 |
| 生成信息（UI 显示） | `info: {raw_prompt, final_prompt, character, lora, seed, workflow, model_family, size, width, height, bytes, elapsed_seconds, watermark_applied, watermark}` | ✅ 覆盖（`_draw_result_info` gpt_magic.py:3811 结构原样） |
| 审核/遮罩 | `sensitive: bool`、`reveal_required: bool`、`safety: {sensitive, reveal_required, audit_status, risk, threshold, backend, reason, message}` | ✅ 覆盖 |
| 完整 meta（调试/复现） | `meta: dict` | ✅ 覆盖（⚠️ 键清单需冻结，见 §7） |
| 历史 ID（供收藏/下载） | `history_id` | ⚠️ **扩展4**：现链路收藏/下载走 history 端点，v1 需在 Result 中显式返回 `history_id` 与 `expires_at`（现 `_draw_job_payload` 里 history 嵌套了 id，但顶层未扁平化） |
| 失败信息 | `error`（job 层） | ✅ 覆盖（但结构松散，见 §5 AppError） |

## 4. JobSnapshot 覆盖核对

| 现有链路 | 契约字段 | 状态 |
| --- | --- | --- |
| 状态枚举 | `status: preparing|queued|running|done|error`（+v1 增加 `interrupted`，lifecycle.md §8） | ⚠️ **扩展5**：v1 增补 `interrupted` 枚举值（只加不改） |
| 队列位置/等待 | `position`、`wait_seconds`、`wait_text`、`queue_info{ahead,estimated_wait_seconds}` | ✅ 覆盖（`_register_webui_draw_queue_entry` main.py:12478） |
| 结果 | `history`（嵌套完整 payload）+ `image_url` | ✅ 覆盖（`_draw_job_payload` gpt_magic.py:3873） |
| 错误 | `error: str` | ⚠️ 结构升级见 §5 |
| 取消 | 无 | ⚠️ **扩展6**：v1 需 `POST /api/v1/jobs/{id}/cancel`（现链路无取消；`_draw_job_task_done` 仅处理 task.cancel 兜底）。新增端点，不改现有字段 |

## 5. AppError 结构化建议（兼容扩展，不动现有 error 字符串）

现链路错误是「面向人的字符串」（`_format_generation_error_for_user` main.py:21961、`_format_validation_missing_model_error` comfyui_api.py:4731、`_format_history_error` comfyui_api.py:3503）。v1 建议：

```json
{"error": {"code": "comfyui_validation_missing_model", "message": "ComfyUI 工作流校验失败：缺少模型文件…", "retryable": false, "detail": {...}}}
```
- 代码映射表（从现有判定函数提取，**全部已有判定依据**）：

| code | 判定来源（现有符号） | retryable |
| --- | --- | --- |
| `comfyui_offline` | `api` 未初始化 / `_queue_workflow` 连接失败（comfyui_api.py:4713） | true |
| `validation_missing_model` | `_format_validation_missing_model_error`（comfyui_api.py:4731） | false |
| `validation_lora_neutralized` | `_neutralize_lora_nodes` 重试路径（comfyui_api.py:821） | true（已自动重试一次） |
| `execution_error` | `_format_history_error`（comfyui_api.py:3503） | false |
| `oom` | `_looks_like_cuda_oom`（comfyui_api.py:4467） | true（已自动降级重试） |
| `cuda_runtime` | `_looks_like_cuda_runtime_failure`（comfyui_api.py:4477） | true |
| `queue_prompt_missing` | `_check_prompt_disappeared_from_queue`（comfyui_api.py:4800） | true |
| `black_screen` | `_is_blank_or_black_image` + `_black_screen_retry_settings`（comfyui_api.py:5890/main.py:25920） | true（已自动重试） |
| `prompt_too_long` / `empty_prompt` | `_handle_webui_draw_generate` 校验（main.py:13804） | false |
| `queue_full` | `_generation_queue_full_message`（main.py:12331） | true |
| `rate_limited` | `_is_lora_download_rate_limited`（main.py:20893，仅下载域） | true |

- **兼容规则**：v1 的 `/api/v1/*` 响应 `error` 字段允许同时存在 `code` 与 `message`；**其他 Agent 已冻结的 `error: str` 契约不修改**，新端点才用结构化格式。

## 6. /api/v1 端点覆盖核对

| v1 端点（建议） | 现有对应 | 覆盖度 |
| --- | --- | --- |
| `POST /api/v1/generate` | `POST /api/draw` | ✅（+扩展 1/2/3） |
| `GET /api/v1/jobs/{id}` | `GET /api/draw/{job_id}` | ✅（+扩展 5） |
| `POST /api/v1/jobs/{id}/cancel` | 无 | ⚠️ 新增（扩展 6） |
| `GET /api/v1/history` + `/{id}/image` | `GET /api/draw/history` + `/image` | ✅（保留 `ttl_hours`） |
| `POST /api/v1/history/{id}/favorite` | `POST /api/draw/history/{item_id}/favorite` | ✅（v1 若不做收藏 UI 可延后，但端点建议保留） |
| `GET /api/v1/loras` | `GET /api/draw/loras` | ✅（含 `live_checked/live_warning/stats`） |
| `GET /api/v1/state` | `GET /api/state` | ✅（裁剪 users/keys/analytics 为可选段） |
| `POST /api/v1/comfy/{action}` | `POST /api/comfy/model/{action}` | ✅（status/switch/save_comfy_settings；download_illustrious V1-NO） |
| `POST /api/v1/pose/generate` | `comfy_action_handler("pose_generate")` | ✅ |
| `POST /api/v1/upload` | 无 | ⚠️ 新增（扩展 1 的 img2img 支持） |
| WS `ws://…/api/v1/ws`（状态推送） | 无（现为轮询） | ⚠️ 新增（只增不替换；轮询端点保持可用） |

## 7. meta 键冻结清单（防 Agent 间撞名）

从现链路提取的稳定键（`_inject_params` comfyui_api.py:4045、`_draw_result_info`、`_handle_webui_draw_generate`）：
`workflow, model_family, model_family_label, model_names, checkpoint_name, checkpoint_override, input_id, output_id, prompt, prompt_key, negative_key, seed, seed_source, seed_count, lora_name, lora_names, lora_node_ids, lora_warning, strength_model, strength_clip, style_lora_name, style_lora_warning, multi_lora, multi_lora_mode, multi_lora_mode_effective, multi_lora_count, multi_lora_role_bindings, multi_lora_regional_warning, img2img_denoise, source_image, mode, elapsed_seconds, execution_error, oom_retry_after_free, prompt_id, image_filename, raw_prompt, translated_prompt, profile_meta*, watermark_applied, watermark, generated_output_audit*, webui_draw_sensitive, webui_draw_reveal_required, webui_draw_elapsed_seconds`
规则：Agent 新增 meta 键必须以 `winefox_` 前缀开头，或先在此清单登记。

## 8. sidecar ready JSON 覆盖核对（lifecycle.md §3 字段）

| 需求 | 字段 | 覆盖 |
| --- | --- | --- |
| UI 判断可用性 | `event:"sidecar_ready"`, `version`, `pid` | ✅ |
| API 地址与认证 | `api_base`, `api_token` | ✅ |
| 数据目录（恢复/迁移提示） | `data_dir`, `migrations` | ✅ |
| ComfyUI 初始状态 | `comfy:{server_address,connected,workflow,model_family,lora_count,checkpoint}` | ✅（源自 `_build_gpt_magic_comfy_status`） |
| 工作流清单（UI 展示可选项） | `comfy.workflows:[{filename,node_classes,missing}]` | ⚠️ **扩展7**：`_fallback_unsupported_workflow`（comfyui_api.py:3882）已有校验数据，v1 增加 `workflows` 数组（可选段，不阻塞 ready） |

## 9. 结论清单（给其他 Agent 的契约冻结建议）

| 编号 | 动作 | 类型 |
| --- | --- | --- |
| C1 | `GenerationRequest` 增加 `source_images: [string] \| null`（data_url 或本地路径）+ `POST /api/v1/upload` | 兼容扩展 |
| C2 | `GenerationRequest.loras` 显式声明条目结构（来自 `set_multi_loras` comfyui_api.py:359 的 13 字段） | 契约冻结（字段已存在） |
| C3 | `GenerationRequest` 增加 `model_family/style_preset/checkpoint` 可选覆盖字段 | 兼容扩展 |
| C4 | `GenerationResult` 顶层扁平化增加 `history_id`、`expires_at` | 兼容扩展 |
| C5 | `JobSnapshot.status` 增补 `interrupted` 枚举（只加不改） | 兼容扩展 |
| C6 | 新增 `POST /api/v1/jobs/{id}/cancel`、`POST /api/v1/upload`、WS 端点 | 新增端点 |
| C7 | `sidecar ready JSON.comfy.workflows` 可选数组 | 兼容扩展 |
| C8 | `AppError` 结构化仅用于新端点；既有 `error: str` 不修改 | 兼容规则 |
| C9 | meta 新键需登记（§7 清单） | 协作规则 |

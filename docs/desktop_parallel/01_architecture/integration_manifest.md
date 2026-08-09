# Integration Manifest：Agent 02–10 集成顺序与验收

> 本文按「winefox-desktop 目标仓库」约定路径（`winefox_core/…`、`desktop_api/…`、`ui/…`、`tauri/…`、`astrbot_adapter/…`）。
> 假设 Agent 分工（**以并行计划书为准，若编号不同按模块名对齐**）：
> 02=CoreEngine+状态机，03=JobManager，04=ComfyUI Transport，05=Prompt/LoRA，06=Storage，07=Desktop API(/api/v1)，08=UI(webview)，09=Tauri 壳+sidecar 进程，10=AstrBot Adapter。
> 集成顺序由依赖方向决定：**Storage → Transport → Prompt/LoRA → JobManager → CoreEngine → Desktop API → UI → Tauri → Adapter**。

## 1. 集成顺序总表

| 次序 | 阶段 | 集成方 | 依赖 | 里程碑（可运行验证） |
| --- | --- | --- | --- | --- |
| 1 | S0 | 06 Storage | 无（契约仅文件布局） | `python -m pytest storage`：db.json 读写、TTL 清理、收藏 sidecar |
| 2 | S0 | 04 ComfyUI Transport | 无（config dict 注入） | 连真实/桩 ComfyUI：fetch_lora_list、generate、黑屏检测 |
| 3 | S0 | 05 Prompt/LoRA | 无（纯函数 + Provider 接口） | 意图识别、LoRA 匹配单测通过 |
| 4 | S1 | 03 JobManager | 06, 04, 05 | 队列状态机单测：preparing→queued→running→done/error |
| 5 | S1 | 02 CoreEngine | 03, 06, 04, 05 | `core.start()` 无 astrbot 依赖可导入；状态广播事件 |
| 6 | S2 | 07 Desktop API | 02 | `POST /api/v1/generate` 桩跑通（mock Transport） |
| 7 | S2 | 08 UI | 07（契约） | 页面加载 + 提交提示词 + 轮询 job + 显示图 |
| 8 | S3 | 09 Tauri 壳 | 07, 08 | sidecar 拉起 → ready JSON → 窗口；托盘/退出 |
| 9 | S3 | 10 AstrBot Adapter | 02 | 消息命令翻译层，与现插件并行冒烟 |
| 10 | S4 | 全员 | 全部 | 端到端验收（§5） |

## 2. 各 Agent 预期路径与交付物

| Agent | 预期路径（建议） | 交付物 | 从本仓库迁入的源码块 |
| --- | --- | --- | --- |
| 06 Storage | `winefox_core/storage/` | `store.py`、`favorite_store.py`、`draw_history.py`、`schema.py` | `GptMagicStore`（gpt_magic.py:640-2364）裁减：用户/额度/卡密/画廊账户可 V1-NO 或最小化 |
| 04 Transport | `winefox_core/comfy/` | `client.py`（httpx）、`workflow_injector.py`、`lora_injector.py`、`errors.py`、`models.py` | `ComfyUI` 类（comfyui_api.py:83-6371）按 §3 拆 4 文件；`_conf_schema.json` 的 `lora_settings`/`img2img_settings` 键不动 |
| 05 Prompt/LoRA | `winefox_core/prompt/` | `intent.py`、`profile_store.py`、`translator.py`、`stabilizers.py`、`llm_provider.py` | `intent_utils.py` 全量；`LoraProfileStore`（civitai_api.py:2292-2639）；main.py 4800-8400 纯函数区 |
| 03 JobManager | `winefox_core/jobs/` | `manager.py`、`queue.py`、`lock.py`、`recovery.py`、`snapshot.py` | `_generation_queue_entries` 结构 + `_register_webui_draw_queue_entry`（main.py:12478）+ 锁家族（main.py:12113-12157）+ `GptMagicService._dispatch_locked/_task_done`（gpt_magic.py:2882-2955） |
| 02 CoreEngine | `winefox_core/engine.py` + `winefox_core/state.py` | 门面 API、状态事件、配置装配、sidecar 入口 `winefox_core/__main__.py` | `ComfyUIPlugin.__init__` 装配顺序（main.py:883-1400）+ `initialize()`（main.py:18392）+ `_build_gpt_magic_comfy_status`（main.py:13071） |
| 07 Desktop API | `desktop_api/` | `app.py`（FastAPI 或 asyncio 原生）、`routes_generate.py`、`routes_history.py`、`routes_comfy.py`、`auth.py`、`ws.py` | `FavoriteGalleryWebUI.draw/draw_status/draw_history/draw_loras/draw_history_favorite`（gpt_magic.py:3975-4270）+ `GptMagicWebUI.state/comfy_model_action`（gpt_magic.py:5538-5578）+ `_draw_safety_payload`（gpt_magic.py:3786） |
| 08 UI | `ui/` | `winefox_draw.html`（改 fetch 路径）、`assets/` | `templates/winefox_draw.html` 全量 + `assets/vendor/three/*` |
| 09 Tauri | `tauri/` | Rust 壳：sidecar spawn、ready 解析、托盘、退出协议、锁文件 | 无源码迁移；协议见 lifecycle.md §3/§7 |
| 10 Adapter | `astrbot_adapter/` | `plugin.py`（Star 子类）、`commands.py`、`llm_tool.py` | main.py 中 `@filter.command` 处理器 40 个、`@filter.on_llm_request`（main.py:15997）、`initialize/terminate`、`jiuhu_draw_image` 工具（main.py:12591-12667） |

## 3. 每个 Agent 的检查项

### 06 Storage
- [ ] `db.json` 字段与现有 `GptMagicStore._load` 默认值兼容（users/keys/history/web_draw_history/cache/gallery_accounts/gallery_access_log/astrbot_config_snapshot）
- [ ] `web_draw_history` TTL（24h 默认，`_web_draw_history_ttl_ms`）与账户隔离逻辑（`_web_draw_history_allowed`）保留
- [ ] favorites sidecar（`*.favorite.json`）与 sha256 去重保留
- [ ] 写盘原子性（临时文件+rename）优于现有全量覆写

### 04 ComfyUI Transport
- [ ] 元组契约 `(bytes|None, str|None, dict)` 不变（generate/generate_video）
- [ ] 超时参数族从 `self.config` 读取（`lora_settings.comfyui_connect_timeout` 等）保留
- [ ] 错误归类：`_format_validation_missing_model_error`（模型缺失）、`_looks_like_cuda_oom`（OOM 重试）、`_looks_like_multi_lora_regional_failure`（safe_chain 回退）→ 统一映射为 `AppError.code`（contract_review.md §5）
- [ ] `_is_blank_or_black_image` 测试（tests/test_black_screen_detect.py）随迁

### 05 Prompt/LoRA
- [ ] `illustrious_has_multi_character_intent` 原样保留（tests 有覆盖）
- [ ] LLM 调用收敛为 `LLMProvider` 接口（现状 8 个散点：`_call_plugin_llm_json` main.py:9713、`_post_llm_chat_with_retry` main.py:9216、`_reverse_img2img_prompt_with_vision_llm` 9357、`_compose_wan_i2v_prompt_with_text_llm` 9501、`_enhance_wan_i2v_prompt_with_vision_llm` 9569、`_run_llm_composition_director` 9968、`_run_llm_multi_character_region_director` 10209、`_match_lora_profile_with_llm` 10592）
- [ ] 未配置 LLM 时降级路径（`_final_prompt_should_skip_llm` main.py:8437）保留

### 03 JobManager
- [ ] JobSnapshot 状态机合法迁移（lifecycle.md §8）
- [ ] 并发上限（`max_concurrent_tasks`，默认 2）+ 生成互斥锁（现 `_generation_state_lock`）语义
- [ ] `estimated_wait_seconds`（最近 12 次时长滑动平均）保留
- [ ] 远端队列核对（45s grace + 3 次确认）委托给 Transport 的 `check_prompt_disappeared`，不复制逻辑

### 02 CoreEngine
- [ ] 全仓 `grep -ri "astrbot" winefox_core/` 为空；`grep -ri "aiohttp" winefox_core/` 为空（依赖边界 R1/R2）
- [ ] `core.start()`/`core.stop()` 幂等；重复 start 抛明确错误
- [ ] 状态事件（comfy_status/job_status/queue）有统一订阅接口
- [ ] sidecar ready JSON 按 lifecycle.md §3 输出

### 07 Desktop API
- [ ] `/api/v1/*` 路由与 contract_review.md 一致（含错误格式）
- [ ] token 认证用 `secrets.compare_digest`（现 gpt_magic.py:5443）
- [ ] 敏感图遮罩逻辑（`_draw_safety_payload`）随 UI 契约保留：`sensitive/reveal_required/safety` 字段
- [ ] 静态资源路径安全（现 `static_asset` 的 resolve 校验，gpt_magic.py:5533）保留

### 08 UI
- [ ] 页面无 `/api/draw`、`/api/state` 残留；全部改 `/api/v1/*`
- [ ] three.js 姿态编辑器可运行（assets/vendor/three/ 三个文件 + license）
- [ ] 断线重连：WS 断开 → 显示重连 → 拉 `/api/v1/state`
- [ ] 黑屏/审核遮罩展示路径（`reveal_required`）可用

### 09 Tauri
- [ ] sidecar 拉起/ready 超时（30s）/崩溃重启（3 次退避）
- [ ] 托盘关闭=隐藏（sidecar 继续）、Quit=优雅退出（5s 超时强杀）
- [ ] 多实例锁（data_dir/.winefox.lock）
- [ ] Python 运行时发现（venv 路径、`sys.executable`）

### 10 AstrBot Adapter
- [ ] 命令翻译层不直接碰 `api`/`store` 内部（只调 CoreEngine 门面）
- [ ] `initialize/terminate` 调 `core.start/stop`
- [ ] 与现插件功能对拍：`酒狐来点` 消息链路输出一致（消息渲染差异允许）
- [ ] Adapter 侧保留 `_handle_gpt_magic_model_action` 等价物转发

## 4. 潜在冲突与协调

| 冲突面 | 说明 | 处置 |
| --- | --- | --- |
| Storage 文件锁 | 现 `GptMagicStore.save()` 全量写 `db.json`；若 Adapter 与 sidecar 同时跑（S3 并行期）会互踩 | 并行期 Adapter 用只读模式打开旧数据目录；正式版数据目录分离（`data/` vs `data_legacy/`） |
| 端口占用 | 现 AstrBot WebUI 占用 1144/6666；Desktop API 建议 18780 | 18780 由 sidecar 动态分配写进 ready JSON；1144/6666 归属 Adapter 不动 |
| `api` 属性名 | main.py 全仓用 `self.api` 指 `ComfyUI` 实例（main.py:1396 `self.api = ComfyUI(...)`）；v1 里 `api` 指 Desktop API | Transport 模块内命名 `client`/`transport`，禁止导出 `api` |
| `meta` dict 漂移 | 生成 meta 是自由 dict（`_inject_params` comfyui_api.py:4045 起 60+ 键） | v1 冻结为 `GenerationResult.meta`（见 contract_review.md §3），新增键需追加而非覆盖 |
| 队列双轨 | 现消息队列与 Web 队列共用 `_generation_queue_entries`（main.py:12441 vs 12478） | v1 JobManager 单一队列；Adapter 请求也走同一队列（`kind=paint|web_draw` 字段保留） |
| 配置同步 | `GptMagicStore._sync_astrbot_config_overrides`（gpt_magic.py:734）把 AstrBot 配置镜像进 db.json | v1 无 AstrBot，删除该同步；config 单一来源是 `config.json` |
| 测试基建 | tests/ 两个文件 import astrbot stub（tests/test_black_screen_detect.py:28 `_ensure_astrbot_stub`） | v1 测试不再需要 stub；Adapter 测试保留 stub 模式 |

## 5. 集成后测试（端到端验收脚本）

```bash
# S0（单元）
python -m unittest discover -s winefox_core/storage/tests -v
python -m unittest discover -s winefox_core/comfy/tests -v    # 含桩 ComfyUI（aiohttp 桩服务器回放 /prompt /history /view）
python -m unittest discover -s winefox_core/prompt/tests -v

# S1（集成，桩 Transport）
python -m unittest discover -s winefox_core/jobs/tests -v
python -m unittest discover -s winefox_core/engine/tests -v   # start/stop/ready JSON

# S2（HTTP 冒烟）
curl -s -X POST http://127.0.0.1:18780/api/v1/generate -H "Authorization: Bearer $TOKEN" \
     -d '{"prompt":"1girl, smile"}'                          # → {"job_id": ...}
curl -s http://127.0.0.1:18780/api/v1/jobs/{job_id}          # → status 轮询到 done
curl -s http://127.0.0.1:18780/api/v1/history                # → 含刚完成的 item，24h TTL

# S3（真实链路，需本机 ComfyUI）
python -m winefox_core --config test.json --no-desktop        # 命令行模式：提交 3 个任务，验证互斥锁与并发=1
# 与真实 ComfyUI 对拍：同一提示词，现插件 vs sidecar 输出 meta 的 seed/workflow/model_family 一致

# S4（全量）
pytest tauri/ ui/ desktop_api/ winefox_core/ --e2e           # 每 Agent 至少 1 个端到端用例
```

## 6. Patch 可直接应用 vs 需手工连接

| 交付 | 类型 | 说明 |
| --- | --- | --- |
| `intent_utils.py`、`watermark.py` | **可直接应用**（零外部依赖，原样复制） | 文件级 copy 即可，测试随迁 |
| `comfyui_api.py` 拆分 | **需手工连接**（约 20% 机械替换 + 80% 接口设计） | 先做 `logging` 替换与 `client.py` 抽取；`_apply_lora_override` 等多 LoRA 注入保留原函数体 |
| `GptMagicStore` | **需手工连接**（裁剪 + 原子写） | V1-NO 字段（keys/gallery_accounts）保留读兼容，删除写路径 |
| `FavoriteGalleryWebUI.draw*` | **需手工连接**（路由改名 + 认证头变化） | 处理器函数体可整体搬移；`_draw_job_payload` 原样保留 |
| `templates/winefox_draw.html` | **可直接应用 + 少量修改**（fetch 路径替换 + 删除画廊/登录面板） | 建议保留原文件为 `ui/legacy/` 对照 |
| `main.py` 纯函数区（4800-8400） | **需手工连接**（按领域切片，量大） | 建议先迁 `_match_lora_profile*` 依赖链（`_profile_*` 家族） |
| `_conf_schema.json` | **可直接应用**（配置结构冻结） | v1 config.json 直接采用该结构 |
| `workflow/*.json`（19 个） | **可直接应用** | 原样复制到 `winefox_core/data/workflow/` |
| AstrBot 命令处理器 | **需手工连接**（Adapter 编写，非复制） | 每条命令重写为门面调用，逻辑裁剪到参数解析+渲染 |

## 7. 风险清单（集成期重点盯防）

1. **meta dict 键冲突**：多个 Agent 各自新增 meta 键时撞名（如 `elapsed_seconds`、`workflow`）→ contract_review.md §3 冻结键清单，新增走追加。
2. **aiohttp 残留**：Transport 拆包时漏改 `_client_session` 调用点（comfyui_api.py:296）→ 用 `grep aiohttp` CI 门禁。
3. **队列锁死锁**：JobManager 复用 `_finalize_generation_queue_and_lock` 的 finally 语义（main.py:12188 在生成器 finally 里释放）→ 单测覆盖取消路径。
4. **sidecar 与 Adapter 并行写数据**：S3 并行期隔离数据目录。
5. **UI 合约漂移**：UI 先写死 `/api/v1` 契约，Desktop API 后实现 → 用 contract_review.md 作为唯一契约源，UI 的 mock 服务器按契约生成。

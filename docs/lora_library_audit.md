# LoRA 库移植对照记录（原能力 → 当前实现 → 本次落点）

生成日期：2026-08-13
范围：`E:\astrbot_plugin_comfyui_pro_UltraPlusMax-master`（只读参考）→ `D:\TaskALLforComfyUIdesktop\integrated\WineFoxDesktop`
方法：CodeGraph 索引 + 逐项源码核对（以源码为准）。

## 0. 基线

| 套件 | 结果 |
|---|---|
| 桌面端 `tests_desktop + desktop_integration_tests` | 588 passed, 10 deselected, 2 xfailed, 21 subtests |
| 原插件 `tests/test_stability_static.py` | 46 passed, 867 subtests passed |

## 1. 架构事实

- 桌面端根目录 `main.py`/`gpt_magic.py` 与原插件**逐字节相同**，但不被桌面运行时引用（AstrBot 耦合的遗留插件）。
- 桌面端 `civitai_api.py`（129 KB）是**桌面适配版**（md5 与原插件不同），已含 `CivitaiClient.search_loras`、`CivitaiRedClient`、`HuggingFaceClient`、`YodayoClient`、JSON 版 `LoraProfileStore`（civitai_api.py:2297）；`desktop_runtime/lora_download.py:24` 直接从中导入。
- 桌面端存在**两个同名 `LoraProfileStore`**：SQLite 版（`winefox_core/storage.py:835`，引擎匹配用）与 JSON 版（`civitai_api.py:2297`，Civitai 下载产物）。本次落点统一为 SQLite 真相源。

## 2. 能力对照表

| # | 原能力（原插件 file:line） | 桌面端当前实现 | 本次落点 |
|---|---|---|---|
| 1 | 搜索编排 `_search_civitai_anima_candidates`（main.py:23154）：本地种子→角色关系→拼写变体→有界并发(Semaphore 4)→别名发现→utility 扩展→兜底/LLM rescue；choice_limit 上限 6 | `LoraDownloadService.search`（lora_download.py:133）仅单源 `search_loras` 直传，无种子/变体/别名发现/兜底 | 新增 `desktop_runtime/lora_search.py` 编排服务（AstrBot-free 移植），复用现有 `CivitaiClient`；落点默认 12/上限 20 |
| 2 | 排序 `_score_civitai_candidate`（main.py:23006）：`round((65*log10(h+1)+35*log10(d+1))*(1+min(max(rel,0),6)*0.12)-max(src,0)*1.5,2)` + 标题+80/中文标题+60/可信中文标题+160/作品命中+180/缺失-45 | 无排序（原顺序直传） | 移植为搜索服务的排序内核 |
| 3 | 候选过滤 `_is_civitai_search_candidate_allowed`（main.py:22966）阻塞 ControlNet 等伪 LoRA；来源冲突跳过/缺失降权 | 无过滤 | 移植 |
| 4 | 搜索客户端 `CivitaiClient.search_loras`（civitai_api.py:881，网页+Meili+REST 三层）、`CivitaiRedClient`(1944)、`HuggingFaceClient`(1961) | 桌面 `civitai_api.py` 已含三者，`search()` 已用 `CivitaiClient.search_loras` | 复用；默认启用 Civitai+HF；Yodayo 不入本期 |
| 5 | 档案提炼 `_refine_civitai_profile_with_llm`（main.py:21484）：JSON {character, aliases≤8, activation_tags≤6, identity_appearance≤16, default_outfit≤16}，失败降级原样 | 无（下载后 JSON upsert 无 LLM 提炼） | 新增 LLM 档案提炼服务（仅 LLM 已配置且开关开时） |
| 6 | 档案存储 `LoraProfileStore.upsert_from_civitai`（civitai_api.py:2498）；identity 权重 llm_character 100/model_title 85/version_title 70/trigger 62/file 45/alias 35；JSON 字段契约逐字列于报告 | SQLite `lora_profiles` 表（storage.py:835）+ JSON 双轨；engine 启动从 JSON 读 | 统一：SQLite `lora_profiles` 唯一真相源；启动幂等导入 JSON；JSON 仅作兼容导入/显式导出 |
| 7 | 匹配 `_best_lora_profile_match` 族（main.py:4534-4804）：exact 100/58、fuzzy 76+ratio*12、CJK 阈值 0.74(len≤4)/0.80、英文 0.88(len≥7)/0.92、sort(score,source_rank,marker_len,len)、top-2 前三元组相同判歧义拒绝、短 CJK 安全边界、身份冲突自动禁用、共享别名阻断 | 简化版 `LoraService.match`（lora_service.py:493）：exact 100 分/fuzzy 70 分、单阈值 75、无歧义/边界/冲突 | 用原算法替换简化版 |
| 8 | 提示词注入 `_prepare_prompt_with_lora_profile`（main.py:11174）：多人连接词/容量截断/强度上限/可用性守卫/警告；`_build_lora_smart_prompt`(6585) | `resolve_prompt_loras`（lora_profiles.py:448）已实现基础注入+触发器合并 | 增强：证据记录、排除项、跨族建议、历史保存最终证据 |
| 9 | 下载：并发 gather + `.part` 临时文件 + `.parts` 分片断点 + 大小校验（civitai_api.py:1618-1960）；隔离目录 `deleted_loras/{stamp}_{name}`（main.py:11710）。**无状态机/SHA256/磁盘预检/同名重命名** | `LoraDownloadService`（lora_download.py）内存态 `queued→resolving→downloading→profiling→completed`，本地/Bridge 双模式，无持久化 | 新增 `lora_download_jobs` 持久化 + 状态机补充 `refreshing/interrupted` + SHA256/预期大小校验 + 同名异源新文件名 + 磁盘预检 + 重启中断恢复（这些都是桌面端新增需求，非原插件存在） |
| 10 | 管理员 API：AstrBot 聊天命令 `/jh`（main.py:19232）分发，**无 HTTP** | `/api/v1/lora/search|downloads|downloads/{id}|bridge/probe`（handlers.py）已存在 | 扩展为完整 REST：本地库列表/详情/刷新、详情、受控链接解析、取消/重试、档案 CRUD、隔离/恢复、匹配预览、同源预览图 |
| 11 | 预览/详情 | 无预览图；search 返回不含触发词证据 | 后端下载预览到 `cache/lora_previews`，同源预览 API 返回；详情阶段解析文件/大小/哈希/正文 |
| 12 | R18 门禁 | 无 | 开关默认关 + `adult-content-v1` 同意模态 + Red 门禁 + DOM 清理 |
| 13 | 设置 | desktop.json `lora_download`（config_adapter.py）无 `lora_library` 段；`SECRET_NAMES` 无 `huggingface_api_token` | 新增 `lora_library` 设置段 + `huggingface_api_token` 密钥 |

## 3. 必须剥离的 AstrBot 耦合（严禁带入桌面核心）

`astrbot.api` 导入、全部 `cmd_*` 处理器、`AstrMessageEvent`/消息链/会话（`_paintable_role_list_sessions`）、权限模型（`_can_manage_lora`/`_is_super_admin`）、`filter` 装饰器与消息组件、`self.config.get("lora_settings")`、`_trace_test_mode` 的 event 透传、AstrBot logger。只移植纯函数/常量/数据契约。

## 4. 安全红线（本次实现约束）

- 日志/DB/API 禁止保存带 token 下载 URL、Cookie、Authorization、绝对路径。
- WebView 不直接加载第三方图片；预览经同源 API + 会话校验 + 魔数/像素校验。
- 浏览器不得传入下载 URL/磁盘路径/凭据。
- 禁止永久删除用户 LoRA；删除只删索引，隔离只移入可恢复目录且恢复仅回原位。

## 5. 最终审查结论（8.6.7）

- SQLite `lora_profiles` 是唯一运行时真相源；JSON 只允许一次性、insert-only 兼容导入和显式导出，不得在重启时覆盖 SQLite 编辑。
- 用户提交的新别名替换旧别名；`disabled`、`quarantined`、非 installed 和 `file_missing` 资产全部禁止自动引用。
- 本地模式的隔离/恢复移动真实文件并提供失败回滚；Bridge/远程清单只读且允许受控下载，不提供删除或远程文件管理协议。
- `LoraLlmMatchService` 已接入匹配预览的保守兜底；无明确提示词证据、低置信或歧义结果不会自动引用。
- Bridge 1.4.2 仅接受 `X-AstrBot-Bridge-Token`，回环地址以外一律鉴权；下载和 `/fetch` 都执行 HTTPS、白名单、公网 DNS 与逐次重定向门禁。
- Bridge 只接收 `.safetensors`，落盘前校验 JSON 头、张量偏移和完整数据区；永久删除路由不存在。

最终回归基线：桌面套件 727 passed / 3 skipped，原插件套件 56 passed / 867 subtests，Rust 67 passed，sidecar 协议 22 passed，桌面集成 57 passed / 1 个遗留插件基线 xfail。发布版本为 WineFox 8.6.7，内置 Bridge 1.4.2。

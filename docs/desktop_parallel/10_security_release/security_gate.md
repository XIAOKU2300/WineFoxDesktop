# WineFox Desktop 安全门禁（Security Gate）

维护者：Agent 10（quality-security）｜基线版本：插件 8.5.9（`astrbot_plugin_comfyui_pro_UltraPlusMax`）

本文档是发布安全门禁的唯一权威清单。硬门禁（HARD）不通过不得发版；
软门禁（SOFT）不通过需要发布经理书面豁免。

## 门禁总表

| ID | 门禁 | 级别 | 现状（基线 8.5.9） | 验收方法 |
|----|------|------|--------------------|----------|
| G01 | 无 LAN 监听（desktop 仅 127.0.0.1） | HARD | 等待集成（desktop 后端未合并） | `tools/desktop_verify.py` G01 + 安装 smoke |
| G02 | 无默认密码（固定密码 `REDACTED_LEGACY_PASSWORD` 不得出厂） | HARD | **发现项**：`_conf_schema.json` 出厂默认含 `admin_password=REDACTED_LEGACY_PASSWORD`、`archive_password=114514`、`password=114514`（插件基线，不修改）；桌面层首启必须强制改密 | `desktop_verify.py` G02 + `/api/v1` 登录测试 |
| G03 | 无固定公网 URL（不硬编码公网服务器） | HARD | 通过（插件无固定公网 URL；配置默认 `127.0.0.1:8188`） | `desktop_verify.py` G03 |
| G04 | 无 query token（鉴权令牌禁止走查询串） | HARD | **发现项**：LoRA Bridge 接受 `?token=`；桌面层必须剥离 | `desktop_verify.py` G04 + Bridge 测试 |
| G05 | 未授权访问必须 401 | HARD | 等待集成（桌面 API）；插件侧 `_check_auth` 已实现 | `/api/v1` 契约测试 `TestAuth` |
| G06 | Host / Origin / CSRF 校验 | HARD | 等待集成（桌面 API 中间件） | `/api/v1` 契约测试 + 手动验证 |
| G07 | 密钥不落 JSON / SQLite / 日志 / 崩溃报告 | HARD | 通过（`_redact_sensitive_text` + 静态扫描） | 硬门禁测试 `TestSecretStorage` + `desktop_verify.py` G05 |
| G08 | TLS 默认验证（出站 TLS 校验默认开启） | HARD | 部分：插件 `server_ssl_verify` 默认 false 仅作用于用户配置的 ComfyUI 端点（有 schema hint）；桌面层全局默认必须为 true | `desktop_verify.py` G06 + 配置审查 |
| G09 | 路径穿越失败（上传/文件名） | HARD | 通过（Bridge `_safe_*_filename` + Fake 服务器 400） | 硬门禁测试 + 集成测试 |
| G10 | 超大上传失败（413，不挂起） | HARD | 通过（客户端将 413 上抛为错误） | 集成测试 `test_oversized_upload_fails_with_status` |
| G11 | Bridge 无 Token 401 | HARD | 通过（`allow_local_requests_without_token=false` 时远端无 Token 拒绝） | 硬门禁测试 `TestBridgeAuth` |
| G12 | 安装包与更新清单签名 | HARD | 等待集成（Agent 09 打包 + 签名证书） | 发布门禁：签名校验脚本（TODO） |
| G13 | 不捆绑模型（v1 不含 TensorFlow/模型权重） | HARD | 通过（仓库无模型文件；nsfw 模型运行时下载） | `desktop_verify.py` G07 |
| G14 | 无 source map / debug / mock 出厂 | SOFT | 通过（仓库无 `.map`；前端由 Agent 08 保证） | `desktop_verify.py` G08 |

## 关键实现与验证位置

- 安全检查器：`tools/desktop_verify.py`（G01–G08 对应 G01..G08 检查项，只读）。
- 硬门禁自动化测试：`desktop_integration_tests/test_security_hard_gates.py`。
- 桌面 /api/v1 契约测试（合并后自动激活）：`desktop_integration_tests/test_api_v1_integration.py`。
- CI：`.github/workflows/desktop-ci.yml`（`verify-tool`、`integration-fake-comfyui` 为 REAL 作业；
  其余 GATE 作业在 `workflow_dispatch gates=true` 时按设计失败）。

## 已发现并必须由桌面层整改的项（基线内无法修改）

1. **G02**：`_conf_schema.json` 出厂默认密码：`gpt_magic_settings.gallery.admin_password='REDACTED_LEGACY_PASSWORD'`、
   `video_settings.archive_password='114514'`、`comfy_nsfw_self_review.password='114514'`。
   桌面层必须在首次启动强制改密并在出厂配置中移除固定默认值；
   对应测试 `test_config_schema_ships_no_fixed_passwords`（XFAIL 记录）。
2. **G04**：`comfyui_custom_nodes/ComfyUI-AstrBot-LoraBridge/__init__.py` 的
   `_request_token()` 接受 `?token=` 查询参数。桌面集成时必须：
   - 只允许 `X-AstrBot-Bridge-Token` 请求头；
   - 在反向代理/桌面网关剥离查询串中的 token；
   - 对应测试 `test_remote_with_correct_query_token_accepted_but_flagged`（XFAIL 记录）。
3. **G08**：桌面 `settings.json` 必须显式 `"tls_verify": true`（`desktop_verify.py` G06 强制）。
   插件基线 `server_ssl_verify` 默认 false 仅作用于用户配置的 ComfyUI 端点（schema hint 已说明）。

## 未授权 401 契约

| 场景 | 期望 |
|------|------|
| 无会话访问 `/api/v1/settings` | 401 `{"error":"unauthorized"}` |
| 默认/空密码登录 | 401 |
| `?token=` 查询鉴权 | 401 |
| Bridge 远端无 Token | 401（`allow_local_requests_without_token=false`） |
| Bridge 远端错误 Token | 401 |

## 发布放行条件

全部 HARD 门禁 PASS，且 `tools/desktop_verify.py` 输出 `passed: true`，
且集成测试套件无 FAIL（桌面契约测试可 SKIP，不可 FAIL 未实现项之外的内容）。

# 集成测试矩阵（Integration Test Matrix）

维护者：Agent 10（quality-security）｜基线：插件 8.5.9

## 运行方式

```bash
# 全部可运行测试（默认排除 desktop_gate）
python -m pytest desktop_integration_tests/ -c desktop_integration_tests/pytest.ini

# 仅看 desktop 契约测试（合并后自动激活，当前 SKIP）
python -m pytest desktop_integration_tests/test_api_v1_integration.py \
  -c desktop_integration_tests/pytest.ini -m desktop_gate -rs

# 硬门禁
python -m pytest desktop_integration_tests/test_security_hard_gates.py -v
```

## 测试资产

- **Fake ComfyUI**：`desktop_integration_tests/fake_comfyui/server.py`。
  端点：`/object_info`、`/models/loras`、`/upload/image`、`/prompt`、`/queue`、
  `/history/{id}`、`/view`、`/free`；可切换模式：`normal / auth_401 /
  connection_drop / missing_node / corrupt_image / view_500 / queue_lost /
  exec_error`；控制端点 `POST /__test/mode`。仅监听 127.0.0.1；不下载、不装模型。
- **生产面**：`comfyui_api.ComfyUI`（插件核心客户端），经 astrbot stub 隔离。

## 矩阵（当前可运行 → 绿）

| # | 场景 | 测试 | 通过 |
|---|------|------|------|
| 1 | bootstrap：地址规范化 / 数据目录 / 工作流加载 / 全部工作流可解析 | TestBootstrap | ✅ |
| 2 | 探针：/object_info 健康模式；401 时探针失败 | TestProbeAndLoras | ✅ |
| 3 | LoRA：object_info+models 合并、磁盘缓存、401 回退 | TestProbeAndLoras | ✅ |
| 4 | 上传：multipart、子目录、缺文件、413 超大、路径穿越 400 | TestUpload | ✅ |
| 5 | 任务：提交→轮询→下载 PNG（happy path，含 img2img 上传） | TestTaskLifecycle | ✅ |
| 6 | 轮询超时：`生成超时` 有界返回 | TestTaskLifecycle | ✅ |
| 7 | 历史：history 载荷形状（status/outputs/images） | TestHistoryFavorites | ✅ |
| 8 | 收藏：安全路径、唯一名、SHA-256 去重、穿越防护 | TestHistoryFavorites | ✅ |
| 9 | 恢复：队列丢失检测（grace+确认次数）、连接中断重试、执行错误上抛 | TestRecovery | ✅ |
| 10 | 错误码：401 / 400 校验（含 LoRA 中和重试）/ 下载重试失败 | TestErrorCodes | ✅ |
| 11 | 故障矩阵：8 种模式全部有界返回错误、可中途切换模式 | TestFaultMatrix | ✅ |
| 12 | 硬门禁：密钥不落日志/JSON、Bridge 401、穿越、TLS 作用域、仓库无真实密钥 | test_security_hard_gates | ✅（1 项 XFAIL 记录桥接 query-token 发现） |

## 矩阵（等待桌面层合并 → 当前 SKIP，激活后必须绿）

| # | 场景 | 契约测试 | 依赖 Agent |
|---|------|----------|-----------|
| D1 | `/api/v1/bootstrap`（app/version/apiVersion） | TestBootstrap | 07 |
| D2 | 未授权 401；空/默认密码 401；query token 401 | TestAuth | 07 |
| D3 | `/api/v1/settings` 密钥脱敏（10 个密钥字段不得泄漏） | TestSettingsMasking | 07 |
| D4 | `/api/v1/comfyui/probe` 对 Fake 服务器 ok | TestProbe | 07 |
| D5 | 上传→任务→轮询→出图全链路 | TestUploadTaskPoll | 07 |
| D6 | 历史列表 + 收藏保存 | TestHistoryFavorites | 07 |
| D7 | 队列丢失恢复：带类型化 `error_code` | TestRecovery | 07 |
| D8 | 错误码契约文档化（401/400/404/413/503） | TestErrorCodes | 07 |

## HTTP 错误码契约（桌面 /api/v1）

| 码 | 含义 | 负载 |
|----|------|------|
| 200 | 成功 | `{"ok": true, ...}` |
| 400 | 参数/工作流校验失败 | `{"ok": false, "error": "...", "code": "validation_error"}` |
| 401 | 未认证/令牌无效 | `{"ok": false, "error": "unauthorized", "code": "unauthorized"}` |
| 404 | 任务/资源不存在 | `{"ok": false, "error": "not_found", "code": "not_found"}` |
| 413 | 上传超限 | `{"ok": false, "error": "...", "code": "upload_too_large"}` |
| 429 | 频率限制 | `{"ok": false, "error": "...", "code": "rate_limited"}` |
| 503 | ComfyUI 不可达/队列丢失 | `{"ok": false, "error": "...", "code": "comfy_unreachable"}` 或 `"queue_lost"` |

## 环境与隔离

- 测试不访问外网；不使用真实密钥/付费 API；不下载模型；不安装 ComfyUI。
- 既有 54 测试（`tests/`）与集成套件互不依赖，可独立运行。

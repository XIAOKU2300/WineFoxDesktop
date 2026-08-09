# WineFox desktop sidecar（Agent 09 占位实现）

这是 WineFox Desktop 的 **fake sidecar**：真实后端（AstrBot + 插件 + ComfyUI
桥接）尚未提供，本实现以相同线协议承载 shell 开发与联调。替换真实 sidecar 时
只需满足下方协议，Tauri shell 无需改动。

## 线协议（Shell ↔ Sidecar，v1）

### stdout —— JSON Lines（唯一通道）

| 事件 | 字段 | 说明 |
|---|---|---|
| `ready`（首行） | `type,protocol,version,host,port,bootstrapPath,impl,implVersion,pid` | `protocol=1`；`version` = 应用版本（必须与 shell 版本一致）；`bootstrapPath` 为一次性 token 路径 |
| `status` | `busy,tasks,task,shutdown` | 任务状态变化即推送；`task` 可为 null |
| `log` | `level,message` | 可展示日志，禁止含密钥 |
| `ack` | `cmd,ok,...` | 命令应答（`ping`→`pong`、`shutdown`、`shutdown-cancel`） |
| `exit` | `code,reason` | 退出前最后一行；随后进程结束 |

非 JSON 的 stdout 行会被 shell 忽略并记警告。

### stderr —— 人类可读日志（shell 会脱敏：`key/token/secret/password/Authorization`、32+ 位 hex、40+ 位 base64、私钥块）

### stdin —— 控制命令（JSON Lines）

```json
{"cmd":"ping"}
{"cmd":"status"}
{"cmd":"shutdown","mode":"wait"}   // wait=等任务完成（默认）；now=取消本地任务立即退出
{"cmd":"shutdown-cancel"}          // 取消待定 wait，返回应用
```

### HTTP —— 仅绑定 127.0.0.1

- `GET /bootstrap/<token>` —— 桌面 UI（token 需与 ready 握手一致）
- `GET /api/health` `GET /api/status` `POST /api/shutdown {"mode":...}`
- `POST /api/tasks/simulate` —— 仅 `--allow-simulate` 时可用（fake 测试）

## 退出语义（不调用 ComfyUI 全局 /interrupt）

1. shell 发起 `shutdown mode=now|wait`；有任务时默认 `wait`。
2. `wait`：任务全部结束后 sidecar 自行 `exit 0`；shell 可随时 `shutdown-cancel` 返回。
3. `now`：只取消 sidecar **本地队列**中的任务，ComfyUI 后端生成任务不打断（跑完丢弃），3 秒内退出。
4. sidecar 进程被 shell 的 Job Object 保护：shell 崩溃/退出时被系统直接终止，不留孤儿。

## 运行

```powershell
python -m winefox_sidecar --allow-simulate        # 开发运行
python -m winefox_sidecar --version               # 打印 8.5.9
python -m winefox_sidecar --selftest              # 自检：启动→health→握手→退出0
```

测试开关：`--no-ready`（不握手）、`--bad-startup`（启动即崩 exit 2）、
`--version-override` / `--protocol-override`（版本/协议不匹配）、
`--crash-after N`（N 秒后崩溃 exit 3）。

退出码：0 干净退出；2 启动失败；3 运行崩溃；4 参数/协议错误。

## PyInstaller

打包由 `packaging/pyinstaller/winefox-sidecar.spec` 完成（onedir、无控制台窗口、
基础包不含 TensorFlow/Playwright Chromium/ComfyUI/模型/CUDA）。

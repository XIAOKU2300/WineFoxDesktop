# WineFox Desktop Shell（Agent 09 · Tauri 2）

```
desktop_shell/
  src/                 前端（无构建步骤的静态页面）
    index.html         本地启动页 / 错误页（状态、日志、重试、退出选择）
    app.html           面板宿主（iframe 载入 sidecar 的 loopback bootstrap）
    main.js            状态渲染、退出流程、单实例 focus 事件
  src-tauri/           Tauri 2 (Rust) shell
    src/
      lib.rs           builder：单实例插件、命令注册、窗口关闭→托盘、退出清理
      sidecar.rs       sidecar 生命周期：spawn/握手/健康检查/崩溃重启/退出流
      handshake.rs     stdout JSON 行解析 + 协议/版本/loopback 校验（含单测）
      sanitize.rs      stderr 脱敏（key/token/hex/base64/PEM）
      job_object.rs    Windows Job Object（kill-on-parent-exit，含单测）
      http_client.rs   极简 HTTP/1.1 客户端（health 检查，含单测）
      tray.rs          托盘：显示/隐藏/退出
      commands.rs      前端命令 + 退出决策（无任务直退 / 有任务三选）
  sidecar/             占位 sidecar（Python，fake 实现，协议见 sidecar/README.md）
```

## 行为契约

- **单实例**：二次启动聚焦已有窗口并触发 `focus-requested`。
- **关闭窗口** → 隐藏到托盘（非退出）；托盘“退出”或界面“退出”才退出。
- **退出**：无任务直接退出；有任务弹出三选：等待任务完成（默认）/ 立即退出 /
  返回应用。全程不调用 ComfyUI 全局 /interrupt（见 sidecar 退出语义）。
- **崩溃**：自动重启（最多 2 次，退避 800ms/1600ms）后进入错误页，可手动重试。
- **防孤儿**：Job Object (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE) + sidecar 自身
  stdin-EOF 看门狗 + 退出时显式 TerminateProcess 三重保障。
- **版本**：shell 与 sidecar 版本必须一致（握手校验，`packaging/version.json`
  为唯一版本源）。

## 开发运行

```powershell
# 1) 用 python 直接跑 sidecar（dev 模式由 shell 自动探测）
cd packaging
python .\tests\test_sidecar_protocol.py          # sidecar 协议测试（22 项）

# 2) 构建/运行 shell（需要 Rust 1.77+ 与 WebView2）
cd desktop_shell
npm i
cd src-tauri
cargo test                                       # Rust 单测（17 项）
cargo build
$env:WINE_FOX_SIDECAR = "..\..\packaging\dist\winefox-sidecar\winefox-sidecar.exe"
.\target\debug\winefox-desktop.exe

# 3) 完整打包见 ..\packaging\build.ps1
```

测试开关（shell）：`WINE_FOX_SIDECAR`（exe 或 .py）、`WINE_FOX_PYTHON`、
`WINE_FOX_READY_TIMEOUT_MS`、`WINE_FOX_MAX_RESTARTS`、`WINE_FOX_LOG_DIR`。

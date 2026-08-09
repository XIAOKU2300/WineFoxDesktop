# 干净 Windows 验收（Clean Windows Acceptance）

维护者：Agent 10（quality-security）

目标：在**无 Python、无 Node、无 ComfyUI、无网络代理**的干净 Windows 10/11 x64
虚拟机上，验证 WineFox Desktop 安装包从零到可用的全过程。当前（基线 8.5.9）为
**流程文档 + 检查项**；实际执行依赖 Agent 09 的安装包工件（GATE PENDING）。

## 前置条件

- 全新 VM 快照（关闭 Windows Defender 实时扫描可选，建议保留）。
- 准备：安装包（NSIS）、校验和（SHA-256）、SBOM、`desktop_verify.py`。
- 网络策略：允许访问用户配置的 ComfyUI（默认 127.0.0.1:8188）；禁止外网直连测试项（可选）。

## 验收步骤

### A. 安装
1. 校验安装包 SHA-256 与发布附注一致。
2. 校验 Authenticode 签名（`Get-AuthenticodeSignature`）→ 签名者有效、时间戳有效。
3. 静默安装：`installer.exe /S`（或 UI 安装）→ 退出码 0。
4. 确认安装目录结构：主程序、`resources/`、`settings.json` 默认存在；**无**模型权重文件。

### B. 首次启动与安全默认值
5. 启动应用，确认仅监听 `127.0.0.1`（`netstat -ano | findstr LISTENING`）。
6. 首次启动必须进入"设置密码"流程；**不接受**空密码或默认密码 `REDACTED_LEGACY_PASSWORD`。
7. `settings.json`：`tls_verify: true`；无固定公网 URL；无 query token。
8. 未登录访问 `/api/v1/*` → 401；Host/Origin 异常 → 403/400。

### C. 功能冒烟（连 Fake/真实 ComfyUI）
9. `/api/v1/bootstrap` → 200。
10. `/api/v1/comfyui/probe` 对正常服务器 → ok；对 401 服务器 → 明确错误。
11. 上传图片 → 任务提交 → 轮询 → 出图；历史列表 → 收藏 → 收藏夹可见。
12. 断网/关停 ComfyUI → 任务超时/队列丢失错误码（不崩溃、不挂死）。

### D. 恢复与数据
13. 删除 `settings.json` 后启动 → 恢复出厂流程可用（重新设密）。
14. 收藏目录与 SQLite 数据库在重装后可迁移；重装不丢数据（版本兼容性说明）。
15. 崩溃报告目录（`%LOCALAPPDATA%\winefox-desktop\logs`、CrashDumps）不含密钥。

### E. 清理
16. 卸载：卸载程序 → 用户数据目录保留询问 → 卸载后无残留进程/服务。
17. 卸载后 `netstat` 无残留监听；注册表 App Paths 清理（如有）。

## 判定

全部通过 → 干净环境验收 PASS。任何一项失败 → 阻断发布，回到对应 Agent 整改。
验收记录归档到发布附注（含 VM 快照标识与日期）。

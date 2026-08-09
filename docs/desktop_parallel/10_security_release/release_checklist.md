# WineFox Desktop 发布检查清单（Release Checklist）

维护者：Agent 10（quality-security）｜基线：插件 8.5.9

## 1. 代码与测试门禁

- [ ] `python -m pytest tests/`：54 个既有测试全绿（46 stability + 8 black-screen）。
- [ ] `python -m pytest desktop_integration_tests/ -c desktop_integration_tests/pytest.ini`：Agent 10 集成套件全绿。
- [ ] `python -m pytest desktop_integration_tests/test_api_v1_integration.py -m desktop_gate -rs`：
      desktop 契约测试**允许 SKIP（WAITING_FOR_INTEGRATION）**，不得以"假通过"掩盖。
- [ ] `python tools/desktop_verify.py --self-test` 通过。
- [ ] 对所有**真实**部署包运行 `python tools/desktop_verify.py --root <pkg> --json report.json`，
      `passed: true` 且无 FAIL 门禁（G01–G08）。
- [ ] CI：`desktop-ci.yml` 的 REAL 作业（unit / integration / verify / sbom / vuln-scan）绿。
- [ ] CI：8 个 GATE 作业（core/backend/rust/frontend/pyinstaller/tauri-nsis/install-smoke/signed-release）
      在 `gates=true` 时按设计失败并输出 "GATE PENDING" 文案——不允许静默跳过或假装可运行。

## 2. 安全门禁（HARD，见 security_gate.md）

- [ ] G01 无 LAN 监听：安装后 `netstat -ano` 只允许 127.0.0.1/[::1] 监听。
- [ ] G02 无默认密码：`REDACTED_LEGACY_PASSWORD` 不得出现在任何配置文件；首次启动强制改密。
- [ ] G03 无固定公网 URL。
- [ ] G04 无 query token（Bridge 查询串 token 已剥离）。
- [ ] G05 未授权 401 / G06 Host/Origin/CSRF 中间件生效。
- [ ] G07 密钥不落 JSON/SQLite/日志/崩溃报告（含 crash dump 扫描）。
- [ ] G08 TLS 默认验证开启。
- [ ] G09/G10 路径穿越与超大上传失败。
- [ ] G11 Bridge 无 Token 401。
- [ ] G12 安装包签名（Authenticode）与更新清单签名（含最小签名校验脚本——TODO，Agent 09 提供工件后落地）。
- [ ] G13 不捆绑模型（包内无 .safetensors/.onnx/.pt；nsfw 模型运行时按需下载并提示风险）。

## 3. 发布工件

- [ ] SBOM：`reports/sbom-python.json`（pip 依赖）生成并归档；Rust/前端/Three.js 清单合并（见 sbom_and_licenses.md）。
- [ ] 漏洞扫描：`pip-audit -r requirements.txt` 无高危未处理项（或已记录豁免）。
- [ ] 安装 smoke：静默安装 → 启动 → `/api/v1/bootstrap` 200 → 关闭（GATE PENDING，Agent 09 合并后启用）。
- [ ] 更新清单（update manifest）签名校验脚本（TODO）。
- [ ] Artifact：集成测试 JUnit、desktop-verify JSON、SBOM 归档到发布附注。

## 4. 发布后

- [ ] 在干净 Windows 环境跑 `clean_windows_acceptance.md` 全流程（首次启动/改密/恢复出厂）。
- [ ] 记录已知问题与豁免（如有）到发布说明。

# WineFox Desktop 集成测试（Agent 10）

本目录是 WineFox Desktop 并行开发 Agent 10（quality-security）的集成测试套件。

- `fake_comfyui/`：Fake ComfyUI 服务器（object_info / models/loras / upload/image /
  prompt / queue / history / view；可切换 normal / 401 / 断连 / 缺节点 / 损坏图片 /
  队列丢失 / 执行错误 / view 500）。仅监听 127.0.0.1。
- `test_plugin_core_integration.py`：插件核心（comfyui_api.ComfyUI）全链路集成。
- `test_fault_injection.py`：故障注入矩阵。
- `test_security_hard_gates.py`：硬门禁（密钥脱敏、Bridge 鉴权、穿越、413、TLS 作用域等）。
- `test_api_v1_integration.py`：桌面 `/api/v1` 契约测试 —— **GATE PENDING**，
  桌面后端（Agent 07）未合并前自动 SKIP（`WAITING_FOR_INTEGRATION`），合并后自动激活。

运行（无需真实 ComfyUI、无需模型、无外网）：

```bash
python -m pytest desktop_integration_tests/ -c desktop_integration_tests/pytest.ini
```

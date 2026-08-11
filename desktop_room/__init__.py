"""WineFox 多人房间：独立于主后端的房间网关（安全、存储、聊天、共享生图）。

房间网关在 sidecar 进程内以独立 aiohttp 应用运行，只监听 127.0.0.1:<随机端口>，
拥有独立的 Cookie / 会话 / SQLite 数据库 / 安全中间件。访客只能经由 HTTPS/WSS
Tunnel 访问；房主通过面板代理（注入 X-WineFox-Room-Host 令牌）访问。

信任模型：房主可读取房间内容（必须读取提示词并调用 ComfyUI）；不承诺对房主
隐藏内容的零知识端到端加密。网络与 Tunnel 服务不能看到未加密传输。
"""

from __future__ import annotations

# 房间网关内部协议版本（独立于主 sidecar 协议）。
from .protocol import ROOM_PROTOCOL_VERSION

__all__ = ["ROOM_PROTOCOL_VERSION"]

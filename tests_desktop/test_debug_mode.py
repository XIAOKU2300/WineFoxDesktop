from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class DebugModeContractTests(unittest.TestCase):
    """Phase B — 调试模式解锁与实验室路由的 JS 契约。

    Rust 侧的点击窗口 / 间隔 / 门禁逻辑由 `desktop_shell/src-tauri/src/debug_mode.rs`
    的单元测试覆盖（10 次解锁、1.2s 间隔重置、6s 窗口过期、DEBUG_MODE_REQUIRED）。
    本文件断言桌面 UI 与 Shell 桥按约定接线：父页面逐次调用 Rust record_logo_click、
    postMessage 校验 source 与 origin、实验室路由仅在调试模式出现。
    """

    def source(self, relative: str) -> str:
        return ROOT.joinpath(relative).read_text(encoding="utf-8")

    def test_shell_validates_source_and_origin(self) -> None:
        main = self.source("desktop_shell/src/main.js")
        self.assertIn("event.source !== panel.contentWindow", main)
        self.assertIn("event.origin !== PANEL_ORIGIN", main)
        self.assertIn('const PANEL_ORIGIN = "http://winefox-panel.localhost"', main)

    def test_shell_forwards_logo_clicks_to_rust(self) -> None:
        main = self.source("desktop_shell/src/main.js")
        self.assertIn('event.data.type === "winefox-ui-debug"', main)
        self.assertIn('invoke("record_logo_click")', main)
        self.assertIn('invoke("get_debug_mode")', main)
        self.assertIn('type: "winefox-ui-debug-reply"', main)

    def test_shell_listens_for_debug_and_room_events(self) -> None:
        main = self.source("desktop_shell/src/main.js")
        self.assertIn('await listen("debug-mode-changed"', main)
        self.assertIn('await listen("room-status"', main)
        self.assertIn('type: "winefox-ui-debug-state"', main)
        self.assertIn('type: "winefox-ui-room-state"', main)

    def test_shell_room_bridge_covers_all_actions(self) -> None:
        main = self.source("desktop_shell/src/main.js")
        for action in ("getState", "start", "stop", "setTunnel", "clearTunnel",
                       "provisionTunnel", "stopTunnel", "cloudflaredAvailable"):
            self.assertIn(f'action === "{action}"', main)

    def test_shell_dispatches_auto_tunnel_to_rust(self) -> None:
        main = self.source("desktop_shell/src/main.js")
        self.assertIn('invoke("provision_room_tunnel")', main)
        self.assertIn('invoke("stop_room_tunnel")', main)
        self.assertIn('invoke("cloudflared_available")', main)

    def test_app_binds_logo_click_and_adds_lab_route_only_in_debug(self) -> None:
        app = self.source("desktop_ui/js/app.js")
        self.assertIn('const DEBUG_ROUTE = { id: "lab"', app)
        self.assertIn("function routes() {", app)
        self.assertIn("debugEnabled ? ROUTES.concat(DEBUG_ROUTE) : ROUTES", app)
        self.assertIn('$(".brand-mark")', app)
        self.assertIn("debugBridge.click()", app)
        self.assertIn("applyDebugState(true)", app)

    def test_app_restores_debug_state_and_syncs_rail(self) -> None:
        app = self.source("desktop_ui/js/app.js")
        self.assertIn("await debugBridge.getState()", app)
        self.assertIn("debugBridge.onDebugState", app)
        self.assertIn("function syncRailAria()", app)

    def test_utils_exports_debug_and_room_bridges(self) -> None:
        utils = self.source("desktop_ui/js/utils.js")
        self.assertIn("export const debugBridge", utils)
        self.assertIn("export const roomBridge", utils)
        self.assertIn('type === "winefox-ui-debug-reply"', utils)
        self.assertIn('type === "winefox-ui-debug-state"', utils)
        self.assertIn('type === "winefox-ui-room-state"', utils)

    def test_lab_view_wires_start_stop_and_tunnel(self) -> None:
        lab = self.source("desktop_ui/js/views/lab.js")
        self.assertIn("roomBridge.start(settings)", lab)
        self.assertIn("roomBridge.stop()", lab)
        self.assertIn("roomBridge.setTunnel(url)", lab)
        self.assertIn("roomBridge.clearTunnel()", lab)
        self.assertIn("roomBridge.provisionTunnel()", lab)
        self.assertIn("roomBridge.stopTunnel()", lab)
        self.assertIn("roomBridge.cloudflaredAvailable()", lab)
        self.assertIn("lab-tunnel-auto-row", lab)
        self.assertIn('res.error === "DEBUG_MODE_REQUIRED"', lab)
        self.assertIn("roomBridge.onState", lab)
        self.assertIn('fetch("/room/v1/catalog"', lab)


if __name__ == "__main__":
    unittest.main()

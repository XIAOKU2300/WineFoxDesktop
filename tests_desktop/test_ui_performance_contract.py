from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class UiPerformanceContractTests(unittest.TestCase):
    def source(self, relative: str) -> str:
        return ROOT.joinpath(relative).read_text(encoding="utf-8")

    def test_navigation_paints_before_mount_and_never_waits_for_view_io(self) -> None:
        source = self.source("desktop_ui/js/app.js")

        self.assertIn("function scheduleMount(entry)", source)
        self.assertIn("requestAnimationFrame(() =>", source)
        self.assertIn("loadingState(`正在打开${definition.label}`)", source)
        self.assertNotIn("await switchView", source)
        self.assertNotIn("diagnostics: api.diagnostics()", source)
        self.assertIn("healthRefreshPromise", source)

    def test_settings_tabs_reuse_dom_and_do_not_consume_global_cache(self) -> None:
        source = self.source("desktop_ui/js/views/settings.js")

        self.assertIn("this.sectionNodes = new Map()", source)
        self.assertIn("this.sectionNodes.has(section)", source)
        self.assertIn('loadingState("正在整理设置", { compact: true })', source)
        self.assertNotIn('store.set("settings", null)', source)
        self.assertNotIn('store.set("secrets", null)', source)
        self.assertNotIn("this.settings = await api.settings()", source)
        self.assertIn("api.saveSettings(patch, { signal })", source)
        self.assertIn("const controller = new AbortController()", source)

    def test_diagnostics_is_single_flight_bounded_and_defers_details(self) -> None:
        source = self.source("desktop_ui/js/views/diagnostics.js")

        self.assertEqual(source.count("api.diagnostics("), 1)
        self.assertIn("if (this.loading) return", source)
        self.assertIn("this.requestDiagnostics(signal), 12000", source)
        self.assertIn("token !== this.loadToken", source)
        self.assertIn('loadingState("正在整理节点与日志", { compact: true })', source)
        self.assertIn("customNodes.slice(0, 120)", source)
        self.assertIn("logTail.lines.slice(-80)", source)
        self.assertIn("const controller = new AbortController()", source)
        self.assertIn("deactivate()", source)
        self.assertIn("controller.abort()", source)

    def test_manual_connection_retry_bypasses_probe_cache(self) -> None:
        app_source = self.source("desktop_ui/js/app.js")
        api_source = self.source("desktop_ui/js/api.js")

        self.assertIn("api.probe({ force: true }, { signal })", app_source)
        self.assertIn("if (healthResult.status", app_source)
        self.assertIn('textContent = "后端离线"', app_source)
        self.assertIn("return;", app_source)
        self.assertIn("probe: (payload = {}, options = {})", api_source)
        self.assertIn("withTimeout((signal) => api.health({ signal })", app_source)
        self.assertIn('store.set("comfyui", null)', app_source)

    def test_model_pagination_appends_rows_without_rebuilding_the_page(self) -> None:
        source = self.source("desktop_ui/js/views/models.js")
        load_more = source.split("loadMoreResources(key) {", 1)[1].split("empty(title", 1)[0]

        self.assertIn("document.createDocumentFragment()", load_more)
        self.assertIn("state.sentinel.before(fragment)", load_more)
        self.assertNotIn("this.renderBody()", load_more)

    def test_lora_workspace_is_the_only_lora_management_surface(self) -> None:
        app = self.source("desktop_ui/js/app.js")
        models = self.source("desktop_ui/js/views/models.js")
        lora = self.source("desktop_ui/js/views/lora.js")

        self.assertIn('{ id: "models", label: "模型与环境"', app)
        self.assertIn('{ id: "lora", label: "LoRA"', app)
        self.assertNotIn('label: "模型与 LoRA"', app)
        self.assertNotIn("loraDownloadPanel", models)
        self.assertNotIn('dataset: { tab: "loras" }', models)
        self.assertIn('this.workspaceTab("installed"', lora)
        self.assertIn('this.workspaceTab("discover"', lora)
        self.assertIn('this.workspaceTab("match"', lora)
        self.assertIn("api.searchLoras", lora)
        self.assertIn("api.loraLibraryList", lora)
        self.assertIn("api.loraLibraryMatchPreview", lora)

    def test_lora_search_cards_open_accessible_details_and_bound_broken_previews(self) -> None:
        source = self.source("desktop_ui/js/views/lora.js")

        self.assertIn("openOnlineDetail(item)", source)
        self.assertIn('dataset: { loraDetail: item.id }', source)
        self.assertIn('role: "button"', source)
        self.assertIn('tabindex: "0"', source)
        self.assertIn('["Enter", " "].includes(event.key)', source)
        self.assertIn('event.key === "Escape"', source)
        self.assertIn('onerror: showPlaceholder', source)
        self.assertIn('thumb.replaceChildren(ic(placeholderIcon))', source)
        self.assertIn('alt: ""', source)

    def test_resource_names_hide_absolute_paths(self) -> None:
        source = self.source("desktop_ui/js/utils.js")

        self.assertIn('value.startsWith("/")', source)

    # ------------------------------------------------------------------
    # 房间访客界面（desktop_room/room_ui/app.js）性能与安全契约
    # ------------------------------------------------------------------
    def test_room_ui_new_messages_append_single_node(self) -> None:
        source = self.source("desktop_room/room_ui/app.js")

        self.assertIn("appendMessage(message)", source)
        self.assertIn("box.append(renderMessage(message))", source)
        self.assertIn("document.createDocumentFragment()", source)
        self.assertIn("box.replaceChildren(frag)", source)
        # 消息列表整体重建不得再走 innerHTML（否则已加载图片会重新请求）。
        self.assertNotIn('"ws-messages").innerHTML', source)

    def test_room_ui_images_use_attachment_urls_not_base64(self) -> None:
        source = self.source("desktop_room/room_ui/app.js")

        self.assertIn("/room/v1/attachments/", source)
        self.assertIn("thumbUrl", source)
        self.assertNotIn("data:image", source)
        self.assertNotIn("btoa(", source)
        self.assertNotIn("atob(", source)

    def test_room_ui_session_token_never_in_local_storage(self) -> None:
        source = self.source("desktop_room/room_ui/app.js")

        # 令牌只经 Cookie 传输（本地 http 也走 document.cookie），
        # 绝不落入 localStorage/sessionStorage。
        self.assertIn("document.cookie", source)
        self.assertNotIn("localStorage.setItem", source)
        self.assertNotIn("localStorage.", source)
        self.assertNotIn("sessionStorage.setItem", source)
        self.assertNotIn("sessionStorage.", source)

    def test_room_ui_password_never_in_url(self) -> None:
        source = self.source("desktop_room/room_ui/app.js")

        # 密码只随 join 请求体提交一次，绝不拼进 query 或 URL。
        self.assertIn("{ name, password }", source)
        self.assertNotIn("location.search", source)
        self.assertNotIn("encodeURIComponent(password", source)

    def test_room_ui_ws_heartbeat_and_bounded_reconnect(self) -> None:
        source = self.source("desktop_room/room_ui/app.js")

        self.assertIn('{ action: "ping" }', source)
        self.assertIn("25000", source)
        self.assertIn("scheduleReconnect", source)
        self.assertIn("3000", source)

    def test_room_ui_client_request_id_matches_protocol(self) -> None:
        source = self.source("desktop_room/room_ui/app.js")

        self.assertIn("room:${state.room.id}:member:${state.member.id}:${uuid()}", source)

    def test_room_ui_api_uses_same_origin_credentials(self) -> None:
        source = self.source("desktop_room/room_ui/app.js")

        self.assertIn('credentials: "same-origin"', source)

    def test_room_ui_catalog_from_guest_safe_fields(self) -> None:
        source = self.source("desktop_room/room_ui/app.js")

        self.assertIn("cat.families", source)
        self.assertIn("cat.workflows", source)
        self.assertIn("cat.loras", source)

    def test_lab_catalog_reads_guest_families_field(self) -> None:
        # 网关目录下发 families（白名单 id/label），面板实验室须读该字段。
        source = self.source("desktop_ui/js/views/lab.js")

        self.assertIn("data.families", source)
        self.assertNotIn("data.modelFamilies", source)

    def test_room_bridge_reply_is_handled_and_waiters_route_by_type(self) -> None:
        # 房间桥请求的回复必须被监听并 resolve 对应 waiter（此前缺失
        # winefox-ui-room-reply 处理，导致每次房间操作 20s 超时提示
        # "本地服务无响应"）；等待队列须按消息类型路由。
        source = self.source("desktop_ui/js/utils.js")

        self.assertIn('event.data?.type === "winefox-ui-room-reply"', source)
        self.assertIn("roomWaiters.get(event.data.id)", source)
        self.assertIn("roomWaiters.delete(event.data.id)", source)
        self.assertIn('messageType === "winefox-ui-room" ? roomWaiters : debugWaiters', source)


if __name__ == "__main__":
    unittest.main()

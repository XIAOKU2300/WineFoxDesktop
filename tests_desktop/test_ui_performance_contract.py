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
        self.assertNotIn("if (entry.hasEntered) return", source)
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

    def test_civitai_credentials_explain_token_and_cookie_acquisition(self) -> None:
        source = self.source("desktop_ui/js/views/settings.js")

        self.assertIn("https://civitai.com/user/account", source)
        self.assertIn("API Keys", source)
        self.assertIn("api/download/models", source)
        self.assertIn("Request Headers", source)
        self.assertIn("Cookie 等同账户登录凭证", source)
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
        self.assertIn("const completeSharedPreview = () =>", source)
        # 新交接契约：is-shared-pending 故意保留，与 is-shared-complete 组合做
        # “先显形、后移除、隔帧重叠”的同帧硬切，目标图不走自身过渡。
        self.assertIn('detailPreview.classList.add("is-loaded", "is-shared-complete")', source)
        self.assertIn("completeSharedPreview();", source)
        # 共享预览用克隆图飞行（flight 不搬真图节点），杜绝 fixed↔布局层交接卡顿；
        # 事件源传 sourceImage 用于克隆，动画完成走 onfinish 回调收尾。
        self.assertIn("sourceImage,", source)
        self.assertIn("cloneNode(false)", source)
        self.assertIn("flight.append(image)", source)
        self.assertIn('transformOrigin: "top left"', source)
        # 8.6.11 动效契约：单段指数减速曲线（本站 --ease-out），无过冲无回弹、
        # 无逐帧 boxShadow/filter 关键帧；开场与返航对称；旧“Q 弹三段关键帧”
        # （overshoot/settle 双缓动叠加）已被废弃，禁止回归。
        self.assertIn("cubic-bezier(0.16, 1, 0.3, 1)", source)
        self.assertNotIn("const overshootX = sx * 1.03;", source)
        self.assertNotIn("const settleX = sx * 0.985;", source)
        self.assertIn("returnSharedPreview(", source)
        self.assertIn("borderRadius: plan.endRadius", source)

        self.assertIn('dataset: { loraDetail: item.id }', source)
        self.assertIn('role: "button"', source)
        self.assertIn('tabindex: "0"', source)
        self.assertIn('["Enter", " "].includes(event.key)', source)
        self.assertIn('event.key === "Escape"', source)
        self.assertIn('onerror: showPlaceholder', source)
        self.assertIn('thumb.replaceChildren(ic(placeholderIcon))', source)
        self.assertIn('alt: ""', source)

    def test_global_card_motion_reuses_history_favorites_lift_language(self) -> None:
        tokens = self.source("desktop_ui/css/tokens.css")
        motion = self.source("desktop_ui/css/motion.css")

        self.assertIn("--card-lift: -2px;", tokens)
        self.assertIn("--card-image-scale: 1.035;", tokens)
        self.assertIn(".lora-market-card:hover, .lora-library-row:hover, .img-cell:hover, .fav-cell:hover", motion)
        self.assertIn("transform: translateY(var(--card-lift));", motion)

    def test_lora_preview_loading_motion_has_a_defined_easing_curve(self) -> None:
        tokens = self.source("desktop_ui/css/tokens.css")
        pages = self.source("desktop_ui/css/pages.css")

        self.assertIn("--ease-in-out: cubic-bezier(0.77, 0, 0.175, 1);", tokens)
        self.assertIn("animation: loraPreviewShimmer 1.25s ease-in-out infinite;", pages)
        self.assertIn("animation: loraPreviewLoading 1.55s ease-in-out infinite;", pages)
    def test_canvas_zoom_keeps_pointer_space_and_visible_pan_bounds(self) -> None:
        source = self.source("desktop_ui/js/views/generate.js")

        # getBoundingClientRect() already contains the current translation;
        # subtracting tx/ty a second time makes repeated zooms fling the image
        # outside the clipped canvas. Keep this as a regression contract.
        self.assertIn("const clampPan = (base) =>", source)
        self.assertIn("const px = (e.clientX - (rect.left + rect.width / 2)) / z.level;", source)
        self.assertIn("const py = (e.clientY - (rect.top + rect.height / 2)) / z.level;", source)
        self.assertIn("z.tx = e.clientX - base.centerX - px * next;", source)
        self.assertIn("z.ty = e.clientY - base.centerY - py * next;", source)
        self.assertNotIn("e.clientX - (rect.left - z.tx)", source)
        self.assertNotIn("e.clientY - (rect.top - z.ty)", source)

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

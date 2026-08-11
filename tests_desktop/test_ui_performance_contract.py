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

    def test_resource_names_hide_absolute_paths(self) -> None:
        source = self.source("desktop_ui/js/utils.js")

        self.assertIn('value.startsWith("/")', source)


if __name__ == "__main__":
    unittest.main()

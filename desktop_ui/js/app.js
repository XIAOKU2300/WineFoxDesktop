/* WineFox Desktop — 应用引导
 * 主题 / 路由 / 向导门禁 / 连接状态 / 视图生命周期。
 * Dev mock：仅当 URL 带 mock=1 或 dev=1 时动态加载 desktop_ui/dev/mock.js（生产默认不启用）。
 */
import { el, $, $$, ic, esc, toast } from "./utils.js";
import { api } from "./api.js";
import { store } from "./store.js";
import { Wizard } from "./views/wizard.js";
import { GenerateView } from "./views/generate.js";
import { HistoryView } from "./views/history.js";
import { FavoritesView } from "./views/favorites.js";
import { ModelsView } from "./views/models.js";
import { SettingsView } from "./views/settings.js";
import { DiagnosticsView } from "./views/diagnostics.js";

const ROUTES = [
  { id: "generate", label: "生成", icon: "gen", ctor: GenerateView, hint: "WineFox 绘图" },
  { id: "history", label: "历史", icon: "history", ctor: HistoryView },
  { id: "favorites", label: "收藏", icon: "fav", ctor: FavoritesView },
  { id: "models", label: "模型与 LoRA", icon: "model", ctor: ModelsView },
  { id: "settings", label: "设置", icon: "gear", ctor: SettingsView },
  { id: "diagnostics", label: "诊断", icon: "diag", ctor: DiagnosticsView },
];

let currentView = null;
let connTimer = null;

function parseRoute() {
  const hash = location.hash.replace(/^#\/?/, "");
  const id = ROUTES.some((r) => r.id === hash) ? hash : "generate";
  return id;
}

function buildRail() {
  const rail = $("#rail");
  rail.innerHTML = "";
  for (const r of ROUTES) {
    const btn = el("a", { class: "rail-item", href: `#/${r.id}`, "aria-current": parseRoute() === r.id ? "page" : undefined, title: r.label }, [
      ic(r.icon),
      el("span", { class: "rail-label" }, esc(r.label)),
    ]);
    if (r.id === "generate") {
      const badge = el("span", { class: "rail-count", id: "rail-jobs", hidden: true }, "0");
      btn.append(badge);
    }
    rail.append(btn);
  }
  rail.append(
    el("div", { class: "rail-sep" }),
    el("div", { class: "rail-foot" }, [
      el("span", { class: "hint" }, "WineFox Desktop"),
      el("span", { class: "hint" }, "v1.0 · 桌面端"),
    ]),
  );
}

async function switchView(routeId) {
  if (currentView?.dispose) currentView.dispose();
  currentView = null;
  const root = $("#view-root");
  root.innerHTML = "";
  root.classList.remove("view"); // 生成页自带布局
  const def = ROUTES.find((r) => r.id === routeId);
  if (!def) return;
  // 生成页使用全屏三栏；其余页面用标准 view 布局
  if (routeId === "generate") {
    root.classList.remove("view");
  } else {
    root.classList.add("view");
  }
  const view = new def.ctor();
  currentView = view;
  await view.mount(root);
}

/* ---------------- 连接状态 ---------------- */
async function refreshHealth() {
  const conn = $("#conn-status");
  try {
    const h = await api.health();
    store.set("health", h);
    if (!conn) return;
    const cf = h.comfyui;
    const connected = cf?.connected;
    conn.className = "conn " + (connected ? "ok" : h.status === "degraded" ? "warn" : "err");
    conn.querySelector(".conn-text").textContent = connected
      ? "ComfyUI 已连接" + (cf.version ? " · " + cf.version : "")
      : h.status === "degraded" ? "部分服务异常" : "ComfyUI 未连接";
  } catch {
    conn.className = "conn err";
    conn.querySelector(".conn-text").textContent = "后端离线";
  }
  // 每 10s 刷新一次状态（浅轮询）
  clearTimeout(connTimer);
  connTimer = setTimeout(refreshHealth, 10000);
}

/* ---------------- 主题 ---------------- */
function bindTheme() {
  store.applyTheme();
  $("#theme-toggle")?.addEventListener("click", () => {
    store.toggleTheme();
    toast(store.get("theme") === "dark" ? "已切换深色主题" : "已切换浅色主题", "info");
  });
  document.addEventListener("keydown", (e) => {
    const tag = document.activeElement?.tagName;
    if (e.key.toLowerCase() === "t" && !e.ctrlKey && !e.metaKey && !e.altKey && tag !== "INPUT" && tag !== "TEXTAREA" && tag !== "SELECT") {
      store.toggleTheme();
    }
  });
}

/* ---------------- Dev mock ---------------- */
async function maybeLoadMock() {
  const qs = new URLSearchParams(location.search);
  const enabled = qs.has("mock") || qs.has("dev");
  if (!enabled) return false;
  try {
    const { installMock } = await import("../dev/mock.js");
    installMock({ scenario: qs.get("scenario") || "success" });
    console.info("[WineFox] dev mock 已启用，scenario =", qs.get("scenario") || "success");
    return true;
  } catch (e) {
    console.warn("[WineFox] dev mock 加载失败", e);
    return false;
  }
}

/* ---------------- 引导 ---------------- */
async function boot() {
  await maybeLoadMock();
  bindTheme();
  buildRail();

  // 先显示外壳
  $("#app-root").hidden = false;

  // 健康 / 首启判断
  let firstRun = true;
  try {
    const h = await api.health();
    store.set("health", h);
    firstRun = !!(h.first_run ?? true);
  } catch {
    firstRun = true; // 后端不可用时仍显示向导（离线可配置）
  }
  if (firstRun) {
    store.set("wizardCompleted", false);
    const wizard = new Wizard(() => {
      refreshHealth();
      buildRail();
    });
    await wizard.start();
  } else {
    store.set("wizardCompleted", true);
  }

  refreshHealth();

  // 路由
  const navigate = () => {
    const id = parseRoute();
    $$("#rail .rail-item").forEach((a) => {
      if (a.getAttribute("href") === `#/${id}`) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
    switchView(id);
  };
  window.addEventListener("hashchange", navigate);
  navigate();

  // 队列计数角标
  store.subscribe((k, v) => {
    if (k !== "jobTally") return;
    const badge = $("#rail-jobs");
    if (!badge) return;
    const n = v.queued + v.running;
    badge.hidden = n === 0;
    badge.textContent = String(n);
  });
}

window.addEventListener("DOMContentLoaded", boot);

/* 导出供诊断页等复用（避免循环导入） */
export { toast };

/* WineFox Desktop application composition and warm-start lifecycle. */
import { el, $, $$, ic, esc, toast } from "./utils.js";
import { api } from "./api.js";
import { store } from "./store.js";
import { GenerateView } from "./views/generate.js";
import { HistoryView } from "./views/history.js";
import { FavoritesView } from "./views/favorites.js";
import { ModelsView } from "./views/models.js";
import { SettingsView } from "./views/settings.js";
import { DiagnosticsView } from "./views/diagnostics.js";

const ROUTES = [
  { id: "generate", label: "生成", icon: "gen", ctor: GenerateView },
  { id: "history", label: "历史", icon: "history", ctor: HistoryView },
  { id: "favorites", label: "收藏", icon: "fav", ctor: FavoritesView },
  { id: "models", label: "模型与 LoRA", icon: "model", ctor: ModelsView },
  { id: "settings", label: "设置", icon: "gear", ctor: SettingsView },
  { id: "diagnostics", label: "诊断", icon: "diag", ctor: DiagnosticsView },
];

let currentEntry = null;
let connTimer = null;
let viewToken = 0;
let enterTimer = null;
const viewCache = new Map();

function parseRoute() {
  const hash = location.hash.replace(/^#\/?/, "");
  return ROUTES.some((route) => route.id === hash) ? hash : "generate";
}

function sendLaunchProgress(label) {
  if (window.parent === window) return;
  window.parent.postMessage({ type: "winefox-ui-progress", label }, "*");
}

function signalReady() {
  if (window.parent === window) return;
  window.parent.postMessage({ type: "winefox-ui-ready" }, "*");
}

function buildRail() {
  const rail = $("#rail");
  rail.innerHTML = "";
  for (const route of ROUTES) {
    const link = el("a", {
      class: "rail-item",
      href: `#/${route.id}`,
      "aria-current": parseRoute() === route.id ? "page" : undefined,
      title: route.label,
    }, [ic(route.icon), el("span", { class: "rail-label" }, esc(route.label))]);
    if (route.id === "generate") {
      link.append(el("span", { class: "rail-count", id: "rail-jobs", hidden: true }, "0"));
    }
    rail.append(link);
  }
  rail.append(
    el("div", { class: "rail-sep" }),
    el("div", { class: "rail-foot" }, [
      el("span", { class: "rail-signature" }, "狐"),
      el("span", { class: "hint" }, "WineFox Desktop"),
      el("span", { class: "hint" }, "v8.5.9 · 本地创作空间"),
    ]),
  );
}

function getViewEntry(routeId) {
  if (viewCache.has(routeId)) return viewCache.get(routeId);

  const definition = ROUTES.find((route) => route.id === routeId);
  if (!definition) return null;
  const root = el("div", {
    class: routeId === "generate" ? "" : "view",
    role: "region",
    "aria-live": "polite",
    hidden: true,
  });
  $("#main").append(root);
  const view = new definition.ctor();
  const entry = { routeId, root, view, mountPromise: null, hasEntered: false };
  viewCache.set(routeId, entry);

  // mount() runs synchronously until its first await, so the loading state is
  // ready before this hidden root can become the active route.
  try {
    entry.mountPromise = Promise.resolve(view.mount(root));
  } catch (error) {
    entry.mountPromise = Promise.reject(error);
  }
  return entry;
}

function playViewEntrance(entry, token) {
  if (entry.hasEntered) return;
  entry.hasEntered = true;
  clearTimeout(enterTimer);
  entry.root.classList.remove("view-motion-enter");
  requestAnimationFrame(() => {
    if (token !== viewToken || currentEntry !== entry) return;
    entry.root.classList.add("view-motion-enter");
    enterTimer = setTimeout(() => {
      if (currentEntry === entry) entry.root.classList.remove("view-motion-enter");
    }, 620);
  });
}

async function switchView(routeId) {
  const token = ++viewToken;
  const entry = getViewEntry(routeId);
  if (!entry) return;

  if (currentEntry && currentEntry !== entry) {
    currentEntry.root.hidden = true;
    currentEntry.root.removeAttribute("id");
    currentEntry.root.classList.remove("view-motion-enter");
  }

  currentEntry = entry;
  entry.root.id = "view-root";
  entry.root.hidden = false;
  playViewEntrance(entry, token);

  try {
    await entry.mountPromise;
  } catch (error) {
    if (token === viewToken && currentEntry === entry) throw error;
  }
}

function applyHealth(health) {
  const conn = $("#conn-status");
  if (!conn || !health) return;
  if (health.status !== "ok") {
    conn.className = "conn warn";
    conn.querySelector(".conn-text").textContent = "部分服务异常";
    return;
  }
  applyComfyUI(store.get("comfyui"));
}

function applyComfyUI(probe) {
  const conn = $("#conn-status");
  if (!conn) return;
  if (!probe) {
    conn.className = "conn warn";
    conn.querySelector(".conn-text").textContent = "正在检测 ComfyUI";
    return;
  }
  const connected = !!(probe.online ?? probe.ok ?? probe.connected);
  conn.className = "conn " + (connected ? "ok" : "err");
  conn.querySelector(".conn-text").textContent = connected
    ? "ComfyUI 已连接" + (probe.version ? " · " + probe.version : "")
    : "ComfyUI 未连接";
}

async function refreshHealth() {
  const [healthResult, comfyResult] = await Promise.allSettled([api.health(), api.comfyui()]);
  if (healthResult.status === "fulfilled") {
    store.set("health", healthResult.value);
  } else {
    const conn = $("#conn-status");
    if (conn) {
      conn.className = "conn err";
      conn.querySelector(".conn-text").textContent = "后端离线";
    }
  }
  if (comfyResult.status === "fulfilled") store.set("comfyui", comfyResult.value);
  applyHealth(store.get("health"));
  clearTimeout(connTimer);
  connTimer = setTimeout(refreshHealth, 10000);
}

function bindTheme() {
  store.applyTheme();
  const toggle = () => {
    const change = () => store.toggleTheme();
    const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!reduced && document.startViewTransition) document.startViewTransition(change);
    else change();
    toast(store.get("theme") === "dark" ? "已切换深色主题" : "已切换浅色主题", "info");
  };
  $("#theme-toggle")?.addEventListener("click", toggle);
  document.addEventListener("keydown", (event) => {
    const tag = document.activeElement?.tagName;
    if (event.key.toLowerCase() === "t" && !event.ctrlKey && !event.metaKey && !event.altKey && tag !== "INPUT" && tag !== "TEXTAREA" && tag !== "SELECT") toggle();
  });
}

async function maybeLoadMock() {
  const query = new URLSearchParams(location.search);
  if (!query.has("mock") && !query.has("dev")) return;
  const { installMock } = await import("../dev/mock.js");
  installMock({ scenario: query.get("scenario") || "success" });
}

function withTimeout(promise, ms = 4000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("preload timeout")), ms);
    promise.then(
      (value) => { clearTimeout(timer); resolve(value); },
      (error) => { clearTimeout(timer); reject(error); },
    );
  });
}

async function preloadWorkspace() {
  sendLaunchProgress("正在读取设置与模型目录");
  const catalog = api.catalog();
  const sources = {
    health: api.health(),
    settings: api.settings(),
    catalog,
    models: catalog,
    secrets: api.secrets(),
    history: api.history({ limit: 24 }),
    categories: api.categories(),
    favorites: api.favorites({ limit: 24 }),
    diagnostics: api.diagnostics(),
    comfyui: api.comfyui(),
  };
  const entries = Object.entries(sources);
  const results = await Promise.allSettled(entries.map(([, promise]) => withTimeout(promise)));
  results.forEach((result, index) => {
    if (result.status === "fulfilled") store.set(entries[index][0], result.value);
  });
  applyHealth(store.get("health"));
  sendLaunchProgress("正在铺开创作界面");
}

function bindJobTally() {
  store.subscribe((key, value) => {
    if (key === "comfyui") {
      applyComfyUI(value);
      return;
    }
    if (key !== "jobTally") return;
    const badge = $("#rail-jobs");
    if (!badge) return;
    const count = value.queued + value.running;
    badge.hidden = count === 0;
    badge.textContent = String(count);
    if (count) badge.animate?.(
      [{ transform: "scale(.8)", opacity: .5 }, { transform: "none", opacity: 1 }],
      { duration: 260, easing: "cubic-bezier(.16,1,.3,1)" },
    );
  });
}

async function boot() {
  await maybeLoadMock();
  bindTheme();
  buildRail();
  bindJobTally();
  await preloadWorkspace();

  // Route roots are retained after their first mount. This preserves editing
  // state and prevents rapid back-and-forth navigation from refetching data.
  $("#view-root")?.remove();

  const navigate = async () => {
    const id = parseRoute();
    $$("#rail .rail-item").forEach((link) => {
      if (link.getAttribute("href") === `#/${id}`) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    await switchView(id);
  };
  window.addEventListener("hashchange", () => { void navigate(); });
  await navigate();

  $("#app-root").hidden = false;
  requestAnimationFrame(() => document.body.classList.add("ui-ready"));
  connTimer = setTimeout(refreshHealth, 10000);
  signalReady();
}

window.addEventListener("DOMContentLoaded", () => {
  boot().catch((error) => {
    console.error("WineFox boot failed", error);
    $("#app-root").hidden = false;
    signalReady();
    toast("界面启动不完整，请打开诊断页查看", "err", 6000);
  });
});

export { toast };

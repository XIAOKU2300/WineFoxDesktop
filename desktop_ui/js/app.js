/* WineFox Desktop application composition and warm-start lifecycle. */
import { el, $, $$, ic, esc, toast, loadingState } from "./utils.js";
import { debugBridge } from "./utils.js";
import { api } from "./api.js";
import { store } from "./store.js";
import { GenerateView } from "./views/generate.js";
import { HistoryView } from "./views/history.js";
import { FavoritesView } from "./views/favorites.js";
import { ModelsView } from "./views/models.js";
import { LoraLibraryView } from "./views/lora.js";
import { SettingsView } from "./views/settings.js";
import { DiagnosticsView } from "./views/diagnostics.js";
import { LabView } from "./views/lab.js";

const ROUTES = [
  { id: "generate", label: "生成", icon: "gen", ctor: GenerateView },
  { id: "history", label: "历史", icon: "history", ctor: HistoryView },
  { id: "favorites", label: "收藏", icon: "fav", ctor: FavoritesView },
  { id: "models", label: "模型与环境", icon: "model", ctor: ModelsView },
  { id: "lora", label: "LoRA", icon: "wand", ctor: LoraLibraryView },
  { id: "settings", label: "设置", icon: "gear", ctor: SettingsView },
  { id: "diagnostics", label: "诊断", icon: "diag", ctor: DiagnosticsView },
];
const DEBUG_ROUTE = { id: "lab", label: "实验室", icon: "flask", ctor: LabView };

let debugEnabled = false;
let currentEntry = null;
let connTimer = null;
let viewToken = 0;
let enterTimer = null;
let healthRefreshPromise = null;
const viewCache = new Map();

function routes() {
  return debugEnabled ? ROUTES.concat(DEBUG_ROUTE) : ROUTES;
}

function parseRoute() {
  const hash = location.hash.replace(/^#\/?/, "");
  return routes().some((route) => route.id === hash) ? hash : "generate";
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
  for (const route of routes()) {
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
      el("span", { class: "rail-signature" }, [el("img", { src: "assets/app-logo.png", alt: "" })]),
      el("span", { class: "hint" }, "WineFox Desktop"),
      el("span", { class: "hint" }, "v8.6.14 · 本地创作空间"),
    ]),
  );
}

function getViewEntry(routeId) {
  if (viewCache.has(routeId)) return viewCache.get(routeId);

  const definition = routes().find((route) => route.id === routeId);
  if (!definition) return null;
  const root = el("div", {
    class: routeId === "generate" ? "" : "view",
    role: "region",
    "aria-live": "polite",
    hidden: true,
  }, [loadingState(`正在打开${definition.label}`)]);
  $("#main").append(root);
  const view = new definition.ctor();
  const entry = {
    routeId,
    root,
    view,
    mountPromise: null,
    mountScheduled: false,
    mounted: false,
    hasEntered: false,
  };
  viewCache.set(routeId, entry);
  return entry;
}

function renderMountFailure(entry, error) {
  console.error(`WineFox route mount failed: ${entry.routeId}`, error);
  entry.root.replaceChildren(
    el("div", { class: "empty", role: "alert" }, [
      ic("warn"),
      el("div", { class: "empty-title" }, "页面暂时无法打开"),
      el("div", { class: "empty-desc" }, "后台操作已停止，请稍后重新进入此页面。"),
    ]),
  );
}

function scheduleMount(entry) {
  if (entry.mounted || entry.mountScheduled) return;
  entry.mountScheduled = true;

  // Allow the loading state and route selection to paint before a view does
  // any synchronous DOM construction. Rapidly skipped routes are not mounted
  // in the background, so they cannot compete with the active page.
  requestAnimationFrame(() => {
    setTimeout(() => {
      entry.mountScheduled = false;
      if (entry.mounted || currentEntry !== entry) return;
      entry.mounted = true;
      try {
        entry.mountPromise = Promise.resolve(entry.view.mount(entry.root));
      } catch (error) {
        entry.mountPromise = Promise.reject(error);
      }
      entry.mountPromise = entry.mountPromise.catch((error) => {
        entry.mounted = false;
        renderMountFailure(entry, error);
        return null;
      });
    }, 0);
  });
}

function playViewEntrance(entry, token) {
  entry.hasEntered = true;
  clearTimeout(enterTimer);
  entry.root.classList.remove("view-motion-enter");
  requestAnimationFrame(() => {
    if (token !== viewToken || currentEntry !== entry) return;
    entry.root.classList.add("view-motion-enter");
    enterTimer = setTimeout(() => {
      if (currentEntry === entry) entry.root.classList.remove("view-motion-enter");
    }, 460);
  });
}

function switchView(routeId) {
  const token = ++viewToken;
  const entry = getViewEntry(routeId);
  if (!entry) return;

  if (currentEntry && currentEntry !== entry) {
    currentEntry.view.deactivate?.();
    currentEntry.root.hidden = true;
    currentEntry.root.removeAttribute("id");
    currentEntry.root.classList.remove("view-motion-enter");
  }

  currentEntry = entry;
  entry.root.id = "view-root";
  entry.root.hidden = false;
  if (entry.mounted) entry.view.activate?.();
  playViewEntrance(entry, token);
  scheduleMount(entry);
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

function setConnectionRetryBusy(busy) {
  const button = $("#conn-retry");
  if (!button) return;
  button.disabled = busy;
  button.classList.toggle("is-loading", busy);
  button.setAttribute("aria-busy", busy ? "true" : "false");
}

function refreshHealth({ manual = false } = {}) {
  if (healthRefreshPromise) {
    if (manual) {
      setConnectionRetryBusy(true);
      healthRefreshPromise.finally(() => setConnectionRetryBusy(false));
    }
    return healthRefreshPromise;
  }
  if (manual) {
    setConnectionRetryBusy(true);
    const connText = $("#conn-status .conn-text");
    if (connText) connText.textContent = "正在重新检测…";
  }
  healthRefreshPromise = Promise.allSettled([
    withTimeout((signal) => api.health({ signal }), 5000),
    withTimeout(
      (signal) => manual ? api.probe({ force: true }, { signal }) : api.comfyui({ signal }),
      5000,
    ),
  ]).then(([healthResult, comfyResult]) => {
    if (healthResult.status === "fulfilled") {
      store.set("health", healthResult.value);
    } else {
      store.set("health", null);
      store.set("comfyui", null);
      const conn = $("#conn-status");
      if (conn) {
        conn.className = "conn err";
        conn.querySelector(".conn-text").textContent = "后端离线";
      }
      return;
    }
    if (comfyResult.status === "fulfilled") {
      store.set("comfyui", comfyResult.value);
      applyHealth(healthResult.value);
      return;
    }
    store.set("comfyui", null);
    const conn = $("#conn-status");
    if (conn) {
      conn.className = "conn err";
      conn.querySelector(".conn-text").textContent = "ComfyUI 检测失败";
    }
  }).catch((error) => {
    console.warn("WineFox health refresh failed", error);
  }).finally(() => {
    if (manual) setConnectionRetryBusy(false);
    healthRefreshPromise = null;
    clearTimeout(connTimer);
    connTimer = setTimeout(refreshHealth, document.hidden ? 30000 : 10000);
  });
  return healthRefreshPromise;
}

function bindConnectionRetry() {
  $("#conn-retry")?.addEventListener("click", () => refreshHealth({ manual: true }));
}

/* ============ 减少动画 · 性能优先引擎 ============
 * 设置里的勾选项保存后立即生效（无需重启）：
 *   勾选（默认）→ 跟随系统 prefers-reduced-motion，且低配设备（≤4 核或 ≤4GB）
 *                 自动启用全套性能优化——这是老机器最直接的效果改变；
 *   取消勾选   → 强制完整动画（html[data-motion=full]），无视系统设置。
 * 生效载体是 <html data-motion>，CSS 性能套装见 tokens.css。 */
function isLowPowerDevice() {
  try {
    const mem = navigator.deviceMemory || navigator.deviceMemory === 0 ? navigator.deviceMemory : Infinity;
    const cores = navigator.hardwareConcurrency || Infinity;
    return mem <= 4 || cores <= 4;
  } catch { return false; }
}

function applyMotionPreference() {
  const root = document.documentElement;
  let pref = "1";
  try { pref = localStorage.getItem("wfx-reduced-motion") ?? "1"; } catch { /* 忽略 */ }
  if (pref === "0") {
    root.dataset.motion = "full";
    return;
  }
  const systemReduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (systemReduce || isLowPowerDevice()) root.dataset.motion = "reduce";
  else delete root.dataset.motion;
}

window.__wfxApplyMotion = applyMotionPreference;
window.__wfxReducedMotion = () =>
  document.documentElement.dataset.motion === "reduce" ||
  (!document.documentElement.dataset.motion &&
    matchMedia("(prefers-reduced-motion: reduce)").matches === true);

function bindTheme() {
  store.applyTheme();
  applyMotionPreference();
  try {
    matchMedia("(prefers-reduced-motion: reduce)").addEventListener("change", applyMotionPreference);
  } catch { /* 旧内核无 addEventListener */ }
  const THEME_TOASTS = { light: "已切换浅色主题", dark: "已切换深色主题", pink: "已切换猛男粉主题" };
  const toggle = () => {
    const change = () => store.toggleTheme();
    const reduced = window.__wfxReducedMotion();
    if (!reduced && document.startViewTransition) document.startViewTransition(change);
    else change();
    toast(THEME_TOASTS[store.get("theme")] || "已切换主题", "info");
  };
  $("#theme-toggle")?.addEventListener("click", toggle);
  document.addEventListener("keydown", (event) => {
    const tag = document.activeElement?.tagName;
    if (event.key.toLowerCase() === "t" && !event.ctrlKey && !event.metaKey && !event.altKey && tag !== "INPUT" && tag !== "TEXTAREA" && tag !== "SELECT") toggle();
  });
}

function applyDebugState(enabled) {
  const changed = debugEnabled !== enabled;
  debugEnabled = enabled;
  store.set("debugEnabled", enabled);
  if (changed) {
    buildRail();
    syncRailAria();
    if (!enabled && parseRoute() === "lab") location.hash = "#/generate";
  }
}

function syncRailAria() {
  const id = parseRoute();
  $$("#rail .rail-item").forEach((link) => {
    if (link.getAttribute("href") === `#/${id}`) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
}

/* 左上角 WineFox Logo：连续点击 10 次解锁调试模式（逐次调用 Rust record_logo_click）。 */
function bindLogoDebug() {
  const mark = $(".brand-mark");
  if (!mark || mark.dataset.debugBound) return;
  mark.dataset.debugBound = "1";
  mark.addEventListener("click", () => {
    void debugBridge.click().then((res) => {
      if (!res.ok || !res.result) return;
      if (res.result.unlocked) {
        mark.style.opacity = "";
        applyDebugState(true);
        toast("调试模式已开启，实验室可用", "ok");
      } else if (res.result.count >= 2) {
        mark.style.opacity = String(0.55 + 0.45 * (res.result.count / 10));
      }
    });
  });
}

async function maybeLoadMock() {
  const query = new URLSearchParams(location.search);
  if (!query.has("mock") && !query.has("dev")) return;
  const { installMock } = await import("../dev/mock.js");
  installMock({ scenario: query.get("scenario") || "success" });
}

function withTimeout(run, ms = 4000) {
  const controller = new AbortController();
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      controller.abort();
      reject(new Error("preload timeout"));
    }, ms);
    let promise;
    try {
      promise = Promise.resolve(run(controller.signal));
    } catch (error) {
      clearTimeout(timer);
      reject(error);
      return;
    }
    promise.then(
      (value) => { clearTimeout(timer); resolve(value); },
      (error) => { clearTimeout(timer); reject(error); },
    );
  });
}

async function preloadWorkspace() {
  sendLaunchProgress("正在读取设置与模型目录");
  const sources = {
    health: (signal) => api.health({ signal }),
    settings: (signal) => api.settings({ signal }),
    catalog: (signal) => api.catalog({ signal }),
    secrets: (signal) => api.secrets({ signal }),
    history: (signal) => api.history({ limit: 24 }, { signal }),
    categories: (signal) => api.categories({ signal }),
    favorites: (signal) => api.favorites({ limit: 24 }, { signal }),
    comfyui: (signal) => api.comfyui({ signal }),
  };
  const entries = Object.entries(sources);
  const results = await Promise.allSettled(entries.map(([, run]) => withTimeout(run)));
  results.forEach((result, index) => {
    if (result.status === "fulfilled") store.set(entries[index][0], result.value);
  });
  // 减少动画以服务端设置为准（localStorage 只是即时生效缓存），
  // 否则勾选状态与实际动效会在重启后各说各话。
  try {
    const serverPref = store.get("settings")?.ui?.reduced_motion;
    if (typeof serverPref === "boolean") {
      localStorage.setItem("wfx-reduced-motion", serverPref ? "1" : "0");
      window.__wfxApplyMotion?.();
    }
  } catch { /* 忽略 */ }
  if (store.get("catalog")) store.set("models", store.get("catalog"));
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
  // 桌面应用体验：屏蔽 WebView 自带的浏览器右键菜单（输入框保留系统菜单，
  // 便于粘贴/拼写）。图片同样禁止拖拽出鬼影。
  window.addEventListener("contextmenu", (event) => {
    const editable = event.target.closest("input, textarea, [contenteditable='true']");
    if (!editable) event.preventDefault();
  });
  window.addEventListener("dragstart", (event) => {
    if (event.target instanceof HTMLImageElement) event.preventDefault();
  });
  bindTheme();
  bindConnectionRetry();
  bindLogoDebug();
  buildRail();
  bindJobTally();
  await preloadWorkspace();

  // 面板刷新时恢复调试模式状态（Rust 内存状态在 Shell 侧保持不变）。
  const debugState = await debugBridge.getState();
  if (debugState.ok && debugState.state?.enabled) applyDebugState(true);
  debugBridge.onDebugState((enabled) => applyDebugState(enabled));

  // Route roots are retained after their first mount. This preserves editing
  // state and prevents rapid back-and-forth navigation from refetching data.
  $("#view-root")?.remove();

  const navigate = () => {
    const id = parseRoute();
    syncRailAria();
    switchView(id);
  };
  window.addEventListener("hashchange", navigate);
  navigate();

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

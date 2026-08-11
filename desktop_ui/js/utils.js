/* WineFox Desktop — 工具函数与内联图标库
 * 安全约定：绝不渲染绝对路径（sanitizeName）；不加载外部资源（图标全部内联）。
 */

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(node.dataset, v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of [].concat(children)) {
    if (c == null) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}

/**
 * Text values are inserted by `el()` with textContent, so HTML escaping here
 * would render entities literally (for example `&amp;`). Keep this helper as
 * a compatibility boundary for existing views, but return display text.
 */
export const esc = (s) => String(s ?? "");

/** Create trusted inline SVG markup in the SVG namespace.
 * `markup` is only used with static literals from the bundled UI; untrusted
 * API values must continue to go through `el()`/textContent.
 */
export function svgEl(viewBox, markup, attrs = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  node.setAttribute("viewBox", viewBox);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    node.setAttribute(k, v === true ? "" : v);
  }
  node.innerHTML = markup;
  return node;
}

export const debounce = (fn, ms) => {
  let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
};

/** Shared WineFox loading state: a restrained ink line and fox seal. */
export function loadingState(label = "正在加载", { compact = false } = {}) {
  return el("div", {
    class: `wfx-loading${compact ? " is-compact" : ""}`,
    role: "status",
    "aria-live": "polite",
    "aria-busy": "true",
  }, [
    el("div", { class: "wfx-loading-mark", "aria-hidden": "true" }, [
      el("span", { class: "wfx-loading-ink" }),
      el("span", { class: "wfx-loading-seal" }, "狐"),
    ]),
    el("span", { class: "wfx-loading-label" }, label),
  ]);
}

/* 只保留路径最后一段（文件名），杜绝绝对路径泄露 */
export function sanitizeName(name) {
  if (name == null) return "";
  const s = String(name).trim();
  if (!s) return "";
  const seg = s.split(/[\\/]/).pop();
  return seg || s.replace(/^[\\/]+/, "");
}

/* Preserve safe ComfyUI-relative subfolders while never exposing local paths. */
export function sanitizeResourceName(name) {
  if (name == null) return "";
  const value = String(name).trim().replace(/\\/g, "/");
  if (!value) return "";
  if (/^[a-z]:\//i.test(value) || value.startsWith("/")) return sanitizeName(value);
  const parts = value.split("/").filter((part) => part && part !== "." && part !== "..");
  return parts.join("/") || sanitizeName(value);
}

export function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function fmtRelative(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const diff = Date.now() - d.getTime();
  const m = Math.floor(diff / 60000);
  if (m < 1) return "刚刚";
  if (m < 60) return `${m} 分钟前`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} 小时前`;
  const day = Math.floor(h / 24);
  if (day < 30) return `${day} 天前`;
  return fmtTime(iso).slice(0, 10);
}

export function fmtBytes(n) {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

export function debouncedSearchInput(placeholder, onInput) {
  const box = el("div", { class: "search" });
  box.append(
    ic("search", { "aria-hidden": "true" }),
    el("input", { class: "input", type: "search", placeholder, "aria-label": placeholder }),
  );
  box.querySelector("input").addEventListener("input", debounce((e) => onInput(e.target.value.trim()), 250));
  return box;
}

export function downloadText(filename, text, mime = "text/plain") {
  const blob = new Blob([text], { type: `${mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const a = el("a", { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

/** Copy plain text without storing it outside the current process. */
export async function copyText(text) {
  const value = String(text ?? "");
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }

  const input = el("textarea", {
    readonly: true,
    "aria-hidden": "true",
    style: "position:fixed;left:-9999px;top:0;opacity:0",
  }, value);
  document.body.append(input);
  let copied = false;
  try {
    input.select();
    copied = document.execCommand("copy");
  } finally {
    input.remove();
  }
  if (!copied) throw new Error("Clipboard is unavailable");
}

/* ================= Toast（全局单例） ================= */
let toastRegion = null;
export function toast(msg, kind = "ok", ms = 3200) {
  if (!toastRegion) toastRegion = $("#toast-region");
  if (!toastRegion) return;
  const icons = { ok: "check", err: "warn", warn: "warn", info: "info" };
  const node = el("div", { class: "toast " + kind, role: kind === "err" ? "alert" : "status" }, [
    el("span", { class: "t-ico" }, [ic(icons[kind] || "info")]),
    el("span", { class: "t-msg" }, msg),
  ]);
  node.style.setProperty("--toast-ms", ms + "ms");
  toastRegion.append(node);
  const t = setTimeout(() => {
    node.classList.add("leave");
    setTimeout(() => node.remove(), 220);
  }, ms);
  node.addEventListener("click", () => { clearTimeout(t); node.remove(); });
}

/** 通过 fetch → blob → objectURL 触发下载：WebView2 下 <a download> 对 http URL 无效 */
export async function downloadUrl(url, filename) {
  const blob = await (await fetch(url)).blob();
  const objectUrl = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = objectUrl;
  a.download = filename || "winefox.png";
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(objectUrl), 5000);
}

/* 保存到用户选择的位置：通过 postMessage 桥调用 Shell 的系统“另存为”对话框。
 * 返回 { ok, path?, cancelled?, downloaded? }；桥不可用时降级为直接下载。 */
let saveSeq = 0;
const saveWaiters = new Map();
window.addEventListener("message", (event) => {
  if (event.data?.type !== "winefox-ui-save-result") return;
  const waiter = saveWaiters.get(event.data.id);
  if (!waiter) return;
  saveWaiters.delete(event.data.id);
  clearTimeout(waiter.timer);
  waiter.resolve(event.data);
});

export function saveImageDialog(url, filename) {
  return new Promise((resolve) => {
    (async () => {
      try {
        const blob = await (await fetch(url)).blob();
        const dataUrl = await new Promise((res, rej) => {
          const reader = new FileReader();
          reader.onload = () => res(reader.result);
          reader.onerror = () => rej(new Error("读取图片失败"));
          reader.readAsDataURL(blob);
        });
        // 不在 Shell iframe 内（浏览器直接打开）→ 降级为直接下载
        if (window.parent === window) {
          await downloadUrl(url, filename);
          resolve({ ok: true, downloaded: true });
          return;
        }
        const id = "save-" + (++saveSeq) + "-" + Date.now();
        const result = await new Promise((res) => {
          const timer = setTimeout(() => {
            saveWaiters.delete(id);
            res({ ok: false, timeout: true });
          }, 60000);
          saveWaiters.set(id, { timer, resolve: res });
          window.parent.postMessage({ type: "winefox-ui-save-image", id, dataUrl, filename }, "*");
        });
        if (!result.ok && !result.cancelled) {
          // 桥失败（旧 Shell）→ 降级为直接下载
          await downloadUrl(url, filename);
          resolve({ ok: true, downloaded: true });
        } else {
          resolve(result);
        }
      } catch {
        resolve({ ok: false, error: "保存失败" });
      }
    })();
  });
}

/* ================= 内联线描图标 ================= */
const ICONS = {
  sun: '<path d="M8 10.8a2.8 2.8 0 1 0 0-5.6 2.8 2.8 0 0 0 0 5.6ZM8 2.6v1.3M8 12.1v1.3M2.6 8h1.3M12.1 8h1.3M4 4l.9.9M11.1 11.1 12 12M12 4l-.9.9M4.9 11.1 4 12" stroke-width="1.4"/>',
  moon: '<path d="M8 2.6a5.4 5.4 0 1 0 5.2 6.9 4.1 4.1 0 0 1-5.2-5.2 5.4 5.4 0 0 0 0-1.7Z" stroke-width="1.4"/>',
  gen: '<path d="M3 4.5h10M3 8h6.5M3 11.5h10M17.5 3v10M12 16l5.5 2.8L23 16" stroke-width="1.5"/>',
  history: '<path d="M3.5 8A5.5 5.5 0 1 1 8 13.5" stroke-width="1.4"/><path d="M3.5 8V4M3.5 8h4M8 5.5V8l2 1.5" stroke-width="1.4"/>',
  fav: '<path d="M8 3.2c-1.9-2.3-5.6-1.4-5.9 1.5-.2 2 1.3 3.7 5.9 7 4.6-3.3 6.1-5 5.9-7C13.6 1.8 9.9.9 8 3.2Z" stroke-width="1.5"/>',
  favFill: '<path d="M8 3.2c-1.9-2.3-5.6-1.4-5.9 1.5-.2 2 1.3 3.7 5.9 7 4.6-3.3 6.1-5 5.9-7C13.6 1.8 9.9.9 8 3.2Z" fill="currentColor"/>',
  model: '<path d="M8 2.8 2.8 5.5v5L8 13.2l5.2-2.7v-5L8 2.8Z" stroke-width="1.4"/><path d="M2.8 5.5 8 8.2l5.2-2.7M8 8.2v5" stroke-width="1.4"/>',
  gear: '<path d="M8 10.2a2.2 2.2 0 1 0 0-4.4 2.2 2.2 0 0 0 0 4.4Z" stroke-width="1.5"/><path d="M8 1.8v1.6M8 12.6v1.6M1.8 8h1.6M12.6 8h1.6M3.6 3.6l1.1 1.1M11.3 11.3l1.1 1.1M12.4 3.6l-1.1 1.1M4.7 11.3l-1.1 1.1" stroke-width="1.4"/>',
  diag: '<path d="M3 12.5h2v-3H3v3ZM7 12.5h2V5.5H7v7ZM11 12.5h2V8.5h-2v4ZM15 12.5h2V3h-2v9.5ZM2 15.5h14" stroke-width="1.4"/>',
  search: '<path d="M7.2 12.2a5 5 0 1 0 0-10 5 5 0 0 0 0 10ZM10.8 10.8 14.5 14.5" stroke-width="1.5"/>',
  x: '<path d="m4 4 8 8M12 4l-8 8" stroke-width="1.5"/>',
  check: '<path d="m3 8 4 4 7-8" stroke-width="1.8"/>',
  copy: '<path d="M5.5 5.5h7v7h-7z" stroke-width="1.3"/><path d="M8.5 3h5.5v5.5" stroke-width="1.3"/>',
  trash: '<path d="M3.5 4.5h9M5.5 4.5V2.8h5v1.7M6.5 7v5M10 7v5M4.2 4.5l.6 8.2a1 1 0 0 0 1 .9h4.4a1 1 0 0 0 1-.9l.6-8.2" stroke-width="1.3"/>',
  restore: '<path d="M3.5 8A5.5 5.5 0 1 1 8 13.5" stroke-width="1.4"/><path d="M3.5 8V4M3.5 8h4M5 6.2 3.5 8 5 9.8" stroke-width="1.3"/>',
  refresh: '<path d="M13.5 4.5A5.5 5.5 0 1 0 14 10M13.5 4.5V1.8M13.5 4.5h-2.7" stroke-width="1.4"/>',
  download: '<path d="M8 2.5v7M5.2 7 8 9.8 10.8 7M3 12h10" stroke-width="1.4"/>',
  upload: '<path d="M8 10.5v-7M5.2 6.5 8 3.7l2.8 2.8M3 12h10" stroke-width="1.4"/>',
  eye: '<path d="M1.8 8S4 3.5 8 3.5 14.2 8 14.2 8 12 12.5 8 12.5 1.8 8 1.8 8Z" stroke-width="1.4"/><path d="M8 9.8a1.8 1.8 0 1 0 0-3.6 1.8 1.8 0 0 0 0 3.6Z" stroke-width="1.3"/>',
  play: '<path d="M4.5 3 12.5 8 4.5 13Z" stroke-width="1.4"/>',
  save: '<path d="M3 2.8h8.5L14.2 5.5V13.2H3V2.8Z" stroke-width="1.4"/><path d="M5 2.8v3.2h6V2.8M5 13.2V9h6v4.2" stroke-width="1.3"/>',
  warn: '<path d="M8 2.5 14.8 13.5H1.2L8 2.5Z" stroke-width="1.4"/><path d="M8 6.5v3M8 11.4v.1" stroke-width="1.5"/>',
  info: '<path d="M8 14.5a6.5 6.5 0 1 0 0-13 6.5 6.5 0 0 0 0 13Z" stroke-width="1.3"/><path d="M8 7.5V11M8 4.8v.1" stroke-width="1.5"/>',
  queue: '<path d="M3 4h10M3 8h10M3 12h6" stroke-width="1.5"/><path d="M14 12.5h2.5V15H14z" stroke-width="1.2"/>',
  flask: '<path d="M6.2 2.8h3.6M7 2.8v4.2L3.4 12a2 2 0 0 0 1.7 3h5.8a2 2 0 0 0 1.7-3L9 7V2.8" stroke-width="1.4"/><path d="M5.6 10h4.8" stroke-width="1.3"/>',
  link: '<path d="M7 9.2 9.2 11a3.4 3.4 0 0 0 4.8 0l1.5-1.5a3.4 3.4 0 0 0-4.8-4.8L9.8 5.6" stroke-width="1.3"/><path d="M9 6.8 6.8 5A3.4 3.4 0 0 0 2 9.8l1.5 1.5a3.4 3.4 0 0 0 4.8 0L9.2 10" stroke-width="1.3"/>',
  wand: '<path d="m4 12 6-6 2 2-6 6-2.5.5L4 12Z" stroke-width="1.3"/><path d="m8.5 7.5 2 2M11.5 3l.5 1.5L13.5 5 12 5.5 11.5 7 11 5.5 9.5 5 11 4.5 11.5 3Z" stroke-width="1.2"/>',
  trashOpen: '<path d="M3 4.5h10M6 6.2 5.8 12a1 1 0 0 0 1 1h4.4a1 1 0 0 0 1-1l-.2-5.8M7.3 7.5v3.8M9.7 7.5v3.8" stroke-width="1.3"/>',
  alert: '<path d="M8 2.5 14.8 13.5H1.2L8 2.5Z" stroke-width="1.4"/><path d="M8 6.5v3M8 11.4v.1" stroke-width="1.5"/>',
  image: '<path d="M2.8 3h10.4a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1H2.8a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z" stroke-width="1.3"/><path d="m2.5 11 3.4-3.4 2.4 2.4 2.2-2.2 3 3" stroke-width="1.3"/><path d="M5.3 6a1 1 0 1 0 0-.1" stroke-width="1.4"/>',
};

export function ic(name, attrs = {}) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", ["ui-icon", attrs.class].filter(Boolean).join(" "));
  svg.setAttribute("width", attrs.width || "16");
  svg.setAttribute("height", attrs.height || "16");
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class" || k === "width" || k === "height") continue;
    svg.setAttribute(k, v);
  }
  svg.innerHTML = ICONS[name] || ICONS.info;
  return svg;
}

/* 状态 → 徽章 */
export const STATUS_META = {
  queued: { label: "排队中", cls: "neutral", pulse: true },
  running: { label: "生成中", cls: "primary", pulse: true },
  completed: { label: "已完成", cls: "ok" },
  failed: { label: "失败", cls: "danger" },
  cancelled: { label: "已取消", cls: "neutral" },
};

/* ================= 自动更新桥（iframe → Shell → Rust） ================= */
let updateSeq = 0;
const updateWaiters = new Map();
const updateStateListeners = new Set();

window.addEventListener("message", (event) => {
  if (event.data?.type === "winefox-ui-update-reply") {
    const waiter = updateWaiters.get(event.data.id);
    if (!waiter) return;
    updateWaiters.delete(event.data.id);
    clearTimeout(waiter.timer);
    waiter.resolve(event.data);
  } else if (event.data?.type === "winefox-ui-update-state") {
    updateStateListeners.forEach((fn) => { try { fn(event.data.state); } catch { /* 忽略 */ } });
  }
});

function updateRequest(action) {
  return new Promise((resolve) => {
    if (window.parent === window) {
      resolve({ ok: false, error: "未运行在桌面壳中" });
      return;
    }
    const id = "up-" + (++updateSeq) + "-" + Date.now();
    const timer = setTimeout(() => {
      updateWaiters.delete(id);
      resolve({ ok: false, error: "更新服务无响应" });
    }, 20000);
    const p = new Promise((res) => updateWaiters.set(id, { timer, resolve: res }));
    window.parent.postMessage({ type: "winefox-ui-update", action, id }, "*");
    p.then(resolve);
  });
}

export const updateBridge = {
  getState: () => updateRequest("getState"),
  check: () => updateRequest("check"),
  download: () => updateRequest("download"),
  onState: (fn) => {
    updateStateListeners.add(fn);
    return () => updateStateListeners.delete(fn);
  },
};

/* ================= 调试模式与房间桥（iframe → Shell → Rust） ================= */
let bridgeSeq = 0;
const debugWaiters = new Map();
const debugStateListeners = new Set();
const roomWaiters = new Map();
const roomStateListeners = new Set();

window.addEventListener("message", (event) => {
  if (event.data?.type === "winefox-ui-debug-reply") {
    const waiter = debugWaiters.get(event.data.id);
    if (!waiter) return;
    debugWaiters.delete(event.data.id);
    clearTimeout(waiter.timer);
    waiter.resolve(event.data);
  } else if (event.data?.type === "winefox-ui-debug-state") {
    debugStateListeners.forEach((fn) => { try { fn(!!event.data.enabled); } catch { /* 忽略 */ } });
  } else if (event.data?.type === "winefox-ui-room-state") {
    roomStateListeners.forEach((fn) => { try { fn(event.data.state); } catch { /* 忽略 */ } });
  }
});

function bridgeRequest(prefix, messageType, action, payload = {}) {
  return new Promise((resolve) => {
    if (window.parent === window) {
      resolve({ ok: false, error: "未运行在桌面壳中" });
      return;
    }
    const id = prefix + "-" + (++bridgeSeq) + "-" + Date.now();
    const timer = setTimeout(() => {
      debugWaiters.delete(id);
      resolve({ ok: false, error: "本地服务无响应" });
    }, 20000);
    const p = new Promise((res) => debugWaiters.set(id, { timer, resolve: res }));
    window.parent.postMessage({ type: messageType, action, ...payload, id }, "*");
    p.then(resolve);
  });
}

export const debugBridge = {
  click: () => bridgeRequest("dbg", "winefox-ui-debug", "click"),
  getState: () => bridgeRequest("dbg", "winefox-ui-debug", "getState"),
  onDebugState: (fn) => {
    debugStateListeners.add(fn);
    return () => debugStateListeners.delete(fn);
  },
};

export const roomBridge = {
  getState: () => bridgeRequest("rm", "winefox-ui-room", "getState"),
  start: (settings) => bridgeRequest("rm", "winefox-ui-room", "start", { settings }),
  stop: () => bridgeRequest("rm", "winefox-ui-room", "stop"),
  setTunnel: (url) => bridgeRequest("rm", "winefox-ui-room", "setTunnel", { url }),
  clearTunnel: () => bridgeRequest("rm", "winefox-ui-room", "clearTunnel"),
  provisionTunnel: () => bridgeRequest("rm", "winefox-ui-room", "provisionTunnel"),
  stopTunnel: () => bridgeRequest("rm", "winefox-ui-room", "stopTunnel"),
  cloudflaredAvailable: () => bridgeRequest("rm", "winefox-ui-room", "cloudflaredAvailable"),
  onState: (fn) => {
    roomStateListeners.add(fn);
    return () => roomStateListeners.delete(fn);
  },
};

export function fmtBytesSize(bytes) {
  if (!bytes || bytes <= 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function fmtSpeed(bps) {
  if (!bps) return "";
  return bps >= 1024 * 1024 ? `${(bps / 1024 / 1024).toFixed(1)} MB/s` : `${Math.round(bps / 1024)} KB/s`;
}

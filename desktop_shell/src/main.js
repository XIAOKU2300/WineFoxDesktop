/* WineFox shell frontend: startup/error page + panel host + exit flow. */
"use strict";

/* ------------------------------------------------------------------ */
/* bridge                                                              */
/* ------------------------------------------------------------------ */
const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;

let current = null; // last AppStatus from the backend

const STATE_TEXT = {
  Stopped: "已停止",
  Starting: "正在启动…",
  Ready: "已就绪",
  Error: "启动失败",
  Stopping: "正在停止…",
};

function $(id) { return document.getElementById(id); }

async function refreshStatus() {
  try {
    current = await invoke("get_status");
  } catch (e) {
    console.error("get_status failed", e);
  }
}

/* ------------------------------------------------------------------ */
/* render (index page)                                                 */
/* ------------------------------------------------------------------ */
function renderIndex() {
  if (!current) return;
  const title = $("status-title"), dot = $("status-dot"), detail = $("status-detail");
  const spinner = $("spinner"), bar = $("progress-bar"), errBox = $("error-box");
  const btnOpen = $("btn-open"), btnRetry = $("btn-retry");

  title.textContent = STATE_TEXT[current.state] || current.state;
  dot.className = "dot " + (current.state === "Ready" ? "ready" : current.state === "Error" ? "error" : "starting");
  spinner.hidden = !(current.state === "Starting");
  bar.hidden = !(current.state === "Starting");

  if (current.state === "Ready") {
    detail.textContent = `端口 ${current.port} · 协议 v${current.protocol} · 重启 ${current.restart_count} 次`;
    btnOpen.disabled = false;
    btnRetry.hidden = true;
    errBox.hidden = true;
  } else if (current.state === "Error") {
    detail.textContent = "启动失败，可重试或查看下方日志";
    btnOpen.disabled = true;
    btnRetry.hidden = false;
    errBox.hidden = false;
    $("error-text").textContent = current.last_error || "未知错误";
    if (current.log_tail && current.log_tail.length) {
      $("log-tail").textContent = current.log_tail.join("\n");
    } else {
      $("log-tail").textContent = "（无日志）";
    }
  } else {
    detail.textContent = "正在拉起本地 sidecar 服务…";
    btnOpen.disabled = true;
    btnRetry.hidden = true;
    errBox.hidden = true;
  }

  if (current.quitting) {
    title.textContent = "正在退出…";
    showQuitting(current);
  }
}

function openPanel() {
  window.location.href = "app.html";
}

/* ------------------------------------------------------------------ */
/* render (app page)                                                   */
/* ------------------------------------------------------------------ */
function renderApp() {
  if (!current) return;
  const dot = $("bar-dot"), st = $("bar-state"), ver = $("bar-version");
  ver.textContent = "v" + (current.version || "?");
  if (current.state === "Ready") {
    dot.className = "dot ready";
    st.textContent = "已连接 · " + (current.busy ? `任务 ${current.tasks}` : "空闲");
    $("panel").src = current.panel_url || "";
  } else if (current.state === "Error") {
    dot.className = "dot error";
    st.textContent = "服务已断开";
    window.location.href = "index.html";
  } else {
    dot.className = "dot starting";
    st.textContent = "重连中…";
    setTimeout(refreshStatus, 800);
  }
  if (current.quitting) showQuitting(current);
}

/* ------------------------------------------------------------------ */
/* exit flow                                                           */
/* ------------------------------------------------------------------ */
let exitModalShown = false;

function showExitModal(busy, tasks) {
  if (exitModalShown) return;
  exitModalShown = true;
  const m = $("exit-modal");
  if (!m) { exitModalShown = false; return; }
  $("exit-task-count").textContent = String(tasks ?? 0);
  m.hidden = false;
  $("exit-wait").onclick = () => { m.hidden = true; exitModalShown = false; confirmExit("wait"); };
  $("exit-now").onclick = () => { m.hidden = true; exitModalShown = false; confirmExit("now"); };
  $("exit-cancel").onclick = () => { m.hidden = true; exitModalShown = false; };
}

function showQuitting(st) {
  const m = $("quitting-modal");
  if (!m) return;
  m.hidden = false;
  const wait = st.shutdown_pending && st.shutdown_mode === "wait";
  $("quitting-title").textContent = wait ? "正在退出…（等待任务完成）" : "正在退出…";
  $("quitting-text").textContent = wait
    ? "不会打断 ComfyUI 后端，任务完成即退出。"
    : "正在停止本地服务…";
  const force = $("quit-force");
  force.hidden = !wait;
  force.onclick = () => confirmExit("now");
  const back = $("quit-back");
  back.hidden = !wait;
  back.onclick = () => { invoke("cancel_exit"); m.hidden = true; };
}

async function beginExit() {
  try {
    const r = await invoke("begin_exit");
    if (r.needs_choice) {
      showExitModal(r.busy, r.tasks);
    } else {
      confirmExit("now");
    }
  } catch (e) {
    console.error("begin_exit failed", e);
    confirmExit("now");
  }
}

async function confirmExit(mode) {
  try {
    await invoke("confirm_exit", { mode });
  } catch (e) {
    console.error("confirm_exit failed", e);
  }
}

/* ------------------------------------------------------------------ */
/* boot                                                                */
/* ------------------------------------------------------------------ */
async function boot() {
  await refreshStatus();

  if (document.getElementById("btn-open")) {
    // index page
    $("btn-open").onclick = openPanel;
    $("btn-retry").onclick = () => invoke("restart_sidecar");
    $("btn-quit").onclick = beginExit;
    $("app-version").textContent = current?.version || "…";
    renderIndex();
  } else {
    // app page
    $("btn-back").onclick = () => { window.location.href = "index.html"; };
    $("btn-quit-app").onclick = beginExit;
    renderApp();
  }

  await listen("sidecar-status", (ev) => {
    current = ev.payload;
    if (document.getElementById("btn-open")) renderIndex();
    else renderApp();
  });

  await listen("exit-requested", () => beginExit());
  await listen("focus-requested", () => {
    // Second instance was launched: re-render so the window reflects reality.
    refreshStatus().then(() => {
      if (document.getElementById("btn-open")) renderIndex();
      else renderApp();
    });
  });
}

document.addEventListener("DOMContentLoaded", boot);

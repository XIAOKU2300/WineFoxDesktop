/* WineFox shell: one continuous launch scene, then the real desktop UI. */
"use strict";

const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;

const $ = (id) => document.getElementById(id);
const PANEL_ORIGIN = "http://winefox-panel.localhost";
let current = null;
let panelReady = false;
let exitModalShown = false;

async function refreshStatus() {
  try {
    current = await invoke("get_status");
    renderStatus();
  } catch (error) {
    showError(String(error));
  }
}

function setLaunchText(text) {
  const detail = $("status-detail");
  if (detail) detail.textContent = text;
}

function showError(message, logs = []) {
  panelReady = false;
  $("panel").classList.remove("ready");
  $("panel").hidden = true;
  $("launch").classList.remove("done");
  $("error-box").hidden = false;
  $("error-text").textContent = message || "本地服务暂时无法启动。";
  $("log-tail").textContent = logs.length ? logs.join("\n") : "";
  setLaunchText("启动没有完成，可以重新尝试");
}

function setPanel(url) {
  const panel = $("panel");
  if (!url || panel.dataset.url === url) return;
  panel.dataset.url = url;
  panel.hidden = false;
  panel.onload = () => setLaunchText("正在预热模型、历史与界面资源");
  panel.src = url;
}

function revealPanel() {
  if (panelReady) return;
  panelReady = true;
  const panel = $("panel");
  panel.hidden = false;
  requestAnimationFrame(() => {
    panel.classList.add("ready");
    $("launch").classList.add("done");
  });
}

function renderStatus() {
  if (!current) return;
  if (current.state === "ready") {
    $("error-box").hidden = true;
    setLaunchText("正在载入创作资料");
    setPanel(current.panelUrl || "");
  } else if (current.state === "error") {
    showError(current.lastError || "本地服务启动失败", current.logTail || []);
  } else if (current.state === "stopping") {
    setLaunchText("正在停止本地服务");
  } else {
    setLaunchText("启动本地服务并整理工作区");
  }
  if (current.quitting) showQuitting(current);
}

function closeExitModal() {
  $("exit-modal").hidden = true;
  exitModalShown = false;
}

function bindExitControls() {
  $("exit-wait").onclick = () => { closeExitModal(); void confirmExit("wait"); };
  $("exit-now").onclick = () => { closeExitModal(); void confirmExit("now"); };
  $("exit-cancel").onclick = closeExitModal;
}

function showExitModal(tasks) {
  if (exitModalShown) return;
  exitModalShown = true;
  $("exit-task-count").textContent = String(tasks ?? 0);
  $("exit-modal").hidden = false;
}

function showQuitting(status) {
  const modal = $("quitting-modal");
  const waiting = status.shutdownPending && status.shutdownMode === "wait";
  modal.hidden = false;
  $("quitting-title").textContent = waiting ? "等待当前任务完成" : "正在退出";
  $("quitting-text").textContent = waiting
    ? "WineFox 会在任务完成后退出，不会中断 ComfyUI。"
    : "正在停止本地服务。";
  $("quit-force").hidden = !waiting;
  $("quit-force").onclick = () => confirmExit("now");
  $("quit-back").hidden = !waiting;
  $("quit-back").onclick = async () => {
    await invoke("cancel_exit");
    modal.hidden = true;
  };
}

async function beginExit() {
  try {
    const result = await invoke("begin_exit");
    if (result.needsChoice) showExitModal(result.tasks);
    else await confirmExit("now");
  } catch (error) {
    console.error("begin_exit failed", error);
    await confirmExit("now");
  }
}

async function confirmExit(mode) {
  try {
    await invoke("confirm_exit", { mode });
  } catch (error) {
    console.error("confirm_exit failed", error);
  }
}

async function boot() {
  bindExitControls();
  $("btn-retry").onclick = async () => {
    $("error-box").hidden = true;
    setLaunchText("正在重新启动本地服务");
    await invoke("restart_sidecar");
  };

  window.addEventListener("message", (event) => {
    const panel = $("panel");
    if (event.source !== panel.contentWindow || !event.data) return;
    if (event.origin !== PANEL_ORIGIN) return;
    if (event.data.type === "winefox-ui-progress" && typeof event.data.label === "string") {
      setLaunchText(event.data.label);
    }
    if (event.data.type === "winefox-ui-ready") revealPanel();
    if (event.data.type === "winefox-ui-save-image") {
      const reply = (payload) => {
        panel.contentWindow.postMessage({ type: "winefox-ui-save-result", id: event.data.id, ...payload }, "*");
      };
      invoke("save_base64_image", {
        dataUrl: event.data.dataUrl,
        filename: event.data.filename || "winefox.png",
      })
        .then((path) => reply({ ok: true, path }))
        .catch((error) => reply({ ok: false, error: String(error) }));
    }
    if (event.data.type === "winefox-ui-pick-dir") {
      const reply = (payload) => {
        panel.contentWindow.postMessage({ type: "winefox-ui-pick-dir-reply", id: event.data.id, ...payload }, "*");
      };
      const command = event.data.kind === "comfyui_python" ? "pick_comfyui_python" : "pick_comfyui_root";
      invoke(command)
        .then((result) => {
          if (result?.cancelled || !result?.path) reply({ ok: false, cancelled: true, error: "已取消" });
          else reply({ ok: true, path: result.path });
        })
        .catch((error) => reply({ ok: false, error: String(error) }));
    }
    if (event.data.type === "winefox-ui-update") {
      const reply = (payload) => {
        panel.contentWindow.postMessage({ type: "winefox-ui-update-reply", id: event.data.id, ...payload }, "*");
      };
      const action = event.data.action;
      if (action === "getState") {
        invoke("get_update_state").then((state) => reply({ ok: true, state })).catch((e) => reply({ ok: false, error: String(e) }));
      } else if (action === "check") {
        invoke("check_update").then((state) => reply({ ok: true, state })).catch((e) => reply({ ok: false, error: String(e) }));
      } else if (action === "download") {
        invoke("download_update").then(() => reply({ ok: true })).catch((e) => reply({ ok: false, error: String(e) }));
      }
    }
    if (event.data.type === "winefox-ui-debug") {
      const reply = (payload) => {
        panel.contentWindow.postMessage({ type: "winefox-ui-debug-reply", id: event.data.id, ...payload }, "*");
      };
      const action = event.data.action;
      if (action === "getState") {
        invoke("get_debug_mode").then((state) => reply({ ok: true, state })).catch((e) => reply({ ok: false, error: String(e) }));
      } else if (action === "click") {
        invoke("record_logo_click").then((result) => reply({ ok: true, result })).catch((e) => reply({ ok: false, error: String(e) }));
      }
    }
    if (event.data.type === "winefox-ui-room") {
      const reply = (payload) => {
        panel.contentWindow.postMessage({ type: "winefox-ui-room-reply", id: event.data.id, ...payload }, "*");
      };
      const action = event.data.action;
      if (action === "getState") {
        invoke("get_room_status").then((state) => reply({ ok: true, state })).catch((e) => reply({ ok: false, error: String(e) }));
      } else if (action === "start") {
        invoke("start_room", { settings: event.data.settings || {} }).then(() => reply({ ok: true })).catch((e) => reply({ ok: false, error: String(e) }));
      } else if (action === "stop") {
        invoke("stop_room").then(() => reply({ ok: true })).catch((e) => reply({ ok: false, error: String(e) }));
      } else if (action === "setTunnel") {
        invoke("set_room_tunnel", { url: event.data.url || "" }).then(() => reply({ ok: true })).catch((e) => reply({ ok: false, error: String(e) }));
      } else if (action === "clearTunnel") {
        invoke("clear_room_tunnel").then(() => reply({ ok: true })).catch((e) => reply({ ok: false, error: String(e) }));
      } else if (action === "provisionTunnel") {
        invoke("provision_room_tunnel").then(() => reply({ ok: true })).catch((e) => reply({ ok: false, error: String(e) }));
      } else if (action === "stopTunnel") {
        invoke("stop_room_tunnel").then(() => reply({ ok: true })).catch((e) => reply({ ok: false, error: String(e) }));
      } else if (action === "cloudflaredAvailable") {
        invoke("cloudflared_available").then((available) => reply({ ok: true, available })).catch((e) => reply({ ok: false, error: String(e) }));
      } else if (action === "openChatroom") {
        invoke("open_room_chatroom", {
          nickname: event.data.nickname ?? null,
          password: event.data.password ?? null,
        }).then(() => reply({ ok: true })).catch((e) => reply({ ok: false, error: String(e) }));
      }
    }
  });

  await listen("update-status", (event) => {
    const panel = $("panel");
    if (panel && panel.contentWindow) {
      panel.contentWindow.postMessage({ type: "winefox-ui-update-state", state: event.payload }, "*");
    }
  });

  await listen("debug-mode-changed", (event) => {
    const panel = $("panel");
    if (panel && panel.contentWindow) {
      panel.contentWindow.postMessage({ type: "winefox-ui-debug-state", enabled: !!event.payload?.enabled }, "*");
    }
  });

  await listen("room-status", (event) => {
    const panel = $("panel");
    if (panel && panel.contentWindow) {
      panel.contentWindow.postMessage({ type: "winefox-ui-room-state", state: event.payload }, "*");
    }
  });

  await listen("sidecar-status", (event) => {
    current = event.payload;
    renderStatus();
  });
  await listen("exit-requested", () => beginExit());
  await listen("focus-requested", () => refreshStatus());
  await refreshStatus();
}

document.addEventListener("DOMContentLoaded", boot);

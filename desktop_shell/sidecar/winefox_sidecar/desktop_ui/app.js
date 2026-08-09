/* WineFox desktop_ui bootstrap page logic (fake sidecar). */
"use strict";

const $ = (id) => document.getElementById(id);

async function refresh() {
  try {
    const r = await fetch("api/status", { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const st = await r.json();
    $("app-version").textContent = st.version || "—";
    $("impl").textContent = st.impl || "—";
    $("task-count").textContent = String(st.tasks ?? 0);
    $("current-task").textContent = st.task
      ? `#${st.task.id} ${st.task.name}（剩余 ${st.task.seconds}s）`
      : "无";
    const sh = st.shutdown || {};
    $("shutdown-state").textContent = sh.pending ? `是（${sh.mode}）` : "否";
    $("health-dot").className = "dot ok";
    $("health-text").textContent = "已连接";
    $("sim-start").disabled = false;
  } catch (e) {
    $("health-dot").className = "dot bad";
    $("health-text").textContent = "连接失败";
    $("sim-start").disabled = true;
  }
}

function boot() {
  const start = $("sim-start");
  if (start) {
    start.addEventListener("click", async () => {
      start.disabled = true;
      try {
        const secs = Math.max(1, Math.min(120, Number($("sim-seconds").value) || 8));
        await fetch("api/tasks/simulate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: "模拟任务-" + Date.now() % 1000, seconds: secs }),
        });
      } catch (e) { /* ignore */ }
      setTimeout(() => refresh(), 400);
    });
  }
  refresh();
  setInterval(refresh, 2000);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}

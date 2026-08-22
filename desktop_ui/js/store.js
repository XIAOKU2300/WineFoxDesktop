/* WineFox Desktop — 内存会话状态
 * 安全：token / 密钥绝不写入 localStorage（后端只返回配置状态）；
 * 仅主题偏好属于非敏感 UI 状态，允许持久化。
 */

const state = {
  health: null,
  settings: null,
  catalog: null,
  wizardCompleted: null,
  historyDirty: false, // 生成任务结束（完成/失败）后置位，历史页据此忽略预取缓存并刷新
  favoritesDirty: false, // 收藏/取消收藏后置位，收藏页据此忽略预取缓存并刷新
  theme: localStorage.getItem("wfx-theme") || "light",
  genParams: null, // 生成页草稿（内存）
  jobTally: { queued: 0, running: 0 },
};

const listeners = new Set();

export const store = {
  get: (k) => state[k],
  set(k, v) {
    state[k] = v;
    listeners.forEach((fn) => fn(k, v));
  },
  subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); },
  applyTheme() {
    const t = ["dark", "pink"].includes(state.theme) ? state.theme : "light";
    document.documentElement.dataset.theme = t;
    document.documentElement.style.colorScheme = t === "pink" ? "light" : t;
  },
  setTheme(name) {
    state.theme = ["dark", "pink"].includes(name) ? name : "light";
    localStorage.setItem("wfx-theme", state.theme);
    this.applyTheme();
  },
  toggleTheme() {
    const order = ["light", "dark", "pink"];
    this.setTheme(order[(order.indexOf(state.theme) + 1) % order.length]);
  },
  setJobTally(queued, running) {
    state.jobTally = { queued, running };
    listeners.forEach((fn) => fn("jobTally", state.jobTally));
  },
};

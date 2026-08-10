/* WineFox Desktop — API 客户端
 * 端点契约（Agent 07 后端实现，见 DESIGN_SYSTEM.md）：
 *   GET  /api/v1/health | /settings | /secrets | /comfyui | /catalog | /favorites | /models
 *   GET  /api/v1/history | /jobs/{id} | /categories | /diagnostics
 *   POST /api/v1/probe | /uploads | /jobs | /jobs/{id}/cancel | /favorites
 *   PUT  /api/v1/settings | /favorites/{id} | /secrets
 *   DELETE /api/v1/secrets/{key} | /favorites/{id} | /history/{id}
 *   POST /history/{id}/favorite|unfavorite|trash|restore|purge
 * 轮询：running 1s / queued 2s / 失败指数退避 1,2,4,8 → 封顶 10s。
 * 安全：密钥只传写操作，GET /secrets 只返回配置状态；token 仅存内存。
 */

export const API_BASE = "/api/v1";

export class ApiError extends Error {
  constructor(status, message, data) {
    super(message || `请求失败 (${status})`);
    this.status = status;
    this.data = data;
  }
}

async function request(path, opts = {}) {
  const { method = "GET", body, headers = {}, query } = opts;
  let url = API_BASE + path;
  if (query) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) {
      if (v != null && v !== "") qs.set(k, v);
    }
    const s = qs.toString();
    if (s) url += "?" + s;
  }
  const init = { method, credentials: "same-origin", headers: { ...headers } };
  if (!["GET", "HEAD"].includes(method.toUpperCase())) {
    init.headers["X-WineFox-Client"] = "1";
  }
  if (body !== undefined) {
    init.body = body;
    if (!(body instanceof FormData)) init.headers["Content-Type"] = "application/json";
  }
  let res;
  try {
    res = await fetch(url, init);
  } catch (e) {
    throw new ApiError(0, "无法连接后端服务（网络不可用或服务未启动）", { offline: true });
  }
  let data = null;
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("json")) {
    try { data = await res.json(); } catch { data = null; }
  }
  if (!res.ok) {
    const msg = data?.error?.message || data?.message || res.statusText || `HTTP ${res.status}`;
    throw new ApiError(res.status, msg, data);
  }
  if (data && typeof data === "object" && data.ok === true && Object.hasOwn(data, "data")) {
    return data.data;
  }
  return data;
}

export const api = {
  get: (path, query) => request(path, { query }),
  post: (path, body) => request(path, { method: "POST", body: body instanceof FormData ? body : JSON.stringify(body) }),
  put: (path, body) => request(path, { method: "PUT", body: JSON.stringify(body) }),
  del: (path) => request(path, { method: "DELETE" }),

  /* ---- 业务端点 ---- */
  health: () => request("/health"),
  settings: () => request("/settings"),
  saveSettings: (patch) => request("/settings", { method: "PATCH", body: JSON.stringify(patch) }),
  secrets: () => request("/secrets"),
  setSecret: (key, value) => request(`/secrets/${encodeURIComponent(key)}`, { method: "PUT", body: JSON.stringify({ value }) }),
  clearSecret: (key) => request(`/secrets/${encodeURIComponent(key)}`, { method: "DELETE" }),
  comfyui: () => request("/comfyui"),
  probe: () => request("/comfyui/probe", { method: "POST" }),
  catalog: () => request("/catalog"),
  models: () => request("/models"),
  uploads: async (formData) => {
    const result = await request("/uploads", { method: "POST", body: formData });
    return {
      ...result,
      name: result.name || result.filename,
      url: `/api/v1/uploads/${encodeURIComponent(result.uploadId)}`,
    };
  },
  submitJob: (payload) => request("/jobs", { method: "POST", body: JSON.stringify(payload) }),
  job: (id) => request(`/jobs/${encodeURIComponent(id)}`),
  cancelJob: (id) => request(`/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
  history: (query) => request("/history", { query }),
  favoriteHistory: (id) => request(`/history/${encodeURIComponent(id)}/favorite`, { method: "POST" }),
  unfavoriteHistory: (id) => request(`/history/${encodeURIComponent(id)}/unfavorite`, { method: "POST" }),
  trashHistory: (id) => request(`/history/${encodeURIComponent(id)}/trash`, { method: "POST" }),
  restoreHistory: (id) => request(`/history/${encodeURIComponent(id)}/restore`, { method: "POST" }),
  purgeHistory: (id) => request(`/history/${encodeURIComponent(id)}/purge`, { method: "POST" }),
  categories: () => request("/categories"),
  favorites: (query) => request("/favorites", { query }),
  createFavorite: (body) => request("/favorites", { method: "POST", body: JSON.stringify(body) }),
  updateFavorite: (id, body) => request(`/favorites/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteFavorite: (id) => request(`/favorites/${encodeURIComponent(id)}`, { method: "DELETE" }),
  diagnostics: () => request("/diagnostics"),
  exportDiagnostics: () => request("/diagnostics/export", { method: "POST" }),
};

/* ================= 任务轮询 =================
 * running → 1s；queued → 2s；错误 → 1,2,4,8 → 封顶 10s；completed/failed/cancelled 停止。
 */
export class JobPoller {
  constructor(onUpdate, onDone, onError) {
    this.onUpdate = onUpdate;
    this.onDone = onDone;
    this.onError = onError;
    this.timer = null;
    this.backoff = 1;
    this.active = new Set();
  }

  _schedule(fn, ms) {
    this.timer = setTimeout(() => { this.timer = null; fn(); }, ms);
  }

  watch(jobId) {
    this.active.add(jobId);
    if (!this.timer) this._poll(jobId, 200);
  }

  _intervalFor(status) {
    if (status === "running") return 1000;
    if (status === "queued") return 2000;
    return 1000;
  }

  async _poll(jobId) {
    if (!this.active.has(jobId)) return;
    let delay = this._intervalFor("queued");
    try {
      const job = await api.job(jobId);
      this.backoff = 1;
      delay = this._intervalFor(job.status);
      this.onUpdate?.(job);
      if (["completed", "failed", "cancelled", "detached"].includes(job.status)) {
        this.active.delete(jobId);
        this.onDone?.(job);
        if (!this.active.size) return;
      }
    } catch (e) {
      delay = Math.min(this.backoff * 1000, 10000);
      this.backoff = Math.min(this.backoff * 2, 10);
      this.onError?.(e, jobId);
      if (this.active.size === 0) return;
    }
    if (this.active.has(jobId)) this._schedule(() => this._poll(jobId), delay);
  }

  stop(jobId) {
    this.active.delete(jobId);
  }

  dispose() {
    this.active.clear();
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }
}

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

export function loraPreviewUrl(token) {
  const value = String(token || "").trim();
  if (globalThis.__WINEFOX_DEV_MOCK__ === true && value.startsWith("data:image/")) return value;
  return `${API_BASE}/lora/preview?token=${encodeURIComponent(value)}`;
}

export class ApiError extends Error {
  constructor(status, message, data) {
    super(message || `请求失败 (${status})`);
    this.status = status;
    this.data = data;
  }
}

async function request(path, opts = {}) {
  const { method = "GET", body, headers = {}, query, signal } = opts;
  let url = API_BASE + path;
  if (query) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) {
      if (v == null || v === "") continue;
      if (Array.isArray(v)) {
        for (const item of v) {
          if (item != null && item !== "") qs.append(k, item);
        }
      } else {
        qs.set(k, v);
      }
    }
    const s = qs.toString();
    if (s) url += "?" + s;
  }
  const init = { method, credentials: "same-origin", headers: { ...headers }, signal };
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
    if (e?.name === "AbortError") throw e;
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
  health: (options = {}) => request("/health", options),
  settings: (options = {}) => request("/settings", options),
  saveSettings: (patch, options = {}) => request("/settings", { ...options, method: "PATCH", body: JSON.stringify(patch) }),
  secrets: (options = {}) => request("/secrets", options),
  setSecret: (key, value, options = {}) => request(`/secrets/${encodeURIComponent(key)}`, { ...options, method: "PUT", body: JSON.stringify({ value }) }),
  clearSecret: (key, options = {}) => request(`/secrets/${encodeURIComponent(key)}`, { ...options, method: "DELETE" }),
  comfyui: (options = {}) => request("/comfyui", options),
  probe: (payload = {}, options = {}) => request("/comfyui/probe", { ...options, method: "POST", body: JSON.stringify(payload) }),
  catalog: (options = {}) => request("/catalog", options),
  models: (options = {}) => request("/models", options),
  searchLoras: (query, family = "illustrious", options = {}) => request("/lora/search", {
    ...options,
    query: {
      q: query,
      family,
      limit: Math.max(1, Math.min(Number(options.limit) || 20, 20)),
      page: Math.max(1, Math.min(Number(options.page) || 1, 20)),
      source: options.sources || [],
      adult_consent: options.adult_consent ? "1" : undefined,
    },
  }),
  startLoraDownload: (resultId, alias = "", options = {}) => request("/lora/downloads", { ...options, method: "POST", body: JSON.stringify({ result_id: resultId, alias, adult_consent: !!options.adult_consent }) }),
  loraDownload: (id, options = {}) => request(`/lora/downloads/${encodeURIComponent(id)}`, options),
  probeLoraBridge: (options = {}) => request("/lora/bridge/probe", { ...options, method: "POST", body: JSON.stringify({}) }),
  testLoraConnection: (options = {}) => request("/lora/test-connection", { ...options, method: "POST", body: JSON.stringify({}) }),
  /* ---- LoRA 本地库（Agent 07 后端，见 docs/lora_library_audit.md） ---- */
  loraLibraryList: (family, options = {}) => request("/lora/library", { ...options, query: { family, include_quarantined: options.include_quarantined ? "1" : undefined } }),
  loraLibraryGet: (id, options = {}) => request(`/lora/library/${encodeURIComponent(id)}`, options),
  loraLibraryUpdate: (id, patch, options = {}) => request(`/lora/library/${encodeURIComponent(id)}`, { ...options, method: "PATCH", body: JSON.stringify(patch) }),
  loraLibraryQuarantine: (id, reason, options = {}) => request(`/lora/library/${encodeURIComponent(id)}/quarantine`, { ...options, method: "POST", body: JSON.stringify({ reason }) }),
  loraLibraryRestore: (id, options = {}) => request(`/lora/library/${encodeURIComponent(id)}/restore`, { ...options, method: "POST" }),
  loraLibraryMatchPreview: (prompt, family, limit, options = {}) => request("/lora/library/match-preview", { ...options, method: "POST", body: JSON.stringify({ prompt, family, limit }) }),
  loraMatchPreview: ({ prompt, family, limit = 10 } = {}) => request("/lora/library/match-preview", { method: "POST", body: JSON.stringify({ prompt, family, limit }) }),
  loraScanLocal: (options = {}) => request("/lora/library/scan-local", { ...options, method: "POST", body: JSON.stringify({}) }),
  loraProfilesImportJson: (options = {}) => request("/lora/profiles/import-json", { ...options, method: "POST", body: JSON.stringify({}) }),
  loraProfilesExportJson: (options = {}) => request("/lora/profiles/export-json", options),

  /* ---- 环境中心（ComfyUI setup，冻结接口，见 docs/desktop_setup_migration.md） ---- */
  setupSummary: (options = {}) => request("/setup/summary", options),
  setupCapabilities: (options = {}) => request("/setup/capabilities", options),
  setupScan: (payload = {}, options = {}) => request("/setup/scans", { ...options, method: "POST", body: JSON.stringify(payload) }),
  setupPlan: (payload = {}, options = {}) => request("/setup/plans", { ...options, method: "POST", body: JSON.stringify(payload) }),
  setupStartJob: (payload, options = {}) => request("/setup/jobs", { ...options, method: "POST", body: JSON.stringify(payload) }),
  setupListJobs: (options = {}) => request("/setup/jobs", options),
  setupJob: (id, options = {}) => request(`/setup/jobs/${encodeURIComponent(id)}`, options),
  setupCancelJob: (id, options = {}) => request(`/setup/jobs/${encodeURIComponent(id)}/cancel`, { ...options, method: "POST" }),
  setupRetryJob: (id, options = {}) => request(`/setup/jobs/${encodeURIComponent(id)}/retry`, { ...options, method: "POST" }),
  setupRollbackJob: (id, options = {}) => request(`/setup/jobs/${encodeURIComponent(id)}/rollback`, { ...options, method: "POST" }),
  setupReprobe: (options = {}) => request("/setup/reprobe", { ...options, method: "POST" }),
  setupRemoteKit: (options = {}) => request("/setup/remote-kit", options),
  setupJobEventsUrl: (id) => `/api/v1/setup/jobs/${encodeURIComponent(id)}/events`,
  setupMirrorProbe: (options = {}) => request("/setup/mirrors/probe", { ...options, method: "POST", body: JSON.stringify({}) }),
  setupMirrorPreference: (options = {}) => request("/setup/mirrors/preference", options),
  setupSetMirrorPreference: (host, options = {}) => request("/setup/mirrors/preference", { ...options, method: "PUT", body: JSON.stringify({ preferred_host: host || "" }) }),
  uploads: async (formData) => {
    const result = await request("/uploads", { method: "POST", body: formData });
    return {
      ...result,
      name: result.name || result.filename,
      url: `/api/v1/uploads/${encodeURIComponent(result.uploadId)}`,
    };
  },
  submitJob: (payload) => request("/jobs", { method: "POST", body: JSON.stringify(payload) }),
  aiListModels: (payload = {}, options = {}) => request("/ai/models", { ...options, method: "POST", body: JSON.stringify(payload) }),
  aiTestModel: (payload = {}, options = {}) => request("/ai/test", { ...options, method: "POST", body: JSON.stringify(payload) }),
  job: (id) => request(`/jobs/${encodeURIComponent(id)}`),
  cancelJob: (id) => request(`/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
  history: (query, options = {}) => request("/history", { ...options, query }),
  favoriteHistory: (id) => request(`/history/${encodeURIComponent(id)}/favorite`, { method: "POST" }),
  unfavoriteHistory: (id) => request(`/history/${encodeURIComponent(id)}/unfavorite`, { method: "POST" }),
  trashHistory: (id) => request(`/history/${encodeURIComponent(id)}/trash`, { method: "POST" }),
  restoreHistory: (id) => request(`/history/${encodeURIComponent(id)}/restore`, { method: "POST" }),
  purgeHistory: (id) => request(`/history/${encodeURIComponent(id)}/purge`, { method: "POST" }),
  categories: (options = {}) => request("/categories", options),
  favorites: (query, options = {}) => request("/favorites", { ...options, query }),
  createFavorite: (body) => request("/favorites", { method: "POST", body: JSON.stringify(body) }),
  updateFavorite: (id, body) => request(`/favorites/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteFavorite: (id) => request(`/favorites/${encodeURIComponent(id)}`, { method: "DELETE" }),
  diagnostics: (options = {}) => request("/diagnostics", options),
  exportDiagnostics: (options = {}) => request("/diagnostics/export", { ...options, method: "POST" }),
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
    // 不能依赖 this.timer 判断：取消任务后残留的退避定时器会让新任务
    // 永远等不到轮询链。每个任务独立起链，_poll 内部有终态自清理。
    this._poll(jobId, 200);
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
        // delete 返回 false 说明另一条链已处理过终态，避免重复 onDone
        if (this.active.delete(jobId)) this.onDone?.(job);
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

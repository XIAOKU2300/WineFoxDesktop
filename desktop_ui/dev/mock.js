/* WineFox Desktop — Dev Mock（仅开发用）
 * 仅在 URL 带 mock=1 或 dev=1 时由 app.js 动态导入；生产默认不启用。
 * 通过替换 fetch 拦截 /api/v1/*，模拟：success / queued / fail / offline / history 场景。
 * 图片全部为内联 SVG data URI（无任何外部请求）。
 */

const SCENARIOS = new Set(["success", "queued", "fail", "offline", "history", "wizard"]);

function svgImage(seed, label, w = 832, h = 1216) {
  const palette = [
    ["#F6E9E3", "#A45C73"], ["#EFE6DA", "#8A6E4B"], ["#E8E4EF", "#6E5B8A"],
    ["#E4EEE8", "#3D7A63"], ["#F2E8D8", "#B07A3C"], ["#E9E2EE", "#7C5E87"],
  ];
  const [bg, fg] = palette[Math.abs(seed) % palette.length];
  const r1 = 18 + (Math.abs(seed) % 26), r2 = 10 + (Math.abs(seed) % 14);
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">
  <rect width="${w}" height="${h}" fill="${bg}"/>
  <circle cx="${w - r1 * 3}" cy="${r1 * 2}" r="${r1}" fill="none" stroke="${fg}" stroke-width="2"/>
  <path d="M${w * 0.3} ${h * 0.42} c ${w * 0.1} ${-h * 0.08} ${w * 0.2} ${-h * 0.08} ${w * 0.3} 0 c ${w * 0.1} ${-h * 0.08} ${w * 0.2} ${-h * 0.08} ${w * 0.3} 0"
        fill="none" stroke="${fg}" stroke-width="2.5" stroke-linecap="round"/>
  <path d="M${w * 0.32} ${h * 0.5} h ${w * 0.36}" stroke="${fg}" stroke-width="1.2" stroke-linecap="round"/>
  <circle cx="${w * 0.5}" cy="${h * 0.56}" r="${Math.min(w, h) * 0.12}" fill="none" stroke="${fg}" stroke-width="1.4"/>
  <text x="${w * 0.5}" y="${h * 0.72}" text-anchor="middle" font-family="Segoe UI, sans-serif" font-size="${Math.min(w, h) * 0.045}" fill="${fg}">${label} · ${Math.abs(seed)}</text>
</svg>`;
  return "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
}

const FAKE_WORKFLOWS = [
  { id: "finish_illustrious_api", label: "Illustrious 标准", mode: "txt2img", family: "illustrious" },
  { id: "finish_illustrious_multi_lora_api", label: "Illustrious 多 LoRA", mode: "txt2img", family: "illustrious" },
  { id: "finish_optimized_api", label: "Anima 优化", mode: "txt2img", family: "anima" },
  { id: "img2img_illustrious_api", label: "Illustrious 图生图", mode: "img2img", family: "illustrious" },
  { id: "img2img_illustrious_multi_lora_staged_api", label: "Illustrious 分段图生图", mode: "img2img", family: "illustrious" },
  { id: "wan_i2v_api", label: "Wan 图生视频", mode: "i2v", family: "anima" },
];

const FAKE_LORAS = [
  { id: "lora_1", label: "角色：星野", family: "illustrious", tags: ["角色"], strength: 0.8 },
  { id: "lora_2", label: "画风：水墨", family: "illustrious", tags: ["画风"], strength: 0.7 },
  { id: "lora_3", label: "角色：莲", family: "illustrious", tags: ["角色"], strength: 0.85 },
  { id: "lora_4", label: "画风：厚涂", family: "illustrious", tags: ["画风"], strength: 0.6 },
  { id: "lora_5", label: "Anima 风格", family: "anima", tags: ["画风"], strength: 0.8 },
];

const FAKE_CHECKPOINTS = [
  { id: "ckpt_1", name: "Illustrious-XL-v2.0.safetensors", family: "illustrious", size: 6925 * 1024 * 1024, compat: "ready" },
  { id: "ckpt_2", name: "perfectdeliberate_v90.safetensors", family: "illustrious", size: 6938 * 1024 * 1024, compat: "ready" },
  { id: "ckpt_3", name: "anima_mix_v3.safetensors", family: "anima", size: 3421 * 1024 * 1024, compat: "ready" },
  { id: "ckpt_4", name: "nova3DCGXL_ilV90.safetensors", family: "illustrious", size: 6890 * 1024 * 1024, compat: "incompatible", warning: "与当前家族配置不匹配（检测到却未在家族配置中登记）" },
  { id: "ckpt_5", name: "dreamlike_anime_2.safetensors", family: "anima", size: 2048 * 1024 * 1024, compat: "missing", missing_reason: "模型文件在磁盘上缺失，请重新放置后刷新" },
];

const FAKE_NODES = [
  { name: "ComfyUI-AstrBot-LoraBridge", installed: true, version: "1.2.0" },
  { name: "comfyui_controlnet_aux", installed: true, version: "1.0.0" },
  { name: "ComfyUI-WanVideoWrapper", installed: false, install_hint: "通过 ComfyUI Manager 安装：ComfyUI-WanVideoWrapper" },
];

const MOCK_PROMPTS = [
  "傍晚的日式庭院，一只有着橙色皮毛的狐狸趴在缘侧，纸灯发出暖光，空气中有淡淡的水汽",
  "水墨风格的山间神社，晨雾缭绕，朱红鸟居若隐若现，远处是层叠的远山",
  "雨后的街道，少女撑着透明雨伞走过便利店，霓虹灯倒映在积水中",
  "冬日窗边，白猫蜷缩在暖炉旁，窗外飘着细雪，一杯热茶冒着白气",
  "夏日祭典，金鱼捞、苹果糖，浴衣少女仰望夜空中的烟火",
];

let seq = 1;
const jobs = new Map();
const history = [];
const favorites = [];
let historyCursorSeq = 0;

function makeJob(payload) {
  const id = "job_" + String(seq++);
  const seed = payload.seed != null && payload.seed >= 0 ? payload.seed : Math.floor(Math.random() * 1e9);
  const job = {
    id,
    status: "queued",
    progress: 0,
    seed,
    mode: payload.mode || "txt2img",
    prompt: payload.prompt || "",
    negative_prompt: payload.negative_prompt,
    width: payload.width, height: payload.height,
    steps: payload.steps, cfg: payload.cfg,
    sampler: payload.sampler, scheduler: payload.scheduler,
    family: payload.family, workflow: payload.workflow,
    created_at: new Date().toISOString(),
    queue_position: jobs.size + 1,
    error: null,
    image: null,
  };
  jobs.set(id, job);
  return job;
}

function completeJob(job, scenario) {
  job.progress = 1;
  job.status = scenario === "fail" ? "failed" : "completed";
  job.error = scenario === "fail" ? "模拟失败：采样器在 40% 处报错（mock scenario=fail）" : null;
  if (job.status === "completed") {
    job.image = { url: svgImage(job.seed, job.mode === "img2img" ? "img2img" : "txt2img", job.width || 832, job.height || 1216) };
  }
  history.unshift({
    id: job.id,
    prompt: job.prompt,
    negative_prompt: job.negative_prompt,
    seed: job.seed,
    width: job.width, height: job.height,
    steps: job.steps, cfg: job.cfg,
    family: job.family, workflow: job.workflow,
    status: job.status,
    favorite: false,
    trashed: false,
    image: job.image,
    created_at: job.created_at,
  });
}

let seeded = false;
function seedHistory() {
  if (seeded) return;
  seeded = true;
  const now = Date.now();
  for (let i = 0; i < 26; i++) {
    const seed = 1000 + i * 137;
    const prompt = MOCK_PROMPTS[i % MOCK_PROMPTS.length];
    const item = {
      id: "hist_" + String(++historyCursorSeq).padStart(4, "0"),
      prompt,
      negative_prompt: i % 3 === 0 ? "低画质, 模糊, 变形" : null,
      seed,
      width: 832, height: 1216,
      steps: 24, cfg: 7,
      family: i % 2 ? "illustrious" : "anima",
      workflow: FAKE_WORKFLOWS[i % FAKE_WORKFLOWS.length].id,
      status: "completed",
      favorite: i % 4 === 0,
      trashed: false,
      image: { url: svgImage(seed, "hist", 832, 1216) },
      created_at: new Date(now - i * 3600 * 1000 * 7).toISOString(),
    };
    history.push(item);
    if (item.favorite) {
      favorites.push({ id: "fav_" + item.id, history_id: item.id, note: ["构图参考", "配色灵感", "待复刻", "光影练习"][i % 4], category: ["参考", "灵感", "角色", "场景"][i % 4], prompt: item.prompt, image: item.image, created_at: item.created_at });
    }
  }
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function installMock({ scenario = "success" } = {}) {
  const sc = SCENARIOS.has(scenario) ? scenario : "success";
  const offline = sc === "offline";

  const base = {
    health: () => json(offline ? { status: "offline" } : {
      status: sc === "fail" ? "degraded" : "ok",
      app: { name: "WineFox Desktop", version: "1.0.0-mock" },
      plugin: { name: "astrbot_plugin_comfyui_pro_UltraPlusMax", version: "8.5.9" },
      comfyui: sc === "fail" ? { connected: false, version: null } : { connected: true, version: "0.3.25" },
      first_run: sc === "wizard",
      time: new Date().toISOString(),
    }),
    settings: () => json({
      wizard: { completed: true },
      comfyui: { server_address: "http://127.0.0.1:8188", ssl_verify: true, connect_timeout: 30, read_timeout: 300 },
      tls: { enabled: false, verify: true, ca_configured: false },
      auth: { mode: "bearer" },
      defaults: { family: "illustrious", workflow: "finish_illustrious_api", steps: 24, cfg: 7, sampler: "euler_ancestral", width: 832, height: 1216 },
      ui: { reduced_motion: true },
    }),
    secrets: () => json({ items: [
      { key: "comfyui_api_key", configured: true, updated_at: "2025-08-01 10:00" },
      { key: "comfyui_client_cert", configured: false },
      { key: "tls_ca_cert", configured: false },
    ] }),
    comfyui: () => json(sc === "fail" ? { connected: false } : { connected: true, version: "0.3.25", devices: [{ type: "cuda", name: "mock" }], nodes_total: 320, custom_nodes: FAKE_NODES, missing: FAKE_NODES.filter((n) => !n.installed) }),
    probe: () => json(sc === "fail" ? {
      result: "warn",
      summary: "版本过旧（mock）",
      steps: [
        { id: "connect", label: "连接 ComfyUI 服务", status: "ok", detail: "已连接 127.0.0.1:8188" },
        { id: "info", label: "读取对象信息 / 版本", status: "ok", detail: "v0.3.25" },
        { id: "nodes", label: "检查自定义节点", status: "warn", detail: "1 个节点缺失：ComfyUI-WanVideoWrapper" },
        { id: "summary", label: "汇总", status: "warn", detail: "缺少 Wan 视频节点" },
      ],
    } : {
      result: "ok",
      summary: "一切正常",
      steps: [
        { id: "connect", label: "连接 ComfyUI 服务", status: "ok", detail: "已连接 127.0.0.1:8188" },
        { id: "info", label: "读取对象信息 / 版本", status: "ok", detail: "v0.3.25" },
        { id: "nodes", label: "检查自定义节点", status: "ok", detail: "全部就绪" },
        { id: "summary", label: "汇总", status: "ok", detail: "模型与节点可用" },
      ],
    }),
    catalog: () => json({
      families: [
        { id: "anima", label: "Anima", description: "经典 Anima 系列，通用二次元插画" },
        { id: "illustrious", label: "Illustrious", description: "Illustrious-XL 系列，多 LoRA 与精细控制" },
      ],
      checkpoints: FAKE_CHECKPOINTS,
      loras: FAKE_LORAS,
      workflows: FAKE_WORKFLOWS,
      nodes: FAKE_NODES,
    }),
    models: () => json({
      checkpoints: FAKE_CHECKPOINTS,
      loras: FAKE_LORAS,
      nodes: FAKE_NODES,
      missing_nodes: FAKE_NODES.filter((n) => !n.installed),
      download: { enabled: false, reason: "v1 不支持在线下载" },
    }),
    diagnostics: () => json({
      app: { name: "WineFox Desktop", version: "1.0.0-mock", build: "dev-mock" },
      plugin: { name: "astrbot_plugin_comfyui_pro_UltraPlusMax", version: "8.5.9" },
      comfyui: sc === "fail" ? { connected: false, version: null, url: "http://127.0.0.1:8188" } : { connected: true, version: "0.3.25", url: "http://127.0.0.1:8188" },
      nodes: { total: 320, missing: FAKE_NODES.filter((n) => !n.installed).map((n) => n.name) },
      config_health: [
        { id: "server", label: "服务地址", status: "ok", detail: "已配置" },
        { id: "api_key", label: "API Key", status: "ok", detail: "已配置" },
        { id: "wan_nodes", label: "Wan 视频节点", status: sc === "fail" ? "warn" : "ok", detail: sc === "fail" ? "ComfyUI-WanVideoWrapper 未安装" : "就绪" },
      ],
      log_tail: {
        truncated: true,
        lines: [
          "[info] WineFox Desktop 启动，插件版本 8.5.9",
          "[info] 连接 ComfyUI 成功（127.0.0.1:8188）",
          sc === "fail" ? "[warn] 自定义节点缺失：ComfyUI-WanVideoWrapper" : "[info] 自定义节点全部就绪",
          "[info] 生成任务完成：seed=123456 · 24 步 · 2.3s",
          sc === "fail" ? "[error] 采样失败：Sampling error (mock)" : "[info] 队列空闲",
        ],
      },
    }),
  };

  // 动态路由
  async function route(path, opts) {
    const method = (opts?.method || "GET").toUpperCase();
    const u = new URL(path, location.origin);

    const wait = (ms) => new Promise((r) => setTimeout(r, ms));

    if (u.pathname === "/api/v1/health") return await wait(150), base.health();
    if (u.pathname === "/api/v1/settings" && method === "GET") return await wait(100), base.settings();
    if (u.pathname === "/api/v1/settings" && method === "PUT") return await wait(120), base.settings();
    if (u.pathname === "/api/v1/secrets" && method === "GET") return await wait(100), base.secrets();
    if (u.pathname.startsWith("/api/v1/secrets/")) {
      const key = decodeURIComponent(u.pathname.split("/").pop());
      if (method === "DELETE") return json({ ok: true, key, configured: false });
      if (method === "PUT") return json({ ok: true, key, configured: true });
    }
    if (u.pathname === "/api/v1/comfyui") return await wait(100), base.comfyui();
    if (u.pathname === "/api/v1/probe") return await wait(600), base.probe();
    if (u.pathname === "/api/v1/catalog") return await wait(150), base.catalog();
    if (u.pathname === "/api/v1/models") return await wait(150), base.models();
    if (u.pathname === "/api/v1/diagnostics/export") return json({ format: "json", content: JSON.stringify({ app: base.diagnostics(), note: "脱敏导出（mock）" }, null, 2) });
    if (u.pathname === "/api/v1/diagnostics") return await wait(150), base.diagnostics();

    if (u.pathname === "/api/v1/uploads" && method === "POST") {
      const file = opts.body?.get?.("file");
      const name = file?.name || "ref.png";
      return json({ url: svgImage(7, "ref", 512, 768), name, width: 512, height: 768 });
    }

    if (u.pathname === "/api/v1/jobs" && method === "POST") {
      const job = makeJob(JSON.parse(opts.body || "{}"));
      // 模拟进度推进
      const advance = () => {
        const j = jobs.get(job.id);
        if (!j) return;
        if (j.status === "queued") {
          j.status = "running";
          j.progress = 0.12;
          setTimeout(advance, sc === "queued" ? 5200 : sc === "fail" ? 500 : 800);
        } else if (j.status === "running") {
          j.progress = Math.min(1, j.progress + (sc === "queued" ? 0.22 : 0.3));
          if (j.progress >= 1) { completeJob(j, sc); }
          else setTimeout(advance, sc === "fail" && j.progress > 0.45 ? 250 : 800);
        }
      };
      setTimeout(advance, sc === "queued" ? 5000 : 300);
      return json(job, 202);
    }
    const jobMatch = u.pathname.match(/^\/api\/v1\/jobs\/([^/]+)(?:\/(cancel))?$/);
    if (jobMatch) {
      const [, id, cancel] = jobMatch;
      const job = jobs.get(id) || history.find((h) => h.id === id);
      if (!job) return json({ error: "任务不存在" }, 404);
      if (cancel) { job.status = "cancelled"; return json({ ok: true, status: "cancelled" }); }
      return json(job);
    }

    if (u.pathname === "/api/v1/history" && method === "GET") {
      seedHistory();
      const limit = parseInt(u.searchParams.get("limit") || "24", 10);
      const cursor = parseInt(u.searchParams.get("cursor") || "0", 10);
      const q = (u.searchParams.get("q") || "").toLowerCase();
      const status = u.searchParams.get("status");
      const trash = u.searchParams.get("trash") === "1";
      let items = history.filter((h) => h.trashed === trash);
      if (q) items = items.filter((h) => (h.prompt || "").toLowerCase().includes(q) || String(h.seed).includes(q));
      if (status === "favorite") items = items.filter((h) => h.favorite);
      else if (status) items = items.filter((h) => h.status === status);
      const total = items.length;
      const page = items.slice(cursor, cursor + limit);
      return json({ items: page, total, next_cursor: cursor + limit < total ? cursor + limit : null, prev_cursor: cursor > 0 ? Math.max(0, cursor - limit) : null });
    }
    const histAct = u.pathname.match(/^\/api\/v1\/history\/([^/]+)\/(favorite|unfavorite|trash|restore|purge)$/);
    if (histAct) {
      const [, id, act] = histAct;
      const it = history.find((h) => h.id === id);
      if (!it) return json({ error: "记录不存在" }, 404);
      if (act === "favorite") it.favorite = true;
      if (act === "unfavorite") it.favorite = false;
      if (act === "trash") it.trashed = true;
      if (act === "restore") it.trashed = false;
      if (act === "purge") history.splice(history.indexOf(it), 1);
      return json({ ok: true });
    }

    if (u.pathname === "/api/v1/categories") {
      seedHistory();
      const counts = {};
      favorites.forEach((f) => { counts[f.category] = (counts[f.category] || 0) + 1; });
      return json({ items: Object.entries(counts).map(([id, count]) => ({ id, label: id, count })) });
    }
    if (u.pathname === "/api/v1/favorites" && method === "GET") {
      seedHistory();
      const q = (u.searchParams.get("q") || "").toLowerCase();
      const cat = u.searchParams.get("category");
      let items = favorites.filter((f) => !cat || f.category === cat);
      if (q) items = items.filter((f) => (f.note || "").toLowerCase().includes(q) || (f.prompt || "").toLowerCase().includes(q));
      return json({ items, total: items.length });
    }
    if (u.pathname === "/api/v1/favorites" && method === "POST") {
      const body = JSON.parse(opts.body || "{}");
      const fav = { id: "fav_" + String(++seq), note: body.note || "", category: body.category || "未分类", prompt: body.prompt || "", image: { url: svgImage(seq, "fav") }, created_at: new Date().toISOString() };
      favorites.unshift(fav);
      return json(fav, 201);
    }
    const favMatch = u.pathname.match(/^\/api\/v1\/favorites\/([^/]+)$/);
    if (favMatch) {
      const id = favMatch[1];
      const idx = favorites.findIndex((f) => f.id === id);
      if (idx < 0) return json({ error: "收藏不存在" }, 404);
      if (method === "DELETE") { favorites.splice(idx, 1); return json({ ok: true }); }
      if (method === "PUT") {
        const body = JSON.parse(opts.body || "{}");
        favorites[idx] = { ...favorites[idx], ...body };
        return json(favorites[idx]);
      }
    }

    return json({ error: `mock 未实现：${method} ${u.pathname}` }, 404);
  }

  const originalFetch = window.fetch;
  window.fetch = async (input, init) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input?.url || "";
    if (url.includes("/api/v1/")) {
      if (offline) {
        await new Promise((r) => setTimeout(r, 120));
        throw new TypeError("Failed to fetch (mock offline)");
      }
      const urlObj = typeof input === "string" ? new URL(input, location.origin) : new URL(input.url, location.origin);
      const pathname = urlObj.pathname;
      // history/favorites 查询串保留
      const opts = init || {};
      opts.body = opts.body;
      return route(pathname + urlObj.search, { ...opts, url: urlObj });
    }
    return originalFetch(input, init);
  };
}

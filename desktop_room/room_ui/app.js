/* WineFox 房间访客界面（自包含，无外部依赖）。
 *
 * 安全与传输约定：
 *   - 会话 Cookie 由服务端设置（Secure/HttpOnly/SameSite=Lax）。
 *     本地回环 http 无 TLS，浏览器不接收 Secure Cookie：join 响应会在非 https
 *     时额外返回 sessionToken，由本页脚本以非 Secure Cookie 落盘（仅本地回环）。
 *     公网 https（Tunnel）仍只走 Cookie，令牌不进页面内存。
 *   - 密码绝不写入 URL/query/localStorage，仅随 join 请求体提交一次。
 *   - 图片只显示附件授权 URL（/room/v1/attachments/*），绝不经 WS 传 Base64。
 *   - 任务幂等 ID：room:<房间公开ID>:member:<成员ID>:<uuid>。
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? "")
    .replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const state = {
    token: null,        // 仅本地 http 时由 join 响应取得
    member: null,       // { id, displayName, role }
    room: null,         // { id(public), name, state }
    settings: null,
    capabilities: [],
    catalog: null,      // families/workflows/loras/checkpoints
    members: [],        // [{ id, displayName, role }]
    messages: [],       // message_payload[]
    jobs: new Map(),    // jobId -> job
    job: null,          // 当前进行中的任务
    result: null,       // { job, url }
    refImage: null,     // { attachmentId, url }
    mode: "txt2img",
    ws: null,
    wsPing: null,
    booted: false,
  };

  /* ---------------- 小工具 ---------------- */
  function toast(text, kind = "info") {
    const el = document.createElement("div");
    el.className = "toast toast-" + kind;
    el.textContent = text;
    document.body.append(el);
    requestAnimationFrame(() => el.classList.add("toast-show"));
    setTimeout(() => {
      el.classList.remove("toast-show");
      setTimeout(() => el.remove(), 240);
    }, 2600);
  }

  function uuid() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const b = crypto.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    return [...b].map((x, i) => (i === 4 || i === 6 || i === 8 || i === 10 ? "-" : "") + x.toString(16).padStart(2, "0")).join("");
  }

  function clientRequestId() {
    if (!state.room || !state.member) return "";
    return `room:${state.room.id}:member:${state.member.id}:${uuid()}`;
  }

  function showScreen(name) {
    ["boot", "join", "workspace"].forEach((k) => {
      const el = $("screen-" + k);
      if (el) el.hidden = k !== name;
    });
    $("screen-closed").hidden = true;
  }

  async function api(path, opts = {}) {
    const init = {
      method: opts.method || (opts.json ? "POST" : "GET"),
      headers: {},
      credentials: "same-origin",
    };
    if (opts.json !== undefined) init.headers["Content-Type"] = "application/json";
    if (opts.body !== undefined) init.body = opts.body;
    if (opts.json !== undefined) init.body = JSON.stringify(opts.json);
    let resp;
    try {
      resp = await fetch(path, init);
    } catch {
      return { ok: false, status: 0, data: { errorCode: "NETWORK_ERROR", error: "网络请求失败" } };
    }
    let data = null;
    try { data = await resp.json(); } catch { /* 非 JSON */ }
    return { ok: resp.ok, status: resp.status, data: data || {} };
  }

  /* ---------------- 启动 ---------------- */
  async function boot() {
    showScreen("boot");
    const info = await api("/room/v1/info");
    if (!info.ok || info.data.room == null) {
      // 房间不存在/网关未就绪
      setClosed("无法连接房间网关。");
      return;
    }
    const room = info.data.room;
    if (room.state !== "online") {
      setClosed(room.state === "closing" ? "房间正在关闭…" : "房间当前不可用。");
      return;
    }
    state.room = { id: room.id, name: room.name, state: room.state };
    $("join-room-name").textContent = room.name || "WineFox 房间";
    $("join-room-owner").textContent = room.ownerName ? `由 ${room.ownerName} 创建` : "";

    // 已有会话（Secure Cookie 或本地 http Cookie）→ 直接进入工作台
    const sess = await api("/room/v1/session");
    if (sess.ok && sess.data.member && sess.data.room) {
      state.member = sess.data.member;
      state.room.id = sess.data.room.id || state.room.id;
      await enterWorkspace();
      return;
    }
    showScreen("join");
    $("join-name").focus();
  }

  /* ---------------- 加入 ---------------- */
  $("join-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = $("join-name").value.trim();
    const password = $("join-password").value;
    setJoinError("");
    const btn = $("join-submit");
    btn.disabled = true;
    try {
      const res = await api("/room/v1/join", { json: { name, password } });
      if (!res.ok) {
        setJoinError(joinErrorMessage(res.data.errorCode, res.data.error));
        return;
      }
      // 本地 http：服务端返回 sessionToken，脚本落盘为非 Secure Cookie（仅回环）。
      if (res.data.sessionToken) {
        document.cookie = "wf_room_session=" + res.data.sessionToken + "; path=/; SameSite=Lax; Max-Age=2592000";
      }
      state.member = res.data.member;
      await enterWorkspace();
    } finally {
      btn.disabled = false;
    }
  });

  function joinErrorMessage(code, fallback) {
    const map = {
      PASSWORD_REQUIRED: "请输入房间密码",
      WRONG_PASSWORD: "密码错误",
      INVALID_NAME: "昵称不合法，请检查后重试",
      NAME_TAKEN: "该昵称已被使用，请换一个",
      ROOM_FULL: "房间人数已满",
      JOIN_PAUSED: "房主已暂停新成员加入",
      RATE_LIMITED_IP: "尝试过于频繁，请稍后再试",
      RATE_LIMITED_ROOM: "尝试过于频繁，请稍后再试",
      ROOM_CLOSED: "房间已关闭",
    };
    return map[code] || fallback || "加入失败，请重试";
  }

  function setJoinError(text) {
    const el = $("join-error");
    el.textContent = text;
    el.hidden = !text;
  }

  /* ---------------- 工作台 ---------------- */
  async function enterWorkspace() {
    $("ws-room-name").textContent = state.room.name || "房间";
    $("ws-room-meta").textContent = state.member.role === "owner" ? `房主 · ${state.member.displayName}` : state.member.displayName;
    showScreen("workspace");
    if (!state.booted) {
      state.booted = true;
      wireWorkspace();
    }
    await Promise.all([loadCatalog(), connectWS()]);
    // 空目录时仍可聊天；生图目录失败不阻塞
    await loadHistory();
  }

  async function loadCatalog() {
    const res = await api("/room/v1/catalog");
    if (!res.ok) return;
    state.settings = res.data.settings || {};
    state.capabilities = res.data.capabilities || [];
    state.catalog = res.data;
    applyCapabilities();
    populateParams();
    updateMeta();
  }

  function applyCapabilities() {
    const caps = state.capabilities;
    const canImg = caps.includes("img2img");
    const canUpload = caps.includes("uploads");
    const canEnhance = caps.includes("promptEnhancement");
    document.querySelectorAll("#g-mode .mode-btn").forEach((b) => {
      if (b.dataset.mode === "img2img") b.hidden = !canImg;
    });
    if (!canImg && state.mode === "img2img") setMode("txt2img");
    $("chat-image").hidden = !canUpload;
    $("g-enhance").hidden = !canEnhance;
    const enabled = [];
    if (caps.includes("txt2img")) enabled.push("文生图");
    if (canImg) enabled.push("图生图");
    if (canEnhance) enabled.push("提示词增强");
    if (canUpload) enabled.push("图片上传");
    $("g-caps").textContent = enabled.join(" · ");
    $("g-mode-hint").textContent = canImg
      ? "图生图：先上传一张参考图，再生成。"
      : "";
  }

  function populateParams() {
    const cat = state.catalog || {};
    const families = (cat.families || []).map((f) => ({ id: f.id, label: f.label }));
    const allowFamilies = (state.settings?.allowedModelFamilies || []).filter(Boolean);
    const famOptions = allowFamilies.length
      ? families.filter((f) => allowFamilies.includes(f.id))
      : families;
    fillSelect("g-family", famOptions.map((f) => [f.id, f.label]), famOptions[0]?.id || (families[0]?.id || "anima"));

    fillSelect("g-w", [512, 768, 832, 1024, 1280].map((v) => [String(v), String(v)]), "832");
    fillSelect("g-h", [512, 768, 832, 1024, 1280].map((v) => [String(v), String(v)]), "1216");
    fillSelect("g-steps", [16, 20, 24, 28, 32, 40].map((v) => [String(v), String(v)]), "24");
    fillSelect("g-cfg", [3.5, 5, 6, 7, 8, 10, 12].map((v) => [String(v), String(v)]), "7");
    fillSelect("g-sampler",
      ["euler", "euler_ancestral", "dpmpp_2m", "dpmpp_2m_sde", "dpmpp_3m_sde", "uni_pc"].map((v) => [v, v]),
      "euler_ancestral");
    fillSelect("g-sched",
      ["normal", "karras", "exponential", "sgm_uniform", "simple"].map((v) => [v, v]),
      "normal");
    fillSelect("g-seed", [], "-1");
    refreshFamilyWorkflowLora();
  }

  function fillSelect(id, pairs, selected) {
    const sel = $(id);
    if (!sel) return;
    sel.innerHTML = "";
    pairs.forEach(([value, label]) => {
      const o = document.createElement("option");
      o.value = value;
      o.textContent = label;
      if (String(value) === String(selected)) o.selected = true;
      sel.append(o);
    });
  }

  function refreshFamilyWorkflowLora() {
    const cat = state.catalog || {};
    const family = $("g-family").value;
    const mode = state.mode;
    let workflows = (cat.workflows || []).filter((w) => !w.mode || w.mode === mode || w.mode === "any");
    const allowWf = (state.settings?.allowedWorkflowIds || []).filter(Boolean);
    if (allowWf.length) workflows = workflows.filter((w) => allowWf.includes(w.id));
    const familyWf = workflows.filter((w) => !w.family || w.family === "any" || w.family === family);
    fillSelect("g-workflow",
      familyWf.map((w) => [w.id, `[${w.family || "通用"}] ${w.label || w.name || w.id}`]),
      familyWf[0]?.id || workflows[0]?.id || "");
    if ($("g-workflow").options.length === 0) {
      fillSelect("g-workflow", [["", "默认（后端选择）"]], "");
    }

    const loras = (cat.loras || []).filter((l) => {
      const f = String(l.family || "any").toLowerCase();
      return f === "any" || !f || f === family;
    });
    fillSelect("g-lora",
      loras.map((l) => [l.id, l.label || l.name || l.id]),
      "");
    $("g-lora-field").hidden = loras.length === 0;
  }

  function updateMeta() {
    const m = state.members.length;
    $("ws-member-count").textContent = `${m} 人`;
    $("ws-room-meta").textContent =
      `${state.member.role === "owner" ? "房主 · " : ""}${state.member.displayName} · ${m} 人`;
    renderMembers();
  }

  function renderMembers() {
    const box = $("ws-members");
    box.innerHTML = "";
    state.members.forEach((m) => {
      const span = document.createElement("span");
      span.className = "member" + (m.role === "owner" ? " owner" : "") + (m.id === state.member.id ? " me" : "");
      span.textContent = m.displayName;
      span.title = m.role === "owner" ? "房主" : m.displayName;
      box.append(span);
    });
  }

  /* ---------------- 实时通道（WS；本地 http 也用 WS，Cookie 由脚本落盘） ---------------- */
  async function connectWS() {
    closeWS();
    const proto = location.protocol === "https:" ? "wss" : "ws";
    try {
      state.ws = new WebSocket(`${proto}://${location.host}/room/v1/ws`);
    } catch {
      scheduleReconnect();
      return;
    }
    state.ws.onopen = () => {
      state.wsPing = setInterval(() => {
        if (state.ws && state.ws.readyState === WebSocket.OPEN) {
          state.ws.send(JSON.stringify({ action: "ping" }));
        }
      }, 25000);
    };
    state.ws.onmessage = (e) => {
      let packet;
      try { packet = JSON.parse(e.data); } catch { return; }
      handleEvent(packet.event, packet.data);
    };
    state.ws.onclose = () => { closeWS(); scheduleReconnect(); };
    state.ws.onerror = () => { /* onclose 处理 */ };
  }

  function closeWS() {
    if (state.wsPing) { clearInterval(state.wsPing); state.wsPing = null; }
    if (state.ws) {
      try { state.ws.close(); } catch { /* ignore */ }
      state.ws = null;
    }
  }

  let reconnectTimer = null;
  function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      if (state.room && state.room.state === "online") void connectWS();
    }, 3000);
  }

  function handleEvent(event, data) {
    switch (event) {
      case "room.snapshot":
        if (data.room) state.room = Object.assign(state.room || {}, {
          id: data.room.id || state.room.id,
          name: data.room.name || state.room.name,
          state: data.room.state || state.room.state,
        });
        if (data.settings) state.settings = data.settings;
        if (data.capabilities) state.capabilities = data.capabilities;
        state.members = data.members || [];
        state.messages = data.messages || [];
        renderMessages();
        updateMeta();
        applyCapabilities();
        break;
      case "member.joined":
        if (data && !state.members.some((m) => m.id === data.id)) state.members.push(data);
        updateMeta();
        toast(`${data?.displayName || "有人"} 加入了房间`, "ok");
        break;
      case "member.left":
        state.members = state.members.filter((m) => m.id !== data?.id);
        updateMeta();
        break;
      case "message.created":
        upsertMessage(data);
        break;
      case "message.deleted":
        state.messages = state.messages.filter((m) => m.id !== data?.id);
        renderMessages();
        break;
      case "job.queued":
        trackJob(data?.job);
        break;
      case "job.progress":
        trackJob(data?.job, data?.progress);
        break;
      case "job.completed":
        trackJob(data?.job);
        break;
      case "job.failed":
        trackJob(data?.job);
        toast(data?.error || "任务失败", "err");
        break;
      case "room.catalog_changed":
      case "room.settings_changed":
        void loadCatalog();
        break;
      case "room.closing":
        setClosed(data?.reason === "host-closed" ? "房主已关闭房间。" : "房间已关闭。");
        break;
      case "room.error":
        toast(data?.error || "房间发生错误", "err");
        break;
      default:
        break;
    }
  }

  function trackJob(job, progress) {
    if (!job) return;
    state.jobs.set(job.id, job);
    if (job.memberId !== state.member.id) return; // 只看自己的任务
    if (job.state === "queued" || job.state === "running") {
      state.job = progress != null ? Object.assign({}, job, { progress }) : job;
      state.result = null;
      renderCanvas();
    } else if (job.state === "completed" || job.state === "failed" || job.state === "cancelled") {
      if (state.job && state.job.id === job.id) state.job = null;
      renderCanvas();
    }
  }

  /* ---------------- 消息 ---------------- */
  async function loadHistory() {
    const res = await api("/room/v1/messages?limit=50");
    if (res.ok && Array.isArray(res.data.messages)) {
      state.messages = res.data.messages;
      renderMessages();
    }
  }

  function upsertMessage(message) {
    if (!message) return;
    const existing = state.messages.findIndex((m) => m.id === message.id);
    if (existing >= 0) {
      state.messages[existing] = message;
      renderMessages();
    } else {
      state.messages.push(message);
      appendMessage(message);
    }
    if (message.type === "generation_result") maybeShowResult(message);
  }

  function memberName(id) {
    const m = state.members.find((x) => x.id === id);
    return m ? m.displayName : `成员#${id}`;
  }

  function avatarColor(id) {
    const pal = ["#496A70", "#9B5067", "#AD8748", "#39745F", "#5B5F8A", "#8A5B7C"];
    let h = 0;
    const s = String(id);
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return pal[h % pal.length];
  }

  // 新消息只追加单条，避免整列重建导致已加载图片重新请求。
  function appendMessage(message) {
    const box = $("ws-messages");
    const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
    box.append(renderMessage(message));
    if (stick) box.scrollTop = box.scrollHeight;
  }

  function renderMessages() {
    const box = $("ws-messages");
    const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
    const frag = document.createDocumentFragment();
    state.messages.forEach((m) => frag.append(renderMessage(m)));
    box.replaceChildren(frag);
    if (stick) box.scrollTop = box.scrollHeight;
  }

  function renderMessage(m) {
    const isMe = m.senderId === state.member.id;
    const row = document.createElement("div");
    row.className = "msg" + (isMe ? " me" : "");
    const avatar = document.createElement("div");
    avatar.className = "msg-avatar";
    avatar.style.background = avatarColor(m.senderId || 0);
    avatar.textContent = (memberName(m.senderId) || "?").trim().charAt(0);
    const body = document.createElement("div");
    body.className = "msg-body";

    if (m.type === "system") {
      const sys = document.createElement("div");
      sys.className = "msg-sys";
      sys.textContent = m.text || "";
      return sys;
    }

    const meta = document.createElement("div");
    meta.className = "msg-meta";
    meta.textContent = `${memberName(m.senderId)} · ${fmtTime(m.createdAt)}`;
    body.append(meta);

    const bubble = document.createElement("div");
    if (m.type === "text") {
      bubble.className = "msg-bubble";
      bubble.textContent = m.text || "";
    } else if (m.type === "image") {
      bubble.className = "msg-bubble";
      const imgs = (m.attachments || []).map((a) => {
        const img = document.createElement("img");
        img.className = "msg-img";
        img.alt = "分享的图片";
        img.src = a.thumbUrl || a.url;
        img.addEventListener("click", () => openLightbox(a.url || a.thumbUrl));
        return img;
      });
      imgs.forEach((i) => bubble.append(i));
    } else if (m.type === "generation_result") {
      const card = document.createElement("div");
      card.className = "msg-result";
      const att = (m.attachments || [])[0];
      const img = document.createElement("img");
      img.className = "msg-img";
      img.alt = "生成结果";
      if (att) {
        img.src = att.thumbUrl || att.url;
        img.addEventListener("click", () => openLightbox(att.url));
        card.append(img);
      }
      const cap = document.createElement("div");
      cap.className = "rr-prompt";
      cap.textContent = "生成结果";
      card.append(cap);
      bubble.append(card);
    } else if (m.type === "generation_failed") {
      bubble.className = "msg-bubble";
      bubble.style.color = "var(--danger)";
      bubble.textContent = m.text || "生成失败";
    } else {
      bubble.className = "msg-bubble";
      bubble.textContent = m.text || "";
    }
    body.append(bubble);

    if (isMe && (m.type === "text" || m.type === "image")) {
      const acts = document.createElement("div");
      acts.className = "msg-actions";
      const del = document.createElement("button");
      del.type = "button";
      del.className = "msg-del";
      del.textContent = "删除";
      del.addEventListener("click", () => { void deleteMessage(m.id); });
      acts.append(del);
      body.append(acts);
    }
    row.append(avatar, body);
    return row;
  }

  function fmtTime(iso) {
    if (!iso) return "";
    try {
      const d = new Date(iso);
      const p = (n) => String(n).padStart(2, "0");
      return `${p(d.getHours())}:${p(d.getMinutes())}`;
    } catch { return ""; }
  }

  async function sendChat() {
    const input = $("chat-input");
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    const res = await api("/room/v1/messages", { json: { text } });
    if (!res.ok) {
      toast(chatErrorMessage(res.data.errorCode, res.data.error), "err");
      input.value = text;
    }
  }

  function chatErrorMessage(code, fallback) {
    const map = {
      CHAT_DISABLED: "房主已关闭聊天",
      EMPTY_TEXT: "消息不能为空",
      TEXT_TOO_LONG: "消息过长",
      CHAT_RATE_LIMITED: "发言过于频繁，请稍后再试",
    };
    return map[code] || fallback || "发送失败";
  }

  async function deleteMessage(id) {
    const res = await api("/room/v1/messages", { json: { action: "delete", messageId: id } });
    if (!res.ok) toast("删除失败", "err");
  }

  async function shareImage(file) {
    const fd = new FormData();
    fd.append("files", file, file.name || "image.png");
    const res = await api("/room/v1/uploads", { method: "POST", body: fd });
    if (!res.ok) {
      toast(uploadErrorMessage(res.data.errorCode, res.data.error), "err");
      return null;
    }
    const att = res.data.message?.attachments?.[0];
    if (!att) { toast("上传成功但未返回附件", "warn"); return null; }
    return att;
  }

  function uploadErrorMessage(code, fallback) {
    const map = {
      UPLOAD_DISABLED: "房主已关闭图片上传",
      FILE_TOO_LARGE: "单个图片不能超过 20MB",
      IMAGE_TOO_LARGE: "图片尺寸过大",
      UPLOAD_RATE_LIMITED: "上传过于频繁，请稍后再试",
      UNSUPPORTED_IMAGE: "不支持的图片格式",
      EMPTY_FILE: "空文件",
    };
    return map[code] || fallback || "上传失败";
  }

  /* ---------------- 生成 ---------------- */
  function maybeShowResult(message) {
    const att = (message.attachments || [])[0];
    const job = state.jobs.get(message.generationJobId);
    if (!att) return;
    state.result = { job: job || { id: message.generationJobId }, url: att.url };
    state.job = null;
    renderCanvas();
  }

  async function submitJob() {
    if (state.job) return;
    const prompt = $("g-prompt").value.trim();
    if (!prompt && state.mode === "txt2img") { toast("请先填写提示词", "warn"); $("g-prompt").focus(); return; }
    const request = {
      mode: state.mode,
      prompt,
      negative_prompt: $("g-neg").value.trim() || null,
      seed: parseInt($("g-seed").value, 10) || -1,
      width: parseInt($("g-w").value, 10) || 832,
      height: parseInt($("g-h").value, 10) || 1216,
      steps: parseInt($("g-steps").value, 10) || 24,
      cfg: parseFloat($("g-cfg").value) || 7,
      sampler: $("g-sampler").value || "euler_ancestral",
      scheduler: $("g-sched").value || "normal",
      family: $("g-family").value || null,
      workflow: $("g-workflow").value || null,
    };
    const lora = $("g-lora").value;
    if (lora) request.loras = [{ id: lora, strength: 0.8 }];
    if (state.mode === "img2img" && state.refImage) request.refAttachmentId = state.refImage.attachmentId;

    const btn = $("g-submit");
    btn.disabled = true;
    try {
      const res = await api("/room/v1/generations", { json: { clientRequestId: clientRequestId(), request } });
      if (!res.ok) {
        toast(generationErrorMessage(res.data.errorCode, res.data.error), "err");
        return;
      }
      const job = res.data.job;
      state.jobs.set(job.id, job);
      if (res.data.created) {
        state.job = job;
        state.result = null;
        renderCanvas();
        $("g-cancel").hidden = false;
      }
    } finally {
      btn.disabled = !!state.job;
    }
  }

  function generationErrorMessage(code, fallback) {
    const map = {
      AUTH_REQUIRED: "会话已失效，请刷新页面重新加入",
      GENERATION_DISABLED: "房主已关闭生图",
      GENERATION_PAUSED: "房主已暂停生图",
      QUEUE_LIMIT_MEMBER: "个人队列已满，请等待当前任务完成",
      QUEUE_LIMIT_ROOM: "房间队列已满，请稍后再试",
      MODEL_NOT_ALLOWED: "该模型系列未获得房主允许",
      WORKFLOW_NOT_ALLOWED: "该工作流未获得房主允许",
      IMG2IMG_DISABLED: "房主已关闭图生图",
      EMPTY_PROMPT: "提示词不能为空",
      PROMPT_TOO_LONG: "提示词过长",
      ATTACHMENT_NOT_FOUND: "参考图已失效，请重新上传",
    };
    return map[code] || fallback || "提交失败，请重试";
  }

  async function cancelJob() {
    if (!state.job) return;
    const res = await api(`/room/v1/generations/${state.job.id}`, { method: "POST" });
    if (!res.ok) toast("取消失败", "err");
  }

  async function enhancePrompt() {
    const prompt = $("g-prompt").value.trim();
    if (!prompt) { toast("请先输入提示词再增强", "warn"); return; }
    const btn = $("g-enhance");
    const prev = btn.textContent;
    btn.disabled = true;
    btn.textContent = "增强中…";
    try {
      const res = await api("/room/v1/generations/enhance", { json: { prompt } });
      if (res.ok && res.data.enhanced) {
        $("g-prompt").value = res.data.enhanced;
        toast("提示词已增强", "ok");
      } else {
        toast(res.data.error || "增强失败", "err");
      }
    } finally {
      btn.disabled = false;
      btn.textContent = prev;
    }
  }

  async function saveResult() {
    if (!state.result) return;
    const a = document.createElement("a");
    a.href = state.result.url;
    a.download = "winefox_room.png";
    a.target = "_blank";
    a.rel = "noopener";
    document.body.append(a);
    a.click();
    a.remove();
  }

  /* ---------------- 画布 ---------------- */
  function renderCanvas() {
    const area = $("canvas-area");
    area.innerHTML = "";
    if (state.job) {
      const stateText = state.job.state === "running" ? "正在生成…" : "排队中…";
      const prog = Number(state.job.progress) || 0;
      $("g-progress-label").textContent = stateText;
      $("g-progress-pct").textContent = prog ? `${Math.round(prog * 100)}%` : "";
      const bar = $("g-progress-bar");
      const fill = bar.querySelector("i");
      if (prog) {
        bar.classList.remove("indeterminate");
        fill.style.width = `${Math.min(100, Math.round(prog * 100))}%`;
      } else {
        bar.classList.add("indeterminate");
        fill.style.width = "";
      }
      $("g-progress").hidden = false;
      $("g-cancel").hidden = false;
      $("g-submit").disabled = true;
      $("g-queue-info").textContent = state.job.queuePosition != null ? `队列第 ${state.job.queuePosition} 位` : "";
      const holder = document.createElement("div");
      holder.className = "canvas-placeholder";
      const b = document.createElement("b");
      b.textContent = stateText;
      const s = document.createElement("span");
      s.textContent = state.job.state === "running" ? "墨色正在画布上汇聚" : "前方还有任务，稍候片刻";
      holder.append(b, s);
      area.append(holder);
      $("g-submit").textContent = "生成中…";
      return;
    }
    $("g-progress").hidden = true;
    $("g-cancel").hidden = true;
    $("g-submit").disabled = false;
    $("g-submit").textContent = "开始生成";
    $("g-queue-info").textContent = "";

    if (state.result) {
      const img = document.createElement("img");
      img.className = "canvas-img";
      img.src = state.result.url;
      img.alt = "生成结果";
      img.addEventListener("click", () => openLightbox(state.result.url));
      area.append(img);
      $("g-save").hidden = false;
      return;
    }
    $("g-save").hidden = true;

    if (state.mode === "img2img") {
      if (state.refImage) {
        const img = document.createElement("img");
        img.className = "canvas-img ref";
        img.src = state.refImage.url;
        img.alt = "参考图";
        img.addEventListener("click", () => openLightbox(state.refImage.url));
        area.append(img);
      } else {
        const holder = document.createElement("div");
        holder.className = "canvas-placeholder";
        const b = document.createElement("b");
        b.textContent = "拖入参考图，或点击选择";
        const s = document.createElement("span");
        s.textContent = "将作为图生图的起点";
        holder.append(b, s);
        area.append(holder);
      }
      return;
    }

    const holder = document.createElement("div");
    holder.className = "canvas-placeholder";
    const b = document.createElement("b");
    b.textContent = "画布";
    const s = document.createElement("span");
    s.textContent = "填写提示词后点击「开始生成」";
    holder.append(b, s);
    area.append(holder);
  }

  function setMode(mode) {
    if (!["txt2img", "img2img"].includes(mode)) return;
    state.mode = mode;
    document.querySelectorAll("#g-mode .mode-btn").forEach((b) => {
      const active = b.dataset.mode === mode;
      b.classList.toggle("mode-active", active);
      b.setAttribute("aria-checked", active ? "true" : "false");
    });
    if (mode === "txt2img") state.refImage = null;
    state.result = null;
    renderCanvas();
    refreshFamilyWorkflowLora();
  }

  /* ---------------- 图片：上传/大图 ---------------- */
  function pickImageFile(cb) {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/*";
    input.hidden = true;
    input.addEventListener("change", () => {
      const f = input.files && input.files[0];
      if (f) cb(f);
    });
    document.body.append(input);
    input.click();
    input.remove();
  }

  function openLightbox(url) {
    const lb = $("lightbox");
    $("lightbox-img").src = url;
    lb.hidden = false;
  }
  $("lightbox-close").addEventListener("click", () => { $("lightbox").hidden = true; $("lightbox-img").src = ""; });
  $("lightbox").addEventListener("click", (e) => {
    if (e.target === $("lightbox")) { $("lightbox").hidden = true; $("lightbox-img").src = ""; }
  });

  /* ---------------- 关闭遮罩 ---------------- */
  function setClosed(reason) {
    $("closed-reason").textContent = reason || "房主已关闭房间。";
    closeWS();
    $("screen-closed").hidden = false;
  }

  /* ---------------- 事件绑定 ---------------- */
  function wireWorkspace() {
    $("chat-send").addEventListener("click", () => { void sendChat(); });
    $("chat-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void sendChat(); }
    });
    $("chat-image").addEventListener("click", () => {
      pickImageFile(async (file) => {
        const att = await shareImage(file);
        if (att) toast("图片已分享", "ok");
      });
    });
    $("ws-leave").addEventListener("click", async () => {
      await api("/room/v1/leave", { method: "POST" });
      closeWS();
      location.reload();
    });
    $("closed-reload").addEventListener("click", () => location.reload());

    document.querySelectorAll("#g-mode .mode-btn").forEach((b) => {
      b.addEventListener("click", () => setMode(b.dataset.mode));
    });
    $("g-family").addEventListener("change", refreshFamilyWorkflowLora);
    $("g-submit").addEventListener("click", () => { void submitJob(); });
    $("g-cancel").addEventListener("click", () => { void cancelJob(); });
    $("g-save").addEventListener("click", () => { void saveResult(); });
    $("g-enhance").addEventListener("click", () => { void enhancePrompt(); });
    $("g-prompt").addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); void submitJob(); }
    });
    $("g-seed-dec").addEventListener("click", () => {
      const v = parseInt($("g-seed").value, 10) || -1;
      $("g-seed").value = String(v <= 0 ? 1 : v - 1);
    });
    $("g-seed-inc").addEventListener("click", () => {
      const v = parseInt($("g-seed").value, 10) || -1;
      $("g-seed").value = String(v < 0 ? 1 : v + 1);
    });

    // 图生图参考图：点击画布选择 / 拖放
    const area = $("canvas-area");
    const stop = (e) => { e.preventDefault(); e.stopPropagation(); };
    area.addEventListener("dragover", (e) => { stop(e); area.classList.add("dragover"); });
    area.addEventListener("dragleave", () => area.classList.remove("dragover"));
    area.addEventListener("drop", (e) => {
      stop(e);
      area.classList.remove("dragover");
      const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (file && /^image\//.test(file.type)) { void setRefImage(file); }
    });
    area.addEventListener("click", () => {
      if (state.mode === "img2img" && !state.refImage && !state.job) {
        pickImageFile((file) => { void setRefImage(file); });
      }
    });
  }

  async function setRefImage(file) {
    const att = await shareImage(file);
    if (!att) return;
    state.refImage = { attachmentId: att.id, url: att.url };
    state.result = null;
    renderCanvas();
    toast("参考图已就绪，可开始生成", "ok");
  }

  /* ---------------- 启动 ---------------- */
  void boot();
})();

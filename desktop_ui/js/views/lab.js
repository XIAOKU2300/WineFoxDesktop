/* WineFox Desktop — 实验室：多人聊天室与房间管理（仅调试模式显示）。
 * 房主身份经 Shell 桥传递，本页通过面板代理访问房间网关（X-WineFox-Room-Host
 * 令牌由 Rust 注入，页面本身不接触令牌）。
 */
import { el, $, $$, ic, esc, toast, loadingState, copyText, isDegenerate } from "../utils.js";
import { roomBridge, debugBridge } from "../utils.js";
import { store } from "../store.js";

// 代理字符校验：后端（manager.py contains_lone_surrogate）是权威；
// 前端不做拦截，避免 WebView/IME 对合法中文（BMP CJK）误判为「无法识别的字符」。
// 中文经 Rust ensure_ascii JSON + sidecar PYTHONUTF8 无损传给后端校验。

export class LabView {
  constructor() {
    this.status = null;
    this.unsub = null;
    this.autoTunnelActive = false;
    this._pendingStart = false;
    this._pendingTimer = null;
  }

  mount(container) {
    this.root = container;
    this.render();
    void this.refresh();
  }

  deactivate() {
    if (this._pendingTimer) { clearTimeout(this._pendingTimer); this._pendingTimer = null; }
    this.unsub?.();
    this.unsub = null;
  }

  activate() {
    if (!this.unsub) {
      this.unsub = roomBridge.onState((state) => {
        if (this._pendingStart) {
          if (state.state === "online") {
            this._pendingStart = false;
            toast("房间已开启", "ok");
          } else if (state.state === "failed" && state.error) {
            this._pendingStart = false;
            toast(state.error, "err");
          }
        }
        this.status = state;
        this.renderStatus();
        // 房间状态变化（尤其是 starting→online）时刷新目录，避免陈旧的
        // "暂时无法读取房间目录" 一直停留。
        void this.loadCatalog();
      });
    }
    void this.refresh();
  }

  async refresh() {
    const res = await roomBridge.getState();
    if (res.ok) {
      this.status = res.state;
      this.renderStatus();
    }
    void this.loadCatalog();
  }

  async loadCatalog() {
    const box = $("#lab-catalog", this.root);
    if (!box) return;
    const state = this.status?.state;
    // 只在房间已上线（网关已绑定端口）时拉取；starting/failed 都不该发请求，
    // 否则网关未就绪会命中"暂时无法读取房间目录"且不再刷新。
    if (state === "starting") {
      box.replaceChildren(loadingState("正在准备房间目录", { compact: true }));
      return;
    }
    if (!state || state === "stopped" || state === "failed") {
      box.replaceChildren(el("div", { class: "empty" }, [
        ic("model"),
        el("div", { class: "empty-title" }, "暂无房间目录"),
        el("div", { class: "empty-desc" }, "开启房间后可在此查看可用的模型、工作流与 LoRA。"),
      ]));
      return;
    }
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        const response = await fetch("/room/v1/catalog", { headers: { accept: "application/json" } });
        if (!response.ok) throw new Error(String(response.status));
        const data = await response.json();
        const families = (data.families || []).map((f) => f.label || f.id || String(f));
        box.replaceChildren(
          el("div", { class: "row gap-2 wrap" }, [
            el("span", { class: "chip" }, esc(`模型 ${families.length}`)),
            el("span", { class: "chip" }, esc(`工作流 ${(data.workflows || []).length}`)),
            el("span", { class: "chip" }, esc(`LoRA ${(data.loras || []).length}`)),
          ]),
        );
        return;
      } catch {
        if (attempt < 2) await new Promise((resolve) => setTimeout(resolve, 400));
      }
    }
    box.replaceChildren(el("div", { class: "empty" }, [
      ic("warn"),
      el("div", { class: "empty-desc" }, "暂时无法读取房间目录。"),
    ]));
  }

  render() {
    const root = this.root;
    root.innerHTML = "";
    root.append(
      el("div", { class: "view-head" }, [
        el("div", {}, [
          el("h1", { class: "h1" }, "实验室"),
          el("div", { class: "lead" }, "调试模式：多人聊天室、房间管理与公网接入。"),
        ]),
        el("div", { class: "toolbar" }, [
          el("span", { class: "chip lab-debug-chip", id: "lab-debug-chip" }, [ic("flask"), esc("调试模式已开启")]),
        ]),
      ]),
      el("div", { class: "stack-4" }, [
        el("section", { class: "section-card", id: "lab-room-form" }, this.form()),
        el("section", { class: "section-card" }, [
          el("div", { class: "sec-head" }, [ic("queue"), el("h3", {}, "房间状态")]),
          el("div", { class: "sec-body" }, [
            el("div", { class: "stack-2", id: "lab-status" }, [loadingState("正在读取房间状态", { compact: true })]),
          ]),
        ]),
        el("section", { class: "section-card" }, [
          el("div", { class: "sec-head" }, [ic("link"), el("h3", {}, "公网接入")]),
          el("div", { class: "sec-body" }, [
            el("p", { class: "lead" }, "将 Cloudflare Tunnel / FRP / 端口转发的公网地址填入下方（HTTPS 或 HTTP）；WineFox 会确认该地址指向本房间后再启用。"),
            el("div", { class: "row gap-2" }, [
              el("div", { class: "grow" }, [
                el("input", { class: "input", id: "lab-tunnel-url", type: "url", placeholder: "http(s)://你的公网地址.example", autocomplete: "off" }),
              ]),
              el("button", { class: "btn", id: "lab-tunnel-set", type: "button" }, [ic("link"), esc("配置")]),
              el("button", { class: "btn btn-ghost", id: "lab-tunnel-clear", type: "button" }, [ic("x"), esc("清除")]),
            ]),
            el("div", { class: "row gap-2", id: "lab-tunnel-auto-row", hidden: true }, [
              el("button", { class: "btn btn-ghost", id: "lab-tunnel-auto", type: "button" }, [ic("wand"), esc("自动创建（cloudflared）")]),
              el("button", { class: "btn btn-ghost", id: "lab-tunnel-auto-stop", type: "button" }, [ic("x"), esc("停止自动隧道")]),
              el("span", { class: "hint", id: "lab-tunnel-auto-hint" }, ""),
            ]),
            el("div", { class: "hint", id: "lab-tunnel-hint" }, "未配置公网地址。"),
          ]),
        ]),
        el("section", { class: "section-card" }, [
          el("div", { class: "sec-head" }, [ic("model"), el("h3", {}, "房间目录")]),
          el("div", { class: "sec-body" }, [
            el("div", { class: "stack-2", id: "lab-catalog" }, []),
          ]),
        ]),
      ]),
    );

    $("#lab-start", root)?.addEventListener("click", () => { void this.startRoom(); });
    $("#lab-stop", root)?.addEventListener("click", () => { void this.stopRoom(); });
    $("#lab-tunnel-set", root)?.addEventListener("click", () => { void this.setTunnel(); });
    $("#lab-tunnel-clear", root)?.addEventListener("click", () => { void this.clearTunnel(); });
    $("#lab-tunnel-auto", root)?.addEventListener("click", () => { void this.provisionTunnel(); });
    $("#lab-tunnel-auto-stop", root)?.addEventListener("click", () => { void this.stopTunnel(); });
    void roomBridge.cloudflaredAvailable().then((res) => {
      const row = $("#lab-tunnel-auto-row", root);
      if (row) row.hidden = !(res && res.ok && res.available);
    });
    this.renderStatus();
  }

  form() {
    return [
      el("div", { class: "sec-head" }, [ic("gen"), el("h3", {}, "开启房间")]),
      el("div", { class: "sec-body" }, [
        el("div", { class: "form-row" }, [
          el("div", { class: "field" }, [
            el("label", { for: "lab-name" }, "房间名称"),
            el("input", { class: "input", id: "lab-name", type: "text", maxlength: 40, placeholder: "例如：周末创作室", autocomplete: "off" }),
          ]),
          el("div", { class: "field" }, [
            el("label", { for: "lab-owner" }, "房主名称"),
            el("input", { class: "input", id: "lab-owner", type: "text", maxlength: 24, placeholder: "你的昵称", autocomplete: "off" }),
          ]),
        ]),
        el("div", { class: "form-row" }, [
          el("div", { class: "field" }, [
            el("label", { for: "lab-password" }, "房间密码"),
            el("input", { class: "input", id: "lab-password", type: "password", maxlength: 64, placeholder: "访客加入需输入", autocomplete: "new-password" }),
          ]),
          el("div", { class: "field" }, [
            el("label", { for: "lab-max" }, "成员上限"),
            el("input", { class: "input", id: "lab-max", type: "number", min: 2, max: 64, value: "8" }),
          ]),
        ]),
        el("div", { class: "form-row" }, [
          el("div", { class: "field" }, [
            el("label", { for: "lab-port" }, "端口"),
            el("input", { class: "input", id: "lab-port", type: "number", min: "1024", max: "65535", placeholder: "留空 = 随机端口", autocomplete: "off" }),
          ]),
          el("div", { class: "field" }, [
            el("label", { for: "lab-bind" }, "绑定地址"),
            el("select", { class: "select", id: "lab-bind" }, [
              el("option", { value: "0.0.0.0" }, "0.0.0.0 — 本机+局域网/公网"),
              el("option", { value: "127.0.0.1" }, "127.0.0.1 — 仅本机"),
            ]),
          ]),
        ]),
        el("div", { class: "stack-2" }, [
          el("label", { class: "check" }, [
            el("input", { type: "checkbox", id: "lab-allow-chat", checked: true }),
            el("span", { class: "box" }, [ic("check")]),
            el("span", {}, esc("允许聊天")),
          ]),
          el("label", { class: "check" }, [
            el("input", { type: "checkbox", id: "lab-allow-upload", checked: true }),
            el("span", { class: "box" }, [ic("check")]),
            el("span", {}, esc("允许图片上传")),
          ]),
          el("label", { class: "check" }, [
            el("input", { type: "checkbox", id: "lab-allow-gen", checked: true }),
            el("span", { class: "box" }, [ic("check")]),
            el("span", {}, esc("允许生图")),
          ]),
          el("label", { class: "check" }, [
            el("input", { type: "checkbox", id: "lab-allow-img2img", checked: true }),
            el("span", { class: "box" }, [ic("check")]),
            el("span", {}, esc("允许图生图")),
          ]),
          el("label", { class: "check" }, [
            el("input", { type: "checkbox", id: "lab-allow-enhance", checked: true }),
            el("span", { class: "box" }, [ic("check")]),
            el("span", {}, esc("允许提示词增强")),
          ]),
        ]),
        el("div", { class: "row gap-2" }, [
          el("button", { class: "btn", id: "lab-start", type: "button" }, [ic("play"), esc("开启房间")]),
          el("button", { class: "btn btn-danger", id: "lab-stop", type: "button", hidden: true }, [ic("trash"), esc("关闭房间")]),
        ]),
      ]),
    ];
  }

  readForm() {
    const val = (id) => ($("#" + id)?.value ?? "").trim();
    const check = (id) => !!$("#" + id)?.checked;
    let max = parseInt($("#lab-max")?.value || "8", 10);
    if (!Number.isFinite(max) || max < 2) max = 2;
    if (max > 64) max = 64;
    const rawPort = val("lab-port");
    return {
      name: val("lab-name"),
      ownerName: val("lab-owner"),
      password: val("lab-password"),
      maxMembers: max,
      port: rawPort === "" ? null : parseInt(rawPort, 10),
      bindHost: $("#lab-bind")?.value || "0.0.0.0",
      allowChat: check("lab-allow-chat"),
      allowUpload: check("lab-allow-upload"),
      allowGeneration: check("lab-allow-gen"),
      allowImg2img: check("lab-allow-img2img"),
      allowPromptEnhancement: check("lab-allow-enhance"),
    };
  }

  async startRoom() {
    const settings = this.readForm();
    if (!settings.name || !settings.ownerName || !settings.password) {
      toast("请填写房间名称、房主名称与密码", "err");
      return;
    }
    if (settings.port !== null && (settings.port < 1024 || settings.port > 65535)) {
      toast("端口需在 1024-65535 之间", "err");
      return;
    }
    const start = $("#lab-start");
    if (start) { start.disabled = true; start.setAttribute("aria-busy", "true"); }
    // 房间已在线时 manager.start 幂等返回、不发布 room_status 事件，
    // 因此只对「从停止→启动」的尝试挂 pending 标记，避免标记滞留误弹 toast。
    if (this.status?.state !== "online") {
      this._pendingStart = true;
      // 兜底：ack 后长时间收不到 online/failed（如 sidecar 中途崩溃），
      // 清除标记恢复按钮，避免永久卡死。state 仍为 starting 时 renderStatus 自会禁用。
      if (this._pendingTimer) clearTimeout(this._pendingTimer);
      this._pendingTimer = setTimeout(() => {
        this._pendingTimer = null;
        if (this._pendingStart) {
          this._pendingStart = false;
          this.renderStatus();
        }
      }, 45000);
    }
    try {
      const res = await roomBridge.start(settings);
      if (!res.ok) {
        this._pendingStart = false;
        if (res.error === "DEBUG_MODE_REQUIRED") toast("需要先在左上角 Logo 连续点击 10 次解锁调试模式", "err");
        else toast(res.error || "开启房间失败", "err");
      }
    } catch {
      this._pendingStart = false;
      toast("开启房间失败：无法连接桌面服务", "err");
    } finally {
      // 启动尚未完成（pending）时保持按钮禁用，直到收到 online/failed 事件
      // 由 renderStatus 恢复，避免开房过程中可重复点击。
      if (start && !this._pendingStart) { start.disabled = false; start.removeAttribute("aria-busy"); }
    }
  }

  async stopRoom() {
    const res = await roomBridge.stop();
    if (!res.ok) toast(res.error || "关闭房间失败", "err");
  }

  async setTunnel() {
    const url = ($("#lab-tunnel-url")?.value || "").trim();
    if (!url) { toast("请输入公网地址", "err"); return; }
    const btn = $("#lab-tunnel-set");
    if (btn) { btn.disabled = true; }
    try {
      const res = await roomBridge.setTunnel(url);
      if (!res.ok) toast(res.error || "配置失败", "err");
    } finally {
      if (btn) { btn.disabled = false; }
    }
  }

  async clearTunnel() {
    this.autoTunnelActive = false;
    const res = await roomBridge.clearTunnel();
    if (!res.ok) toast(res.error || "清除失败", "err");
    this.renderStatus();
  }

  async provisionTunnel() {
    const btn = $("#lab-tunnel-auto", this.root);
    const hint = $("#lab-tunnel-auto-hint", this.root);
    if (btn) btn.disabled = true;
    if (hint) hint.textContent = "正在创建并校验隧道，可能需要几十秒…";
    try {
      const res = await roomBridge.provisionTunnel();
      if (res.ok) {
        this.autoTunnelActive = true;
        toast("自动隧道已建立", "ok");
      } else {
        toast(res.error || "创建失败", "err");
      }
    } finally {
      if (btn) btn.disabled = false;
      if (hint) hint.textContent = "";
      this.renderStatus();
    }
  }

  async stopTunnel() {
    this.autoTunnelActive = false;
    const res = await roomBridge.stopTunnel();
    if (!res.ok) toast(res.error || "停止失败", "err");
    this.renderStatus();
  }

  renderStatus() {
    const box = $("#lab-status", this.root);
    const stop = $("#lab-stop", this.root);
    const debugChip = $("#lab-debug-chip", this.root);
    const tunnelHint = $("#lab-tunnel-hint", this.root);
    if (!box) return;
    const s = this.status;

    // 开房过程中（starting 或启动未决）禁用开启按钮，online/failed 后恢复；
    // 由 renderStatus 统一管理，避免 startRoom 的 finally 在 pending 期间恢复按钮。
    const startBtn = $("#lab-start", this.root);
    if (startBtn) {
      const busy = s?.state === "starting" || (this._pendingStart && s?.state !== "online" && s?.state !== "failed");
      startBtn.disabled = busy;
      if (busy) startBtn.setAttribute("aria-busy", "true");
      else startBtn.removeAttribute("aria-busy");
    }

    if (debugChip) {
      const enabled = store.get("debugEnabled");
      debugChip.hidden = !enabled;
    }

    if (!s || !s.state || s.state === "stopped" || s.state === "") {
      const empty = el("div", { class: "empty" }, [
        ic("queue"),
        el("div", { class: "empty-desc" }, "当前没有运行中的房间。"),
      ]);
      // 调试模式下聊天室入口始终可见：房间未开时点入向导会提示先开房。
      if (store.get("debugEnabled")) {
        box.replaceChildren(
          empty,
          el("div", { class: "row gap-2", style: "justify-content:center" }, [this.chatEntryButton()]),
        );
      } else {
        box.replaceChildren(empty);
      }
      if (stop) stop.hidden = true;
      if (tunnelHint) tunnelHint.textContent = "未配置公网地址。";
      return;
    }

    // starting 期间不显示关闭按钮：此刻 stop() 会在启动协程收尾前清空状态，
    // 随后 start() 仍会把状态写成 online，造成竞态。
    const open = s.state === "online";
    if (stop) stop.hidden = !open;
    const autoStop = $("#lab-tunnel-auto-stop", this.root);
    if (autoStop) autoStop.hidden = !(open && this.autoTunnelActive);
    if (tunnelHint) {
      tunnelHint.textContent = s.tunnelUrl
        ? `公网地址：${s.tunnelUrl}（已连接：${s.tunnelConnected ? "是" : "否"}）`
        : "未配置公网地址。";
    }

    const rows = [
      ["状态", s.state === "online" ? "运行中" : (s.state === "starting" ? "正在开启…" : s.state)],
      s.name ? ["房间", s.name] : null,
      ["成员", `${s.members ?? 0} / ${s.maxMembers ?? 0}`],
      s.localUrl ? ["本地地址", s.localUrl] : null,
      s.lanUrl ? ["局域网地址", s.lanUrl] : null,
      s.tunnelUrl ? ["公网地址", s.tunnelUrl] : null,
    ].filter(Boolean);

    // 开房进度：state=starting 时显示真实进度条 + 当前阶段文字。
    let progressBlock = null;
    if (s.state === "starting" && s.progress != null) {
      const pct = Math.max(0, Math.min(100, Number(s.progress) || 0));
      progressBlock = el("div", { class: "stack-2", id: "lab-start-progress" }, [
        el("div", { class: "row gap-2", style: "justify-content:space-between" }, [
          el("span", { class: "hint" }, esc(s.stage || "正在开启房间…")),
          el("span", { class: "hint" }, esc(`${pct}%`)),
        ]),
        el("div", { class: "progress" }, [el("i", { style: `width: ${pct}%` })]),
      ]);
    }

    // 邀请地址优先级：公网隧道 → 局域网地址 → 本机地址（尽量给对方一个可用的链接）。
    const inviteUrl = s.tunnelUrl || s.lanUrl || s.localUrl;
    const copyBtn = inviteUrl
      ? el("button", { class: "btn btn-sm", id: "lab-copy-url", type: "button" }, [ic("copy"), esc("复制邀请地址")])
      : null;
    if (copyBtn) {
      copyBtn.addEventListener("click", async () => {
        if (!inviteUrl) return;
        try { await copyText(inviteUrl); toast("邀请地址已复制", "ok"); }
        catch { toast("复制失败，请手动选择地址", "err"); }
      });
    }
    const lanCopy = s.lanUrl
      ? el("button", { class: "btn btn-sm", id: "lab-copy-lan", type: "button" }, [ic("copy"), esc("复制局域网地址")])
      : null;
    if (lanCopy) {
      lanCopy.addEventListener("click", async () => {
        try { await copyText(s.lanUrl); toast("局域网地址已复制", "ok"); }
        catch { toast("复制失败，请手动选择地址", "err"); }
      });
    }
    // 调试模式下聊天室入口始终显示（房间未开时点入向导会提示先开房）。
    const enterBtn = store.get("debugEnabled") ? this.chatEntryButton() : null;

    // 不可把 null 直接传给 replaceChildren：浏览器会强制转为文本节点 "null"。
    box.replaceChildren(...[
      el("div", { class: "summary-table" }, rows.map(([k, v]) => el("div", { class: "summary-row" }, [
        el("span", { class: "summary-key" }, esc(k)),
        el("span", { class: "summary-value" }, esc(String(v))),
      ]))),
      progressBlock,
      (copyBtn || lanCopy || enterBtn) ? el("div", { class: "row gap-2 wrap" }, [copyBtn, lanCopy, enterBtn].filter(Boolean)) : null,
      !isDegenerate(s.verifyWarning) ? el("div", { class: "notice warn" }, [ic("warn"), el("span", {}, esc(s.verifyWarning))]) : null,
      !isDegenerate(s.error) ? el("div", { class: "notice warn", role: "alert" }, [ic("warn"), el("span", {}, esc(s.error))]) : null,
    ].filter(Boolean));
  }

  /* 调试模式下始终可见的"进入聊天室"按钮。 */
  chatEntryButton() {
    const btn = el("button", { class: "btn btn-primary", id: "lab-enter-chat", type: "button" }, [ic("queue"), esc("进入聊天室")]);
    btn.addEventListener("click", () => this.openWizard());
    return btn;
  }

  /* 进入聊天室向导：两步（认证链接 → 昵称/密码）。 */
  openWizard() {
    const overlay = el("div", { class: "overlay", role: "dialog", "aria-modal": "true", "aria-labelledby": "wizard-title" });
    const resultNotice = el("div", { class: "notice", id: "wizard-result", hidden: true }, [ic("info"), el("span", {})]);
    const httpHint = el("div", { class: "notice warn", id: "wizard-http-hint", hidden: true }, [ic("warn"), el("span", {}, "HTTP 明文连接：仅限可信局域网/隧道使用")]);

    const step1 = el("div", { class: "stack-3", id: "wizard-step-1" }, [
      el("div", { class: "field" }, [
        el("label", { for: "wizard-url" }, "公网域名或局域网 IP（可含 http/https 与端口）"),
        el("input", { id: "wizard-url", class: "input", type: "text", placeholder: "例如 https://chat.example.com 或 192.168.1.5:8787", autocomplete: "off" }),
      ]),
      el("div", { class: "row gap-2" }, [
        el("button", { class: "btn", id: "wizard-confirm", type: "button" }, [ic("link"), esc("确认认证链接")]),
      ]),
    ]);

    const step2 = el("div", { class: "stack-3", id: "wizard-step-2", hidden: true }, [
      el("div", { class: "field" }, [
        el("label", { for: "wizard-name" }, "昵称"),
        el("input", { id: "wizard-name", class: "input", type: "text", maxlength: 24, autocomplete: "off" }),
      ]),
      el("div", { class: "field" }, [
        el("label", { for: "wizard-password" }, "密码"),
        el("input", { id: "wizard-password", class: "input", type: "password", autocomplete: "new-password" }),
      ]),
      el("div", { class: "notice" }, [ic("info"), el("span", {}, "昵称请勿与房主名称重复")]),
      el("div", { class: "row gap-2" }, [
        el("button", { class: "btn btn-primary", id: "wizard-enter", type: "button" }, [ic("queue"), esc("进入聊天室")]),
      ]),
    ]);

    overlay.append(el("div", { class: "modal" }, [
      el("div", { class: "modal-head" }, [
        el("h3", { id: "wizard-title" }, "进入聊天室"),
        el("button", { class: "icon-btn", type: "button", "aria-label": "关闭" }, [ic("x")]),
      ]),
      el("div", { class: "modal-body" }, [
        resultNotice,
        httpHint,
        step1,
        step2,
      ]),
      el("div", { class: "modal-foot" }, [
        el("button", { class: "btn btn-ghost", type: "button", id: "wizard-cancel" }, "取消"),
      ]),
    ]));

    const previousFocus = document.activeElement;
    document.body.append(overlay);
    const close = () => {
      overlay.remove();
      document.removeEventListener("keydown", onKey);
      previousFocus?.focus();
    };
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
    overlay.querySelector(".modal-head .icon-btn").addEventListener("click", close);
    $("#wizard-cancel", overlay)?.addEventListener("click", close);
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); close(); return; }
      if (e.key !== "Tab") return;
      const focusable = $$("button:not([disabled]), input:not([disabled])", overlay);
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
    };
    document.addEventListener("keydown", onKey);

    const setNotice = (kind, text) => {
      resultNotice.className = "notice " + kind;
      resultNotice.replaceChildren(ic(kind === "warn" ? "warn" : "info"), el("span", {}, text));
      resultNotice.hidden = false;
    };

    $("#wizard-confirm", overlay).addEventListener("click", async () => {
      const input = $("#wizard-url", overlay);
      const url = (input?.value || "").trim();
      if (!url) { toast("请填写公网域名或局域网 IP", "warn"); return; }
      if (!this.status || this.status.state !== "online") {
        httpHint.hidden = true;
        setNotice("warn", "当前没有运行中的房间，请先在实验室开启房间");
        return;
      }
      const btn = $("#wizard-confirm", overlay);
      if (btn.disabled) return;
      btn.disabled = true;
      try {
        const res = await roomBridge.setTunnel(url);
        if (res.ok) {
          setNotice("info", `认证成功：${res.url || url}`);
          httpHint.hidden = !String(url).startsWith("http://");
          step1.hidden = true;
          step2.hidden = false;
          $("#wizard-name", overlay)?.focus();
        } else {
          httpHint.hidden = true;
          setNotice("warn", res.error || "认证失败");
        }
      } finally {
        btn.disabled = false;
      }
    });

    $("#wizard-enter", overlay).addEventListener("click", async () => {
      const btn = $("#wizard-enter", overlay);
      if (btn.disabled) return;
      const nick = ($("#wizard-name", overlay)?.value || "").trim();
      const pwd = ($("#wizard-password", overlay)?.value || "").trim();
      btn.disabled = true;
      try {
        const res = await roomBridge.openChatroom(nick || null, pwd || null);
        if (res.ok) {
          close();
          toast("已打开聊天室", "ok");
        } else {
          toast(res.error || "进入聊天室失败", "err");
        }
      } finally {
        btn.disabled = false;
      }
    });

    $("#wizard-url", overlay)?.focus();
  }
}

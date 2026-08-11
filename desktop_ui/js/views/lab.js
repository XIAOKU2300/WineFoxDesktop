/* WineFox Desktop — 实验室：多人聊天室与房间管理（仅调试模式显示）。
 * 房主身份经 Shell 桥传递，本页通过面板代理访问房间网关（X-WineFox-Room-Host
 * 令牌由 Rust 注入，页面本身不接触令牌）。
 */
import { el, $, $$, ic, esc, toast, loadingState, copyText } from "../utils.js";
import { roomBridge, debugBridge } from "../utils.js";
import { store } from "../store.js";

export class LabView {
  constructor() {
    this.status = null;
    this.unsub = null;
    this.autoTunnelActive = false;
  }

  mount(container) {
    this.root = container;
    this.render();
    void this.refresh();
  }

  deactivate() {
    this.unsub?.();
    this.unsub = null;
  }

  activate() {
    if (!this.unsub) {
      this.unsub = roomBridge.onState((state) => {
        this.status = state;
        this.renderStatus();
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
    if (!this.status?.state || this.status.state === "stopped") {
      box.replaceChildren(el("div", { class: "empty" }, [
        ic("model"),
        el("div", { class: "empty-title" }, "暂无房间目录"),
        el("div", { class: "empty-desc" }, "开启房间后可在此查看可用的模型、工作流与 LoRA。"),
      ]));
      return;
    }
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
    } catch {
      box.replaceChildren(el("div", { class: "empty" }, [
        ic("warn"),
        el("div", { class: "empty-desc" }, "暂时无法读取房间目录。"),
      ]));
    }
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
            el("p", { class: "lead" }, "将 Cloudflare Tunnel 或 FRP 的公网 HTTPS 地址填入下方；WineFox 会校验地址后再启用。"),
            el("div", { class: "row gap-2" }, [
              el("div", { class: "grow" }, [
                el("input", { class: "input", id: "lab-tunnel-url", type: "url", placeholder: "https://你的公网地址.example", autocomplete: "off" }),
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
    try {
      const res = await roomBridge.start(settings);
      if (!res.ok) {
        if (res.error === "DEBUG_MODE_REQUIRED") toast("需要先在左上角 Logo 连续点击 10 次解锁调试模式", "err");
        else toast(res.error || "开启房间失败", "err");
      }
    } finally {
      if (start) { start.disabled = false; start.removeAttribute("aria-busy"); }
    }
  }

  async stopRoom() {
    const res = await roomBridge.stop();
    if (!res.ok) toast(res.error || "关闭房间失败", "err");
  }

  async setTunnel() {
    const url = ($("#lab-tunnel-url")?.value || "").trim();
    if (!url) { toast("请输入公网 HTTPS 地址", "err"); return; }
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

    if (debugChip) {
      const enabled = store.get("debugEnabled");
      debugChip.hidden = !enabled;
    }

    if (!s || !s.state || s.state === "stopped" || s.state === "") {
      box.replaceChildren(el("div", { class: "empty" }, [
        ic("queue"),
        el("div", { class: "empty-desc" }, "当前没有运行中的房间。"),
      ]));
      if (stop) stop.hidden = true;
      if (tunnelHint) tunnelHint.textContent = "未配置公网地址。";
      return;
    }

    const open = ["online", "starting"].includes(s.state);
    if (stop) stop.hidden = !open;
    const autoStop = $("#lab-tunnel-auto-stop", this.root);
    if (autoStop) autoStop.hidden = !(open && this.autoTunnelActive);
    if (tunnelHint) {
      tunnelHint.textContent = s.tunnelUrl
        ? `公网地址：${s.tunnelUrl}（已连接：${s.tunnelConnected ? "是" : "否"}）`
        : "未配置公网地址。";
    }

    const rows = [
      ["状态", s.state === "online" ? "运行中" : s.state],
      s.name ? ["房间", s.name] : null,
      ["成员", `${s.members ?? 0} / ${s.maxMembers ?? 0}`],
      s.localUrl ? ["本地地址", s.localUrl] : null,
      s.lanUrl ? ["局域网地址", s.lanUrl] : null,
      s.tunnelUrl ? ["公网地址", s.tunnelUrl] : null,
    ].filter(Boolean);

    const inviteUrl = s.tunnelUrl || s.localUrl;
    const copyBtn = inviteUrl
      ? el("button", { class: "btn btn-sm", id: "lab-copy-url", type: "button" }, [ic("copy"), esc("复制邀请地址")])
      : null;
    if (copyBtn) {
      copyBtn.addEventListener("click", () => {
        if (inviteUrl) { void copyText(inviteUrl); toast("邀请地址已复制", "ok"); }
      });
    }
    const lanCopy = s.lanUrl
      ? el("button", { class: "btn btn-sm", id: "lab-copy-lan", type: "button" }, [ic("copy"), esc("复制局域网地址")])
      : null;
    if (lanCopy) {
      lanCopy.addEventListener("click", () => {
        void copyText(s.lanUrl);
        toast("局域网地址已复制", "ok");
      });
    }
    const enterBtn = s.state === "online"
      ? el("button", { class: "btn btn-primary", id: "lab-enter-chat", type: "button" }, [ic("queue"), esc("进入聊天室")])
      : null;
    if (enterBtn) enterBtn.addEventListener("click", () => this.openWizard());

    box.replaceChildren(
      el("div", { class: "summary-table" }, rows.map(([k, v]) => el("div", { class: "summary-row" }, [
        el("span", { class: "summary-key" }, esc(k)),
        el("span", { class: "summary-value" }, esc(String(v))),
      ]))),
      (copyBtn || lanCopy || enterBtn) ? el("div", { class: "row gap-2 wrap" }, [copyBtn, lanCopy, enterBtn].filter(Boolean)) : null,
      s.verifyWarning ? el("div", { class: "notice warn" }, [ic("warn"), el("span", {}, esc(s.verifyWarning))]) : null,
      s.error ? el("div", { class: "notice warn", role: "alert" }, [ic("warn"), el("span", {}, esc(s.error))]) : null,
    );
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

/* WineFox Desktop — 设置页
 * 分组：ComfyUI / TLS / 认证 / AI 提供商 / 默认参数 / 界面。
 * 密钥只显示配置状态（GET /secrets），值仅写入不回读；token 不落 localStorage。
 */
import { el, $, $$, ic, esc, toast } from "../utils.js";
import { api } from "../api.js";
import { store } from "../store.js";

export class SettingsView {
  constructor() {
    this.section = "comfyui";
    this.settings = null;
    this.secrets = [];
    this.loaded = false;
  }

  async mount(container) {
    this.root = container;
    this.render();
    this.settings = store.get("settings");
    const cachedSecrets = store.get("secrets");
    store.set("settings", null);
    store.set("secrets", null);
    if (!this.settings) {
      try { this.settings = await api.settings(); } catch { this.settings = null; }
    }
    if (cachedSecrets) this.secrets = cachedSecrets.items || [];
    else {
      try { this.secrets = (await api.secrets()).items || []; } catch { this.secrets = []; }
    }
    this.renderBody();
    this.loaded = true;
  }

  render() {
    const root = this.root;
    root.innerHTML = "";
    root.append(
      el("div", { class: "view-head" }, [
        el("div", {}, [
          el("h1", { class: "h1" }, "设置"),
          el("div", { class: "lead" }, "连接与默认行为。密钥只显示配置状态，不显示内容。"),
        ]),
      ]),
      el("div", { class: "settings-layout" }, [
        el("nav", { class: "settings-nav", "aria-label": "设置分类" }, [
          ["comfyui", "ComfyUI 连接"], ["tls", "TLS"], ["auth", "认证与密钥"], ["ai_provider", "AI 提供商"], ["defaults", "默认参数"], ["ui", "界面"],
        ].map(([id, label]) => {
          const b = el("button", { type: "button", "aria-current": this.section === id ? "true" : "false", dataset: { sec: id } }, esc(label));
          b.addEventListener("click", () => { this.section = id; this.renderNav(); this.renderBody(); });
          return b;
        })),
        el("div", { class: "settings-main", id: "s-body" }, [el("div", { class: "load-more" }, "加载中…")]),
      ]),
    );
  }

  renderNav() {
    $$(".settings-nav button", this.root).forEach((b) => b.setAttribute("aria-current", b.dataset.sec === this.section ? "true" : "false"));
  }

  renderBody() {
    const body = $("#s-body");
    if (!body) return;
    if (!this.settings) {
      body.innerHTML = "";
      body.append(el("div", { class: "empty" }, [ic("warn"), el("div", { class: "empty-title" }, "无法读取设置"), el("div", { class: "empty-desc" }, "请检查后端服务是否可用。")]));
      return;
    }
    body.innerHTML = "";
    const content = this[`sec_${this.section}`]();
    body.append(...(Array.isArray(content) ? content : [content]));
    this.attachSaveButtons();
    if (!this._eventsBound) { this.bindEvents(); this._eventsBound = true; }
  }

  input(id, value, opts = {}) {
    return el("input", { id, class: "input", type: "text", value: value ?? "", ...opts });
  }

  select(id, options, value) {
    return el("select", { id, class: "select" }, options.map((o) => {
      const [v, label] = Array.isArray(o) ? o : [o, o];
      return el("option", { value: v, selected: String(value) === String(v) }, esc(label));
    }));
  }

  /* ---------- ComfyUI ---------- */
  sec_comfyui() {
    const s = this.settings?.comfyui || {};
    return [
      el("div", { class: "section-card" }, [
        el("div", { class: "sec-head" }, [el("h3", {}, "ComfyUI 连接"), el("span", { class: "badge ok" }, "配置状态正常")]),
        el("div", { class: "sec-body" }, [
          el("div", { class: "field" }, [
            el("label", { for: "s-srv" }, "服务地址"),
            this.input("s-srv", s.server_address, { placeholder: "http://127.0.0.1:8188", autocomplete: "url" }),
            el("span", { class: "hint" }, "本地默认 http://127.0.0.1:8188"),
          ]),
          el("div", { class: "form-row" }, [
            el("div", { class: "field" }, [
              el("label", { for: "s-timeout" }, "连接超时（秒）"),
              this.input("s-timeout", s.connect_timeout ?? 30, { type: "number", min: "3" }),
            ]),
            el("div", { class: "field" }, [
              el("label", { for: "s-readtimeout" }, "读取超时（秒）"),
              this.input("s-readtimeout", s.read_timeout ?? 300, { type: "number", min: "10" }),
            ]),
          ]),
          el("button", { class: "btn btn-sm", id: "s-test", type: "button" }, [ic("refresh"), esc("测试连接")]),
        ]),
      ]),
    ];
  }

  /* ---------- TLS ---------- */
  sec_tls() {
    const t = this.settings?.tls || {};
    return [
      el("div", { class: "section-card" }, [
        el("div", { class: "sec-head" }, [el("h3", {}, "TLS 传输")]),
        el("div", { class: "sec-body" }, [
          el("label", { class: "check" }, [
            el("input", { type: "checkbox", id: "s-tls-on", checked: !!t.enabled }),
            el("span", { class: "box" }, [ic("check")]),
            el("span", {}, esc("启用 HTTPS 传输")),
          ]),
          el("div", { id: "s-tls-sub", class: "stack-3" }, [
            el("label", { class: "check" }, [
              el("input", { type: "checkbox", id: "s-tls-verify", checked: t.verify !== false }),
              el("span", { class: "box" }, [ic("check")]),
              el("span", {}, esc("校验服务器证书")),
            ]),
            el("label", { class: "check" }, [
              el("input", { type: "checkbox", id: "s-tls-ca", checked: !!t.ca_configured }),
              el("span", { class: "box" }, [ic("check")]),
              el("span", {}, esc("使用自定义 CA 证书")),
            ]),
          ]),
          el("div", { class: "hint" }, "自签名或内网证书通常需要关闭证书校验或配置自定义 CA。"),
        ]),
      ]),
    ];
  }

  /* ---------- 认证与密钥 ---------- */
  sec_auth() {
    const a = this.settings?.auth || {};
    return [
      el("div", { class: "section-card" }, [
        el("div", { class: "sec-head" }, [el("h3", {}, "认证方式")]),
        el("div", { class: "sec-body" }, [
          el("div", { class: "field" }, [
            el("label", { for: "s-amode" }, "认证模式"),
            this.select("s-amode", [["none", "无需认证"], ["bearer", "Bearer Token / API Key"], ["basic", "Basic 用户名密码"]], a.mode || "none"),
          ]),
        ]),
      ]),
      el("div", { class: "section-card" }, [
        el("div", { class: "sec-head" }, [el("h3", {}, "密钥状态")]),
        el("div", { class: "sec-body stack-2" }, this.secretRows()),
      ]),
    ];
  }

  secretRows(definitions = null) {
    const defaults = definitions || [
      { key: "comfyui_api_key", label: "ComfyUI API Key", desc: "用于反向代理 / 网关认证" },
      { key: "comfyui_client_cert", label: "客户端证书（可选）", desc: "mTLS 双向认证" },
      { key: "tls_ca_cert", label: "自定义 CA 证书", desc: "受信任的本地根证书" },
    ];
    const rows = defaults.map((d) => {
      const st = this.secrets.find((x) => x.key === d.key);
      const configured = !!st?.configured;
      return el("div", { class: "secret-row" }, [
        el("div", { class: "grow" }, [
          el("div", { class: "s-name" }, esc(d.label)),
          el("div", { class: "s-desc" }, esc(d.desc)),
        ]),
        configured
          ? el("span", { class: "badge ok" }, `已配置${st.updated_at ? " · " + esc(st.updated_at) : ""}`)
          : el("span", { class: "badge warn" }, "未配置"),
        configured
          ? el("button", { class: "btn btn-sm btn-danger", type: "button", "data-clear": d.key, "aria-label": `清除 ${d.label}` }, "清除")
          : el("button", { class: "btn btn-sm", type: "button", "data-set": d.key, "aria-label": `设置 ${d.label}` }, "设置"),
      ]);
    });
    const wrap = el("div", { class: "stack-2" }, rows);
    wrap.addEventListener("click", async (e) => {
      const setBtn = e.target.closest("[data-set]");
      const clearBtn = e.target.closest("[data-clear]");
      if (setBtn) this.setSecretDialog(setBtn.dataset.set);
      if (clearBtn) {
        try {
          await api.clearSecret(clearBtn.dataset.clear);
          this.secrets = (await api.secrets()).items || [];
          toast("已清除", "ok");
          this.renderBody();
        } catch (err) { toast("操作失败：" + err.message, "err"); }
      }
    });
    return wrap;
  }

  setSecretDialog(key) {
    const label = ({ comfyui_api_key: "ComfyUI API Key", ai_api_key: "AI API Key", comfyui_client_cert: "客户端证书", tls_ca_cert: "自定义 CA 证书" })[key] || key;
    const isApiKey = key === "comfyui_api_key" || key === "ai_api_key";
    const overlay = el("div", { class: "overlay", role: "dialog", "aria-modal": "true", "aria-labelledby": "sk-title", "aria-describedby": "sk-desc" });
    overlay.append(el("div", { class: "modal" }, [
      el("div", { class: "modal-head" }, [el("h3", { id: "sk-title" }, esc(`设置 ${label}`)), el("button", { class: "icon-btn", type: "button", "aria-label": "关闭" }, [ic("x")])]),
      el("div", { class: "modal-body" }, [
        el("p", { class: "text-sm text-muted", id: "sk-desc" }, "密钥仅写入后端本机存储，界面不会回显；不会存储到浏览器 localStorage。"),
        isApiKey
          ? el("div", { class: "field" }, [
            el("label", { for: "sk-val" }, "API Key"),
            el("input", { id: "sk-val", class: "input", type: "password", autocomplete: "new-password" }),
          ])
          : el("div", { class: "field" }, [
            el("label", { for: "sk-val" }, "证书内容（PEM）"),
            el("textarea", { id: "sk-val", class: "textarea mono", rows: 6, placeholder: "-----BEGIN CERTIFICATE-----…" }),
          ]),
      ]),
      el("div", { class: "modal-foot" }, [
        el("button", { class: "btn", type: "button", id: "sk-cancel" }, "取消"),
        el("button", { class: "btn btn-primary", type: "button", id: "sk-save" }, "保存"),
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
    const onKey = (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        close();
        return;
      }
      if (e.key !== "Tab") return;
      const focusable = $$("button:not([disabled]), input:not([disabled]), textarea:not([disabled])", overlay);
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
    };
    document.addEventListener("keydown", onKey);
    $("#sk-cancel", overlay)?.addEventListener("click", close);
    $("#sk-save", overlay)?.addEventListener("click", async () => {
      const v = $("#sk-val", overlay).value.trim();
      if (!v) { toast("内容不能为空", "warn"); return; }
      try {
        await api.setSecret(key, v);
        this.secrets = (await api.secrets()).items || [];
        close();
        toast("已保存", "ok");
        this.renderBody();
      } catch (e) { toast("保存失败：" + e.message, "err"); }
    });
    $("#sk-val", overlay)?.focus();
  }

  /* ---------- AI 提供商 ---------- */
  sec_ai_provider() {
    const provider = this.settings?.ai_provider || {};
    return [
      el("div", { class: "section-card" }, [
        el("div", { class: "sec-head" }, [
          el("h3", {}, "OpenAI 兼容接口"),
          el("span", { class: "badge " + (provider.enabled ? "ok" : "") }, provider.enabled ? "已启用" : "未启用"),
        ]),
        el("div", { class: "sec-body" }, [
          el("label", { class: "check" }, [
            el("input", { type: "checkbox", id: "s-ai-enabled", checked: !!provider.enabled }),
            el("span", { class: "box" }, [ic("check")]),
            el("span", {}, esc("启用 AI 提示词优化")),
          ]),
          el("div", { class: "field" }, [
            el("label", { for: "s-ai-base-url" }, "Base URL"),
            this.input("s-ai-base-url", provider.base_url || "", {
              type: "url", placeholder: "https://api.openai.com/v1", autocomplete: "url", spellcheck: "false",
            }),
            el("span", { class: "hint" }, "填写 OpenAI 兼容接口的完整 /v1 地址。"),
          ]),
          el("div", { class: "form-row" }, [
            el("div", { class: "field" }, [
              el("label", { for: "s-ai-model" }, "主模型"),
              this.input("s-ai-model", provider.model || "", { placeholder: "例如 gpt-4.1", autocomplete: "off", spellcheck: "false" }),
              el("span", { class: "hint" }, "用于提示词优化与主要文本任务。"),
            ]),
            el("div", { class: "field" }, [
              el("label", { for: "s-ai-utility-model" }, "轻量模型"),
              this.input("s-ai-utility-model", provider.utility_model || "", { placeholder: "例如 gpt-4.1-mini", autocomplete: "off", spellcheck: "false" }),
              el("span", { class: "hint" }, "用于快速分类和短文本任务。"),
            ]),
          ]),
          el("div", { class: "field" }, [
            el("label", { for: "s-ai-vision-model" }, "视觉模型"),
            this.input("s-ai-vision-model", provider.vision_model || "", { placeholder: "例如 gpt-4.1", autocomplete: "off", spellcheck: "false" }),
            el("span", { class: "hint" }, "用于理解图生图参考图；可与主模型相同。"),
          ]),
          el("div", { class: "divider" }),
          el("div", { class: "field" }, [
            el("label", { id: "s-ai-key-label" }, "API Key"),
            el("div", { "aria-labelledby": "s-ai-key-label" }, this.secretRows([
              { key: "ai_api_key", label: "AI API Key", desc: "仅保存在系统密钥存储中，不会回显" },
            ])),
          ]),
        ]),
      ]),
    ];
  }

  /* ---------- 默认参数 ---------- */
  sec_defaults() {
    const d = this.settings?.defaults || {};
    const cat = store.get("catalog");
    const families = cat?.families || [];
    const workflows = (cat?.workflows || []).filter((workflow) => workflow.mode !== "video");
    return [
      el("div", { class: "section-card" }, [
        el("div", { class: "sec-head" }, [el("h3", {}, "默认参数")]),
        el("div", { class: "sec-body" }, [
          el("div", { class: "form-row" }, [
            el("div", { class: "field" }, [
              el("label", { for: "sd-family" }, "默认模型族"),
              el("select", { id: "sd-family", class: "select" }, families.length
                ? families.map((f) => el("option", { value: f.id, selected: (d.family || "anima") === f.id }, esc(f.label)))
                : [el("option", { value: "anima", selected: (d.family || "anima") === "anima" }, "Anima"), el("option", { value: "illustrious", selected: d.family === "illustrious" }, "Illustrious")]),
            ]),
            el("div", { class: "field" }, [
              el("label", { for: "sd-workflow" }, "默认工作流"),
              el("select", { id: "sd-workflow", class: "select" }, [
                el("option", { value: "", selected: !d.workflow }, "自动选择"),
                ...workflows.map((workflow) => el("option", {
                  value: workflow.id,
                  selected: d.workflow === workflow.id || d.workflow === workflow.name,
                }, esc(`[${workflow.family || "通用"} / ${workflow.mode || "通用"}] ${workflow.label}`))),
              ]),
              el("span", { class: "hint" }, `${workflows.length} 个内置工作流已载入。`),
            ]),
          ]),
          el("div", { class: "notice info" }, [
            ic("info"),
            el("span", {}, "步数、CFG、采样器和调度器默认使用工作流内置值；仅在生成页显式开启高级覆盖时才会改写。"),
          ]),
          el("div", { class: "form-row" }, [
            el("div", { class: "field" }, [
              el("label", { for: "sd-w" }, "默认宽度"),
              this.input("sd-w", d.width ?? 832, { type: "number", min: "256", max: "2048" }),
            ]),
            el("div", { class: "field" }, [
              el("label", { for: "sd-h" }, "默认高度"),
              this.input("sd-h", d.height ?? 1216, { type: "number", min: "256", max: "2048" }),
            ]),
          ]),
        ]),
      ]),
    ];
  }

  /* ---------- 界面 ---------- */
  sec_ui() {
    const u = this.settings?.ui || {};
    return [
      el("div", { class: "section-card" }, [
        el("div", { class: "sec-head" }, [el("h3", {}, "界面")]),
        el("div", { class: "sec-body" }, [
          el("div", { class: "field" }, [
            el("label", {}, "主题"),
            el("div", { class: "row gap-2" }, [
              el("button", { class: "cat-chip", type: "button", id: "s-theme-light", "aria-pressed": store.get("theme") !== "dark" ? "true" : "false" }, "浅色"),
              el("button", { class: "cat-chip", type: "button", id: "s-theme-dark", "aria-pressed": store.get("theme") === "dark" ? "true" : "false" }, "深色"),
            ]),
          ]),
          el("label", { class: "check" }, [
            el("input", { type: "checkbox", id: "s-reduced", checked: u.reduced_motion ?? true }),
            el("span", { class: "box" }, [ic("check")]),
            el("span", {}, esc("跟随系统减少动画（prefers-reduced-motion）")),
          ]),
        ]),
      ]),
    ];
  }

  /* ---------- 事件（挂载时一次性绑定） ---------- */
  bindEvents() {
    const body = $("#s-body");
    if (!body) return;
    // TLS 开关联动子选项
    body.addEventListener("change", (e) => {
      if (e.target.id === "s-tls-on") {
        const sub = $("#s-tls-sub");
        if (sub) sub.hidden = !e.target.checked;
      }
    });
    body.addEventListener("click", (e) => {
      if (e.target.closest("#s-test")) {
        this.testConnection();
      }
      const t = e.target.closest("#s-theme-light") || e.target.closest("#s-theme-dark");
      if (t) {
        store.toggleTheme();
        $$("#s-body .cat-chip[aria-pressed]").forEach((c) => c.setAttribute("aria-pressed", "false"));
        t.setAttribute("aria-pressed", "true");
      }
    });
    // 保存按钮（委托）
    body.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-save-section]");
      if (btn) this.saveSection(btn.dataset.saveSection);
    });
  }

  attachSaveButtons() {
    const body = $("#s-body");
    if (!body) return;
    // 每个 section-card 底部追加保存按钮
    $$(".section-card", body).forEach((card) => {
      if (card.querySelector(".sec-save")) return;
      const btn = el("button", { class: "btn btn-primary sec-save", type: "button", "data-save-section": this.section, style: "align-self:flex-start" }, "保存");
      card.querySelector(".sec-body").append(btn);
    });
  }

  async testConnection() {
    try {
      const res = await api.probe();
      store.set("comfyui", res);
      toast(res.result === "ok" ? "连接正常" : "连接异常：" + (res.summary || ""), res.result === "ok" ? "ok" : "err");
    } catch (e) {
      toast("连接失败：" + e.message, "err");
    }
  }

  async saveSection(section) {
    const body = $("#s-body");
    const val = (id) => body.querySelector("#" + id)?.value;
    const checked = (id) => !!body.querySelector("#" + id)?.checked;
    const patch = {};
    if (section === "comfyui") {
      patch.comfyui = {
        server_address: val("s-srv")?.trim(),
        connect_timeout: parseInt(val("s-timeout"), 10) || undefined,
        read_timeout: parseInt(val("s-readtimeout"), 10) || undefined,
      };
    } else if (section === "tls") {
      patch.tls = { enabled: checked("s-tls-on"), verify: checked("s-tls-verify"), ca_configured: checked("s-tls-ca") };
    } else if (section === "auth") {
      patch.auth = { mode: val("s-amode") || "none" };
    } else if (section === "ai_provider") {
      const enabled = checked("s-ai-enabled");
      const baseUrl = val("s-ai-base-url")?.trim() || "";
      if (enabled && !baseUrl) {
        toast("启用提示词优化前，请填写 Base URL。", "warn");
        body.querySelector("#s-ai-base-url")?.focus();
        return;
      }
      if (baseUrl) {
        try {
          const parsed = new URL(baseUrl);
          if (parsed.protocol !== "http:" && parsed.protocol !== "https:") throw new Error("unsupported protocol");
        } catch {
          toast("Base URL 必须是有效的 HTTP 或 HTTPS 地址。", "warn");
          body.querySelector("#s-ai-base-url")?.focus();
          return;
        }
      }
      patch.ai_provider = {
        enabled,
        base_url: baseUrl,
        model: val("s-ai-model")?.trim() || "",
        utility_model: val("s-ai-utility-model")?.trim() || "",
        vision_model: val("s-ai-vision-model")?.trim() || "",
      };
    } else if (section === "defaults") {
      patch.defaults = {
        family: val("sd-family"),
        workflow: val("sd-workflow")?.trim() || null,
        width: parseInt(val("sd-w"), 10) || undefined,
        height: parseInt(val("sd-h"), 10) || undefined,
      };
    } else if (section === "ui") {
      patch.ui = { reduced_motion: checked("s-reduced") };
    }
    await api.saveSettings(patch);
    this.settings = await api.settings();
    store.set("settings", this.settings);
    toast("已保存", "ok");
  }
}

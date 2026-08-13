/* WineFox Desktop — 设置页
 * 分组：ComfyUI / TLS / 认证 / AI 提供商 / 默认参数 / 界面。
 * 密钥只显示配置状态（GET /secrets），值仅写入不回读；token 不落 localStorage。
 */
import { el, $, $$, ic, esc, toast, loadingState, updateBridge, fmtBytesSize, fmtSpeed } from "../utils.js";
import { api } from "../api.js";
import { store } from "../store.js";

export class SettingsView {
  constructor() {
    this.section = "comfyui";
    this.settings = null;
    this.secrets = [];
    this.loaded = false;
    this.sectionNodes = new Map();
    this.sectionToken = 0;
    this.loadToken = 0;
    this.saving = false;
    this.probing = false;
  }

  async mount(container) {
    this.root = container;
    this.render();
    const token = ++this.loadToken;
    const cachedSettings = store.get("settings");
    const cachedSecrets = store.get("secrets");
    const [settingsResult, secretsResult] = await Promise.allSettled([
      cachedSettings ? Promise.resolve(cachedSettings) : withDeadline((signal) => api.settings({ signal }), 6000, "读取设置超时"),
      cachedSecrets ? Promise.resolve(cachedSecrets) : withDeadline((signal) => api.secrets({ signal }), 6000, "读取密钥状态超时"),
    ]);
    if (token !== this.loadToken) return;
    this.settings = settingsResult.status === "fulfilled" ? settingsResult.value : null;
    this.secrets = secretsResult.status === "fulfilled" ? secretsResult.value?.items || [] : [];
    if (this.settings) store.set("settings", this.settings);
    if (secretsResult.status === "fulfilled") store.set("secrets", { items: this.secrets });
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
          ["comfyui", "ComfyUI 连接"], ["tls", "TLS"], ["auth", "认证与密钥"], ["ai_provider", "AI 提供商"], ["lora", "LoRA"], ["defaults", "默认参数"], ["ui", "界面"], ["about", "关于与更新"],
        ].map(([id, label]) => {
          const b = el("button", { type: "button", "aria-current": this.section === id ? "true" : "false", dataset: { sec: id } }, esc(label));
          b.addEventListener("click", () => this.activateSection(id));
          return b;
        })),
        el("div", { class: "settings-main", id: "s-body" }, [loadingState("正在读取设置")]),
      ]),
    );
    this.bindEvents();
    this._eventsBound = true;
  }

  renderNav() {
    $$(".settings-nav button", this.root).forEach((b) => b.setAttribute("aria-current", b.dataset.sec === this.section ? "true" : "false"));
  }

  activateSection(section) {
    if (section === this.section) return;
    this.section = section;
    this.renderNav();
    if (this.sectionNodes.has(section)) {
      this.renderBody();
      return;
    }
    const token = ++this.sectionToken;
    const body = $("#s-body", this.root);
    body?.replaceChildren(loadingState("正在整理设置", { compact: true }));
    requestAnimationFrame(() => {
      setTimeout(() => {
        if (token === this.sectionToken && section === this.section) this.renderBody();
      }, 0);
    });
  }

  renderBody() {
    const body = $("#s-body", this.root);
    if (!body) return;
    if (!this.settings) {
      body.replaceChildren(el("div", { class: "empty" }, [ic("warn"), el("div", { class: "empty-title" }, "无法读取设置"), el("div", { class: "empty-desc" }, "请检查后端服务是否可用。")]));
      return;
    }
    let nodes = this.sectionNodes.get(this.section);
    if (!nodes) {
      const content = this[`sec_${this.section}`]();
      // replaceChildren 收到 null 会渲染出文本 "null"，这里统一过滤掉空项。
      nodes = (Array.isArray(content) ? content : [content]).filter(Boolean);
      this.sectionNodes.set(this.section, nodes);
      body.replaceChildren(...nodes);
      this.attachSaveButtons();
      return;
    }
    body.replaceChildren(...nodes.filter(Boolean));
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

  /* ---------- LoRA 下载 ---------- */
  sec_lora() {
    const l = this.settings?.lora_download || {};
    return [
      el("div", { class: "section-card" }, [
        el("div", { class: "sec-head" }, [el("h3", {}, "LoRA 下载")]),
        el("div", { class: "sec-body" }, [
          el("div", { class: "field" }, [
            el("label", { for: "s-lora-mode" }, "下载模式"),
            this.select("s-lora-mode", [
              ["auto", "自动"],
              ["local", "本地下载"],
              ["comfyui_bridge", "ComfyUI Bridge"],
            ], l.mode || "auto"),
            el("span", { class: "hint" }, "auto：填了本地目录就本地下载，否则交给 ComfyUI 所在机器下载。"),
          ]),
          el("div", { class: "field" }, [
            el("label", { for: "s-lora-dir" }, "本地下载目录"),
            this.input("s-lora-dir", l.local_dir || "", { placeholder: "ComfyUI 的 models/loras 目录", autocomplete: "off", spellcheck: "false" }),
            el("span", { class: "hint" }, "必须指向 ComfyUI 实际读取的 models/loras；跨机器建议使用 ComfyUI Bridge，可留空。"),
          ]),
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
    wrap.addEventListener("click", (e) => {
      const setBtn = e.target.closest("[data-set]");
      const clearBtn = e.target.closest("[data-clear]");
      if (setBtn) this.setSecretDialog(setBtn.dataset.set);
      if (clearBtn && !clearBtn.disabled) {
        clearBtn.disabled = true;
        const key = clearBtn.dataset.clear;
        void (async () => {
          try {
            await withDeadline((signal) => api.clearSecret(key, { signal }), 6000, "清除密钥超时");
            this.setSecretState(key, false);
            toast("已清除", "ok");
          } catch (err) {
            clearBtn.disabled = false;
            toast("操作失败：" + err.message, "err");
          }
        })();
      }
    });
    return wrap;
  }

  setSecretState(key, configured) {
    const current = this.secrets.find((item) => item.key === key) || { key };
    this.secrets = this.secrets.filter((item) => item.key !== key);
    this.secrets.push({ ...current, key, configured, updated_at: configured ? current.updated_at : null });
    store.set("secrets", { items: this.secrets });

    for (const root of [this.root, ...[...this.sectionNodes.values()].flat()]) {
      if (!root) continue;
      const buttons = $$('button[data-set], button[data-clear]', root);
      for (const button of buttons) {
        if ((button.dataset.set || button.dataset.clear) !== key) continue;
        const row = button.closest(".secret-row");
        const badge = row?.querySelector(".badge");
        const label = row?.querySelector(".s-name")?.textContent || key;
        if (badge) {
          badge.className = `badge ${configured ? "ok" : "warn"}`;
          badge.textContent = configured ? "已配置" : "未配置";
        }
        delete button.dataset.set;
        delete button.dataset.clear;
        if (configured) button.dataset.clear = key;
        else button.dataset.set = key;
        button.className = configured ? "btn btn-sm btn-danger" : "btn btn-sm";
        button.textContent = configured ? "清除" : "设置";
        button.setAttribute("aria-label", `${configured ? "清除" : "设置"} ${label}`);
        button.disabled = false;
      }
    }
  }

  setSecretDialog(key) {
    const label = ({ comfyui_api_key: "ComfyUI API Key", ai_api_key: "提示词 AI API Key", vision_api_key: "视觉理解 AI API Key", comfyui_client_cert: "客户端证书", tls_ca_cert: "自定义 CA 证书" })[key] || key;
    const isApiKey = key === "comfyui_api_key" || key === "ai_api_key" || key === "vision_api_key";
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
      const save = $("#sk-save", overlay);
      if (save?.disabled) return;
      if (save) disabledButton(save, true, "保存中…");
      try {
        await withDeadline((signal) => api.setSecret(key, v, { signal }), 6000, "保存密钥超时");
        this.setSecretState(key, true);
        close();
        toast("已保存", "ok");
      } catch (e) {
        if (save) disabledButton(save, false, "保存");
        toast("保存失败：" + e.message, "err");
      }
    });
    $("#sk-val", overlay)?.focus();
  }

  /* ---------- AI 提供商 ---------- */
  sec_ai_provider() {
    const provider = this.settings?.ai_provider || {};
    const vision = this.settings?.vision_provider || {};
    const followsText = vision.follow_text_provider !== false;
    return [
      el("div", { class: "section-card" }, [
        el("div", { class: "sec-head" }, [
          el("h3", {}, "提示词 AI"),
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
            el("label", { for: "s-ai-timeout" }, "请求超时（秒）"),
            this.input("s-ai-timeout", provider.timeout_seconds ?? 60, { type: "number", min: "5", max: "180", step: "1" }),
          ]),
          el("div", { class: "divider" }),
          el("div", { class: "field" }, [
            el("label", { id: "s-ai-key-label" }, "API Key"),
            el("div", { "aria-labelledby": "s-ai-key-label" }, this.secretRows([
              { key: "ai_api_key", label: "提示词 AI API Key", desc: "仅用于提示词优化和文本任务" },
            ])),
          ]),
        ]),
      ]),
      el("div", { class: "section-card" }, [
        el("div", { class: "sec-head" }, [
          el("h3", {}, "视觉理解 AI"),
          el("span", { class: "badge " + (vision.enabled ? "ok" : "") }, vision.enabled ? "已启用" : "未启用"),
        ]),
        el("div", { class: "sec-body" }, [
          el("label", { class: "check" }, [
            el("input", { type: "checkbox", id: "s-vision-enabled", checked: !!vision.enabled }),
            el("span", { class: "box" }, [ic("check")]),
            el("span", {}, esc("启用参考图视觉理解")),
          ]),
          el("label", { class: "check" }, [
            el("input", { type: "checkbox", id: "s-vision-follow", checked: followsText }),
            el("span", { class: "box" }, [ic("check")]),
            el("span", {}, esc("跟随提示词 AI 的接口、模型和密钥")),
          ]),
          el("div", { class: "notice info" }, [
            ic("info", { width: "16", height: "16" }),
            el("span", { id: "s-vision-follow-note" }, followsText
              ? "当前沿用提示词 AI 的 Base URL、主模型和 API Key；视觉请求仍使用下方独立超时。"
              : "当前使用独立视觉接口和 vision_api_key，不会读取提示词 AI 的密钥。"),
          ]),
          el("div", { class: "field" }, [
            el("label", { for: "s-vision-base-url" }, "视觉 Base URL"),
            this.input("s-vision-base-url", vision.base_url || "", {
              type: "url", placeholder: "https://api.openai.com/v1", autocomplete: "url", spellcheck: "false", disabled: followsText,
            }),
          ]),
          el("div", { class: "form-row" }, [
            el("div", { class: "field" }, [
              el("label", { for: "s-vision-model" }, "视觉模型"),
              this.input("s-vision-model", vision.model || "", { placeholder: "例如 gpt-4.1", autocomplete: "off", spellcheck: "false", disabled: followsText }),
            ]),
            el("div", { class: "field" }, [
              el("label", { for: "s-vision-timeout" }, "请求超时（秒）"),
              this.input("s-vision-timeout", vision.timeout_seconds ?? 90, { type: "number", min: "5", max: "180", step: "1" }),
            ]),
          ]),
          el("div", { class: "divider" }),
          el("div", { class: "field" }, [
            el("label", { id: "s-vision-key-label" }, "独立 API Key"),
            el("div", { "aria-labelledby": "s-vision-key-label" }, this.secretRows([
              { key: "vision_api_key", label: "视觉理解 AI API Key", desc: "仅在关闭“跟随提示词 AI”时使用" },
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
            ic("info", { width: "16", height: "16" }),
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
    const body = $("#s-body", this.root);
    if (!body) return;
    // TLS 开关联动子选项
    body.addEventListener("change", (e) => {
      if (e.target.id === "s-tls-on") {
        const sub = $("#s-tls-sub");
        if (sub) sub.hidden = !e.target.checked;
      }
      if (e.target.id === "s-vision-follow") {
        ["s-vision-base-url", "s-vision-model"].forEach((id) => {
          const input = body.querySelector("#" + id);
          if (input) input.disabled = e.target.checked;
        });
        const note = body.querySelector("#s-vision-follow-note");
        if (note) note.textContent = e.target.checked
          ? "当前沿用提示词 AI 的 Base URL、主模型和 API Key；视觉请求仍使用下方独立超时。"
          : "当前使用独立视觉接口和 vision_api_key，不会读取提示词 AI 的密钥。";
      }
    });
    body.addEventListener("click", (e) => {
      if (e.target.closest("#s-test")) {
        this.testConnection();
      }
      const t = e.target.closest("#s-theme-light") || e.target.closest("#s-theme-dark");
      if (t) {
        const wantsDark = t.id === "s-theme-dark";
        if ((store.get("theme") === "dark") !== wantsDark) store.toggleTheme();
        $$(".cat-chip[aria-pressed]", body).forEach((c) => c.setAttribute("aria-pressed", "false"));
        t.setAttribute("aria-pressed", "true");
      }
    });
    // 保存按钮（委托）
    body.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-save-section]");
      if (btn) void this.saveSection(btn.dataset.saveSection, btn);
    });
  }

  attachSaveButtons() {
    const body = $("#s-body", this.root);
    if (!body) return;
    // 每个 section-card 底部追加保存按钮
    $$(".section-card", body).forEach((card) => {
      if (card.querySelector(".sec-save")) return;
      const btn = el("button", { class: "btn btn-primary sec-save", type: "button", "data-save-section": this.section, style: "align-self:flex-start" }, "保存");
      card.querySelector(".sec-body").append(btn);
    });
  }

  async testConnection() {
    if (this.probing) return;
    this.probing = true;
    const btn = $("#s-test", this.root);
    if (btn) disabledButton(btn, true, "检测中…");
    try {
      const res = await withDeadline((signal) => api.probe({ force: true }, { signal }), 12000, "连接检测超时，已取消本次等待");
      store.set("comfyui", res);
      toast(res.result === "ok" ? "连接正常" : "连接异常：" + (res.summary || ""), res.result === "ok" ? "ok" : "err");
    } catch (e) {
      toast("连接失败：" + e.message, "err");
    } finally {
      this.probing = false;
      if (btn) disabledButton(btn, false, "测试连接");
    }
  }

  async saveSection(section, button) {
    if (this.saving) return;
    const body = $("#s-body", this.root);
    if (!body) return;
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
        timeout_seconds: Math.max(5, Math.min(parseInt(val("s-ai-timeout"), 10) || 60, 180)),
      };
      const followText = checked("s-vision-follow");
      const visionBaseUrl = val("s-vision-base-url")?.trim() || "";
      if (!followText && checked("s-vision-enabled") && !visionBaseUrl) {
        toast("启用独立视觉理解前，请填写视觉 Base URL。", "warn");
        body.querySelector("#s-vision-base-url")?.focus();
        return;
      }
      if (!followText && visionBaseUrl) {
        try {
          const parsed = new URL(visionBaseUrl);
          if (parsed.protocol !== "http:" && parsed.protocol !== "https:") throw new Error("unsupported protocol");
        } catch {
          toast("视觉 Base URL 必须是有效的 HTTP 或 HTTPS 地址。", "warn");
          body.querySelector("#s-vision-base-url")?.focus();
          return;
        }
      }
      patch.vision_provider = {
        enabled: checked("s-vision-enabled"),
        follow_text_provider: followText,
        base_url: visionBaseUrl,
        model: val("s-vision-model")?.trim() || "",
        timeout_seconds: Math.max(5, Math.min(parseInt(val("s-vision-timeout"), 10) || 90, 180)),
      };
    } else if (section === "defaults") {
      patch.defaults = {
        family: val("sd-family"),
        workflow: val("sd-workflow")?.trim() || null,
        width: parseInt(val("sd-w"), 10) || undefined,
        height: parseInt(val("sd-h"), 10) || undefined,
      };
    } else if (section === "lora") {
      patch.lora_download = {
        mode: val("s-lora-mode") || "auto",
        local_dir: val("s-lora-dir")?.trim() || "",
      };
    } else if (section === "ui") {
      patch.ui = { reduced_motion: checked("s-reduced") };
    }
    this.saving = true;
    if (button) disabledButton(button, true, "保存中…");
    try {
      const updated = await withDeadline((signal) => api.saveSettings(patch, { signal }), 8000, "保存设置超时，已取消本次请求");
      this.settings = updated || { ...this.settings, ...patch };
      store.set("settings", this.settings);
      // Keep the currently visible controls untouched. Dropping only this
      // cached reference makes the normalized server values appear the next
      // time the section is opened without discarding drafts in other tabs.
      this.sectionNodes.delete(section);
      toast("已保存", "ok");
    } catch (error) {
      toast("保存失败：" + error.message, "err");
    } finally {
      this.saving = false;
      if (button) disabledButton(button, false, "保存");
    }
  }
  /* ---------- 关于与更新 ---------- */
  sec_about() {
    const nodes = [
      el("div", { class: "section-card" }, [
        el("div", { class: "sec-head" }, [el("h3", {}, "关于与更新"), el("span", { class: "badge neutral" }, "稳定版")]),
        el("div", { class: "sec-body stack-3" }, [
          el("div", { class: "row wrap gap-3" }, [
            el("div", { class: "pm-block" }, [el("span", { class: "pm-label" }, "当前版本"), el("span", { class: "pm-val mono", id: "up-version" }, "—")]),
            el("div", { class: "pm-block" }, [el("span", { class: "pm-label" }, "更新渠道"), el("span", { class: "pm-val" }, "稳定版")]),
            el("div", { class: "pm-block" }, [el("span", { class: "pm-label" }, "上次检查"), el("span", { class: "pm-val", id: "up-last-check" }, "—")]),
          ]),
          el("div", { class: "row gap-2" }, [
            el("button", { class: "btn btn-sm", type: "button", id: "up-check" }, [ic("refresh"), esc("检查更新")]),
          ]),
          el("div", { id: "up-status", class: "up-status" }, [loadingState("正在获取更新状态", { compact: true })]),
        ]),
      ]),
    ];
    this.bindAboutEvents();
    this._aboutUnsub = updateBridge.onState((state) => this.renderUpdateState(state));
    updateBridge.getState().then((res) => {
      if (res.ok) this.renderUpdateState(res.state);
      else this.renderUpdateState({ phase: "failed", error: res.error || "无法连接更新服务" });
    });
    return nodes;
  }

  bindAboutEvents() {
    $("#up-check", this.root)?.addEventListener("click", () => {
      updateBridge.check().then((res) => {
        if (!res.ok) this.renderUpdateState({ phase: "failed", error: res.error || "检查失败" });
      });
    });
  }

  renderUpdateState(state) {
    const box = $("#up-status", this.root);
    if (!box) return;
    const lc = $("#up-last-check", this.root);
    if (lc && state.lastChecked) lc.textContent = new Date(state.lastChecked).toLocaleString("zh-CN");
    const ver = $("#up-version", this.root);
    if (ver && state.currentVersion) ver.textContent = state.currentVersion;
    const busy = state.busyTasks > 0;
    const active = ["checking", "downloading", "verifying", "installing", "restarting"].includes(state.phase);
    const checkButton = $("#up-check", this.root);
    if (checkButton) checkButton.disabled = active;
    let node;
    switch (state.phase) {
      case "idle":
        node = el("p", { class: "text-sm text-muted" }, "尚未检查更新。");
        break;
      case "checking":
        node = loadingState("正在检查更新…", { compact: true });
        break;
      case "upToDate":
        node = el("p", { class: "up-ok" }, [ic("check"), el("span", {}, `已是最新版本 ${state.currentVersion || ""}`)]);
        break;
      case "available":
        node = el("div", { class: "up-available stack-2" }, [
          el("div", { class: "stack-1" }, [
            el("b", { class: "up-new" }, `发现新版本 ${state.newVersion || ""}`),
            el("span", { class: "text-xs text-muted" }, [
              esc(`当前 ${state.currentVersion || "—"} · ${state.pubDate ? "发布时间 " + new Date(state.pubDate).toLocaleString("zh-CN") : ""} · 安装包 ${fmtBytesSize(state.sizeBytes)}`),
            ]),
          ]),
          state.notes ? el("p", { class: "up-notes text-sm" }, esc(state.notes)) : null,
          el("div", { class: "row gap-2" }, [
            el("button", { class: "btn btn-primary btn-sm", type: "button", id: "up-install", disabled: state.canInstall === false }, [ic("download"), esc(busy ? "等待任务完成后更新" : "下载并安装")]),
            el("button", { class: "btn btn-sm", type: "button", id: "up-later" }, "稍后提醒"),
          ]),
          busy ? el("span", { class: "text-xs", style: "color:var(--warn)" }, "当前有生成任务，安装需等任务完成后再执行。") : null,
        ]);
        break;
      case "downloading": {
        const pct = state.totalBytes ? Math.min(100, Math.round((state.downloadedBytes / state.totalBytes) * 100)) : null;
        node = el("div", { class: "stack-2" }, [
          el("div", { class: "row-between gap-3" }, [
            el("span", { class: "text-sm" }, "正在下载更新…"),
            el("span", { class: "text-xs mono text-muted" }, esc(`${fmtBytesSize(state.downloadedBytes)} / ${fmtBytesSize(state.totalBytes)}${state.speedBps ? " · " + fmtSpeed(state.speedBps) : ""}`)),
          ]),
          el("div", { class: "progress" + (pct ? "" : " indeterminate") }, [el("i", { style: pct ? `width:${pct}%` : "" })]),
        ]);
        break;
      }
      case "verifying":
        node = loadingState("正在验证更新签名…", { compact: true });
        break;
      case "installing":
        node = loadingState("正在安装更新，请稍候…", { compact: true });
        break;
      case "restarting":
        node = loadingState("更新完成，正在重启…", { compact: true });
        break;
      case "failed":
        node = el("p", { class: "text-sm", style: "color:var(--danger)" }, [ic("warn"), esc(` ${state.error || "检查失败，请检查网络"}`)]);
        break;
      default:
        node = el("p", { class: "text-sm text-muted" }, "—");
    }
    box.replaceChildren(node);
    $("#up-install", box)?.addEventListener("click", () => {
      updateBridge.download().then((res) => {
        if (!res.ok) toast(res.error || "下载失败", "err");
      });
    });
    $("#up-later", box)?.addEventListener("click", () => {
      this.renderUpdateState({ phase: "idle" });
    });
  }

}

function withDeadline(run, ms, message) {
  const controller = new AbortController();
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      controller.abort();
      reject(new Error(message));
    }, ms);
    let promise;
    try {
      promise = Promise.resolve(run(controller.signal));
    } catch (error) {
      clearTimeout(timer);
      reject(error);
      return;
    }
    promise.then(
      (value) => { clearTimeout(timer); resolve(value); },
      (error) => { clearTimeout(timer); reject(error); },
    );
  });
}

function disabledButton(button, disabled, label) {
  if (!button.dataset.wfxIcon && button.querySelector("svg")) button.dataset.wfxIcon = "refresh";
  button.disabled = disabled;
  button.replaceChildren(...(!disabled && button.dataset.wfxIcon ? [ic(button.dataset.wfxIcon), label] : [label]));
}

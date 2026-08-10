/* WineFox Desktop — 首次运行向导
 * 步骤：欢迎 → ComfyUI → 认证/TLS → 连接检测 → Checkpoint/LoRA/节点 → 模型族/工作流 → 旧数据导入 → 完成
 * 数据即时保存（PUT /settings）；密钥只写不读回。
 */
import { el, $, $$, ic, esc, svgEl } from "../utils.js";
import { api } from "../api.js";
import { store } from "../store.js";

const BRAND_MARKUP = '<path d="M4 13c0-5 3.5-9 10-9s10 4 10 9c0 6.5-5 12-10 12S4 19.5 4 13Z" stroke-width="1.6"/><path d="M9.2 13.8c1.6 1.4 3.4 1.4 4.8.2 1.4 1.2 3.2 1.2 4.8-.2" stroke-width="1.5"/><path d="M13 16.4v1.6" stroke-width="1.5" stroke-linecap="round"/>';
const HERO_MARKUP = '<circle class="sun" cx="122" cy="24" r="13" stroke-width="1.3"/><path d="M18 86h114M18 74h70M26 96h96" stroke-width="1.2" stroke-linecap="round"/><path d="M30 62c10-16 30-20 44-6 14-14 34-10 44 6" stroke-width="1.4" stroke-linecap="round"/><path d="M34 50h56" stroke-width="1" stroke-linecap="round"/>';

function heroArt() {
  return el("div", { class: "hero-art" }, [
    svgEl("0 0 150 110", HERO_MARKUP, { class: "hero-line", "aria-hidden": "true" }),
  ]);
}

export class Wizard {
  constructor(onDone) {
    this.onDone = onDone;
    this.steps = [
      { id: "welcome", title: "欢迎" },
      { id: "comfyui", title: "ComfyUI" },
      { id: "auth", title: "认证 / TLS" },
      { id: "probe", title: "连接检测" },
      { id: "assets", title: "模型与节点" },
      { id: "family", title: "模型族 / 工作流" },
      { id: "migrate", title: "旧数据导入" },
      { id: "finish", title: "完成" },
    ];
    this.index = 0;
    this.data = {};
    this.probeResult = null;
    this.catalog = null;
    this.completed = false;
  }

  async start() {
    const root = $("#wizard-root");
    root.hidden = false;
    // 预取 catalog（失败不阻塞）
    try { this.catalog = await api.catalog(); } catch { this.catalog = null; }
    try { this.data.auth = await api.secrets(); } catch { this.data.auth = { items: [] }; }
    this.render();
  }

  render() {
    const root = $("#wizard-root");
    const step = this.steps[this.index];
    root.innerHTML = "";
    root.append(el("div", { class: "wizard" }, [
      el("div", { class: "wizard-top" }, [
        el("div", { class: "brand" }, [
          svgEl("0 0 28 28", BRAND_MARKUP, { class: "brand-mark", "aria-hidden": "true" }),
          el("div", { class: "w-title" }, "WineFox Desktop 首次设置"),
        ]),
        el("span", { class: "w-step" }, `第 ${this.index + 1} / ${this.steps.length} 步`),
      ]),
      el("div", { class: "wizard-body" }, [
        el("aside", { class: "wizard-rail", "aria-label": "向导步骤" }, this.steps.map((s, i) =>
          el("div", { class: "w-step" + (i === this.index ? " active" : i < this.index ? " done" : ""), role: "listitem" }, [
            el("span", { class: "w-num", "aria-hidden": "true" }, i < this.index ? "✓" : String(i + 1)),
            el("span", {}, s.title),
          ]))),
        el("div", { class: "wizard-main" }, [
          el("div", { class: `wizard-page wizard-page-${step.id}`, role: "group", "aria-label": step.title }, this[`page_${step.id}`]()),
          el("div", { class: "wizard-foot" }, this.foot()),
        ]),
      ]),
    ]));
    // 焦点管理：进入新步骤聚焦首个可交互元素
    const first = root.querySelector("input, select, button:not(.w-back)");
    first?.focus();
    this.afterMount?.(step.id);
  }

  foot() {
    const isFirst = this.index === 0;
    const isLast = this.index === this.steps.length - 1;
    const canGo = this.canNext();
    const back = el("button", { class: "btn w-back", type: "button" }, [ic("x")]);
    back.append(esc("返回"));
    if (isFirst) back.style.visibility = "hidden";
    back.addEventListener("click", () => { this.index--; this.render(); });
    const next = el("button", { class: "btn btn-primary btn-lg", type: "button", disabled: !canGo }, [
      esc(isLast ? "进入 WineFox" : "下一步"),
    ]);
    next.addEventListener("click", () => this.next());
    // 键盘：Ctrl+Enter 下一步
    const skip = isLast
      ? el("button", { class: "w-skip", type: "button" }, esc("重新检查"))
      : el("button", { class: "w-skip", type: "button" }, esc("跳过此步"));
    skip.addEventListener("click", () => {
      if (isLast) {
        // 回到连接检测步骤并重新探测
        this.probeResult = null;
        this.index = this.steps.findIndex((s) => s.id === "probe");
      } else {
        this.index++;
      }
      this.render();
    });
    return [back, skip, next];
  }

  canNext() {
    const id = this.steps[this.index].id;
    if (id === "comfyui") return !!(this.data.server_address || "").trim();
    if (id === "probe") return this.probeResult?.result === "ok";
    if (id === "assets") return true;
    if (id === "auth") return true;
    return true;
  }

  async next() {
    const id = this.steps[this.index].id;
    if (id === "comfyui") await this.saveComfyui();
    if (id === "auth") await this.saveAuth();
    if (id === "probe" && !this.probeResult) await this.runProbe();
    if (id === "finish") return this.finish();
    if (this.steps[this.index].id === "probe" && this.probeResult?.result !== "ok") return;
    this.index++;
    if (this.steps[this.index].id === "probe") this.render(); // probe 页进入时自动检测
    else this.render();
  }

  async saveComfyui() {
    try {
      await api.saveSettings({
        comfyui: {
          server_address: this.data.server_address.trim(),
          ssl_verify: !!this.data.ssl_verify,
        },
      });
    } catch (e) { /* 后端未就绪时静默，probe 会暴露问题 */ }
  }

  async saveAuth() {
    try {
      const patches = { auth: { mode: this.data.auth_mode || "none" }, tls: this.data.tls || {} };
      await api.saveSettings(patches);
      if (this.data.api_key) await api.setSecret("comfyui_api_key", this.data.api_key);
    } catch { /* 静默 */ }
  }

  async finish() {
    this.completed = true;
    try { await api.saveSettings({ wizard: { completed: true } }); } catch { /* 静默 */ }
    $("#wizard-root").hidden = true;
    store.set("wizardCompleted", true);
    this.onDone?.();
  }

  /* ---------------- 页面 ---------------- */

  page_welcome() {
    return [
      heroArt(),
      el("h1", { class: "h1" }, "欢迎使用 WineFox Desktop"),
      el("p", { class: "subtitle" },
        "将 ComfyUI 的力量带入桌面：文生图、图生图、LoRA、模型管理与完整历史。"),
      el("div", { class: "welcome-list", role: "list" }, [
        el("div", { class: "welcome-item", role: "listitem" }, [ic("gen"), el("div", { class: "welcome-item-copy" }, [
          el("strong", {}, "8 步完成基本配置"), el("span", {}, "约 2 分钟，随时可以返回修改"),
        ])]),
        el("div", { class: "welcome-item", role: "listitem" }, [ic("model"), el("div", { class: "welcome-item-copy" }, [
          el("strong", {}, "自动检查资源"), el("span", {}, "识别模型、LoRA 与自定义节点"),
        ])]),
        el("div", { class: "welcome-item", role: "listitem" }, [ic("diag"), el("div", { class: "welcome-item-copy" }, [
          el("strong", {}, "密钥只保存在本机"), el("span", {}, "界面只显示配置状态，不回显内容"),
        ])]),
      ]),
    ];
  }

  page_comfyui() {
    return [
      el("h2", { class: "h2" }, "连接 ComfyUI"),
      el("p", { class: "subtitle" }, "填写 ComfyUI 服务地址。支持 http:// 与 https://。"),
      el("div", { class: "card card-pad stack-4" }, [
        el("div", { class: "field" }, [
          el("label", { for: "w-srv" }, "ComfyUI 服务地址"),
          el("input", { id: "w-srv", class: "input", type: "text", placeholder: "http://127.0.0.1:8188", value: this.data.server_address || "", autocomplete: "url" }),
          el("span", { class: "hint" }, "本地默认为 http://127.0.0.1:8188，远程服务器请填写完整地址"),
        ]),
        el("label", { class: "check" }, [
          el("input", { type: "checkbox", checked: !!this.data.ssl_verify }),
          el("span", { class: "box" }, [ic("check")]),
          el("span", {}, esc("验证 TLS 证书（自签名证书请取消勾选）")),
        ]),
      ]),
    ];
  }

  page_auth() {
    const tls = this.data.tls || {};
    return [
      el("h2", { class: "h2" }, "认证与 TLS"),
      el("p", { class: "subtitle" }, "ComfyUI 反向代理常需要 API Key 认证；TLS 用于加密传输。"),
      el("div", { class: "card card-pad stack-4" }, [
        el("div", { class: "field" }, [
          el("label", { for: "w-amode" }, "认证方式"),
          el("select", { id: "w-amode", class: "select" }, [
            el("option", { value: "none", selected: (this.data.auth_mode || "none") === "none" }, "无需认证"),
            el("option", { value: "bearer", selected: this.data.auth_mode === "bearer" }, "Bearer Token / API Key"),
            el("option", { value: "basic", selected: this.data.auth_mode === "basic" }, "Basic 用户名密码"),
          ]),
        ]),
        el("div", { id: "w-key-wrap", class: "field", hidden: (this.data.auth_mode || "none") === "none" }, [
          el("label", { for: "w-key" }, "API Key（仅写入，不回显）"),
          el("input", { id: "w-key", class: "input", type: "password", placeholder: "输入后点击下一步即保存", autocomplete: "off" }),
          el("span", { class: "hint" }, this.keyHint()),
        ]),
        el("div", { class: "divider" }),
        el("label", { class: "check" }, [
          el("input", { type: "checkbox", id: "w-tls", checked: !!tls.enabled }),
          el("span", { class: "box" }, [ic("check")]),
          el("span", {}, esc("启用 TLS（HTTPS 传输）")),
        ]),
        el("div", { id: "w-tls-wrap", class: "stack-3", hidden: !tls.enabled }, [
          el("label", { class: "check" }, [
            el("input", { type: "checkbox", id: "w-tls-verify", checked: tls.verify !== false }),
            el("span", { class: "box" }, [ic("check")]),
            el("span", {}, esc("校验服务器证书")),
          ]),
          el("label", { class: "check" }, [
            el("input", { type: "checkbox", id: "w-tls-ca", checked: !!tls.ca_configured }),
            el("span", { class: "box" }, [ic("check")]),
            el("span", {}, esc("使用自定义 CA 证书（受信任的本地证书）")),
          ]),
        ]),
      ]),
    ];
  }

  keyHint() {
    const items = this.data.auth?.items || [];
    const found = items.find((i) => i.key === "comfyui_api_key");
    return found?.configured
      ? "已配置（上次设置时间 " + (found.updated_at || "—") + "）；留空保持不变"
      : "尚未配置";
  }

  page_probe() {
    return [
      el("h2", { class: "h2" }, "连接检测"),
      el("p", { class: "subtitle" }, `正在检测 ${esc(this.data.server_address || "")} 并检查所需自定义节点…`),
      el("div", { class: "probe-steps", id: "probe-steps" }, [
        el("div", { class: "probe-step running" }, [
          el("span", { class: "p-ico" }), el("span", { class: "p-label" }, "连接 ComfyUI 服务"),
        ]),
        el("div", { class: "probe-step" }, [
          el("span", { class: "p-ico" }), el("span", { class: "p-label" }, "读取对象信息 / 版本"),
        ]),
        el("div", { class: "probe-step" }, [
          el("span", { class: "p-ico" }), el("span", { class: "p-label" }, "检查自定义节点"),
        ]),
        el("div", { class: "probe-step" }, [
          el("span", { class: "p-ico" }), el("span", { class: "p-label" }, "汇总模型与节点可用性"),
        ]),
      ]),
    ];
  }

  afterMount(id) {
    if (id === "comfyui") {
      const srv = $("#w-srv");
      srv.addEventListener("input", () => {
        this.data.server_address = srv.value;
        const next = $(".wizard-foot .btn-primary");
        if (next) next.disabled = !srv.value.trim();
      });
      $$("#wizard-root input[type=checkbox]")[0]?.addEventListener("change", (e) => {
        this.data.ssl_verify = e.target.checked;
      });
    }
    if (id === "auth") {
      const mode = $("#w-amode");
      const keyWrap = $("#w-key-wrap");
      mode.addEventListener("change", () => {
        this.data.auth_mode = mode.value;
        keyWrap.hidden = mode.value === "none";
      });
      $("#w-key")?.addEventListener("input", (e) => { this.data.api_key = e.target.value.trim(); });
      $("#w-tls")?.addEventListener("change", (e) => {
        $("#w-tls-wrap").hidden = !e.target.checked;
        this.data.tls = { ...(this.data.tls || {}), enabled: e.target.checked };
      });
      $("#w-tls-verify")?.addEventListener("change", (e) => { this.data.tls = { ...(this.data.tls || {}), verify: e.target.checked }; });
      $("#w-tls-ca")?.addEventListener("change", (e) => { this.data.tls = { ...(this.data.tls || {}), ca_configured: e.target.checked }; });
    }
    if (id === "probe" && !this.probeResult) {
      // 自动开始检测
      setTimeout(() => this.runProbe(), 120);
    }
  }

  setProbeStep(i, status, detail) {
    const steps = $$("#probe-steps .probe-step");
    const s = steps[i];
    if (!s) return;
    s.className = "probe-step " + status;
    let d = s.querySelector(".p-detail");
    if (!d) { d = el("div", { class: "p-detail" }); s.append(d); }
    d.textContent = detail || "";
  }

  async runProbe() {
    const id = this.steps[this.index].id;
    if (id !== "probe") return;
    // 重置步骤显示
    for (let i = 0; i < 4; i++) this.setProbeStep(i, "", "");
    try {
      const res = await api.probe();
      (res.steps || []).forEach((s, i) => this.setProbeStep(i, s.status, s.detail));
      this.probeResult = res;
    } catch (e) {
      this.setProbeStep(0, "fail", e.message);
      this.setProbeStep(1, "fail", "未连接");
      this.probeResult = { result: "fail", summary: e.message };
    }
    const next = $(".wizard-foot .btn-primary");
    if (next) next.disabled = this.probeResult?.result !== "ok";
  }

  page_assets() {
    const probe = this.probeResult;
    const cat = this.catalog;
    const ck = cat?.checkpoints?.length ?? "—";
    const lr = cat?.loras?.length ?? "—";
    const missing = (cat?.nodes || []).filter((n) => !n.installed);
    return [
      el("h2", { class: "h2" }, "模型与节点"),
      el("p", { class: "subtitle" }, "以下是检测到的资源；缺失项可在后续「模型与 LoRA」页持续跟踪。"),
      el("div", { class: "summary-table card", style: "overflow:hidden" }, [
        el("table", { class: "summary-table" }, [
          el("tbody", {}, [
            el("tr", {}, [el("th", {}, "Checkpoint 主模型"), el("td", {}, esc(String(ck)), " 个可用")]),
            el("tr", {}, [el("th", {}, "LoRA"), el("td", {}, esc(String(lr)), " 个可用")]),
            el("tr", {}, [el("th", {}, "工作流"), el("td", {}, esc(String(cat?.workflows?.length ?? "—")), " 个可用")]),
            el("tr", {}, [
              el("th", {}, "自定义节点"),
              el("td", {}, missing.length
                ? [el("span", { class: "badge danger" }, esc(`${missing.length} 个缺失`)), el("span", { class: "text-sm text-muted", style: "margin-left:8px" }, esc(missing.map((n) => n.name).join("、")))]
                : el("span", { class: "badge ok" }, "全部就绪")),
            ]),
          ]),
        ]),
      ]),
    ];
  }

  page_family() {
    const cat = this.catalog;
    const families = cat?.families?.length ? cat.families : [
      { id: "anima", label: "Anima", description: "经典 Anima 系列，通用二次元插画" },
      { id: "illustrious", label: "Illustrious", description: "Illustrious-XL 系列，多 LoRA 与精细控制" },
    ];
    const workflows = cat?.workflows || [];
    return [
      el("h2", { class: "h2" }, "模型族与工作流"),
      el("p", { class: "subtitle" }, "选择默认模型族与生成工作流，之后随时可在生成页切换。"),
      el("div", { class: "field" }, [
        el("label", {}, "默认模型族"),
        el("div", { class: "stack-2" }, families.map((f) => {
          const btn = el("button", { type: "button", class: "family-option", "aria-pressed": (this.data.family || "anima") === f.id }, [
            el("span", { class: "family-option-title" }, esc(f.label)),
            el("span", { class: "family-option-desc" }, esc(f.description || "")),
          ]);
          btn.addEventListener("click", () => { this.data.family = f.id; this.render(); });
          return btn;
        })),
      ]),
      el("div", { class: "field" }, [
        el("label", { for: "w-wf" }, "默认工作流"),
        el("select", { id: "w-wf", class: "select" }, workflows.length ? workflows.map((w) =>
          el("option", { value: w.id, selected: (this.data.workflow || "") === w.id }, `${w.label}${w.mode ? "（" + w.mode + "）" : ""}`))
          : [el("option", { value: "default" }, "默认（后端自动选择）")]),
      ]),
    ];
  }

  page_migrate() {
    return [
      el("h2", { class: "h2" }, "旧数据导入"),
      el("p", { class: "subtitle" }, "可导入旧版插件的数据（收藏、提示词笔记本、生成统计）。数据只会上传到本机后端。"),
      el("div", { class: "card card-pad stack-4" }, [
        el("div", { class: "migration-picker" }, [
          el("div", { class: "stack-1" }, [
            el("b", {}, "从旧版本导入"),
            el("span", { class: "text-sm text-muted" }, "选择旧版数据文件（JSON），合并到当前库"),
          ]),
          el("input", { id: "w-file", class: "input migration-file", type: "file", accept: ".json,application/json" }),
        ]),
        el("label", { class: "check" }, [
          el("input", { type: "checkbox", id: "w-keep", checked: true }),
          el("span", { class: "box" }, [ic("check")]),
          el("span", {}, esc("保留现有数据，仅追加不覆盖")),
        ]),
        el("div", { class: "hint" }, "找不到旧数据文件？可直接跳过，历史与收藏将从新开始记录。"),
      ]),
    ];
  }

  page_finish() {
    const cat = this.catalog;
    const missing = (cat?.nodes || []).filter((n) => !n.installed);
    return [
      heroArt(),
      el("h2", { class: "h2" }, "一切就绪"),
      el("p", { class: "subtitle" },
        this.probeResult?.result === "ok"
          ? "连接检测通过，可以开始生成第一张图了。"
          : "已跳过连接检测，可在「诊断」页随时重新检查。"),
      el("div", { class: "summary-table card", style: "overflow:hidden" }, [
        el("table", { class: "summary-table" }, [
          el("tbody", {}, [
            el("tr", {}, [el("th", {}, "ComfyUI"), el("td", {}, esc(this.data.server_address || "—"))]),
            el("tr", {}, [
              el("th", {}, "模型族"),
              el("td", {}, esc((this.data.family === "illustrious" ? "Illustrious" : "Anima"))),
            ]),
            el("tr", {}, [
              el("th", {}, "缺失节点"),
              el("td", {}, missing.length ? el("span", { class: "badge warn" }, esc(`${missing.length} 个待安装`)) : el("span", { class: "badge ok" }, "无")),
            ]),
          ]),
        ]),
      ]),
      el("p", { class: "text-sm text-muted" }, "提示：之后可在「设置」中随时修改以上内容。"),
    ];
  }
}

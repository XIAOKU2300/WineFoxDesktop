/* WineFox Desktop — 生成页
 * 布局：左（模型族/工作流/LoRA/队列）｜中（画布，拖放）｜右（参数）
 * 支持 txt2img / img2img；任务轮询 running 1s / queued 2s / 错误退避至 10s。
 */
import { el, $, $$, ic, esc, sanitizeName, sanitizeResourceName, fmtRelative, loadingState, copyText, STATUS_META, toast } from "../utils.js";
import { api } from "../api.js";
import { JobPoller } from "../api.js";
import { store } from "../store.js";

const STEPS_OPTS = [16, 20, 24, 28, 32, 40];
const CFG_OPTS = [3.5, 5, 6, 7, 8, 10, 12];
const SAMPLERS = ["euler", "euler_ancestral", "dpmpp_2m", "dpmpp_2m_sde", "dpmpp_3m_sde", "uni_pc"];
const SCHEDULERS = ["normal", "karras", "exponential", "sgm_uniform", "simple"];

export class GenerateView {
  constructor() {
    this.poller = new JobPoller(
      (job) => this.onJobUpdate(job),
      (job) => this.onJobDone(job),
      (err) => this.onJobError(err),
    );
    this.state = {
      mode: "txt2img",
      family: "anima",
      workflow: "",
      loras: new Map(), // id -> { label, strength }
      refImage: null,   // { url, width, height }
      job: null,
      result: null,     // 最近完成结果 { job, url }
      seed: -1,
      workflowOverrides: false,
      loraExpanded: false,
      loraQuery: "",
      loraVisible: 40,
    };
    store.set("genParams", this.state);
    this.root = null;
    this.loraObserver = null;
    this.loraFallbackTarget = null;
    this.loraFallbackHandler = null;
    this.loraPageFrame = null;
  }

  async mount(container) {
    this.root = container;
    if (!store.get("catalog")) {
      try { store.set("catalog", await api.catalog()); } catch { /* 离线可用 */ }
    }
    const cat = store.get("catalog") || { families: [], checkpoints: [], loras: [], workflows: [] };
    // 默认值
    if (!this.state.workflow) {
      const wf = cat.workflows.find((w) => w.mode === "txt2img");
      if (wf) this.state.workflow = wf.id;
    }
    this.render();
  }

  render() {
    const root = this.root;
    this.teardownLoraPagination();
    root.classList.add("generate-view");
    root.innerHTML = "";
    root.append(el("div", { class: "gen" }, [this.leftPanel(), this.centerPanel(), this.rightPanel()]));
    this.afterMount();
  }

  /* ============ 左栏：模型族 / 工作流 / LoRA ============ */
  leftPanel() {
    const cat = store.get("catalog") || {};
    const families = cat.families?.length ? cat.families : [{ id: "anima", label: "Anima" }, { id: "illustrious", label: "Illustrious" }];
    const workflows = (cat.workflows || []).filter((w) => !w.mode || w.mode === this.state.mode || w.mode === "any");
    const loras = this.familyLoras(cat.loras || []);

    return el("aside", { class: "gen-panel gen-left", "aria-label": "生成导航" }, [
      el("div", { class: "panel-head" }, [ic("model"), esc("模型与工作流")]),
      el("div", { class: "panel-body" }, [
        el("div", { class: "field" }, [
          el("label", { for: "g-family" }, "模型族"),
          el("select", { id: "g-family", class: "select" }, families.map((f) =>
            el("option", { value: f.id, selected: this.state.family === f.id }, esc(f.label)))),
        ]),
        el("div", { class: "field" }, [
          el("label", { for: "g-workflow" }, `工作流 · ${workflows.length} 个可用`),
          el("select", { id: "g-workflow", class: "select" }, workflows.length
            ? workflows.map((w) => el("option", { value: w.id, selected: this.state.workflow === w.id }, esc(`[${w.family || "通用"}] ${w.label}`)))
            : [el("option", { value: "" }, "默认（后端选择）")]),
        ]),
        el("div", { class: "divider" }),
        el("div", { class: "lora-heading" }, [
          el("button", {
            class: "lora-disclosure", id: "g-lora-toggle", type: "button",
            "aria-expanded": this.state.loraExpanded ? "true" : "false",
            "aria-controls": "g-lora-drawer",
            "aria-label": `${this.state.loraExpanded ? "收起" : "展开"} LoRA 列表`,
          }, [
            el("span", { class: "lora-chevron", "aria-hidden": "true" }),
            el("span", {}, "LoRA"),
            el("span", { class: "lora-total", id: "g-lora-total" }, this.loraCountText(loras.length)),
          ]),
          el("button", { class: "icon-btn", id: "g-refresh-loras", type: "button", title: "刷新 LoRA 列表", "aria-label": "刷新 LoRA 列表" }, [ic("refresh")]),
        ]),
        this.loraDrawer(loras),
        el("div", { class: "divider" }),
        el("div", { class: "stack-2" }, [
          el("label", { class: "text-sm", style: "font-weight:600" }, "队列"),
          el("div", { id: "g-queue", class: "text-xs text-muted" }, "—"),
        ]),
      ]),
    ]);
  }

  loraCountText(total) {
    const selected = this.state.loras.size;
    return selected ? `${selected} 已选 · ${total}` : String(total);
  }

  familyLoras(loras) {
    const family = this.state.family;
    return (loras || []).filter((lora) => {
      const value = String(lora?.family || "any").toLowerCase();
      return value === "any" || !value || value === family;
    });
  }

  normalizeLoraSearch(value) {
    return String(value ?? "")
      .normalize("NFKC")
      .toLocaleLowerCase("zh-CN")
      .replace(/[\\/_.-]+/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  loraSearchText(lora) {
    const values = [];
    const append = (value) => {
      if (Array.isArray(value)) value.forEach(append);
      else if (value != null && value !== "") values.push(String(value));
    };
    for (const key of ["id", "name", "label", "character", "aliases", "tags", "normalized_key", "normalizedKey", "trigger_words"]) {
      append(lora?.[key]);
    }
    return this.normalizeLoraSearch(values.join(" "));
  }

  filteredLoras(loras) {
    const query = this.normalizeLoraSearch(this.state.loraQuery);
    if (!query) return loras;
    const terms = query.split(" ").filter(Boolean);
    return loras.filter((lora) => {
      const haystack = this.loraSearchText(lora);
      const compactHaystack = haystack.replace(/\s+/g, "");
      return terms.every((term) => haystack.includes(term) || compactHaystack.includes(term.replace(/\s+/g, "")));
    });
  }

  loraDrawer(loras) {
    const filtered = this.filteredLoras(loras);
    return el("section", {
      class: "lora-drawer", id: "g-lora-drawer",
      hidden: !this.state.loraExpanded,
      "aria-label": "LoRA 选择器",
    }, [
      el("div", { class: "lora-search" }, [
        ic("search", { "aria-hidden": "true" }),
        el("input", {
          id: "g-lora-search", class: "input", type: "search",
          value: this.state.loraQuery, autocomplete: "off",
          placeholder: "搜索名称、角色或中文别名…",
          "aria-label": "搜索 LoRA 名称、角色或别名",
        }),
        el("button", {
          class: "lora-search-clear", id: "g-lora-search-clear", type: "button",
          hidden: !this.state.loraQuery, title: "清除搜索", "aria-label": "清除 LoRA 搜索",
        }, [ic("x")]),
      ]),
      el("div", { class: "lora-result-meta", id: "g-lora-result-meta", role: "status", "aria-live": "polite" },
        this.loraResultLabel(loras.length, filtered.length)),
      el("div", {
        class: "lora-list", id: "g-lora-list", role: "list",
        tabindex: "0", "aria-label": "LoRA 列表",
      }, this.loraListContent(loras, filtered)),
    ]);
  }

  loraResultLabel(total, filtered) {
    if (!total) return "尚未检测到 LoRA";
    return this.state.loraQuery ? `匹配 ${filtered} / ${total} 项` : `${total} 项 · 可搜索中文别名`;
  }

  loraListContent(loras, filtered = this.filteredLoras(loras)) {
    if (!loras.length) {
      return [el("p", { class: "lora-empty text-xs text-muted" }, "未检测到 LoRA（可在「模型与 LoRA」页查看状态）")];
    }
    if (!filtered.length) {
      return [el("p", { class: "lora-empty text-xs text-muted" }, "没有匹配项，换一个名称、角色名或别名试试。")];
    }
    const visible = filtered.slice(0, this.state.loraVisible);
    const nodes = visible.map((lora) => this.loraChip(lora));
    if (visible.length < filtered.length) {
      nodes.push(el("div", { class: "lora-sentinel", id: "g-lora-sentinel" }, [
        loadingState("正在载入更多 LoRA", { compact: true }),
      ]));
    }
    return nodes;
  }

  renderLoraResults() {
    const list = $("#g-lora-list", this.root);
    if (!list) return;
    this.teardownLoraPagination();
    const loras = this.familyLoras(store.get("catalog")?.loras || []);
    const filtered = this.filteredLoras(loras);
    list.replaceChildren(...this.loraListContent(loras, filtered));
    const meta = $("#g-lora-result-meta", this.root);
    if (meta) meta.textContent = this.loraResultLabel(loras.length, filtered.length);
    const clear = $("#g-lora-search-clear", this.root);
    if (clear) clear.hidden = !this.state.loraQuery;
    this.updateLoraCount(loras.length);
    if (this.state.loraExpanded) this.setupLoraPagination();
  }

  updateLoraCount(total = this.familyLoras(store.get("catalog")?.loras || []).length) {
    const count = $("#g-lora-total", this.root);
    if (count) count.textContent = this.loraCountText(total);
  }

  setupLoraPagination() {
    this.teardownLoraPagination();
    const list = $("#g-lora-list", this.root);
    const sentinel = $("#g-lora-sentinel", this.root);
    if (!list || !sentinel || !this.state.loraExpanded) return;

    if (typeof IntersectionObserver === "function") {
      this.loraObserver = new IntersectionObserver((entries) => {
        if (entries.some((entry) => entry.isIntersecting)) this.loadMoreLoras();
      }, { root: list, rootMargin: "72px 0px", threshold: 0.01 });
      this.loraObserver.observe(sentinel);
      return;
    }

    this.loraFallbackTarget = list;
    this.loraFallbackHandler = () => {
      if (list.scrollHeight - list.scrollTop - list.clientHeight <= 72) this.loadMoreLoras();
    };
    list.addEventListener("scroll", this.loraFallbackHandler, { passive: true });
  }

  teardownLoraPagination() {
    this.loraObserver?.disconnect();
    this.loraObserver = null;
    if (this.loraFallbackTarget && this.loraFallbackHandler) {
      this.loraFallbackTarget.removeEventListener("scroll", this.loraFallbackHandler);
    }
    this.loraFallbackTarget = null;
    this.loraFallbackHandler = null;
  }

  loadMoreLoras() {
    if (this.loraPageFrame != null) return;
    this.loraObserver?.disconnect();
    this.loraPageFrame = requestAnimationFrame(() => {
      this.loraPageFrame = null;
      const loras = this.familyLoras(store.get("catalog")?.loras || []);
      const filtered = this.filteredLoras(loras);
      if (this.state.loraVisible >= filtered.length) return;
      const list = $("#g-lora-list", this.root);
      const sentinel = $("#g-lora-sentinel", this.root);
      if (!list || !sentinel) return;
      const start = this.state.loraVisible;
      const end = Math.min(start + 40, filtered.length);
      sentinel.before(...filtered.slice(start, end).map((lora) => this.loraChip(lora)));
      this.state.loraVisible = end;
      if (end >= filtered.length) {
        this.teardownLoraPagination();
        sentinel.remove();
      } else {
        this.setupLoraPagination();
      }
    });
  }

  loraChip(l) {
    const id = l.id ?? l.name;
    const label = sanitizeResourceName(l.name || l.label || id) || "未命名 LoRA";
    const active = this.state.loras.has(id);
    const aliases = [l.character, ...(Array.isArray(l.aliases) ? l.aliases : []), ...(Array.isArray(l.tags) ? l.tags : [])].filter(Boolean);
    const btn = el("button", {
      type: "button", class: "lora-chip",
      "aria-pressed": active ? "true" : "false",
      title: esc(aliases.length ? `${label} · ${aliases.join("、")}` : label),
    }, [
      el("span", { class: "l-name" }, esc(label)),
      el("span", { class: "l-strength" }, active ? Number(this.state.loras.get(id).strength).toFixed(1) : "—"),
    ]);
    btn.addEventListener("click", () => {
      if (this.state.loras.has(id)) this.state.loras.delete(id);
      else {
        const strength = Number(l.strength);
        this.state.loras.set(id, { label, strength: Number.isFinite(strength) ? strength : 0.8 });
      }
      const selected = this.state.loras.has(id);
      btn.setAttribute("aria-pressed", selected ? "true" : "false");
      btn.querySelector(".l-strength").textContent = selected ? this.state.loras.get(id).strength.toFixed(1) : "—";
      this.updateLoraCount();
    });
    const copy = el("button", {
      type: "button", class: "icon-btn lora-copy",
      title: "复制完整 LoRA 名称", "aria-label": `复制 ${label}`,
    }, [ic("copy")]);
    copy.addEventListener("click", async () => {
      try {
        await copyText(label);
        toast("LoRA 名称已复制", "ok");
      } catch {
        toast("无法访问剪贴板", "warn");
      }
    });
    return el("div", { class: "lora-chip-row", role: "listitem" }, [btn, copy]);
  }

  /* ============ 中栏：画布 ============ */
  centerPanel() {
    const s = this.state;
    const result = s.result;
    let content;
    if (s.job) {
      content = [
        el("div", { class: "canvas-placeholder" }, [
          ic("queue"),
          el("b", {}, s.job.status === "running" ? "正在生成…" : "排队中…"),
          el("span", {}, "生成期间可继续编辑参数"),
        ]),
        this.progressCard(),
      ];
    } else if (result) {
      content = [
        el("img", { class: "canvas-img", src: result.url, alt: "生成结果预览" }),
        this.actionsBar(),
      ];
    } else if (s.mode === "img2img" && !s.refImage) {
      content = [
        el("div", { class: "canvas-placeholder" }, [
          ic("upload"),
          el("b", {}, "拖入参考图，或点击选择"),
          el("span", {}, "支持 PNG / JPG / WebP，将作为图生图的起点"),
        ]),
      ];
    } else if (s.mode === "img2img" && s.refImage) {
      content = [
        el("img", { class: "canvas-img", src: s.refImage.url, alt: "参考图" }),
        this.actionsBar(),
      ];
    } else {
      content = [
        el("div", { class: "canvas-placeholder" }, [
          ic("image"),
          el("b", {}, "画布"),
          el("span", {}, "填写提示词后点击「开始生成」；结果会显示在这里并进入历史"),
        ]),
      ];
    }
    return el("section", { class: "gen-panel gen-center", "aria-label": "画布" }, [
      el("div", { class: "canvas-wrap" }, [
        el("div", { class: "canvas-area" + (s.mode === "img2img" ? "" : ""), id: "canvas-area", tabindex: "0", role: "img" }, content),
        el("div", { class: "gen-actions gen-submit-bar" }, [
          el("button", { class: "btn btn-primary", id: "g-submit", type: "button", disabled: !!s.job }, [
            s.mode === "img2img" ? ic("wand") : ic("play"),
            esc(s.job ? "生成中…" : "开始生成"),
          ]),
          el("button", { class: "btn btn-sm", id: "g-cancel", type: "button", hidden: !s.job }, "取消"),
          el("span", { class: "queue-info", id: "g-queue-mini" }, ""),
        ]),
      ]),
    ]);
  }

  progressCard() {
    const j = this.state.job;
    const meta = STATUS_META[j.status] || STATUS_META.queued;
    return el("div", { class: "gen-progress", role: "status", "aria-live": "polite" }, [
      el("div", { class: "gp-row" }, [
        el("span", { class: "gp-state" }, [el("span", { class: "spin" }), esc(meta.label)]),
        el("span", {}, esc(j.progress ? `${Math.round(j.progress * 100)}%` : "")),
      ]),
      el("div", { class: "progress" + (j.progress ? "" : " indeterminate") }, [el("i", { style: j.progress ? `width:${Math.round(j.progress * 100)}%` : "" })]),
    ]);
  }

  actionsBar() {
    const s = this.state;
    const isRef = s.mode === "img2img" && s.refImage && !s.result;
    const autoMatches = s.result?.job?.meta?.auto_lora?.matches || [];
    const autoLabel = autoMatches.map((match) => match.character || match.key).filter(Boolean).join("、");
    return el("div", { class: "gen-actions" }, [
      el("button", { class: "btn btn-sm", type: "button", id: "g-save", disabled: isRef }, [ic("download"), esc("保存")]),
      el("button", { class: "btn btn-sm", type: "button", id: "g-fav", disabled: isRef }, [ic("fav"), esc("收藏")]),
      el("button", { class: "btn btn-sm", type: "button", id: "g-copy", disabled: isRef }, [ic("copy"), esc("复制")]),
      el("button", { class: "btn btn-sm", type: "button", id: "g-regen", disabled: isRef }, [ic("refresh"), esc("重生成")]),
      isRef ? el("button", { class: "btn btn-sm btn-danger", type: "button", id: "g-clearref" }, "移除参考图") : null,
      el("span", {
        class: "queue-info",
        title: autoLabel ? `自动匹配 LoRA：${autoLabel}` : "",
      }, s.result ? `${autoLabel ? `LoRA ${autoLabel} · ` : ""}seed ${s.result.job.seed ?? "—"} · ${fmtRelative(s.result.job.created_at)}` : ""),
    ]);
  }

  /* ============ 右栏：参数 ============ */
  rightPanel() {
    const s = this.state;
    const p = s.params || {};
    return el("aside", { class: "gen-panel gen-right", "aria-label": "生成参数" }, [
      el("div", { class: "panel-head" }, [ic("gear"), esc("参数")]),
      el("div", { class: "panel-body" }, [
        el("div", { class: "mode-switch", role: "radiogroup", "aria-label": "生成模式", dataset: { mode: s.mode } }, [
          el("span", { class: "mode-switch-thumb", "aria-hidden": "true" }),
          el("button", {
            type: "button", role: "radio", "aria-checked": s.mode === "txt2img" ? "true" : "false",
            tabindex: s.mode === "txt2img" ? "0" : "-1", id: "g-mode-txt", dataset: { mode: "txt2img" },
          }, "文生图"),
          el("button", {
            type: "button", role: "radio", "aria-checked": s.mode === "img2img" ? "true" : "false",
            tabindex: s.mode === "img2img" ? "0" : "-1", id: "g-mode-img", dataset: { mode: "img2img" },
          }, "图生图"),
        ]),
        el("div", { class: "field" }, [
          el("label", { for: "g-prompt" }, "提示词"),
          el("textarea", { id: "g-prompt", class: "textarea", rows: 5, placeholder: "描述你想生成的画面…" }, p.prompt || ""),
        ]),
        el("div", { class: "field" }, [
          el("label", { for: "g-neg" }, "负面提示词"),
          el("textarea", { id: "g-neg", class: "textarea", rows: 3, placeholder: "不想出现的内容…" }, p.negative || ""),
        ]),
        el("div", { class: "field" }, [
          el("label", { for: "g-seed" }, "种子（-1 = 随机）"),
          el("div", { class: "num-group" }, [
            el("button", { type: "button", class: "num-btn", id: "g-seed-dec", "aria-label": "种子减一" }, "−"),
            el("input", { id: "g-seed", class: "input", type: "number", min: "-1", step: "1", value: s.seed }), 
            el("button", { type: "button", class: "num-btn", id: "g-seed-inc", "aria-label": "种子加一" }, "+"),
          ]),
        ]),
        el("div", { class: "form-row" }, [
          el("div", { class: "field" }, [
            el("label", { for: "g-w" }, "宽度"),
            el("select", { id: "g-w", class: "select" }, [512, 768, 832, 1024, 1280].map((v) =>
              el("option", { value: v, selected: (p.width || 832) === v }, String(v)))),
          ]),
          el("div", { class: "field" }, [
            el("label", { for: "g-h" }, "高度"),
            el("select", { id: "g-h", class: "select" }, [512, 768, 832, 1024, 1280].map((v) =>
              el("option", { value: v, selected: (p.height || 1216) === v }, String(v)))),
          ]),
        ]),
        el("div", { class: "workflow-override stack-3" }, [
          el("div", { class: "row-between gap-3" }, [
            el("div", { class: "stack-1" }, [
              el("b", { class: "text-sm" }, "覆盖工作流采样参数"),
              el("span", { class: "text-xs text-muted" }, "默认关闭，使用所选工作流中已经调好的参数。"),
            ]),
            el("label", { class: "switch" }, [
              el("input", { id: "g-override", type: "checkbox", checked: s.workflowOverrides, "aria-label": "覆盖工作流采样参数" }),
              el("span", { class: "track", "aria-hidden": "true" }),
            ]),
          ]),
          s.workflowOverrides ? el("div", { class: "stack-3 workflow-override-fields" }, [
            el("div", { class: "form-row" }, [
              el("div", { class: "field" }, [
                el("label", { for: "g-steps" }, "步数"),
                el("select", { id: "g-steps", class: "select" }, STEPS_OPTS.map((v) =>
                  el("option", { value: v, selected: (p.steps || 24) === v }, String(v)))),
              ]),
              el("div", { class: "field" }, [
                el("label", { for: "g-cfg" }, "CFG"),
                el("select", { id: "g-cfg", class: "select" }, CFG_OPTS.map((v) =>
                  el("option", { value: v, selected: (p.cfg ?? 7) === v }, String(v)))),
              ]),
            ]),
            el("div", { class: "form-row" }, [
              el("div", { class: "field" }, [
                el("label", { for: "g-sampler" }, "采样器"),
                el("select", { id: "g-sampler", class: "select" }, SAMPLERS.map((v) =>
                  el("option", { value: v, selected: (p.sampler || "euler_ancestral") === v }, esc(v)))),
              ]),
              el("div", { class: "field" }, [
                el("label", { for: "g-sched" }, "调度器"),
                el("select", { id: "g-sched", class: "select" }, SCHEDULERS.map((v) =>
                  el("option", { value: v, selected: (p.scheduler || "normal") === v }, esc(v)))),
              ]),
            ]),
          ]) : null,
        ]),
        el("div", { class: "divider" }),
        el("p", { class: "text-xs text-muted" }, "提示：Ctrl+Enter 快速生成；生成中可继续调整参数（下次生效）。"),
      ]),
    ]);
  }

  /* ============ 事件绑定（每次渲染后调用） ============ */
  afterMount() {
    const s = this.state;
    const g = (id) => $("#" + id, this.root);

    // 模式切换
    const modeButtons = [g("g-mode-txt"), g("g-mode-img")].filter(Boolean);
    modeButtons.forEach((button) => {
      button.addEventListener("click", (event) => this.setMode(button.dataset.mode, { restoreFocus: event.detail === 0 }));
      button.addEventListener("keydown", (event) => {
        const target = {
          ArrowLeft: "txt2img", ArrowUp: "txt2img", Home: "txt2img",
          ArrowRight: "img2img", ArrowDown: "img2img", End: "img2img",
        }[event.key];
        if (!target) return;
        event.preventDefault();
        this.setMode(target, { restoreFocus: true });
      });
    });

    // 家族 / 工作流
    g("g-family")?.addEventListener("change", (e) => {
      s.family = e.target.value;
      s.loraVisible = 40;
      const workflows = (store.get("catalog")?.workflows || []).filter((workflow) =>
        !workflow.mode || workflow.mode === s.mode || workflow.mode === "any");
      const familyWorkflows = workflows.filter((workflow) => !workflow.family || workflow.family === "any" || workflow.family === s.family);
      if (!familyWorkflows.some((workflow) => workflow.id === s.workflow)) s.workflow = familyWorkflows[0]?.id || workflows[0]?.id || "";
      this.render();
    });
    g("g-workflow")?.addEventListener("change", (e) => { s.workflow = e.target.value; });
    g("g-lora-toggle")?.addEventListener("click", (event) => {
      s.loraExpanded = !s.loraExpanded;
      event.currentTarget.setAttribute("aria-expanded", s.loraExpanded ? "true" : "false");
      event.currentTarget.setAttribute("aria-label", `${s.loraExpanded ? "收起" : "展开"} LoRA 列表`);
      const drawer = g("g-lora-drawer");
      if (drawer) drawer.hidden = !s.loraExpanded;
      if (s.loraExpanded) this.setupLoraPagination();
      else this.teardownLoraPagination();
    });
    g("g-lora-search")?.addEventListener("input", (event) => {
      s.loraQuery = event.target.value;
      s.loraVisible = 40;
      this.renderLoraResults();
    });
    g("g-lora-search-clear")?.addEventListener("click", () => {
      s.loraQuery = "";
      s.loraVisible = 40;
      const input = g("g-lora-search");
      if (input) input.value = "";
      this.renderLoraResults();
      input?.focus();
    });
    g("g-refresh-loras")?.addEventListener("click", async () => {
      try {
        store.set("catalog", await api.catalog());
        s.loraVisible = 40;
        this.render();
        toast("模型与 LoRA 目录已更新", "ok");
      } catch (error) {
        toast("目录刷新失败：" + error.message, "err");
      }
    });
    g("g-override")?.addEventListener("change", (e) => {
      s.workflowOverrides = e.target.checked;
      this.render();
    });

    // 参数
    g("g-prompt")?.addEventListener("input", (e) => { s.params = s.params || {}; s.params.prompt = e.target.value; });
    g("g-neg")?.addEventListener("input", (e) => { s.params = s.params || {}; s.params.negative = e.target.value; });
    g("g-seed")?.addEventListener("input", (e) => { s.seed = parseInt(e.target.value, 10) || -1; });
    g("g-seed-dec")?.addEventListener("click", () => { s.seed = (s.seed <= 0 ? 1 : s.seed) - 1; g("g-seed").value = s.seed; });
    g("g-seed-inc")?.addEventListener("click", () => { s.seed = s.seed < 0 ? 1 : s.seed + 1; g("g-seed").value = s.seed; });
    for (const id of ["g-w", "g-h", "g-steps", "g-cfg", "g-sampler", "g-sched"]) {
      g(id)?.addEventListener("change", (e) => {
        s.params = s.params || {};
        const key = { "g-w": "width", "g-h": "height", "g-steps": "steps", "g-cfg": "cfg", "g-sampler": "sampler", "g-sched": "scheduler" }[id];
        s.params[key] = id === "g-steps" || id === "g-w" || id === "g-h" ? parseInt(e.target.value, 10) : id === "g-cfg" ? parseFloat(e.target.value) : e.target.value;
      });
    }

    // 提交 / 取消
    g("g-submit")?.addEventListener("click", () => this.submit());
    g("g-cancel")?.addEventListener("click", () => this.cancel());

    // 结果操作
    g("g-save")?.addEventListener("click", () => this.saveResult());
    g("g-fav")?.addEventListener("click", () => this.favoriteResult());
    g("g-copy")?.addEventListener("click", () => this.copyResult());
    g("g-regen")?.addEventListener("click", () => { s.seed = -1; this.submit(); });
    g("g-clearref")?.addEventListener("click", () => { s.refImage = null; s.result = null; this.render(); });

    // 拖放（img2img 参考图）
    this.bindDrop();

    // Ctrl+Enter 快速生成
    g("g-prompt")?.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); this.submit(); }
    });
    if (s.loraExpanded) this.setupLoraPagination();
  }

  setMode(mode, { restoreFocus = false } = {}) {
    const s = this.state;
    if (!["txt2img", "img2img"].includes(mode)) return;
    if (s.mode !== mode) {
      s.mode = mode;
      if (mode === "txt2img") s.refImage = null;
      s.result = null;

      const workflows = (store.get("catalog")?.workflows || []).filter((workflow) =>
        !workflow.mode || workflow.mode === mode || workflow.mode === "any");
      if (!workflows.some((workflow) => workflow.id === s.workflow)) {
        s.workflow = workflows[0]?.id || "";
      }
      this.render();
    }
    if (restoreFocus) {
      requestAnimationFrame(() => {
        const id = mode === "img2img" ? "#g-mode-img" : "#g-mode-txt";
        $(id, this.root)?.focus();
      });
    }
  }

  bindDrop() {
    const area = $("#canvas-area", this.root);
    if (!area) return;
    const stop = (e) => { e.preventDefault(); e.stopPropagation(); };
    area.addEventListener("dragenter", stop);
    area.addEventListener("dragover", (e) => { stop(e); area.classList.add("dragover"); });
    area.addEventListener("dragleave", () => area.classList.remove("dragover"));
    area.addEventListener("drop", (e) => {
      stop(e);
      area.classList.remove("dragover");
      const file = e.dataTransfer?.files?.[0];
      if (file && /^image\//.test(file.type)) this.setRefImage(file);
    });
    area.addEventListener("click", () => {
      if (this.state.mode === "img2img" && !this.state.refImage) {
        const input = el("input", { type: "file", accept: "image/*", hidden: true });
        input.addEventListener("change", () => { const f = input.files?.[0]; if (f) this.setRefImage(f); });
        document.body.append(input);
        input.click();
        input.remove();
      }
    });
  }

  async setRefImage(file) {
    try {
      const fd = new FormData();
      fd.append("file", file, file.name || "ref.png");
      const res = await api.uploads(fd);
      this.state.refImage = { url: res.url, name: sanitizeName(res.name || file.name), width: res.width, height: res.height };
      this.state.result = null;
      this.render();
    } catch (e) {
      toast("上传失败：" + e.message, "err");
    }
  }

  /* ============ 任务 ============ */
  async submit() {
    const s = this.state;
    if (s.job) return;
    const p = s.params || {};
    if (!(p.prompt || "").trim() && s.mode === "txt2img") {
      toast("请先填写提示词", "warn");
      $("#g-prompt", this.root)?.focus();
      return;
    }
    const payload = {
      mode: s.mode,
      prompt: (p.prompt || "").trim(),
      negative_prompt: (p.negative || "").trim() || null,
      seed: s.seed,
      width: p.width || 832,
      height: p.height || 1216,
      family: s.family,
      workflow: s.workflow || null,
      loras: [...s.loras.entries()].map(([id, v]) => ({ id, strength: v.strength })),
      ref_image: s.refImage ? { url: s.refImage.url } : null,
    };
    if (s.workflowOverrides) {
      Object.assign(payload, {
        steps: p.steps || 24,
        cfg: p.cfg ?? 7,
        sampler: p.sampler || "euler_ancestral",
        scheduler: p.scheduler || "normal",
      });
    }
    // 保证 -1 随机时后端回传真实种子
    const btn = $("#g-submit", this.root);
    if (btn) btn.disabled = true;
    try {
      const job = await api.submitJob(payload);
      s.job = job;
      s.result = null;
      this.render();
      this.poller.watch(job.id);
    } catch (e) {
      toast("提交失败：" + e.message, "err");
      if (btn) btn.disabled = false;
    }
  }

  async cancel() {
    const s = this.state;
    if (!s.job) return;
    try { await api.cancelJob(s.job.id); } catch { /* 忽略 */ }
    this.poller.stop(s.job.id);
    s.job = null;
    this.render();
    toast("已取消任务", "ok");
  }

  onJobUpdate(job) {
    const s = this.state;
    s.job = job;
    const bar = $("#g-submit", this.root);
    if (bar) bar.disabled = !!job;
    const area = $(".gen-center .canvas-wrap");
    if (area && s.job) {
      const existing = area.querySelector(".gen-progress");
      const holder = area.querySelector(".canvas-placeholder");
      if (holder && !existing) area.prepend(this.progressCard());
      const prog = area.querySelector(".gen-progress");
      if (prog) {
        const pct = prog.querySelector(".gp-row span:last-child");
        const barEl = prog.querySelector(".progress i");
        if (pct) pct.textContent = job.progress ? `${Math.round(job.progress * 100)}%` : "";
        if (barEl) {
          barEl.style.width = job.progress ? `${Math.round(job.progress * 100)}%` : "";
          prog.querySelector(".progress").classList.toggle("indeterminate", !job.progress);
        }
        prog.querySelector(".gp-state").lastChild.textContent = (STATUS_META[job.status] || STATUS_META.queued).label;
      }
    }
    // 队列计数
    store.setJobTally(job.queue_position != null ? job.queue_position : 0, job.status === "running" ? 1 : 0);
  }

  onJobDone(job) {
    const s = this.state;
    s.job = null;
    store.setJobTally(0, 0);
    if (job.status === "completed" && job.image) {
      s.result = { job, url: job.image.url };
      s.seed = job.seed ?? s.seed;
      this.render();
      toast("生成完成", "ok");
    } else {
      toast(job.error || "任务未完成", job.status === "failed" ? "err" : "warn");
      this.render();
    }
  }

  onJobError(err) {
    toast("轮询失败：" + err.message, "warn");
  }

  /* ============ 结果操作 ============ */
  async saveResult() {
    const r = this.state.result;
    if (!r) return;
    try {
      const a = el("a", { href: r.url, download: `winefox_${r.job.seed ?? "img"}.png` });
      document.body.append(a); a.click(); a.remove();
    } catch { toast("保存失败", "err"); }
  }

  async favoriteResult() {
    const r = this.state.result;
    if (!r?.job?.id) return;
    try {
      await api.favoriteHistory(r.job.id);
      toast("已加入收藏", "ok");
    } catch (e) { toast("收藏失败：" + e.message, "err"); }
  }

  async copyResult() {
    const r = this.state.result;
    if (!r) return;
    try {
      const blob = await (await fetch(r.url)).blob();
      await navigator.clipboard.write([new ClipboardItem({ [blob.type || "image/png"]: blob })]);
      toast("已复制到剪贴板", "ok");
    } catch {
      toast("复制失败（浏览器不支持）", "warn");
    }
  }

  dispose() {
    this.teardownLoraPagination();
    if (this.loraPageFrame != null) cancelAnimationFrame(this.loraPageFrame);
    this.loraPageFrame = null;
    this.poller.dispose();
  }
}

/* WineFox Desktop — 生成页
 * 布局：左（模型族/工作流/LoRA/队列）｜中（画布，拖放）｜右（参数）
 * 支持 txt2img / img2img；任务轮询 running 1s / queued 2s / 错误退避至 10s。
 */
import { el, $, $$, ic, esc, sanitizeName, sanitizeResourceName, fmtRelative, loadingState, copyText, STATUS_META, toast, downloadUrl, saveImageDialog } from "../utils.js";
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
      // 默认模型族跟随设置（向导/设置页配置的 defaults.family），此前硬编码
      // anima 会与设置的 illustrious 脱节，还会连带选错初始工作流。
      family: store.get("settings")?.defaults?.family || "anima",
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
      autoLoraMatches: [], // 生成前自动匹配预览：提交时作为 auto_lora_confirmations
      autoLoraBusy: false,
      autoLoraPreviewReady: false,
    };
    store.set("genParams", this.state);
    this.root = null;
    this.loraObserver = null;
    this.loraFallbackTarget = null;
    this.loraFallbackHandler = null;
    this.loraPageFrame = null;
    this._autoPreviewTimer = null;
  }

  async mount(container) {
    this.root = container;
    if (!store.get("catalog")) {
      try { store.set("catalog", await api.catalog()); } catch { /* 离线可用 */ }
    }
    const cat = store.get("catalog") || { families: [], checkpoints: [], loras: [], workflows: [] };
    this.syncWorkflow(this.state.family, this.state.mode);
    this.render();
  }

  render() {
    const root = this.root;
    this.teardownLoraPagination();
    this._canvasZoomCleanup?.();
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
          el("button", { class: "icon-btn", id: "g-reset-lora", type: "button", title: "重置已选 LoRA", "aria-label": "重置已选 LoRA", disabled: !this.state.loras.size }, [ic("x")]),
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

  /* 工作流与「模型族 + 模式」保持一致。
   * 旧版三处各自为政（挂载/切模式/切族），任一场景漏掉族匹配时，
   * 状态里的 workflow id 就会指向另一族的工作流——而 <select> 在状态
   * 不匹配任何选项时会默认显示第一项，界面看着选上了、实际提交的
   * 是过期 id，生成直接失败，用户必须手动再点一次工作流才恢复。
   * 统一收敛到这里：优先当前族 + 当前模式的组合，其次任意族同模式。 */
  syncWorkflow(family = this.state.family, mode = this.state.mode) {
    const s = this.state;
    const workflows = (store.get("catalog")?.workflows || []).filter((w) =>
      !w.mode || w.mode === mode || w.mode === "any");
    const fits = (list) => list.some((w) => w.id === s.workflow);
    if (!fits(workflows)) {
      s.workflow = workflows[0]?.id || "";
      return s.workflow;
    }
    const familyWorkflows = workflows.filter((w) =>
      !w.family || w.family === "any" || w.family === family);
    if (!fits(familyWorkflows)) {
      s.workflow = familyWorkflows[0]?.id || s.workflow;
    }
    return s.workflow;
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
      return [el("p", { class: "lora-empty text-xs text-muted" }, "未检测到 LoRA（可在「LoRA」工作台查看状态）")];
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
    const reset = $("#g-reset-lora", this.root);
    if (reset) reset.disabled = !this.state.loras.size;
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
      // 仅支持单选：点击已选项取消，点击其他项替换当前选择
      const active = this.state.loras.has(id);
      this.state.loras.clear();
      if (!active) {
        const strength = Number(l.strength);
        this.state.loras.set(id, { label, strength: Number.isFinite(strength) ? strength : 0.8 });
      }
      this.renderLoraResults();
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
        el("div", { class: "gen-wait" }, [
          el("div", { class: "gen-wait-art", "aria-hidden": "true" }, [
            el("span", { class: "gw-halo" }),
            el("span", { class: "gw-orbit gw-orbit-1" }),
            el("span", { class: "gw-orbit gw-orbit-2" }),
            el("span", { class: "gw-core" }, [el("img", { src: "assets/app-logo.png", alt: "" })]),
          ]),
          el("b", {}, s.job.status === "running" ? "正在生成…" : "排队中…"),
          el("span", {}, s.job.status === "running" ? "墨色正在画布上汇聚" : "前方还有任务，稍候片刻"),
        ]),
        this.progressCard(),
      ];
      const dbg = this.promptDebugCard();
      if (dbg) content.push(dbg);
    } else if (result) {
      const img = el("img", { class: "canvas-img", src: result.url, alt: "生成结果预览" });
      img.addEventListener("click", () => {
        if (this.canvasZoom?.moved) { this.canvasZoom.moved = false; return; }
        this.previewResult();
      });
      content = [img];
    } else if (s.mode === "img2img" && !s.refImage) {
      content = [
        el("div", { class: "canvas-placeholder" }, [
          ic("upload"),
          el("b", {}, "拖入参考图，或点击选择"),
          el("span", {}, "支持 PNG / JPG / WebP，将作为图生图的起点"),
        ]),
      ];
    } else if (s.mode === "img2img" && s.refImage) {
      const ref = el("img", { class: "canvas-img", src: s.refImage.url, alt: "参考图" });
      ref.addEventListener("click", () => {
        if (this.canvasZoom?.moved) { this.canvasZoom.moved = false; return; }
        this.previewRefImage();
      });
      content = [ref];
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
        this.promptDebugCard(),
        this.autoLoraPanel(),
        el("div", { class: "gen-actions gen-submit-bar" }, [
          el("button", { class: "btn btn-primary", id: "g-submit", type: "button", disabled: !!s.job }, [
            s.mode === "img2img" ? ic("wand") : ic("play"),
            esc(s.job ? "生成中…" : "开始生成"),
          ]),
          el("button", { class: "btn btn-sm", id: "g-cancel", type: "button", hidden: !s.job }, "取消"),
          ...this.resultActions(),
          el("span", { class: "queue-info", id: "g-queue-mini" }, this.queueInfoText()),
        ]),
      ]),
    ]);
  }

  progressCard() {
    const j = this.state.job;
    const meta = STATUS_META[j.status] || STATUS_META.queued;
    return el("div", { class: "gen-progress", role: "status", "aria-live": "polite" }, [
      el("div", { class: "gp-row" }, [
        el("span", { class: "gp-state" }, [
          el("span", { class: "gp-dots", "aria-hidden": "true" }, [el("i"), el("i"), el("i")]),
          esc(meta.label),
        ]),
        el("span", { class: "gp-pct" }, esc(j.progress ? `${Math.round(j.progress * 100)}%` : "")),
      ]),
      el("div", { class: "progress" + (j.progress ? "" : " indeterminate") }, [el("i", { style: j.progress ? `width:${Math.round(j.progress * 100)}%` : "" })]),
    ]);
  }

  /* 调试模式（连点 Logo 10 次开启）下展示提示词处理全貌：
   * 原文 → 清理后 → 转译最终 prompt / 负面词，以及来源（llm/fallback）与告警。 */
  promptDebugCard() {
    if (!store.get("debugEnabled")) return null;
    const s = this.state;
    const dbg = s.job?.meta?.prompt_debug || s.result?.job?.meta?.prompt_debug;
    if (!dbg) return null;
    const rows = [
      ["原始输入", dbg.raw_prompt],
      ["清理后（去 seed/标记）", dbg.cleaned_prompt],
      ["最终正向 prompt", dbg.final_prompt],
      ["最终负面 prompt", dbg.negative_prompt],
      ["转译来源", `${dbg.source === "llm" ? "LLM 转译" : "未转译（原文直出）"} · ${dbg.family || "—"}`],
    ];
    if (Array.isArray(dbg.matches) && dbg.matches.length) {
      rows.push(["LoRA/角色匹配", dbg.matches.map((m) => `${m.kind}:${m.value}`).join("、")]);
    }
    if (Array.isArray(dbg.loras) && dbg.loras.length) {
      rows.push(["应用 LoRA", dbg.loras.map((l) => `${l.name}${l.strength != null ? ` @${Number(l.strength).toFixed(2)}` : ""}`).join("、")]);
    }
    if (Array.isArray(dbg.warnings) && dbg.warnings.length) {
      rows.push(["告警", dbg.warnings.join("；")]);
    }
    return el("div", { class: "gen-debug", id: "g-prompt-debug" }, [
      el("div", { class: "gen-debug-head" }, [
        el("b", {}, "提示词处理详情（调试）"),
        el("button", {
          class: "btn btn-sm", type: "button", title: "复制最终正向 prompt",
          onclick: () => { void copyText(dbg.final_prompt || "").then(() => toast("已复制最终 prompt", "ok")); },
        }, [ic("copy"), esc("复制")]),
      ]),
      ...rows.map(([label, value]) =>
        el("div", { class: "gen-debug-row" }, [
          el("span", { class: "gen-debug-label" }, esc(label)),
          el("span", { class: "gen-debug-val" }, esc(value || "—")),
        ])),
    ]);
  }

  /* 结果操作按钮：仅当画布里有图（生成结果或 img2img 参考图）时出现在“开始生成”旁 */
  resultActions() {
    const s = this.state;
    const isRef = s.mode === "img2img" && s.refImage && !s.result;
    if (!s.result && !isRef) return [];
    return [
      el("span", { class: "bar-sep", "aria-hidden": "true" }),
      el("button", { class: "btn btn-sm", type: "button", id: "g-regen", disabled: isRef, title: "恢复上次提示词并重新生成", "aria-label": "重新生成" }, [ic("refresh"), esc("重生成")]),
      el("button", { class: "btn btn-sm", type: "button", id: "g-zoom", disabled: isRef, title: "放大预览生成结果", "aria-label": "放大预览" }, [ic("eye"), esc("放大")]),
      el("button", { class: "btn btn-sm", type: "button", id: "g-save", disabled: isRef }, [ic("download"), esc("保存")]),
      el("button", { class: "btn btn-sm", type: "button", id: "g-fav", disabled: isRef }, [ic("fav"), esc("收藏")]),
      el("button", { class: "btn btn-sm", type: "button", id: "g-copy", disabled: isRef }, [ic("copy"), esc("复制")]),
      isRef ? el("button", { class: "btn btn-sm btn-danger", type: "button", id: "g-clearref" }, "移除参考图") : null,
    ];
  }

  queueInfoText() {
    const s = this.state;
    // 任务进行中优先展示实时队列/进度，结束后回落到最近结果的 seed 与时间。
    if (s.job) return this.queueStatusText();
    const autoMatches = s.result?.job?.meta?.auto_lora?.matches || [];
    const autoLabel = autoMatches.map((match) => match.character || match.key).filter(Boolean).join("、");
    return s.result ? `${autoLabel ? `LoRA ${autoLabel} · ` : ""}seed ${s.result.job.seed ?? "—"} · ${fmtRelative(s.result.job.created_at)}` : "";
  }

  /* 左栏「队列」与提交栏的实时状态。旧版只写了初始「—」，之后无人更新，
   * 排队位置与进度对用户完全不可见。 */
  queueStatusText() {
    const j = this.state.job;
    if (!j) return "队列空闲";
    if (j.status === "running") {
      const pct = Number(j.progress);
      return Number.isFinite(pct) && pct > 0 ? `生成中 · ${Math.min(100, Math.round(pct * 100))}%` : "生成中…";
    }
    if (j.status === "queued" || j.status === "recovering") {
      const pos = Number(j.queue_position);
      return Number.isFinite(pos) && pos > 1 ? `排队中 · 前方 ${pos - 1} 个任务` : "排队中";
    }
    return (STATUS_META[j.status] || STATUS_META.queued).label;
  }

  renderQueueStatus() {
    const queue = $("#g-queue", this.root);
    if (queue) queue.textContent = this.queueStatusText();
    const mini = $("#g-queue-mini", this.root);
    if (mini) mini.textContent = this.queueInfoText();
  }

  /* ============ 生成前自动匹配预览 + 用户移除 ============ */
  autoLoraPanel() {
    const s = this.state;
    let panel;
    if (s.autoLoraBusy) {
      panel = el("section", { class: "auto-lora-panel", id: "g-autolora-panel", "aria-live": "polite" }, [
        el("div", { class: "auto-lora-hint text-xs text-muted" }, "正在匹配提示词中的 LoRA…"),
      ]);
      return panel;
    }
    const matches = s.autoLoraMatches;
    if (!matches.length) {
      return el("section", { class: "auto-lora-panel", id: "g-autolora-panel", hidden: true, "aria-live": "polite" });
    }
    const rows = matches.map((m) => {
      const badge = m.source === "llm"
        ? el("span", { class: "badge llm-hit" }, "LLM 辅助命中")
        : el("span", { class: "badge rule-hit" }, "规则命中");
      const nsfw = m.nsfw ? el("span", { class: "badge warn" }, "敏感内容") : null;
      const score = Number(m.score);
      const scoreText = Number.isFinite(score) ? ` · 匹配度 ${Math.round(score * 100)}%` : "";
      const strength = Number.isFinite(Number(m.strength_model)) ? ` · 强度 ${Number(m.strength_model).toFixed(2)}` : "";
      const meta = [m.character, m.filename].filter(Boolean).join(" · ");
      const remove = el("button", {
        class: "icon-btn auto-lora-remove", type: "button",
        title: "移除该自动引用", "aria-label": `移除 ${m.name || m.character || "该 LoRA"}`,
      }, [ic("x")]);
      remove.dataset.name = String(m.name ?? m.key ?? m.id ?? "");
      return el("div", { class: "auto-lora-row" }, [
        el("div", { class: "grow" }, [
          el("div", { class: "row wrap gap-2" }, [
            el("b", { class: "auto-lora-name" }, esc(m.name || m.character || "未命名 LoRA")),
            badge,
            nsfw,
          ]),
          meta ? el("div", { class: "text-xs text-muted" }, esc(meta + scoreText + strength)) : null,
        ]),
        remove,
      ]);
    });
    panel = el("section", { class: "auto-lora-panel", id: "g-autolora-panel", "aria-live": "polite" }, [
      el("div", { class: "auto-lora-head" }, [
        el("span", { class: "hint" }, `将自动引用 ${rows.length} 个 LoRA，提交前可移除`),
        el("button", { class: "btn btn-sm", type: "button", id: "g-autolora-clear" }, "全部移除"),
      ]),
      ...rows,
    ]);
    panel.addEventListener("click", (event) => {
      const remove = event.target.closest(".auto-lora-remove");
      if (remove) { this.removeAutoLoraMatch(remove.dataset.name); return; }
      if (event.target.closest("#g-autolora-clear")) { this.clearAutoLoraMatches(); }
    });
    return panel;
  }

  refreshAutoLoraPanel() {
    const old = $("#g-autolora-panel", this.root);
    const next = this.autoLoraPanel();
    if (old && old.parentNode) old.replaceWith(next);
  }

  removeAutoLoraMatch(name) {
    const s = this.state;
    s.autoLoraMatches = s.autoLoraMatches.filter((m) => String(m.name ?? m.key ?? m.id) !== String(name));
    this.refreshAutoLoraPanel();
  }

  clearAutoLoraMatches() {
    this.state.autoLoraMatches = [];
    this.refreshAutoLoraPanel();
  }

  scheduleAutoLoraPreview() {
    const s = this.state;
    if (this._autoPreviewTimer != null) { clearTimeout(this._autoPreviewTimer); this._autoPreviewTimer = null; }
    const prompt = (s.params?.prompt || "").trim();
    const family = s.family;
    const seq = (s._autoPreviewSeq = (s._autoPreviewSeq || 0) + 1);
    s.autoLoraBusy = true;
    s.autoLoraPreviewReady = false;
    s.autoLoraMatches = [];
    this.refreshAutoLoraPanel();
    if (!prompt) {
      s.autoLoraBusy = false;
      s.autoLoraPreviewReady = false;
      this.refreshAutoLoraPanel();
      return;
    }
    this._autoPreviewTimer = setTimeout(async () => {
      this._autoPreviewTimer = null;
      try {
        const result = await api.loraMatchPreview({ prompt, family });
        if (seq !== s._autoPreviewSeq || !this.root?.isConnected) return;
        s.autoLoraBusy = false;
        s.autoLoraPreviewReady = true;
        s.autoLoraMatches = Array.isArray(result?.matches) ? result.matches : [];
        this.refreshAutoLoraPanel();
      } catch {
        if (seq !== s._autoPreviewSeq || !this.root?.isConnected) return;
        s.autoLoraBusy = false;
        s.autoLoraPreviewReady = false;
        s.autoLoraMatches = [];
        this.refreshAutoLoraPanel();
      }
    }, 450);
  }

  cancelAutoLoraPreview() {
    if (this._autoPreviewTimer != null) { clearTimeout(this._autoPreviewTimer); this._autoPreviewTimer = null; }
    this.state._autoPreviewSeq = (this.state._autoPreviewSeq || 0) + 1;
  }

  /* 「重生成」：把上次提交的提示词回填到输入框，交由用户修改后再生成 */
  restoreLastParams() {
    const s = this.state;
    const last = s.lastParams;
    if (!last) return;
    s.params = s.params || {};
    s.params.prompt = last.prompt || "";
    s.params.negative = last.negative_prompt || "";
    s.seed = -1;
    const prompt = $("#g-prompt", this.root);
    if (prompt) prompt.value = s.params.prompt;
    const neg = $("#g-neg", this.root);
    if (neg) neg.value = s.params.negative;
    const seed = $("#g-seed", this.root);
    if (seed) seed.value = "-1";
    prompt?.focus();
    prompt?.setSelectionRange?.(s.params.prompt.length, s.params.prompt.length);
    toast("已恢复上次提示词，修改后点击「开始生成」", "info");
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
      s.autoLoraMatches = [];
      s.autoLoraBusy = false;
      s.autoLoraPreviewReady = false;
      this.cancelAutoLoraPreview();
      // 换族后清掉不属于新族的已选 LoRA，避免提交跨族组合导致生成异常
      const valid = new Set(this.familyLoras(store.get("catalog")?.loras || [])
        .map((lora) => lora.id ?? lora.name));
      for (const id of [...s.loras.keys()]) {
        if (!valid.has(id)) s.loras.delete(id);
      }
      this.syncWorkflow();
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
    g("g-reset-lora")?.addEventListener("click", () => {
      if (!s.loras.size) return;
      s.loras.clear();
      this.renderLoraResults();
      toast("已清除 LoRA 选择", "ok");
    });
    g("g-refresh-loras")?.addEventListener("click", async () => {
      try {
        store.set("catalog", await api.catalog());
        s.loraVisible = 40;
        this.render();
        toast("模型目录与 LoRA 库已更新", "ok");
      } catch (error) {
        toast("目录刷新失败：" + error.message, "err");
      }
    });
    g("g-override")?.addEventListener("change", (e) => {
      s.workflowOverrides = e.target.checked;
      this.render();
    });

    // 参数
    g("g-prompt")?.addEventListener("input", (e) => {
      s.params = s.params || {};
      s.params.prompt = e.target.value;
      this.scheduleAutoLoraPreview();
    });
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
    g("g-zoom")?.addEventListener("click", () => this.previewResult());
    g("g-save")?.addEventListener("click", () => this.saveResult());
    g("g-fav")?.addEventListener("click", () => this.favoriteResult());
    g("g-copy")?.addEventListener("click", () => this.copyResult());
    g("g-regen")?.addEventListener("click", () => this.restoreLastParams());
    g("g-clearref")?.addEventListener("click", () => { s.refImage = null; s.result = null; this.render(); });

    // 拖放（img2img 参考图）
    this.bindDrop();

    // Ctrl+Enter 快速生成
    g("g-prompt")?.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); this.submit(); }
    });
    if (s.loraExpanded) this.setupLoraPagination();
    this.bindCanvasZoom();
    this.renderQueueStatus();
  }

  setMode(mode, { restoreFocus = false } = {}) {
    const s = this.state;
    if (!["txt2img", "img2img"].includes(mode)) return;
    if (s.mode !== mode) {
      s.mode = mode;
      if (mode === "txt2img") s.refImage = null;
      s.result = null;
      s.autoLoraMatches = [];
      s.autoLoraBusy = false;
      s.autoLoraPreviewReady = false;
      this.cancelAutoLoraPreview();
      this.syncWorkflow();
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
    this.cancelAutoLoraPreview();
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
    // 只有预览请求已成功完成时才发送确认数组。请求仍在进行或失败时省略
    // 该字段，让后端规则匹配正常兜底；成功预览后 [] 才表示用户明确全删。
    if (s.autoLoraPreviewReady && !s.autoLoraBusy) {
      payload.auto_lora_confirmations = s.autoLoraMatches.map((m) => ({
        name: m.name || m.key || m.filename,
        // 只传真实的档案强度；缺省交给后端按 profile/族默认处理，
        // 绝不能把匹配分数（>90）当强度传。
        strength: Number.isFinite(Number(m.strength_model)) && Number(m.strength_model) > 0 && Number(m.strength_model) <= 1.5
          ? Number(m.strength_model)
          : undefined,
      }));
    }
    if (s.workflowOverrides) {
      Object.assign(payload, {
        steps: p.steps || 24,
        cfg: p.cfg ?? 7,
        sampler: p.sampler || "euler_ancestral",
        scheduler: p.scheduler || "normal",
      });
    }
    // 记录本次提交的参数，供「重生成」回填
    s.lastParams = {
      prompt: payload.prompt,
      negative_prompt: payload.negative_prompt || "",
      seed: s.seed,
      width: payload.width,
      height: payload.height,
    };
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
        const pct = prog.querySelector(".gp-pct");
        const barEl = prog.querySelector(".progress > i");
        if (pct) pct.textContent = job.progress ? `${Math.round(job.progress * 100)}%` : "";
        if (barEl) {
          barEl.style.width = job.progress ? `${Math.round(job.progress * 100)}%` : "";
          prog.querySelector(".progress").classList.toggle("indeterminate", !job.progress);
        }
        prog.querySelector(".gp-state").lastChild.textContent = (STATUS_META[job.status] || STATUS_META.queued).label;
      }
      // 等待卡的主/副文案随轮询刷新，否则从「排队中」开始就永远显示排队。
      const wait = area.querySelector(".gen-wait");
      if (wait) {
        const running = job.status === "running";
        const heading = wait.querySelector("b");
        const subline = heading?.nextElementSibling;
        if (heading) heading.textContent = running ? "正在生成…" : "排队中…";
        if (subline) subline.textContent = running ? "墨色正在画布上汇聚" : "前方还有任务，稍候片刻";
      }
      // 调试模式：prompt 处理完成后（meta.prompt_debug 出现）即时插入详情卡。
      if (job.meta?.prompt_debug && !area.querySelector("#g-prompt-debug")) {
        const dbg = this.promptDebugCard();
        if (dbg) (area.querySelector(".canvas-area") || area).after(dbg);
      }
    }
    // 队列计数与左栏/提交栏实时状态
    store.setJobTally(job.queue_position != null ? job.queue_position : 0, job.status === "running" ? 1 : 0);
    this.renderQueueStatus();
  }

  onJobDone(job) {
    const s = this.state;
    s.job = null;
    store.setJobTally(0, 0);
    store.set("historyDirty", true); // 历史数据已变化，历史页下次进入时重新拉取
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
    const res = await saveImageDialog(r.url, `winefox_${r.job.seed ?? "img"}.png`);
    if (res.ok && res.downloaded) toast("已开始保存图片", "ok");
    else if (res.ok && res.path) toast("已保存到 " + res.path, "ok");
    else if (!res.ok && !res.cancelled) toast("保存失败，请重试", "err");
  }

  async favoriteResult() {
    const r = this.state.result;
    if (!r?.job?.id) return;
    const btn = $("#g-fav", this.root);
    try {
      await api.favoriteHistory(r.job.id);
      store.set("favoritesDirty", true);
      if (btn) {
        btn.classList.remove("fav-pop");
        void btn.offsetWidth;
        btn.classList.add("fav-pop");
        btn.replaceChildren(ic("favFill"), esc("收藏"));
        const ico = btn.querySelector("svg");
        if (ico) ico.style.color = "var(--danger)";
      }
      toast("已加入收藏", "ok");
    } catch (e) { toast("收藏失败：" + e.message, "err"); }
  }

  /* ============ 画布图片滚轮缩放 + 拖拽平移（仅当画布里有图） ============ */
  bindCanvasZoom() {
    const area = $("#canvas-area", this.root);
    if (!area) return;
    const img = area.querySelector(".canvas-img");
    if (!img) return;
    const z = { level: 1, tx: 0, ty: 0, moved: false };
    this.canvasZoom = z;
    // Keep the transform centered. A top-left origin amplifies tiny
    // coordinate errors and can hide the whole image at max zoom.
    img.style.transformOrigin = "center center";

    // getBoundingClientRect() already includes the current transform. Keep
    // the layout-space rectangle separate so repeated zooms do not apply the
    // translation twice (which previously could fling the image completely
    // outside the clipped canvas after one or two wheel events).
    const layoutRect = () => {
      const rect = img.getBoundingClientRect();
      const level = Math.max(0.001, z.level);
      const width = rect.width / level;
      const height = rect.height / level;
      const centerX = rect.left + rect.width / 2 - z.tx;
      const centerY = rect.top + rect.height / 2 - z.ty;
      return {
        left: centerX - width / 2,
        top: centerY - height / 2,
        width,
        height,
        centerX,
        centerY,
      };
    };
    const clampPan = (base) => {
      if (!base?.width || !base?.height) return;
      const areaRect = area.getBoundingClientRect();
      const width = base.width * z.level;
      const height = base.height * z.level;
      // Always leave a small visible edge so a zoomed image cannot vanish
      // behind .canvas-area { overflow: hidden } and become unrecoverable.
      const visible = Math.min(36, Math.max(12, Math.min(width, height)));
      const minTx = areaRect.left + visible - base.centerX - width / 2;
      const maxTx = areaRect.right - visible - base.centerX + width / 2;
      const minTy = areaRect.top + visible - base.centerY - height / 2;
      const maxTy = areaRect.bottom - visible - base.centerY + height / 2;
      z.tx = Math.min(Math.max(z.tx, minTx), maxTx);
      z.ty = Math.min(Math.max(z.ty, minTy), maxTy);
    };
    const apply = (base = layoutRect()) => {
      clampPan(base);
      img.style.transform = `translate(${z.tx}px, ${z.ty}px) scale(${z.level})`;
      let badge = area.querySelector(".zoom-badge");
      if (z.level === 1 && z.tx === 0 && z.ty === 0) {
        badge?.remove();
        return;
      }
      if (!badge) {
        badge = el("span", { class: "zoom-badge", role: "status" });
        area.append(badge);
      }
      badge.textContent = `×${z.level.toFixed(2).replace(/\.?0+$/, "")}`;
    };
    const onWheel = (e) => {
      e.preventDefault();
      const rect = img.getBoundingClientRect();
      const base = layoutRect();
      // Pointer coordinates are measured from the transformed rectangle;
      // subtracting tx/ty here a second time was the disappearing-image bug.
      const px = (e.clientX - (rect.left + rect.width / 2)) / z.level;
      const py = (e.clientY - (rect.top + rect.height / 2)) / z.level;
      const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
      const next = Math.min(6, Math.max(0.25, z.level * factor));
      if (next === z.level) return;
      z.level = next;
      z.tx = e.clientX - base.centerX - px * next;
      z.ty = e.clientY - base.centerY - py * next;
      apply(base);
    };
    let drag = null;
    const onDown = (e) => {
      if (e.button !== 0) return;
      drag = { x: e.clientX, y: e.clientY, tx: z.tx, ty: z.ty };
      z.moved = false;
      img.setPointerCapture?.(e.pointerId);
      img.classList.add("dragging");
    };
    const onMove = (e) => {
      if (!drag) return;
      const base = layoutRect();
      if (Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) > 3) z.moved = true;
      z.tx = drag.tx + (e.clientX - drag.x);
      z.ty = drag.ty + (e.clientY - drag.y);
      apply(base);
    };
    const onUp = (e) => {
      drag = null;
      if (e?.pointerId != null) img.releasePointerCapture?.(e.pointerId);
      img.classList.remove("dragging");
    };
    const onDbl = (e) => {
      e.preventDefault();
      const rect = img.getBoundingClientRect();
      const base = layoutRect();
      const px = (e.clientX - (rect.left + rect.width / 2)) / z.level;
      const py = (e.clientY - (rect.top + rect.height / 2)) / z.level;
      if (z.level !== 1) {
        z.level = 1; z.tx = 0; z.ty = 0;
      } else {
        z.level = 2;
        z.tx = e.clientX - base.centerX - px * z.level;
        z.ty = e.clientY - base.centerY - py * z.level;
      }
      apply(base);
    };
    area.addEventListener("wheel", onWheel, { passive: false });
    img.addEventListener("pointerdown", onDown);
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    img.addEventListener("dblclick", onDbl);
    this._canvasZoomCleanup = () => {
      area.removeEventListener("wheel", onWheel);
      img.removeEventListener("pointerdown", onDown);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      img.removeEventListener("dblclick", onDbl);
    };
  }

  /* ============ 参考图放大预览 ============ */
  previewRefImage() {
    const r = this.state.refImage;
    if (!r) return;
    const overlay = el("div", { class: "overlay preview-overlay", role: "dialog", "aria-modal": "true", "aria-label": "参考图预览" });
    const modal = el("div", { class: "modal" }, [
      el("div", { class: "modal-head" }, [
        el("h3", {}, "参考图"),
        el("button", { class: "icon-btn", type: "button", "aria-label": "关闭" }, [ic("x")]),
      ]),
      el("div", { class: "preview-body" }, [el("img", { src: r.url, alt: "参考图大图" })]),
    ]);
    overlay.append(modal);
    document.body.append(overlay);
    requestAnimationFrame(() => requestAnimationFrame(() => overlay.classList.add("open")));
    const onKey = (e) => { if (e.key === "Escape") close(); };
    const close = () => {
      overlay.classList.add("closing");
      document.removeEventListener("keydown", onKey);
      setTimeout(() => overlay.remove(), 180);
    };
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
    modal.querySelector(".modal-head .icon-btn").addEventListener("click", close);
    document.addEventListener("keydown", onKey);
    modal.querySelector(".modal-head .icon-btn").focus();
  }

  /* ============ 结果放大预览 ============ */
  previewResult() {
    const r = this.state.result;
    if (!r) return;
    const j = r.job || {};
    const m = j.meta || {};
    const val = (a, b) => (a != null && a !== "" ? a : b != null && b !== "" ? b : "—");
    const dim = val(j.width, m.width) !== "—" ? `${j.width ?? m.width}×${j.height ?? m.height}` : "—";
    const overlay = el("div", { class: "overlay preview-overlay", role: "dialog", "aria-modal": "true", "aria-label": "生成结果预览" });
    const modal = el("div", { class: "modal" }, [
      el("div", { class: "modal-head" }, [
        el("h3", {}, "生成结果"),
        el("button", { class: "icon-btn", type: "button", "aria-label": "关闭" }, [ic("x")]),
      ]),
      el("div", { class: "preview-body" }, [
        el("img", { src: r.url, alt: "生成结果大图" }),
        el("div", { class: "preview-meta" }, [
          el("div", { class: "pm-block" }, [el("span", { class: "pm-label" }, "提示词"), el("span", { class: "pm-val" }, esc(val(j.prompt, m.prompt)))]),
          el("div", { class: "pm-block" }, [el("span", { class: "pm-label" }, "负面提示词"), el("span", { class: "pm-val" }, esc(val(j.negative_prompt, m.negative_prompt)))]),
          el("div", { class: "row wrap gap-3" }, [
            el("div", { class: "pm-block" }, [el("span", { class: "pm-label" }, "种子"), el("span", { class: "pm-val mono" }, esc(String(val(j.seed, m.seed))))]),
            el("div", { class: "pm-block" }, [el("span", { class: "pm-label" }, "尺寸"), el("span", { class: "pm-val mono" }, esc(dim))]),
            el("div", { class: "pm-block" }, [el("span", { class: "pm-label" }, "步数 / CFG"), el("span", { class: "pm-val mono" }, esc(`${val(j.steps, m.steps)} / ${val(j.cfg, m.cfg)}`))]),
          ]),
          el("div", { class: "pm-block" }, [el("span", { class: "pm-label" }, "模型族 / 工作流"), el("span", { class: "pm-val" }, esc(`${val(j.family, m.family)} / ${sanitizeName(val(j.workflow, m.workflow))}`))]),
          el("div", { class: "pm-block" }, [el("span", { class: "pm-label" }, "创建时间"), el("span", { class: "pm-val" }, esc(fmtRelative(j.created_at)))]),
          el("div", { class: "row gap-2" }, [
            el("a", { class: "btn btn-primary btn-sm", href: r.url, download: `winefox_${j.seed ?? "img"}.png`, target: "_blank", rel: "noopener" }, [ic("download"), esc("保存")]),
            el("button", { class: "btn btn-sm", type: "button", id: "pv-fav" }, [ic("fav"), esc("收藏")]),
            el("button", { class: "btn btn-sm", type: "button", id: "pv-copy" }, [ic("copy"), esc("复制")]),
          ]),
        ]),
      ]),
    ]);
    overlay.append(modal);
    document.body.append(overlay);
    requestAnimationFrame(() => requestAnimationFrame(() => overlay.classList.add("open")));
    const onKey = (e) => { if (e.key === "Escape") close(); };
    const close = () => {
      overlay.classList.add("closing");
      document.removeEventListener("keydown", onKey);
      setTimeout(() => overlay.remove(), 180);
    };
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
    modal.querySelector(".modal-head .icon-btn").addEventListener("click", close);
    document.addEventListener("keydown", onKey);
    modal.querySelector(".modal-head .icon-btn").focus();
    const favBtn = $("#pv-fav", modal);
    favBtn?.addEventListener("click", async () => {
      try {
        await api.favoriteHistory(j.id);
        store.set("favoritesDirty", true);
        if (favBtn) {
          favBtn.classList.remove("fav-pop");
          void favBtn.offsetWidth;
          favBtn.classList.add("fav-pop");
          favBtn.replaceChildren(ic("favFill"), esc("收藏"));
          const ico = favBtn.querySelector("svg");
          if (ico) ico.style.color = "var(--danger)";
        }
        toast("已加入收藏", "ok");
      } catch (e) { toast("收藏失败：" + e.message, "err"); }
    });
    $("#pv-copy", modal)?.addEventListener("click", async () => {
      try {
        const blob = await (await fetch(r.url)).blob();
        await navigator.clipboard.write([new ClipboardItem({ [blob.type || "image/png"]: blob })]);
        toast("已复制到剪贴板", "ok");
      } catch { toast("复制失败（浏览器不支持）", "warn"); }
    });
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
    this.cancelAutoLoraPreview();
    this.teardownLoraPagination();
    if (this.loraPageFrame != null) cancelAnimationFrame(this.loraPageFrame);
    this.loraPageFrame = null;
    this.poller.dispose();
  }
}

/* WineFox Desktop — 环境配置 tab（ComfyUI 环境中心）
 *
 * 契约见 docs/desktop_setup_migration.md 第 8 节：
 *   - 只读扫描 + profile 计划 + 结构化进度；全程 ink-line 墨线动画（禁用 circular spinner）；
 *   - 进度通过 requestAnimationFrame 批量更新 DOM，job 更新 ≤4/s、文本状态 ≤2/s；
 *   - 事件流：SSE 优先，连接失败或流中断时退化 1s 轮询 + 指数退避；
 *   - 绝不展示绝对路径 / token（后端已脱敏，这里只信任 display/basename 字段）。
 */
import { el, $, ic, esc, loadingState, toast, fmtBytes } from "./utils.js";
import { api } from "./api.js";

const PROFILE_LABELS = { lite: "精简", full: "进阶", complete: "完整" };
const STEP_LABELS = {
  nodes: "自定义节点",
  models: "模型下载",
  bridge: "LoraBridge",
  pip: "Python 依赖",
};

const ACTIVE_STATUSES = new Set(["pending", "running", "rolling_back"]);

const STATE_META = {
  ready: { label: "环境就绪", cls: "ok" },
  repair_needed: { label: "需要修复", cls: "warn" },
  no_root: { label: "未找到根目录", cls: "neutral" },
};

/* ================= 目录选择桥（Rust pick_comfyui_root 在阶段 6 接入） ================= */
let pickSeq = 0;
const pickWaiters = new Map();

window.addEventListener("message", (event) => {
  if (event.data?.type !== "winefox-ui-pick-dir-reply") return;
  const waiter = pickWaiters.get(event.data.id);
  if (!waiter) return;
  pickWaiters.delete(event.data.id);
  clearTimeout(waiter.timer);
  waiter.resolve(event.data);
});

function pickDirectory(kind) {
  return new Promise((resolve) => {
    if (window.parent === window) {
      resolve({ ok: false, error: "未运行在桌面壳中" });
      return;
    }
    const id = "pick-" + (++pickSeq) + "-" + Date.now();
    const timer = setTimeout(() => {
      pickWaiters.delete(id);
      resolve({ ok: false, error: "目录选择超时" });
    }, 120000);
    const promise = new Promise((res) => pickWaiters.set(id, { timer, resolve: res }));
    window.parent.postMessage({ type: "winefox-ui-pick-dir", kind, id }, "*");
    promise.then(resolve);
  });
}

export class EnvSetupView {
  constructor() {
    this.root = null;
    this.loaded = false;
    this.loading = false;
    this.loadError = null;
    this.summary = null;
    this.capabilities = null;
    this.plan = null;
    this.profile = null;
    this.planning = false;
    this.starting = false;
    this.activeJobId = null;
    this.jobData = null;
    this.currentItem = null;
    this.currentPip = null;
    this.rollbackFailure = null;
    this.eventSource = null;
    this.pollTimer = null;
    this.pollDelay = 1000;
    this.pendingFrame = null;
    this.lastJobRender = 0;
    this.lastStatusUpdate = 0;
    this.lastStatusText = "";
    this._disposed = false;
    this.loadToken = 0;
  }

  // ------------------------------------------------------------- 生命周期

  attach(container) {
    this.root = container;
    this._disposed = false;
    // 代理监听：header/状态卡上的按钮在每次 render 里重建，用单一监听避免累积
    if (!this._attachedListener) {
      this._attachedListener = (event) => {
        if (event.target.closest("[data-pick-root]")) { void this.pickRoot(); return; }
        if (event.target.closest("#setup-rescan")) { void this.rescan(); return; }
        if (event.target.closest("#setup-repair")) { void this.repair(); }
      };
      container.addEventListener("click", this._attachedListener);
    }
    if (!this.loaded && !this.loading) {
      this.render();
      void this.load();
    } else {
      this.render();
      if (this.activeJobId && !this.eventSource) this.subscribeJob(this.activeJobId);
    }
  }

  dispose() {
    this._disposed = true;
    if (this.root && this._attachedListener) {
      this.root.removeEventListener("click", this._attachedListener);
      this._attachedListener = null;
    }
    this.closeStream();
    if (this.pollTimer) clearTimeout(this.pollTimer);
    this.pollTimer = null;
    if (this.pendingFrame) cancelAnimationFrame(this.pendingFrame);
    this.pendingFrame = null;
    this.loadToken++;
  }

  async load() {
    const token = ++this.loadToken;
    this.loading = true;
    this.loadError = null;
    try {
      const [summary, capabilities, jobs] = await Promise.all([
        api.setupSummary(),
        api.setupCapabilities(),
        api.setupListJobs(),
      ]);
      if (token !== this.loadToken || this._disposed) return;
      this.summary = summary;
      this.capabilities = capabilities;
      this.loaded = true;
      this.loading = false;
      // 恢复：若存在未结束的任务，自动接管其进度
      const running = (jobs.items || []).find((job) => ACTIVE_STATUSES.has(job.status));
      if (running) this.openJob(running.id, running);
      else this.render();
    } catch (error) {
      if (token !== this.loadToken || this._disposed) return;
      this.loading = false;
      this.loadError = error;
      this.render();
    }
  }

  // ------------------------------------------------------------- 渲染骨架

  render() {
    const root = this.root;
    if (!root) return;
    root.replaceChildren(
      this.renderHeader(),
      el("div", { id: "setup-body", class: "setup-body" }, this.renderBodySections()),
    );
  }

  renderHeader() {
    // 此 tab 嵌在「模型与环境」页内，用紧凑的 section 头而非页面级 view-head
    return el("div", { class: "setup-head" }, [
      el("div", {}, [
        el("h2", { class: "setup-title" }, "环境配置"),
        el("div", { class: "lead" }, "扫描 ComfyUI 环境，按档案安装自定义节点、模型与 LoraBridge，全程可取消、重试与回滚。"),
      ]),
      el("div", { class: "row gap-2" }, [
        el("button", { class: "btn btn-sm", type: "button", dataset: { pickRoot: "1" } }, esc("选择目录")),
        el("button", { class: "btn btn-sm", id: "setup-rescan", type: "button" }, [ic("refresh"), esc("重新扫描")]),
      ]),
    ]);
  }

  renderBodySections() {
    if (this.loadError) {
      return [el("div", { class: "empty" }, [
        ic("warn"),
        el("div", { class: "empty-title" }, "无法读取环境信息"),
        el("div", { class: "empty-desc" }, esc(this.loadError.message)),
      ])];
    }
    if (!this.loaded || this.loading) {
      return [loadingState("正在扫描 ComfyUI 环境")];
    }
    if (this.activeJobId) {
      return [this.renderJobPanel(), this.renderStatus()];
    }
    return [this.renderStatus(), this.renderManual(), this.renderPlan()];
  }

  // ------------------------------------------------------------- 状态卡片

  renderStatus() {
    const s = this.summary || {};
    const rootInfo = s.root || null;
    const nodes = s.nodes || [];
    const models = s.models || [];
    const bridge = s.bridge || {};
    const python = s.python || {};
    const disk = s.disk || {};
    const stateMeta = STATE_META[s.state_code] || { label: s.state_code || "未知", cls: "neutral" };
    const missingNodes = nodes.filter((n) => !n.present).length;
    const corruptModels = models.filter((m) => m.corrupt).length;
    const presentModels = models.filter((m) => m.present && !m.corrupt).length;
    const manualMissing = (s.manual || []).filter((m) => !m.present).length;

    const repairBtn = s.state_code === "repair_needed"
      ? el("button", { class: "btn btn-sm", id: "setup-repair", type: "button" }, [ic("wand"), esc("修复缺失项")])
      : null;

    const cards = el("div", { id: "setup-status", class: "setup-cards" }, [
      this.card("根目录", rootInfo
        ? el("div", { class: "setup-card-line" }, [
            el("span", { class: "setup-card-name", title: rootInfo.display }, esc(rootInfo.name)),
            el("span", { class: "badge " + stateMeta.cls }, esc(stateMeta.label)),
          ])
        : el("div", { class: "setup-card-line" }, [
            el("span", { class: "setup-card-value muted" }, "未找到"),
            el("button", { class: "btn btn-sm", type: "button", dataset: { pickRoot: "1" } }, esc("选择目录")),
          ])),
      this.card("自定义节点",
        nodes.length
          ? el("div", { class: "setup-card-line" }, [
              el("span", { class: "setup-card-value" }, esc(`${nodes.length - missingNodes}/${nodes.length} 就绪`)),
              missingNodes ? el("span", { class: "badge warn" }, esc(`${missingNodes} 缺失`)) : null,
            ])
          : el("span", { class: "setup-card-value muted" }, "暂无节点"),
          missingNodes ? "warn" : "ok"),
      this.card("模型", models.length
        ? el("div", { class: "setup-card-line" }, [
            el("span", { class: "setup-card-value" }, esc(`${presentModels}/${models.length} 完整`)),
            corruptModels ? el("span", { class: "badge warn" }, esc(`${corruptModels} 损坏`)) : null,
          ])
        : el("span", { class: "setup-card-value muted" }, "暂无模型"),
          corruptModels ? "warn" : "ok"),
      this.card("LoraBridge",
        el("span", { class: "setup-card-value" + (bridge.installed ? "" : " muted") }, bridge.installed ? esc(`已安装 ${bridge.version || ""}`.trim()) : "未安装"),
        bridge.installed ? "ok" : "warn"),
      this.card("Python",
        el("span", { class: "setup-card-value" + (python.found ? "" : " muted") }, python.found ? esc(python.executable || "已找到") : "未找到"),
        python.found ? "ok" : "warn"),
      this.card("磁盘", disk.available
        ? el("span", { class: "setup-card-value" }, esc(`${disk.free_gb ?? "—"} GB 可用`))
        : el("span", { class: "setup-card-value muted" }, "不可用")),
    ]);

    return el("div", { class: "stack-3" }, [
      cards,
      manualMissing && repairBtn
        ? el("div", { class: "row wrap gap-2" }, [
            repairBtn,
            el("span", { class: "hint" }, `${manualMissing} 个文件需手动放置，见下方清单`),
          ])
        : (repairBtn ? el("div", { class: "row gap-2" }, [repairBtn]) : null),
    ]);
  }

  card(label, valueNode, tone) {
    return el("div", { class: "setup-card" + (tone ? " is-" + tone : "") }, [
      el("div", { class: "setup-card-label" }, esc(label)),
      valueNode,
    ]);
  }

  // ------------------------------------------------------------- 手动文件

  renderManual() {
    const missing = (this.summary?.manual || []).filter((m) => !m.present);
    if (!missing.length) return null;
    // summary 的 manual 只有 {name, present}；dest 从 capabilities 补齐
    const destByName = new Map((this.capabilities?.manual_files || []).map((mf) => [mf.name, mf.dest]));
    return el("section", { id: "setup-manual", class: "section-card" }, [
      el("div", { class: "sec-head" }, [
        el("div", {}, [
          el("h3", "需手动放置的文件"),
          el("div", { class: "hint" }, "以下文件无法自动下载，请放入对应目录后重新扫描。"),
        ]),
      ]),
      el("div", { class: "sec-body" }, missing.map((m) => el("div", { class: "setup-manual-row" }, [
        el("span", { class: "m-name" }, esc(m.name)),
        el("span", { class: "m-sub" }, esc(destByName.get(m.name) || m.name)),
      ]))),
    ]);
  }

  // ------------------------------------------------------------- 计划

  renderPlan() {
    const profiles = this.capabilities?.profiles || [];
    const plan = this.plan;
    const chips = profiles.map((id) => {
      const chip = el("button", {
        class: "setup-profile-chip" + (this.profile === id ? " on" : ""),
        type: "button",
        role: "radio",
        "aria-checked": this.profile === id ? "true" : "false",
        dataset: { profile: id },
      }, [
        el("span", { class: "setup-profile-name" }, esc(PROFILE_LABELS[id] || id)),
        el("span", { class: "setup-profile-id" }, esc(id)),
      ]);
      chip.addEventListener("click", () => void this.chooseProfile(id));
      return chip;
    });

    let planArea;
    if (this.planning) {
      planArea = el("div", { class: "setup-plan-area" }, [loadingState("正在生成安装计划", { compact: true })]);
    } else if (plan) {
      planArea = this.renderPlanSteps(plan);
    } else {
      planArea = el("div", { class: "setup-plan-area hint" }, "选择一个档案以生成安装计划。");
    }

    const startBtn = el("button", {
      class: "btn btn-primary setup-start",
      type: "button",
      id: "setup-start",
      disabled: !plan || this.starting,
    }, [ic("play"), esc(this.starting ? "正在启动…" : "开始安装")]);
    if (plan && !this.starting) startBtn.addEventListener("click", () => void this.startInstall());

    return el("section", { id: "setup-plan", class: "section-card" }, [
      el("div", { class: "sec-head" }, [
        el("div", {}, [
          el("h3", "安装计划"),
          el("div", { class: "hint" }, "按档案安装缺失的节点与模型；已就绪的项目会自动跳过。"),
        ]),
      ]),
      el("div", { class: "sec-body" }, [
        el("div", { class: "setup-profile-row", role: "radiogroup", "aria-label": "安装档案" }, chips),
        planArea,
        plan ? this.renderPlanMeta(plan) : null,
        el("div", { class: "row gap-2 setup-start-row" }, [startBtn]),
      ]),
    ]);
  }

  renderPlanMeta(plan) {
    const parts = [];
    if (plan.estimated_bytes > 0) parts.push(el("span", { class: "badge neutral" }, esc(`约需 ${fmtBytes(plan.estimated_bytes)}`)));
    if (plan.manual_required) parts.push(el("span", { class: "badge warn" }, "需手动放置文件"));
    if (plan.steps?.length) parts.push(el("span", { class: "badge neutral" }, esc(`${plan.steps.length} 个步骤`)));
    return el("div", { class: "row wrap gap-2" }, parts);
  }

  renderPlanSteps(plan) {
    const steps = plan.steps || [];
    if (!steps.length) return el("div", { class: "notice info" }, "没有需要安装的项目，环境已就绪。");
    return el("ol", { class: "setup-steps is-plan" }, steps.map((step) => el("li", { class: "setup-step" }, [
      el("span", { class: "setup-step-pill" }, esc(STEP_LABELS[step.kind] || step.kind)),
      el("div", { class: "grow" }, [
        el("div", { class: "setup-step-label" }, esc(step.label)),
        el("div", { class: "setup-step-sub" }, esc(this.stepItemNames(step))),
      ]),
    ])));
  }

  stepItemNames(step) {
    const items = step.items || [];
    if (!items.length) return step.kind === "pip" ? (step.packages || []).join("、") : "";
    return items.map((item) => item.name || item.id).join("、");
  }

  // ------------------------------------------------------------- job 面板

  renderJobPanel() {
    const panel = el("div", { id: "setup-job-panel", class: "section-card" });
    this.renderJobPanelInto(panel);
    return panel;
  }

  renderJobPanelInto(panel) {
    const job = this.jobData;
    if (!job) {
      panel.replaceChildren(el("div", { class: "sec-body" }, [loadingState("正在同步任务状态", { compact: true })]));
      return;
    }
    const head = el("div", { class: "sec-head" }, [
      el("div", {}, [
        el("h3", this.jobHeadTitle(job.status)),
        el("div", { class: "hint" }, this.jobHeadHint(job)),
      ]),
      el("span", { class: "badge " + this.jobStatusCls(job.status) }, esc(this.jobStatusLabel(job.status))),
    ]);
    const body = el("div", { class: "sec-body" }, [
      this.renderJobStatusLine(job),
      this.renderJobSteps(job),
      this.renderJobActions(job),
    ]);
    panel.replaceChildren(head, body);
  }

  jobHeadTitle(status) {
    if (status === "success") return "安装完成";
    if (status === "failed") return "安装失败";
    if (status === "cancelled") return "已取消";
    if (status === "rolled_back") return "已回滚";
    if (status === "rolling_back") return "正在回滚";
    return "正在安装";
  }

  jobHeadHint(job) {
    if (!job.profile) return "";
    return PROFILE_LABELS[job.profile] ? `档案：${PROFILE_LABELS[job.profile]}` : `档案：${job.profile}`;
  }

  jobStatusLabel(status) {
    return { running: "进行中", pending: "排队中", success: "成功", failed: "失败", cancelled: "已取消", rolling_back: "回滚中", rolled_back: "已回滚" }[status] || status || "未知";
  }

  jobStatusCls(status) {
    return { running: "primary pulse", pending: "neutral", success: "ok", failed: "danger", cancelled: "neutral", rolling_back: "primary pulse", rolled_back: "neutral" }[status] || "neutral";
  }

  jobStatusText(job) {
    if (this.currentItem?.type === "model" && this.currentItem.size) {
      return `正在下载 · 已下载 ${fmtBytes(this.currentItem.size)}`;
    }
    if (this.currentItem) return "正在安装组件";
    if (this.currentPip) return `正在安装依赖 ${this.currentPip}`;
    if (job.status === "rolling_back") return this.rollbackFailure ? `回滚受阻：${this.rollbackFailure}` : "正在恢复安装前的文件…";
    if (job.status === "failed") return job.error_message || "某一步骤失败";
    if (job.status === "running") {
      const running = job.steps.find((s) => s.status === "running");
      return running ? `正在执行：${running.label}` : "准备就绪";
    }
    return "";
  }

  renderJobStatusLine(job) {
    let text = this.jobStatusText(job);
    if (!text) { this.lastStatusText = ""; return null; }
    if (text !== this.lastStatusText) {
      const now = performance.now();
      if (now - this.lastStatusUpdate < 500) {
        text = this.lastStatusText; // 文本状态 ≤2/s：500ms 窗口内沿用旧文本，避免闪跳
      } else {
        this.lastStatusText = text;
        this.lastStatusUpdate = now;
      }
    }
    return el("div", { class: "setup-job-status" }, esc(text));
  }

  renderJobSteps(job) {
    if (!job.steps?.length) return el("div", { class: "hint" }, "任务已提交，等待开始…");
    return el("ol", { class: "setup-steps" }, job.steps.map((step) => this.renderJobStep(step)));
  }

  renderJobStep(step) {
    const running = step.status === "running";
    const sub = [];
    if (running && step.kind === "models") {
      sub.push(loadingState(
        this.currentItem ? `正在下载 ${this.currentItem.id}${this.currentItem.size ? ` · ${fmtBytes(this.currentItem.size)}` : ""}` : "正在下载",
        { compact: true },
      ));
    } else if (running && step.kind === "nodes") {
      sub.push(el("div", { class: "hint" }, `正在安装节点 ${step.items_done + 1}/${step.items_total || "…"}`));
    } else if (running) {
      sub.push(el("div", { class: "hint" }, "执行中…"));
    }
    if (step.error_message) sub.push(el("div", { class: "setup-step-err" }, esc(step.error_message)));
    else if (step.items_total > 0 && !running) sub.push(el("div", { class: "hint" }, esc(`${step.items_done}/${step.items_total} 项完成`)));
    return el("li", { class: "setup-step is-" + (step.status || "pending") }, [
      el("span", { class: "setup-step-pill is-" + (step.status || "pending") }, esc(this.stepStatusText(step))),
      el("div", { class: "grow" }, [
        el("div", { class: "setup-step-label" }, esc(step.label)),
        ...sub,
      ]),
    ]);
  }

  stepStatusText(step) {
    if (step.status === "running") return "进行中";
    if (step.status === "success") return "完成";
    if (step.status === "failed") return "失败";
    return "等待";
  }

  renderJobActions(job) {
    const actions = [];
    if (job.status === "running" || job.status === "pending" || job.status === "rolling_back") {
      actions.push(el("button", { class: "btn btn-danger btn-sm", id: "setup-cancel", type: "button" }, [ic("x"), esc("取消安装")]));
    }
    if (job.status === "failed") {
      actions.push(el("button", { class: "btn btn-sm", id: "setup-retry", type: "button" }, [ic("restore"), esc("重试失败步骤")]));
      actions.push(el("button", { class: "btn btn-sm", id: "setup-rollback", type: "button" }, [ic("trash"), esc("回滚本次安装")]));
    }
    if (["success", "failed", "cancelled", "rolled_back"].includes(job.status)) {
      actions.push(el("button", { class: "btn btn-primary btn-sm", id: "setup-reprobe", type: "button" }, [ic("refresh"), esc("重新探测")]));
      actions.push(el("button", { class: "btn btn-sm", id: "setup-back", type: "button" }, [ic("check"), esc("完成")]));
    }
    if (job.status === "success") {
      actions.push(el("div", { class: "setup-restart-hint" }, "安装完成。ComfyUI 重启后会自动重新探测；也可以点「重新探测」立即刷新 Bridge 与模型列表。"));
    }
    if (!actions.length) return null;
    const wrap = el("div", { class: "row wrap gap-2 setup-job-actions" }, actions);
    if (job.status === "running" || job.status === "pending") {
      const cancel = $("#setup-cancel", wrap);
      if (cancel) cancel.addEventListener("click", () => void this.cancelJob());
    }
    if (job.status === "failed") {
      $("#setup-retry", wrap)?.addEventListener("click", () => void this.retryJob());
      $("#setup-rollback", wrap)?.addEventListener("click", () => void this.rollbackJob());
    }
    if (["success", "failed", "cancelled", "rolled_back"].includes(job.status)) {
      $("#setup-reprobe", wrap)?.addEventListener("click", () => void this.reprobe());
      $("#setup-back", wrap)?.addEventListener("click", () => void this.finishToPlan());
    }
    return wrap;
  }

  // ------------------------------------------------------------- 事件/SSE

  _scheduleJobRender() {
    if (this.pendingFrame) return;
    this.pendingFrame = requestAnimationFrame(() => {
      this.pendingFrame = null;
      if (this._disposed) return;
      const now = performance.now();
      if (now - this.lastJobRender < 250) return; // job 更新 ≤4/s
      this.lastJobRender = now;
      const panel = $("#setup-job-panel", this.root);
      if (panel) this.renderJobPanelInto(panel);
      else this.render();
    });
  }

  openJob(jobId, seed) {
    this.closeStream();
    this.activeJobId = jobId;
    this.jobData = seed ? { ...seed } : null;
    this.currentItem = null;
    this.currentPip = null;
    this.rollbackFailure = null;
    this.render();
    void this.syncJob();
    this.subscribeJob(jobId);
  }

  async syncJob() {
    if (!this.activeJobId || this._disposed) return;
    try {
      const job = await api.setupJob(this.activeJobId);
      if (!this.activeJobId || this._disposed) return;
      this.jobData = job;
      this._scheduleJobRender();
    } catch { /* 忽略，SSE 会补全状态 */ }
  }

  subscribeJob(jobId) {
    this.closeStream();
    this.sseHealthy = false;
    let source;
    try {
      source = new EventSource(api.setupJobEventsUrl(jobId));
    } catch {
      this.schedulePoll(500);
      return;
    }
    this.eventSource = source;
    source.onmessage = (event) => {
      this.sseHealthy = true;
      if (this.pollTimer) { clearTimeout(this.pollTimer); this.pollTimer = null; }
      let data;
      try { data = JSON.parse(event.data); } catch { return; }
      this._onEvent(data);
    };
    source.onerror = () => {
      // 服务器在终止事件后主动结束流；此时无需轮询，直接关闭。
      if (!this.jobIsActive()) { this.closeStream(); return; }
      // SSE 失败/中断 → 退化为轮询（指数退避）；EventSource 会自动重连。
      this.sseHealthy = false;
      this.schedulePoll(500);
    };
  }

  jobIsActive() {
    const status = this.jobData?.status;
    return ACTIVE_STATUSES.has(status) || (!status && !!this.activeJobId);
  }

  schedulePoll(delay) {
    if (this.pollTimer || this._disposed) return;
    this.pollTimer = setTimeout(() => {
      this.pollTimer = null;
      if (!this.activeJobId || this._disposed) return;
      void (async () => {
        try {
          const job = await api.setupJob(this.activeJobId);
          if (!this.activeJobId || this._disposed) return;
          this.jobData = job;
          this._scheduleJobRender();
          if (this.jobIsActive()) this.schedulePoll(this.pollDelay);
          else this.closeStream();
        } catch {
          this.pollDelay = Math.min(this.pollDelay * 2, 10000);
          if (this.jobIsActive()) this.schedulePoll(this.pollDelay);
        }
      })();
    }, delay);
  }

  closeStream() {
    if (this.eventSource) {
      try { this.eventSource.close(); } catch { /* 忽略 */ }
      this.eventSource = null;
    }
  }

  _onEvent(event) {
    if (this._disposed) return;
    const job = this.jobData || (this.jobData = { id: this.activeJobId, profile: "", status: "running", steps: [], error_code: null, error_message: null });
    switch (event.type) {
      case "job_started":
        job.profile = event.profile || job.profile;
        job.status = "running";
        break;
      case "step_started": {
        const step = job.steps?.[event.step];
        if (step) { step.status = "running"; step.error_message = null; }
        break;
      }
      case "step_finished": {
        const step = job.steps?.[event.step];
        if (step) step.status = "success";
        // item_finished 被后端节流（0.5s），步骤收尾时清掉残留的当前项
        if (this.currentItem && this.currentItem.step === event.step) this.currentItem = null;
        if (this.currentPip) this.currentPip = null;
        break;
      }
      case "step_failed": {
        const step = job.steps?.[event.step];
        if (step) { step.status = "failed"; step.error_code = event.code; step.error_message = event.message; }
        job.error_code = job.error_code || event.code;
        job.error_message = event.message;
        break;
      }
      case "item_started":
        this.currentItem = { id: event.id, type: event.kind, step: event.step, size: 0 };
        this.currentPip = null;
        break;
      case "item_finished":
        this.currentItem = null;
        break;
      case "item_progress":
        if (this.currentItem && this.currentItem.id === event.id) this.currentItem.size = event.size;
        break;
      case "pip_started":
        this.currentPip = event.package;
        this.currentItem = null;
        break;
      case "bridge_installed":
        break;
      case "job_finished":
        job.status = event.status || "success";
        this.currentItem = null;
        this.currentPip = null;
        this.finishJob();
        break;
      case "job_cancelled":
        job.status = "cancelled";
        this.currentItem = null;
        this.currentPip = null;
        this.finishJob();
        break;
      case "rollback_started":
        job.status = "rolling_back";
        break;
      case "rollback_finished":
        job.status = event.status || "rolled_back";
        this.currentItem = null;
        this.currentPip = null;
        this.finishJob();
        break;
      case "rollback_item_failed":
        this.rollbackFailure = event.message;
        break;
      case "keepalive":
        break;
      default:
        break;
    }
    if (!["job_finished", "job_cancelled", "rollback_finished"].includes(event.type)) {
      this._scheduleJobRender();
    }
  }

  finishJob() {
    this.closeStream();
    if (this.pollTimer) { clearTimeout(this.pollTimer); this.pollTimer = null; }
    this.pollDelay = 1000;
    this._scheduleJobRender();
    void this.refreshSummary();
  }

  async refreshSummary() {
    const token = ++this.loadToken;
    try {
      const summary = await api.setupSummary();
      if (token !== this.loadToken || this._disposed) return;
      this.summary = summary;
      this._scheduleJobRender();
    } catch { /* 保留旧 summary */ }
  }

  // ------------------------------------------------------------- 操作

  async chooseProfile(id) {
    if (this.planning) return;
    this.profile = id;
    this.plan = null;
    this.planning = true;
    this.render();
    try {
      const plan = await api.setupPlan({ profile: id, only_missing: true });
      if (this.profile !== id || this._disposed) return;
      this.plan = plan;
      this.planning = false;
      this.render();
    } catch (error) {
      if (this.profile !== id || this._disposed) return;
      this.planning = false;
      this.render();
      toast("生成计划失败：" + error.message, "err");
    }
  }

  async repair() {
    if (this.planning) return;
    this.profile = "repair";
    this.plan = null;
    this.planning = true;
    this.render();
    try {
      const plan = await api.setupPlan({ repair: true });
      if (this._disposed) return;
      this.plan = plan;
      this.planning = false;
      this.render();
    } catch (error) {
      if (this._disposed) return;
      this.planning = false;
      this.render();
      toast("生成修复计划失败：" + error.message, "err");
    }
  }

  async startInstall() {
    if (!this.plan || this.starting) return;
    this.starting = true;
    this.render();
    try {
      // root 由后端以缓存扫描结果决定，UI 只回传 plan，避免携带绝对路径
      const job = await api.setupStartJob({ plan: this.plan });
      this.starting = false;
      this.plan = null;
      this.profile = null;
      this.openJob(job.id);
    } catch (error) {
      this.starting = false;
      this.render();
      toast("无法开始安装：" + error.message, "err");
    }
  }

  async cancelJob() {
    if (!this.activeJobId) return;
    try {
      await api.setupCancelJob(this.activeJobId);
    } catch (error) {
      toast("取消失败：" + error.message, "err");
    }
  }

  async retryJob() {
    if (!this.activeJobId) return;
    try {
      const result = await api.setupRetryJob(this.activeJobId);
      this.openJob(result.id);
    } catch (error) {
      toast("重试失败：" + error.message, "err");
    }
  }

  async rollbackJob() {
    if (!this.activeJobId) return;
    try {
      await api.setupRollbackJob(this.activeJobId);
      // 回滚事件经 SSE 到达；这里先本地置位避免空窗
      this.jobData.status = "rolling_back";
      this._scheduleJobRender();
    } catch (error) {
      toast("回滚失败：" + error.message, "err");
    }
  }

  async reprobe() {
    const button = $("#setup-reprobe", this.root);
    if (button) button.disabled = true;
    try {
      const result = await api.setupReprobe();
      toast(result.bridge?.installed ? "已重新探测，Bridge 可用" : "已重新探测，但 Bridge 尚未就绪", result.bridge?.installed ? "ok" : "warn");
      await this.load();
    } catch (error) {
      toast("重新探测失败：" + error.message, "err");
      if (button) button.disabled = false;
    }
  }

  finishToPlan() {
    this.activeJobId = null;
    this.jobData = null;
    this.currentItem = null;
    this.currentPip = null;
    this.rollbackFailure = null;
    this.closeStream();
    void this.load();
  }

  async rescan() {
    const button = $("#setup-rescan", this.root);
    if (button) button.disabled = true;
    try {
      await api.setupScan({ force: true });
      await this.load();
    } catch (error) {
      toast("重新扫描失败：" + error.message, "err");
    } finally {
      if (button) button.disabled = false;
    }
  }

  async pickRoot() {
    const result = await pickDirectory("comfyui_root");
    if (!result.ok || !result.path) {
      toast(result.error || "未选择目录", "warn");
      return;
    }
    try {
      await api.setupScan({ root: result.path, force: true });
      await this.load();
      toast("已切换根目录", "ok");
    } catch (error) {
      toast("目录校验失败：" + error.message, "err");
    }
  }
}

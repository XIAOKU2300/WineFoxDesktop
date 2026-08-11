/* WineFox Desktop — 模型与 LoRA 页
 * 展示本地资源，并通过后台任务搜索/安装 Civitai LoRA。
 */
import { el, $, $$, ic, esc, sanitizeResourceName, fmtBytes, loadingState, copyText, toast } from "../utils.js";
import { api } from "../api.js";
import { store } from "../store.js";
import { EnvSetupView } from "../setup.js";

export class ModelsView {
  constructor() {
    this.tab = "checkpoints";
    this.data = null;
    this.loaded = false;
    this.envView = null;
    this.visible = { checkpoints: 40, loras: 40, nodes: 40 };
    this.resourceObserver = null;
    this.resourceFallback = null;
    this.resourcePageFrame = null;
    this.paginationState = null;
    this.onlineQuery = "";
    this.onlineFamily = store.get("settings")?.defaults?.family || "illustrious";
    this.onlineResults = [];
    this.onlineLoading = false;
    this.searchController = null;
    this.downloadPoll = null;
  }

  async mount(container) {
    this.root = container;
    this.render();
    this.data = store.get("models");
    store.set("models", null);
    if (this.data) {
      this.renderBody();
      this.loaded = true;
    } else {
      await this.load();
    }
  }

  render() {
    const root = this.root;
    root.innerHTML = "";
    root.append(
      el("div", { class: "view-head" }, [
        el("div", {}, [
          el("h1", { class: "h1" }, "模型与 LoRA"),
          el("div", { class: "lead" }, "检查本地资源，也可以搜索 Civitai 并把 LoRA 安装到当前 ComfyUI。"),
        ]),
        el("button", { class: "btn btn-sm", id: "m-refresh", type: "button" }, [ic("refresh"), esc("重新扫描")]),
      ]),
      el("div", { class: "family-tabs", role: "tablist", "aria-label": "资源类型" }, [
        ["checkpoints", "Checkpoint 主模型"], ["loras", "LoRA"], ["nodes", "自定义节点"], ["env", "环境配置"],
      ].map(([id, label]) => {
        const b = el("button", { class: "cat-chip", type: "button", role: "tab", "aria-selected": this.tab === id ? "true" : "false", dataset: { tab: id } }, esc(label));
        b.addEventListener("click", () => { this.tab = id; this.renderTabs(); this.renderBody(); });
        return b;
      })),
      el("div", { id: "m-body", class: "stack-3" }, [loadingState("正在读取模型目录")]),
    );
    $("#m-refresh")?.addEventListener("click", () => this.load());
  }

  renderTabs() {
    $$(".family-tabs .cat-chip", this.root).forEach((b) => {
      b.setAttribute("aria-selected", b.dataset.tab === this.tab ? "true" : "false");
    });
  }

  async load() {
    const body = $("#m-body");
    if (!body) return;
    this.teardownPagination();
    body.replaceChildren(loadingState("正在扫描模型与 LoRA"));
    try {
      this.data = await api.models();
      this.renderBody();
      this.loaded = true;
    } catch (e) {
      body.replaceChildren(el("div", { class: "empty" }, [ic("warn"), el("div", { class: "empty-title" }, "无法获取模型信息"), el("div", { class: "empty-desc" }, esc(e.message))]));
    }
  }

  renderBody() {
    const body = $("#m-body");
    if (!body) return;
    this.teardownPagination();
    this.paginationState = null;
    if (this.tab === "env") {
      this.mountEnv(body);
      return;
    }
    const d = this.data || {};
    const summary = el("div", { class: "row wrap gap-2" }, [
      el("span", { class: "badge ok" }, esc(`${(d.checkpoints || []).length} 个 checkpoint`)),
      el("span", { class: "badge ok" }, esc(`${(d.loras || []).length} 个 LoRA`)),
      el("span", { class: "badge neutral" }, esc(`${d.lora_profiles?.loaded || 0} 个角色档案`)),
      el("span", { class: "badge warn" }, esc(`${(d.missing_nodes || []).length} 个节点缺失`)),
      d.download?.enabled ? el("span", { class: "badge ok" }, "在线获取可用") : null,
    ]);
    let list;
    if (this.tab === "checkpoints") list = this.checkpointList(d.checkpoints || []);
    else if (this.tab === "loras") list = this.loraList(d.loras || []);
    else list = this.nodeList(d);
    const content = [summary];
    if (this.tab === "loras") content.push(this.loraDownloadPanel(d.download || {}));
    content.push(list);
    body.replaceChildren(...content);
    this.setupPagination();
  }

  loraDownloadPanel(capabilities) {
    const mode = capabilities.mode || "auto";
    const modeText = mode === "local" ? "本机目录" : mode === "comfyui_bridge" ? "ComfyUI Bridge" : "自动选择";
    const panel = el("section", { class: "lora-online section-card", "aria-labelledby": "lora-online-title" }, [
      el("div", { class: "sec-head" }, [
        el("div", {}, [
          el("h3", { id: "lora-online-title" }, "获取新 LoRA"),
          el("div", { class: "hint" }, `来源 Civitai · 安装方式 ${modeText}`),
        ]),
        el("button", { class: "btn btn-sm", type: "button", id: "lora-bridge-probe" }, [ic("refresh"), esc("检查安装通道")]),
      ]),
      el("form", { class: "lora-searchbar", id: "lora-search-form" }, [
        el("div", { class: "field grow" }, [
          el("label", { for: "lora-online-query" }, "角色名、LoRA 名称或中文别称"),
          el("input", { class: "input", id: "lora-online-query", type: "search", value: this.onlineQuery, maxlength: "120", placeholder: "例如：维里奈、Verina、角色 LoRA", autocomplete: "off" }),
        ]),
        el("div", { class: "field lora-family-field" }, [
          el("label", { for: "lora-online-family" }, "模型族"),
          el("select", { class: "select", id: "lora-online-family" }, [
            el("option", { value: "illustrious", selected: this.onlineFamily === "illustrious" }, "Illustrious"),
            el("option", { value: "anima", selected: this.onlineFamily === "anima" }, "Anima"),
          ]),
        ]),
        el("button", { class: "btn btn-primary lora-search-submit", type: "submit", disabled: this.onlineLoading }, [ic("search"), esc(this.onlineLoading ? "搜索中…" : "搜索")]),
      ]),
      el("div", { class: "lora-channel-status hint", id: "lora-channel-status" }, this.downloadHint(capabilities)),
      el("div", { class: "lora-online-results", id: "lora-online-results", "aria-live": "polite" }, this.renderOnlineResults()),
    ]);
    $("#lora-search-form", panel)?.addEventListener("submit", (event) => {
      event.preventDefault();
      void this.searchOnlineLoras();
    });
    $("#lora-bridge-probe", panel)?.addEventListener("click", (event) => void this.probeDownloadChannel(event.currentTarget));
    panel.addEventListener("click", (event) => {
      const button = event.target.closest("[data-lora-download]");
      if (button && !button.disabled) void this.startOnlineDownload(button);
    });
    return panel;
  }

  downloadHint(capabilities) {
    if (capabilities.mode === "local" || (capabilities.mode === "auto" && capabilities.local_configured)) {
      return "下载会直接写入你在设置中指定的 ComfyUI models/loras 目录。";
    }
    return "下载会交给 ComfyUI Bridge，在 ComfyUI 所在机器写入 models/loras；首次使用请先安装 Bridge。";
  }

  renderOnlineResults() {
    if (this.onlineLoading) return [loadingState("正在搜索 Civitai LoRA", { compact: true })];
    if (!this.onlineResults.length) {
      return [el("div", { class: "lora-online-empty" }, "输入角色名或中文别称开始搜索；结果不会自动下载。")];
    }
    return this.onlineResults.map((item) => this.onlineResultRow(item));
  }

  onlineResultRow(item) {
    const words = Array.isArray(item.trained_words) ? item.trained_words.filter(Boolean).slice(0, 6) : [];
    const meta = [item.base_model || "未知底模", item.version_name, item.download_count ? `${item.download_count.toLocaleString()} 次下载` : ""].filter(Boolean).join(" · ");
    return el("article", { class: "lora-search-result", dataset: { resultId: item.id } }, [
      el("div", { class: "lora-result-main" }, [
        el("div", { class: "row wrap gap-2" }, [
          el("h4", { class: "lora-result-title" }, esc(item.name || "Civitai LoRA")),
          item.nsfw ? el("span", { class: "badge warn" }, "敏感内容") : el("span", { class: "badge neutral" }, "公开模型"),
        ]),
        el("div", { class: "m-sub" }, esc(meta)),
        words.length ? el("div", { class: "lora-trigger-preview" }, [
          el("span", { class: "hint" }, "触发词"),
          ...words.map((word) => el("span", { class: "tag" }, esc(word))),
        ]) : el("div", { class: "hint" }, "下载时会继续读取页面并提取触发词。"),
      ]),
      el("div", { class: "lora-result-actions" }, [
        el("input", { class: "input lora-alias-input", type: "text", maxlength: "80", placeholder: "中文别称（可选）", "aria-label": `${item.name || "LoRA"} 的中文别称` }),
        el("button", { class: "btn btn-primary btn-sm", type: "button", "data-lora-download": item.id }, [ic("download"), esc("安装")]),
      ]),
      el("div", { class: "lora-download-state", "aria-live": "polite" }),
    ]);
  }

  async searchOnlineLoras() {
    const query = $("#lora-online-query", this.root)?.value?.trim() || "";
    const family = $("#lora-online-family", this.root)?.value || "illustrious";
    if (!query) { toast("先输入 LoRA 名称或角色别称", "warn"); return; }
    this.onlineQuery = query;
    this.onlineFamily = family;
    this.onlineLoading = true;
    this.searchController?.abort();
    const controller = new AbortController();
    this.searchController = controller;
    const timeout = setTimeout(() => controller.abort(), 45000);
    this.renderBody();
    try {
      const result = await api.searchLoras(query, family, { signal: controller.signal });
      if (controller.signal.aborted) return;
      this.onlineResults = Array.isArray(result?.items) ? result.items : [];
      if (!this.onlineResults.length) toast("没有找到匹配当前模型族的 LoRA", "warn");
    } catch (error) {
      if (error?.name !== "AbortError") toast("LoRA 搜索失败：" + error.message, "err");
      else toast("LoRA 搜索超时，请检查 Civitai 网络或代理", "warn");
      this.onlineResults = [];
    } finally {
      clearTimeout(timeout);
      if (this.searchController === controller) this.searchController = null;
      this.onlineLoading = false;
      if (this.root?.isConnected) this.renderBody();
    }
  }

  async probeDownloadChannel(button) {
    if (button.disabled) return;
    button.disabled = true;
    const status = $("#lora-channel-status", this.root);
    status?.replaceChildren(loadingState("正在检查 ComfyUI Bridge", { compact: true }));
    try {
      const result = await api.probeLoraBridge();
      status?.replaceChildren(el("span", { class: result.ok ? "text-ok" : "text-warn" }, esc(result.message || (result.ok ? "Bridge 已就绪" : "Bridge 不可用"))));
    } catch (error) {
      status?.replaceChildren(el("span", { class: "text-warn" }, esc("检查失败：" + error.message)));
    } finally {
      button.disabled = false;
    }
  }

  async startOnlineDownload(button) {
    const row = button.closest(".lora-search-result");
    const state = $(".lora-download-state", row);
    const alias = $(".lora-alias-input", row)?.value?.trim() || "";
    button.disabled = true;
    state?.replaceChildren(loadingState("正在创建下载任务", { compact: true }));
    try {
      const job = await api.startLoraDownload(button.dataset.loraDownload, alias);
      this.pollLoraDownload(job.id, row, button);
    } catch (error) {
      button.disabled = false;
      state?.replaceChildren(el("div", { class: "notice err" }, esc(error.message)));
    }
  }

  pollLoraDownload(jobId, row, button) {
    if (this.downloadPoll) clearTimeout(this.downloadPoll);
    const tick = async () => {
      if (!row?.isConnected) return;
      const state = $(".lora-download-state", row);
      try {
        const job = await api.loraDownload(jobId);
        const progress = job.progress || {};
        const percent = Number.isFinite(progress.percent) ? progress.percent : null;
        state?.replaceChildren(el("div", { class: "lora-progress" }, [
          loadingState(job.message || "正在下载 LoRA", { compact: true }),
          el("div", { class: "lora-progress-track", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": percent ?? "" }, [
            el("span", { style: `width:${percent ?? 12}%` }),
          ]),
        ]));
        if (job.status === "completed") {
          state?.replaceChildren(el("div", { class: "notice ok" }, esc(`${job.result?.filename || "LoRA"} 已安装，触发词档案已更新。`)));
          toast("LoRA 安装完成", "ok");
          button.disabled = false;
          this.downloadPoll = null;
          setTimeout(() => { if (this.root?.isConnected) void this.load(); }, 500);
          return;
        }
        if (["failed", "cancelled"].includes(job.status)) {
          state?.replaceChildren(el("div", { class: "notice err" }, esc(job.error?.message || "LoRA 下载失败")));
          button.disabled = false;
          this.downloadPoll = null;
          return;
        }
        this.downloadPoll = setTimeout(tick, 1000);
      } catch (error) {
        state?.replaceChildren(el("div", { class: "notice err" }, esc("读取下载进度失败：" + error.message)));
        button.disabled = false;
        this.downloadPoll = null;
      }
    };
    void tick();
  }

  checkpointList(items) {
    if (!items.length) return this.empty("未检测到 checkpoint", "将模型放入 ComfyUI 的 models/checkpoints 目录后重新扫描。");
    return this.resourceList(items, "checkpoints", (m) => {
      const name = sanitizeResourceName(m.name);
      const row = el("div", { class: "model-row" + (m.compat === "missing" ? " missing" : m.compat === "incompatible" ? " incompatible" : "") }, [
        el("div", { class: "grow" }, [
          this.copyableName(name, "Checkpoint"),
          el("div", { class: "m-sub" }, esc(`${m.family || "通用"} · ${fmtBytes(m.size)}`)),
        ]),
        this.compatBadge(m),
        el("span", { class: "m-warn" }, esc(m.missing_reason || m.warning || "")),
      ]);
      return row;
    });
  }

  loraList(items) {
    if (!items.length) return this.empty("未检测到 LoRA", "将 LoRA 放入 ComfyUI 的 models/loras 目录后重新扫描。");
    return this.resourceList(items, "loras", (m) => {
      const name = sanitizeResourceName(m.name);
      const asList = (value) => Array.isArray(value) ? value : value ? [value] : [];
      const aliases = [...new Set([m.character, ...asList(m.aliases), ...asList(m.tags)].filter(Boolean))];
      const row = el("div", { class: "model-row" + (m.compat === "missing" ? " missing" : m.compat === "incompatible" ? " incompatible" : "") }, [
        el("div", { class: "grow" }, [
          this.copyableName(name, "LoRA"),
          el("div", { class: "m-sub" }, esc(`${m.family || "通用"}${aliases.length ? " · 别名 " + aliases.join(" / ") : ""}`)),
        ]),
        this.compatBadge(m),
        el("span", { class: "m-warn" }, esc(m.missing_reason || m.warning || "")),
      ]);
      return row;
    });
  }

  compatBadge(m) {
    if (m.compat === "ready") return el("span", { class: "badge ok" }, "可用");
    if (m.compat === "missing") return el("span", { class: "badge warn" }, "缺失");
    if (m.compat === "incompatible") return el("span", { class: "badge danger" }, "不兼容");
    return el("span", { class: "badge" }, m.compat || "未知");
  }

  nodeList(d) {
    const nodes = d.nodes || d.custom_nodes || [];
    if (!nodes.length) return this.empty("未检测到自定义节点信息", "连接 ComfyUI 后可查看节点兼容状态。");
    return this.resourceList(nodes, "nodes", (n) => {
      const ok = n.installed !== false;
      const row = el("div", { class: "model-row" + (ok ? "" : " missing") }, [
        el("div", { class: "grow" }, [
          el("div", { class: "m-name" }, esc(n.name)),
          el("div", { class: "m-sub" }, esc(n.version || (ok ? "已安装" : "未安装"))),
        ]),
        ok ? el("span", { class: "badge ok" }, "就绪") : el("span", { class: "badge warn" }, "缺失"),
        ok ? null : el("span", { class: "m-warn" }, esc(n.install_hint || "请通过 ComfyUI Manager 安装此节点")),
      ]);
      return row;
    });
  }

  resourceList(items, key, renderRow) {
    const limit = this.visible[key] || 40;
    const list = el("div", { class: "model-list" }, items.slice(0, limit).map(renderRow));
    if (items.length > limit) {
      const sentinel = el("div", { class: "model-sentinel", dataset: { resource: key } }, [
        loadingState(`正在载入更多 ${key === "loras" ? "LoRA" : "资源"}`, { compact: true }),
      ]);
      list.append(sentinel);
      this.paginationState = { items, key, renderRow, sentinel };
    }
    return list;
  }

  copyableName(name, kind) {
    const label = name || `未命名 ${kind}`;
    const copy = el("button", {
      class: "icon-btn m-copy",
      type: "button",
      title: `复制完整${kind}名称`,
      "aria-label": `复制 ${label}`,
    }, [ic("copy")]);
    copy.addEventListener("click", async () => {
      try {
        await copyText(label);
        toast(`${kind} 名称已复制`, "ok");
      } catch {
        toast("无法访问剪贴板", "warn");
      }
    });
    return el("div", { class: "m-name-wrap" }, [
      el("div", { class: "m-name", title: label }, esc(label)),
      copy,
    ]);
  }

  setupPagination() {
    const sentinel = $(".model-sentinel", this.root);
    if (!sentinel) return;
    if (typeof IntersectionObserver === "function") {
      this.resourceObserver = new IntersectionObserver((entries) => {
        if (entries.some((entry) => entry.isIntersecting)) this.loadMoreResources(sentinel.dataset.resource);
      }, { root: this.root, rootMargin: "160px 0px", threshold: 0.01 });
      this.resourceObserver.observe(sentinel);
      return;
    }

    const onScroll = () => {
      if (this.root.scrollHeight - this.root.scrollTop - this.root.clientHeight <= 160) {
        this.loadMoreResources(sentinel.dataset.resource);
      }
    };
    this.resourceFallback = onScroll;
    this.root.addEventListener("scroll", onScroll, { passive: true });
  }

  teardownPagination() {
    this.resourceObserver?.disconnect();
    this.resourceObserver = null;
    if (this.resourceFallback && this.root) this.root.removeEventListener("scroll", this.resourceFallback);
    this.resourceFallback = null;
  }

  loadMoreResources(key) {
    if (!key || this.resourcePageFrame != null) return;
    const state = this.paginationState;
    if (!state || state.key !== key || !state.sentinel.isConnected) return;
    this.resourceObserver?.disconnect();
    this.resourcePageFrame = requestAnimationFrame(() => {
      this.resourcePageFrame = null;
      const start = this.visible[key] || 40;
      const end = Math.min(start + 40, state.items.length);
      const fragment = document.createDocumentFragment();
      state.items.slice(start, end).forEach((item) => fragment.append(state.renderRow(item)));
      state.sentinel.before(fragment);
      this.visible[key] = end;
      if (end >= state.items.length) {
        state.sentinel.remove();
        this.paginationState = null;
        this.teardownPagination();
      } else if (typeof IntersectionObserver === "function") {
        this.setupPagination();
      }
    });
  }

  empty(title, desc) {
    return el("div", { class: "empty" }, [ic("model"), el("div", { class: "empty-title" }, title), el("div", { class: "empty-desc" }, desc)]);
  }

  mountEnv(container) {
    if (!this.envView) this.envView = new EnvSetupView();
    this.envView.attach(container);
  }

  // app.js 在离开视图时调用 deactivate，返回时调用 activate（可选链，无需改 app.js）
  deactivate() {
    this.envView?.dispose();
  }

  activate() {
    if (this.tab === "env" && this.root) {
      const body = $("#m-body", this.root);
      if (body) this.mountEnv(body);
    }
  }

  dispose() {
    this.searchController?.abort();
    this.searchController = null;
    if (this.downloadPoll) clearTimeout(this.downloadPoll);
    this.downloadPoll = null;
    this.teardownPagination();
    if (this.resourcePageFrame != null) cancelAnimationFrame(this.resourcePageFrame);
    this.resourcePageFrame = null;
    this.paginationState = null;
  }
}

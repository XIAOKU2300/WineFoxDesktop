/* WineFox Desktop — 模型与 LoRA 页
 * 展示 checkpoint / LoRA / 自定义节点状态；v1 无在线下载（缺失给出安装指引）。
 */
import { el, $, $$, ic, esc, sanitizeResourceName, fmtBytes, loadingState, copyText, toast } from "../utils.js";
import { api } from "../api.js";
import { store } from "../store.js";

export class ModelsView {
  constructor() {
    this.tab = "checkpoints";
    this.data = null;
    this.loaded = false;
    this.visible = { checkpoints: 40, loras: 40, nodes: 40 };
    this.resourceObserver = null;
    this.resourceFallback = null;
    this.resourcePageFrame = null;
    this.paginationState = null;
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
          el("div", { class: "lead" }, "兼容状态一目了然；缺失项会给出安装指引（v1 不支持在线下载）。"),
        ]),
        el("button", { class: "btn btn-sm", id: "m-refresh", type: "button" }, [ic("refresh"), esc("重新扫描")]),
      ]),
      el("div", { class: "family-tabs", role: "tablist", "aria-label": "资源类型" }, [
        ["checkpoints", "Checkpoint 主模型"], ["loras", "LoRA"], ["nodes", "自定义节点"],
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
    const d = this.data || {};
    const summary = el("div", { class: "row wrap gap-2" }, [
      el("span", { class: "badge ok" }, esc(`${(d.checkpoints || []).length} 个 checkpoint`)),
      el("span", { class: "badge ok" }, esc(`${(d.loras || []).length} 个 LoRA`)),
      el("span", { class: "badge neutral" }, esc(`${d.lora_profiles?.loaded || 0} 个角色档案`)),
      el("span", { class: "badge warn" }, esc(`${(d.missing_nodes || []).length} 个节点缺失`)),
      d.download?.enabled === false ? el("span", { class: "badge neutral" }, "v1：无在线下载") : null,
    ]);
    let list;
    if (this.tab === "checkpoints") list = this.checkpointList(d.checkpoints || []);
    else if (this.tab === "loras") list = this.loraList(d.loras || []);
    else list = this.nodeList(d);
    body.replaceChildren(summary, list);
    this.setupPagination();
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

  dispose() {
    this.teardownPagination();
    if (this.resourcePageFrame != null) cancelAnimationFrame(this.resourcePageFrame);
    this.resourcePageFrame = null;
    this.paginationState = null;
  }
}

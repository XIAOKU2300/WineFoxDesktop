/* WineFox Desktop — 模型与 LoRA 页
 * 展示 checkpoint / LoRA / 自定义节点状态；v1 无在线下载（缺失给出安装指引）。
 */
import { el, $, $$, ic, esc, sanitizeName, fmtBytes } from "../utils.js";
import { api } from "../api.js";

export class ModelsView {
  constructor() {
    this.tab = "checkpoints";
    this.data = null;
    this.loaded = false;
  }

  async mount(container) {
    this.root = container;
    this.render();
    await this.load();
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
      el("div", { id: "m-body", class: "stack-3" }, [el("div", { class: "load-more" }, "加载中…")]),
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
    body.innerHTML = "";
    body.append(el("div", { class: "load-more" }, "扫描中…"));
    try {
      this.data = await api.models();
      this.renderBody();
      this.loaded = true;
    } catch (e) {
      body.innerHTML = "";
      body.append(el("div", { class: "empty" }, [ic("warn"), el("div", { class: "empty-title" }, "无法获取模型信息"), el("div", { class: "empty-desc" }, esc(e.message))]));
    }
  }

  renderBody() {
    const body = $("#m-body");
    if (!body) return;
    const d = this.data || {};
    const summary = el("div", { class: "row wrap gap-2" }, [
      el("span", { class: "badge ok" }, esc(`${(d.checkpoints || []).length} 个 checkpoint`)),
      el("span", { class: "badge ok" }, esc(`${(d.loras || []).length} 个 LoRA`)),
      el("span", { class: "badge warn" }, esc(`${(d.missing_nodes || []).length} 个节点缺失`)),
      d.download?.enabled === false ? el("span", { class: "badge neutral" }, "v1：无在线下载") : null,
    ]);
    let list;
    if (this.tab === "checkpoints") list = this.checkpointList(d.checkpoints || []);
    else if (this.tab === "loras") list = this.loraList(d.loras || []);
    else list = this.nodeList(d);
    body.innerHTML = "";
    body.append(summary, list);
  }

  checkpointList(items) {
    if (!items.length) return this.empty("未检测到 checkpoint", "将模型放入 ComfyUI 的 models/checkpoints 目录后重新扫描。");
    return el("div", { class: "model-list" }, items.map((m) => {
      const row = el("div", { class: "model-row" + (m.compat === "missing" ? " missing" : m.compat === "incompatible" ? " incompatible" : "") }, [
        el("div", { class: "grow" }, [
          el("div", { class: "m-name" }, esc(sanitizeName(m.name))),
          el("div", { class: "m-sub" }, esc(`${m.family || "通用"} · ${fmtBytes(m.size)}`)),
        ]),
        this.compatBadge(m),
        el("span", { class: "m-warn" }, esc(m.missing_reason || m.warning || "")),
      ]);
      return row;
    }));
  }

  loraList(items) {
    if (!items.length) return this.empty("未检测到 LoRA", "将 LoRA 放入 ComfyUI 的 models/loras 目录后重新扫描。");
    return el("div", { class: "model-list" }, items.map((m) => {
      const row = el("div", { class: "model-row" + (m.compat === "missing" ? " missing" : m.compat === "incompatible" ? " incompatible" : "") }, [
        el("div", { class: "grow" }, [
          el("div", { class: "m-name" }, esc(sanitizeName(m.name))),
          el("div", { class: "m-sub" }, esc(`${m.family || "通用"}${m.tags?.length ? " · " + m.tags.join(" / ") : ""}`)),
        ]),
        this.compatBadge(m),
        el("span", { class: "m-warn" }, esc(m.missing_reason || m.warning || "")),
      ]);
      return row;
    }));
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
    return el("div", { class: "model-list" }, nodes.map((n) => {
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
    }));
  }

  empty(title, desc) {
    return el("div", { class: "empty" }, [ic("model"), el("div", { class: "empty-title" }, title), el("div", { class: "empty-desc" }, desc)]);
  }
}

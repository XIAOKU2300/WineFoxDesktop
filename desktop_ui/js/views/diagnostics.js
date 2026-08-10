/* WineFox Desktop — 诊断页
 * 版本 / ComfyUI 状态 / 节点缺失 / 配置健康 / 脱敏日志导出。
 * 完全离线可用：任何请求失败都以「不可用」状态呈现，不抛错。
 */
import { el, $, $$, ic, esc, sanitizeName, toast, downloadText, loadingState } from "../utils.js";
import { api } from "../api.js";
import { store } from "../store.js";

export class DiagnosticsView {
  constructor() {
    this.data = null;
    this.error = null;
  }

  async mount(container) {
    this.root = container;
    this.render();
    const prefetched = store.get("diagnostics");
    store.set("diagnostics", null);
    if (prefetched) {
      this.data = prefetched;
      this.renderBody();
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
          el("h1", { class: "h1" }, "诊断"),
          el("div", { class: "lead" }, "环境信息与健康检查；导出日志已脱敏，不含密钥与绝对路径。"),
        ]),
        el("div", { class: "toolbar" }, [
          el("button", { class: "btn btn-sm", id: "d-refresh", type: "button" }, [ic("refresh"), esc("重新检查")]),
          el("button", { class: "btn btn-sm", id: "d-export", type: "button" }, [ic("download"), esc("导出脱敏日志")]),
        ]),
      ]),
      el("div", { id: "d-body", class: "stack-4" }, [loadingState("正在汇集诊断信息")]),
    );
    $("#d-refresh")?.addEventListener("click", () => this.load());
    $("#d-export")?.addEventListener("click", () => this.exportLogs());
  }

  async load() {
    const body = $("#d-body");
    if (!body) return;
    body.replaceChildren(loadingState("正在汇集诊断信息"));
    try {
      this.data = await api.diagnostics();
      this.error = null;
    } catch (e) {
      this.data = null;
      this.error = e;
    }
    this.renderBody();
  }

  renderBody() {
    const body = $("#d-body");
    if (!body) return;
    body.replaceChildren();
    if (!this.data) {
      body.append(
        el("div", { class: "card card-pad stack-3" }, [
          el("div", { class: "row gap-3" }, [ic("warn"), el("b", {}, "无法获取诊断数据")]),
          el("p", { class: "text-sm text-muted" }, esc(this.error?.message || "后端服务不可用。诊断页仍然可用：请检查 WineFox 服务是否已启动。")),
        ]),
      );
      return;
    }
    const d = this.data;
    const app = d.app || {};
    const plugin = d.plugin || {};
    const cf = d.comfyui || {};
    const nodes = d.nodes || {};
    const catalog = d.catalog || {};
    const cfg = Array.isArray(d.config_health) ? d.config_health : [];
    const failures = Array.isArray(d.failures) ? d.failures : [];
    const connected = Boolean(cf.connected ?? cf.online ?? cf.status === "ok");
    const workflowReady = catalog.compatible_workflows == null
      ? `${catalog.valid_workflows ?? 0} / ${catalog.workflows ?? 0} 个结构有效`
      : `${catalog.compatible_workflows} / ${catalog.workflows ?? 0} 个可用`;

    const grid = el("div", { class: "diag-grid" }, [
      this.item("WineFox 应用", esc(app.name || "WineFox Desktop"), esc(app.version || "—"), esc(app.build || "")),
      this.item("源插件版本", esc(plugin.version || "—"), esc(plugin.name || "ComfyUI Pro"), esc(plugin.runtime || "桌面适配")),
      this.item(
        "ComfyUI 服务",
        connected
          ? [el("span", { class: "badge ok" }, "已连接"), cf.version ? el("span", { class: "mono" }, esc(cf.version)) : null]
          : el("span", { class: "badge danger" }, "未连接"),
        esc(cf.url ? urlHost(cf.url) : "未配置地址"),
        cf.latency_ms != null ? esc(`检测耗时 ${Math.round(cf.latency_ms)} ms`) : esc(cf.error?.message || "等待连接检测"),
      ),
      this.item(
        "ComfyUI 节点",
        nodes.total != null ? esc(`${nodes.total} 个`) : "未取得",
        nodes.missing?.length
          ? el("span", { class: "badge warn" }, esc(`${nodes.missing.length} 个缺失`))
          : el("span", { class: "badge ok" }, "无缺失项"),
        esc(nodes.detail || "节点数据来自最近一次连接检测"),
      ),
      this.item("工作流", esc(workflowReady), esc(`${catalog.valid_workflows ?? 0} 个结构校验通过`), "仅统计图像工作流"),
      this.item("模型与 LoRA", esc(`${catalog.checkpoints ?? 0} / ${catalog.loras ?? 0}`), "checkpoint / LoRA", esc(`已载入 ${catalog.lora_profiles ?? 0} 份别名档案`)),
    ]);
    body.append(grid);

    if (failures.length) {
      body.append(
        el("div", { class: "section-card" }, [
          el("div", { class: "sec-head" }, [el("h3", {}, "需要处理")]),
          el("div", { class: "sec-body stack-2" }, failures.map((failure) =>
            el("div", { class: "model-row" }, [
              el("div", { class: "grow" }, [
                el("div", { class: "m-name" }, esc(failure.message || failure.code || "诊断项异常")),
                failure.action ? el("div", { class: "m-sub" }, esc(failure.action)) : null,
              ]),
              el("span", { class: `badge ${failure.status === "fail" ? "danger" : "warn"}` }, esc(failure.code || "注意")),
            ]))),
        ]),
      );
    }

    // 配置健康
    body.append(
      el("div", { class: "section-card" }, [
        el("div", { class: "sec-head" }, [el("h3", {}, "配置健康")]),
        el("div", { class: "sec-body stack-2" }, cfg.length ? cfg.map((c) =>
          el("div", { class: "model-row" }, [
            el("div", { class: "grow" }, [el("div", { class: "m-name" }, esc(c.label || c.id)), el("div", { class: "m-sub" }, esc(c.detail || ""))]),
            el("span", { class: "badge " + ({ ok: "ok", warn: "warn", fail: "danger" }[c.status] || "") }, esc({ ok: "正常", warn: "注意", fail: "异常" }[c.status] || c.status)),
          ])) : el("p", { class: "text-sm text-muted" }, "无配置项")),
        ]),
    );

    // 缺失节点
    if (nodes.missing?.length) {
      body.append(
        el("div", { class: "section-card" }, [
          el("div", { class: "sec-head" }, [el("h3", {}, "缺失节点")]),
          el("div", { class: "sec-body stack-2" }, nodes.missing.map((n) =>
            el("div", { class: "model-row missing" }, [
              el("div", { class: "grow" }, [el("div", { class: "m-name" }, esc(sanitizeName(n)))]),
              el("span", { class: "m-warn" }, "请通过 ComfyUI Manager 安装后重启"),
            ]))),
        ]),
      );
    }

    // 节点明细来自同一份诊断快照，避免额外触发一次完整 ComfyUI 探测。
    const customNodes = Array.isArray(nodes.items) ? nodes.items : [];
    if (customNodes?.length) {
      body.append(
        el("div", { class: "section-card" }, [
          el("div", { class: "sec-head" }, [el("h3", {}, "自定义节点")]),
          el("div", { class: "sec-body stack-2" }, customNodes.map((n) =>
            el("div", { class: "model-row" + (n.installed === false ? " missing" : "") }, [
              el("div", { class: "grow" }, [el("div", { class: "m-name" }, esc(sanitizeName(n.name)))]),
              el("span", { class: "badge " + (n.installed === false ? "warn" : "ok") }, n.installed === false ? "缺失" : "就绪"),
            ]))),
        ]),
      );
    }

    // 最近错误 / 日志摘要
    const logTail = d.log_tail || {};
    body.append(
      el("div", { class: "section-card" }, [
        el("div", { class: "sec-head" }, [el("h3", {}, "日志摘要"), el("span", { class: "text-xs text-muted" }, esc(logTail.truncated ? "已截断" : "已脱敏"))]),
        el("div", { class: "sec-body" }, logTail.lines?.length
          ? el("div", { class: "log-box", tabindex: "0", "aria-label": "日志摘要" }, logTail.lines.map((line) => this.logLine(line)))
          : el("p", { class: "text-sm text-muted" }, esc(logTail.detail || "当前还没有可读取的运行日志。"))),
      ]),
    );
  }

  item(label, value, sub, hint) {
    const children = [
      el("div", { class: "d-label" }, esc(label)),
      el("div", { class: "d-value" }, value),
    ];
    if (sub) children.push(el("div", { class: "d-sub" }, sub));
    if (hint) children.push(el("div", { class: "d-sub" }, hint));
    return el("div", { class: "card card-pad diag-item" }, children);
  }

  logLine(line) {
    const m = String(line);
    const span = el("span", {});
    if (/error|失败|exception/i.test(m)) span.className = "le";
    else if (/warn|警告/i.test(m)) span.className = "lw";
    else if (/info|信息/i.test(m)) span.className = "li";
    span.textContent = m + "\n";
    return span;
  }

  async exportLogs() {
    const btn = $("#d-export");
    if (btn) btn.disabled = true;
    try {
      const res = await api.exportDiagnostics();
      const content = res.content || JSON.stringify(res, null, 2);
      downloadText(`winefox_diagnostics_${new Date().toISOString().slice(0, 10)}.log`, content, "text/plain");
      toast("已导出脱敏日志", "ok");
    } catch (e) {
      // 离线：导出当前可见诊断信息
      const fallback = JSON.stringify({ note: "后端不可用，导出为本地摘要", error: e.message, generated_at: new Date().toISOString(), diagnostics: this.data }, null, 2);
      downloadText(`winefox_diagnostics_${new Date().toISOString().slice(0, 10)}.json`, fallback, "application/json");
      toast("后端不可用，已导出本地摘要", "warn");
    }
    if (btn) btn.disabled = false;
  }
}

function urlHost(url) {
  try { return new URL(url).host; } catch { return "—"; }
}

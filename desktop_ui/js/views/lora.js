/* WineFox Desktop — LoRA 库页
 * 本地 LoRA 库管理：自动匹配预览、库列表、别名/触发词编辑、隔离/恢复/删除、R18 内容确认。
 * 对应后端端点（已注册）：GET/PATCH/DELETE /lora/library、{id}、{id}/quarantine、{id}/restore，
 * POST /lora/library/match-preview。
 */
import { el, $, $$, ic, esc, toast, loadingState, copyText } from "../utils.js";
import { api } from "../api.js";
import { store } from "../store.js";

export class LoraLibraryView {
  constructor() {
    this.family = store.get("settings")?.defaults?.family || "illustrious";
    this.items = [];
    this.loaded = false;
    this.loadToken = 0;
    this.previewBusy = false;
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
          el("h1", { class: "h1" }, "LoRA 库"),
          el("div", { class: "lead" }, "管理本地 LoRA：自动匹配预览、别名与触发词维护、隔离与恢复。"),
        ]),
      ]),
      this.matchPreviewPanel(),
      this.libraryPanel(),
    );
    this.bindEvents();
  }

  /* ---------- 匹配预览 ---------- */
  matchPreviewPanel() {
    const panel = el("section", { class: "lora-online section-card", "aria-labelledby": "lora-mp-title" }, [
      el("div", { class: "sec-head" }, [
        el("div", {}, [
          el("h3", { id: "lora-mp-title" }, "LoRA 自动匹配预览"),
          el("div", { class: "hint" }, "输入提示词，预览会命中的本地 LoRA 与连接词，不会改动库内容。"),
        ]),
      ]),
      el("form", { class: "lora-searchbar", id: "lora-mp-form" }, [
        el("div", { class: "field grow" }, [
          el("label", { for: "lora-mp-prompt" }, "提示词"),
          el("textarea", { class: "textarea", id: "lora-mp-prompt", rows: 2, placeholder: "例如：维里奈，高马尾，少女……", autocomplete: "off" }),
        ]),
        el("div", { class: "field lora-family-field" }, [
          el("label", { for: "lora-mp-family" }, "模型族"),
          el("select", { class: "select", id: "lora-mp-family" }, [
            el("option", { value: "illustrious", selected: this.family === "illustrious" }, "Illustrious"),
            el("option", { value: "anima", selected: this.family === "anima" }, "Anima"),
          ]),
        ]),
        el("button", { class: "btn btn-primary lora-search-submit", type: "submit", id: "lora-mp-run", disabled: this.previewBusy }, [ic("wand"), esc(this.previewBusy ? "匹配中…" : "匹配预览")]),
      ]),
      el("div", { class: "lora-online-results", id: "lora-mp-results", "aria-live": "polite" }, [
        el("div", { class: "lora-online-empty" }, "输入提示词后点击“匹配预览”，结果只会展示当前模型族的命中。"),
      ]),
    ]);
    $("#lora-mp-form", panel)?.addEventListener("submit", (event) => {
      event.preventDefault();
      void this.runMatchPreview();
    });
    return panel;
  }

  async runMatchPreview() {
    const prompt = $("#lora-mp-prompt", this.root)?.value?.trim() || "";
    const family = $("#lora-mp-family", this.root)?.value || "illustrious";
    if (!prompt) { toast("先输入提示词再预览", "warn"); return; }
    if (this.previewBusy) return;
    this.previewBusy = true;
    const run = $("#lora-mp-run", this.root);
    if (run) { run.disabled = true; run.replaceChildren(ic("wand"), "匹配中…"); }
    const body = $("#lora-mp-results", this.root);
    body?.replaceChildren(loadingState("正在匹配 LoRA", { compact: true }));
    try {
      const result = await api.loraLibraryMatchPreview(prompt, family, 10);
      if (!this.root?.isConnected) return;
      const matches = Array.isArray(result?.matches) ? result.matches : [];
      const nsfw = matches.filter((m) => m.nsfw);
      const safe = matches.filter((m) => !m.nsfw);
      if (!matches.length) {
        body?.replaceChildren(el("div", { class: "lora-online-empty" }, "没有匹配到任何本地 LoRA。"));
        toast("没有匹配到 LoRA", "warn");
        return;
      }
      if (nsfw.length) {
        const ok = await ensureR18Consent();
        if (!this.root?.isConnected) return;
        if (!ok) {
          body?.replaceChildren(
            el("div", { class: "notice info" }, `匹配结果包含 ${nsfw.length} 条成人/敏感内容，未同意查看，已隐藏。`),
            ...safe.map((m) => this.matchRow(m)),
          );
          if (!safe.length) body?.append(el("div", { class: "lora-online-empty" }, "没有可显示的其他匹配。"));
          return;
        }
      }
      body?.replaceChildren(...matches.map((m) => this.matchRow(m)));
    } catch (error) {
      if (this.root?.isConnected) {
        body?.replaceChildren(el("div", { class: "lora-online-empty" }, "匹配失败：" + (error?.message || "未知错误")));
      }
      toast("LoRA 匹配失败：" + (error?.message || "未知错误"), "err");
    } finally {
      this.previewBusy = false;
      if (run && this.root?.isConnected) { run.disabled = false; run.replaceChildren(ic("wand"), "匹配预览"); }
    }
  }

  matchRow(m) {
    const asList = (v) => Array.isArray(v) ? v : v ? [v] : [];
    const aliases = asList(m.aliases).filter(Boolean).slice(0, 4);
    const triggers = asList(m.triggers).filter(Boolean).slice(0, 6);
    const meta = [m.character, m.family, m.filename].filter(Boolean).join(" · ");
    const score = Number(m.score);
    const scoreText = Number.isFinite(score) ? `匹配度 ${Math.round(score * 100)}%` : "";
    const relevanceText = m.relevance ? `相关度 ${m.relevance}` : "";
    return el("article", { class: "lora-search-result", dataset: { resultId: m.id } }, [
      el("div", { class: "lora-result-main" }, [
        el("div", { class: "row wrap gap-2" }, [
          el("h4", { class: "lora-result-title" }, esc(m.name || m.character || "未命名 LoRA")),
          m.nsfw ? el("span", { class: "badge warn" }, "敏感内容") : null,
          m.disabled ? el("span", { class: "badge danger" }, "已禁用") : null,
        ]),
        meta ? el("div", { class: "m-sub" }, esc(meta)) : null,
        [scoreText, relevanceText].filter(Boolean).length ? el("div", { class: "m-sub" }, esc([scoreText, relevanceText].filter(Boolean).join(" · "))) : null,
        m.connector ? el("div", { class: "m-sub" }, [el("span", { class: "hint" }, "连接词"), el("span", { class: "tag" }, esc(m.connector))]) : null,
        aliases.length ? el("div", { class: "m-sub" }, [el("span", { class: "hint" }, "别名"), ...aliases.map((a) => el("span", { class: "tag" }, esc(a)))]) : null,
        triggers.length ? el("div", { class: "lora-trigger-preview" }, [el("span", { class: "hint" }, "触发词"), ...triggers.map((w) => el("span", { class: "tag" }, esc(w)))]) : null,
        m.note ? el("div", { class: "text-sm text-muted" }, esc(m.note)) : null,
      ]),
    ]);
  }

  /* ---------- 本地库列表 ---------- */
  libraryPanel() {
    const panel = el("section", { class: "section-card", "aria-labelledby": "lora-lib-title" }, [
      el("div", { class: "sec-head" }, [
        el("div", {}, [
          el("h3", { id: "lora-lib-title" }, "本地库"),
          el("div", { class: "hint" }, "已安装的 LoRA 记录，可维护别名、触发词、隔离与禁用。"),
        ]),
        el("div", { class: "row wrap gap-2" }, [
          el("select", { class: "select", id: "lora-lib-family", "aria-label": "按模型族过滤" }, [
            el("option", { value: "illustrious", selected: this.family === "illustrious" }, "Illustrious"),
            el("option", { value: "anima", selected: this.family === "anima" }, "Anima"),
          ]),
          el("button", { class: "btn btn-sm", type: "button", id: "lora-lib-refresh" }, [ic("refresh"), esc("刷新")]),
        ]),
      ]),
      el("div", { id: "lora-lib-body", class: "stack-3" }, [loadingState("正在读取 LoRA 库", { compact: true })]),
    ]);
    return panel;
  }

  async load() {
    const body = $("#lora-lib-body", this.root);
    const token = ++this.loadToken;
    if (body) body.replaceChildren(loadingState("正在读取 LoRA 库", { compact: true }));
    try {
      const result = await api.loraLibraryList(this.family);
      if (token !== this.loadToken || !this.root?.isConnected) return;
      this.items = Array.isArray(result?.items) ? result.items : [];
      this.loaded = true;
      this.renderList();
    } catch (error) {
      if (token !== this.loadToken || !this.root?.isConnected) return;
      if (body) {
        body.replaceChildren(el("div", { class: "empty" }, [
          ic("warn"),
          el("div", { class: "empty-title" }, "无法读取 LoRA 库"),
          el("div", { class: "empty-desc" }, esc(error?.message || "未知错误")),
        ]));
      }
      toast("LoRA 库读取失败：" + (error?.message || "未知错误"), "err");
    }
  }

  renderList() {
    const body = $("#lora-lib-body", this.root);
    if (!body) return;
    if (!this.items.length) {
      body.replaceChildren(this.empty("本地库为空", "当前模型族下还没有 LoRA 记录，可在“模型与 LoRA”页搜索安装。"));
      return;
    }
    body.replaceChildren(el("div", { class: "model-list" }, this.items.map((item) => this.libraryRow(item))));
  }

  libraryRow(item) {
    const asList = (v) => Array.isArray(v) ? v : v ? [v] : [];
    const aliases = [...new Set([item.character, ...asList(item.aliases)].filter(Boolean))];
    const triggers = asList(item.triggers).filter(Boolean).slice(0, 6);
    const quarantined = item.state === "quarantined";
    const stateBadge = quarantined
      ? el("span", { class: "badge warn" }, "已隔离")
      : el("span", { class: "badge ok" }, "已安装");
    const disabledBadge = item.disabled ? el("span", { class: "badge danger" }, "已禁用") : null;
    const label = item.name || "未命名 LoRA";
    return el("div", { class: "model-row" }, [
      el("div", { class: "grow" }, [
        el("div", { class: "m-name-wrap" }, [
          el("div", { class: "m-name", title: label }, esc(label)),
          el("button", { class: "icon-btn m-copy", type: "button", title: "复制完整名称", "aria-label": `复制 ${label}` }, [ic("copy")]),
        ]),
        el("div", { class: "m-sub" }, esc(`${item.family || "通用"} · ${item.filename || ""}${aliases.length ? " · 别名 " + aliases.join(" / ") : ""}`)),
        triggers.length ? el("div", { class: "lora-trigger-preview" }, [el("span", { class: "hint" }, "触发词"), ...triggers.map((w) => el("span", { class: "tag" }, esc(w)))]) : null,
      ]),
      stateBadge,
      disabledBadge,
      el("div", { class: "lora-result-actions" }, [
        el("button", { class: "btn btn-sm", type: "button", "data-edit": item.id }, "编辑"),
        quarantined
          ? el("button", { class: "btn btn-sm", type: "button", "data-restore": item.id }, [ic("restore"), esc("恢复")])
          : el("button", { class: "btn btn-sm", type: "button", "data-quarantine": item.id }, "隔离"),
        el("button", { class: "btn btn-sm", type: "button", "data-toggle": item.id }, item.disabled ? "启用" : "禁用"),
        el("button", { class: "btn btn-sm btn-danger", type: "button", "data-delete": item.id }, [ic("trash"), esc("删除")]),
      ]),
    ]);
  }

  empty(title, desc) {
    return el("div", { class: "empty" }, [ic("model"), el("div", { class: "empty-title" }, title), el("div", { class: "empty-desc" }, desc)]);
  }

  findItem(id) {
    return this.items.find((item) => String(item.id) === String(id)) || null;
  }

  bindEvents() {
    const root = this.root;
    $("#lora-lib-refresh", root)?.addEventListener("click", () => void this.load());
    $("#lora-lib-family", root)?.addEventListener("change", (event) => {
      this.family = event.target.value || "illustrious";
      const previewFamily = $("#lora-mp-family", root);
      if (previewFamily) previewFamily.value = this.family;
      void this.load();
    });
    root.addEventListener("click", (event) => {
      const edit = event.target.closest("[data-edit]");
      if (edit) { this.editDialog(this.findItem(edit.dataset.edit)); return; }
      const toggle = event.target.closest("[data-toggle]");
      if (toggle) { void this.toggleItem(toggle.dataset.toggle); return; }
      const quarantine = event.target.closest("[data-quarantine]");
      if (quarantine) { void this.quarantineItem(quarantine.dataset.quarantine); return; }
      const restore = event.target.closest("[data-restore]");
      if (restore) { void this.restoreItem(restore.dataset.restore); return; }
      const del = event.target.closest("[data-delete]");
      if (del) { void this.deleteItem(del.dataset.delete); return; }
      const copy = event.target.closest(".m-copy");
      if (copy) { void this.copyRowName(copy); }
    });
  }

  async copyRowName(button) {
    const name = button.closest(".model-row")?.querySelector(".m-name")?.textContent || "";
    try {
      await copyText(name);
      toast("LoRA 名称已复制", "ok");
    } catch {
      toast("无法访问剪贴板", "warn");
    }
  }

  async toggleItem(id) {
    const item = this.findItem(id);
    if (!item) return;
    try {
      await api.loraLibraryUpdate(id, { disabled: !item.disabled });
      toast(item.disabled ? "已启用" : "已禁用", "ok");
      void this.load();
    } catch (error) {
      toast("操作失败：" + (error?.message || "未知错误"), "err");
    }
  }

  async quarantineItem(id) {
    const item = this.findItem(id);
    if (!item) return;
    const ok = await this.confirmDialog("隔离 LoRA", `确定将 “${item.name || "该 LoRA"}” 隔离吗？隔离后自动匹配时不会再命中。`, "隔离");
    if (!ok) return;
    try {
      await api.loraLibraryQuarantine(id, "手动隔离");
      toast("已隔离", "ok");
      void this.load();
    } catch (error) {
      toast("隔离失败：" + (error?.message || "未知错误"), "err");
    }
  }

  async restoreItem(id) {
    try {
      await api.loraLibraryRestore(id);
      toast("已恢复", "ok");
      void this.load();
    } catch (error) {
      toast("恢复失败：" + (error?.message || "未知错误"), "err");
    }
  }

  async deleteItem(id) {
    const item = this.findItem(id);
    if (!item) return;
    const ok = await this.confirmDialog("删除 LoRA", `确定从库中删除 “${item.name || "该 LoRA"}” 吗？此操作不可撤销。`, "删除");
    if (!ok) return;
    try {
      await api.loraLibraryDelete(id);
      toast("已删除", "ok");
      void this.load();
    } catch (error) {
      toast("删除失败：" + (error?.message || "未知错误"), "err");
    }
  }

  /* ---------- 编辑弹窗（仿 setSecretDialog 骨架） ---------- */
  editDialog(item) {
    if (!item) return;
    const asList = (v) => Array.isArray(v) ? v : v ? [v] : [];
    const overlay = el("div", { class: "overlay", role: "dialog", "aria-modal": "true", "aria-labelledby": "le-title", "aria-describedby": "le-desc" });
    overlay.append(el("div", { class: "modal" }, [
      el("div", { class: "modal-head" }, [el("h3", { id: "le-title" }, esc(`编辑 ${item.name || "LoRA"}`)), el("button", { class: "icon-btn", type: "button", "aria-label": "关闭" }, [ic("x")])]),
      el("div", { class: "modal-body stack-2" }, [
        el("p", { class: "text-sm text-muted", id: "le-desc" }, "别名与触发词用英文逗号分隔，留空表示清空。"),
        el("div", { class: "field" }, [
          el("label", { for: "le-aliases" }, "别名"),
          el("input", { id: "le-aliases", class: "input", type: "text", value: asList(item.aliases).join(", "), autocomplete: "off" }),
        ]),
        el("div", { class: "field" }, [
          el("label", { for: "le-role" }, "角色"),
          el("input", { id: "le-role", class: "input", type: "text", value: item.role || "", autocomplete: "off" }),
        ]),
        el("div", { class: "field" }, [
          el("label", { for: "le-triggers" }, "触发词"),
          el("input", { id: "le-triggers", class: "input", type: "text", value: asList(item.triggers).join(", "), autocomplete: "off" }),
        ]),
        el("label", { class: "check" }, [
          el("input", { type: "checkbox", id: "le-disabled", checked: !!item.disabled }),
          el("span", { class: "box" }, [ic("check")]),
          el("span", {}, "禁用（自动匹配时忽略）"),
        ]),
      ]),
      el("div", { class: "modal-foot" }, [
        el("button", { class: "btn", type: "button", id: "le-cancel" }, "取消"),
        el("button", { class: "btn btn-primary", type: "button", id: "le-save" }, "保存"),
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
    $("#le-cancel", overlay)?.addEventListener("click", close);
    $("#le-save", overlay)?.addEventListener("click", async () => {
      const save = $("#le-save", overlay);
      if (save?.disabled) return;
      const split = (v) => String(v).split(",").map((s) => s.trim()).filter(Boolean);
      const patch = {
        aliases: split($("#le-aliases", overlay).value),
        role: ($("#le-role", overlay).value || "").trim(),
        triggers: split($("#le-triggers", overlay).value),
        disabled: !!$("#le-disabled", overlay).checked,
      };
      if (save) { save.disabled = true; save.textContent = "保存中…"; }
      try {
        await api.loraLibraryUpdate(item.id, patch);
        close();
        toast("已保存", "ok");
        void this.load();
      } catch (error) {
        if (save) { save.disabled = false; save.textContent = "保存"; }
        toast("保存失败：" + (error?.message || "未知错误"), "err");
      }
    });
    $("#le-aliases", overlay)?.focus();
  }

  /* ---------- 二次确认弹窗 ---------- */
  confirmDialog(title, desc, okLabel = "确认") {
    return new Promise((resolve) => {
      const overlay = el("div", { class: "overlay", role: "dialog", "aria-modal": "true", "aria-labelledby": "cf-title", "aria-describedby": "cf-desc" });
      overlay.append(el("div", { class: "modal" }, [
        el("div", { class: "modal-head" }, [el("h3", { id: "cf-title" }, esc(title)), el("button", { class: "icon-btn", type: "button", "aria-label": "关闭" }, [ic("x")])]),
        el("div", { class: "modal-body" }, [el("p", { class: "text-sm text-muted", id: "cf-desc" }, esc(desc))]),
        el("div", { class: "modal-foot" }, [
          el("button", { class: "btn", type: "button", id: "cf-cancel" }, "取消"),
          el("button", { class: "btn btn-danger", type: "button", id: "cf-ok" }, esc(okLabel)),
        ]),
      ]));
      const previousFocus = document.activeElement;
      document.body.append(overlay);
      let done = false;
      const finish = (value) => {
        if (done) return;
        done = true;
        overlay.remove();
        document.removeEventListener("keydown", onKey);
        previousFocus?.focus();
        resolve(value);
      };
      const close = () => finish(false);
      overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
      overlay.querySelector(".modal-head .icon-btn").addEventListener("click", close);
      const onKey = (e) => {
        if (e.key === "Escape") {
          e.preventDefault();
          close();
          return;
        }
        if (e.key !== "Tab") return;
        const focusable = $$("button:not([disabled])", overlay);
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
      };
      document.addEventListener("keydown", onKey);
      $("#cf-cancel", overlay)?.addEventListener("click", close);
      $("#cf-ok", overlay)?.addEventListener("click", () => finish(true));
      $("#cf-ok", overlay)?.focus();
    });
  }

  dispose() {
    this.loadToken += 1;
  }
}

/* ---------- R18 同意弹窗（仿 setSecretDialog 骨架） ----------
 * 首次渲染 nsfw 匹配前调用；同意后写入 localStorage，仅存本机。
 * 返回 Promise<boolean>：true 表示同意查看，false 表示拒绝。
 */
export function ensureR18Consent() {
  return new Promise((resolve) => {
    if (localStorage.getItem("winefox.r18_consent") === "1") {
      resolve(true);
      return;
    }
    const overlay = el("div", { class: "overlay", role: "dialog", "aria-modal": "true", "aria-labelledby": "r18-title", "aria-describedby": "r18-desc" });
    overlay.append(el("div", { class: "modal" }, [
      el("div", { class: "modal-head" }, [el("h3", { id: "r18-title" }, "内容提醒"), el("button", { class: "icon-btn", type: "button", "aria-label": "关闭" }, [ic("warn")])]),
      el("div", { class: "modal-body" }, [
        el("p", { class: "text-sm text-muted", id: "r18-desc" }, "以下内容包含成人/敏感内容。仅在你确认同意后才会显示相关条目；你的选择只保存在本机。"),
      ]),
      el("div", { class: "modal-foot" }, [
        el("button", { class: "btn", type: "button", id: "r18-cancel" }, "取消"),
        el("button", { class: "btn btn-primary", type: "button", id: "r18-ok" }, "同意查看"),
      ]),
    ]));
    const previousFocus = document.activeElement;
    document.body.append(overlay);
    let done = false;
    const finish = (value) => {
      if (done) return;
      done = true;
      overlay.remove();
      document.removeEventListener("keydown", onKey);
      previousFocus?.focus();
      resolve(value);
    };
    const close = () => finish(false);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
    overlay.querySelector(".modal-head .icon-btn").addEventListener("click", close);
    const onKey = (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        close();
        return;
      }
      if (e.key !== "Tab") return;
      const focusable = $$("button:not([disabled])", overlay);
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
    };
    document.addEventListener("keydown", onKey);
    $("#r18-cancel", overlay)?.addEventListener("click", close);
    $("#r18-ok", overlay)?.addEventListener("click", () => {
      localStorage.setItem("winefox.r18_consent", "1");
      finish(true);
    });
    $("#r18-ok", overlay)?.focus();
  });
}

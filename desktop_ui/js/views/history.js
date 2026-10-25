/* WineFox Desktop — 历史页
 * cursor 分页 + 搜索 + 预览 + 收藏 + 回收站（软删除）。
 */
import { el, $, $$, ic, esc, fmtRelative, sanitizeName, debouncedSearchInput, toast, isDegenerate, saveImageDialog } from "../utils.js";
import { api } from "../api.js";
import { store } from "../store.js";

const PAGE_SIZE = 24;

export class HistoryView {
  constructor() {
    this.cursor = null;
    this.prevCursors = [];
    this.q = "";
    this.filter = "all";
    this.trash = false;
    this.total = null;
    this.loaded = false;
    this.selectMode = false;
    this.selected = new Set(); // 批量选择：记录 id
    this.pageItems = [];
    this.loadToken = 0;
    this.loadingMore = false;
    this.batchBusy = false;
  }

  async mount(container) {
    this.root = container;
    this.render();
    const dirty = store.get("historyDirty");
    const prefetched = dirty ? null : store.get("history");
    store.set("history", null);
    store.set("historyDirty", false);
    await this.load(false, prefetched);
  }

  /** 视图切回时刷新当前页数据：生成页完成任务后历史立即可见，无需重启。 */
  activate() {
    if (!this.root) return;
    store.set("historyDirty", false);
    this.load(false);
  }

  render() {
    const root = this.root;
    root.innerHTML = "";
    // replaceChildren/append 收到 null 会渲染出文本 "null"，末尾条件项需过滤。
    root.append(...[
      el("div", { class: "view-head" }, [
        el("div", {}, [
          el("h1", { class: "h1" }, this.trash ? "回收站" : "历史"),
          el("div", { class: "lead" }, this.trash ? "已删除的记录保留在这里，可恢复或彻底清除。" : "全部生成记录，支持搜索与收藏。"),
        ]),
        el("div", { class: "toolbar" }, [
          this.trash ? null : debouncedSearchInput("搜索提示词 / 种子…", (v) => { this.q = v; this.cursor = null; this.prevCursors = []; this.load(); }),
          this.trash ? null : el("select", { class: "select", "aria-label": "状态筛选", style: "width:auto", id: "h-filter" }, [
            el("option", { value: "all" }, "全部状态"),
            el("option", { value: "completed" }, "已完成"),
            el("option", { value: "failed" }, "失败"),
            el("option", { value: "favorite" }, "已收藏"),
          ]),
          el("button", { class: "btn btn-sm" + (this.selectMode ? " btn-primary" : ""), id: "h-select", type: "button", "aria-pressed": this.selectMode ? "true" : "false" }, [
            ic("check"), esc(this.selectMode ? "完成" : "选择"),
          ]),
          el("button", { class: "btn btn-sm" + (this.trash ? " btn-danger" : ""), id: "h-trash", type: "button" }, [
            ic("trashOpen"), esc(this.trash ? "返回历史" : "回收站"),
          ]),
        ]),
      ]),
      el("div", { id: "h-body", class: "stack-3" }, [
        el("div", { class: "img-grid" + (this.selectMode ? " select-mode" : ""), id: "h-grid" }, []),
        el("div", { class: "load-more" }, "加载中…"),
      ]),
      this.selectMode ? this.batchBar() : null,
    ].filter(Boolean));
    const filter = $("#h-filter");
    if (filter) {
      filter.value = this.filter;
      filter.addEventListener("change", (e) => { this.filter = e.target.value; this.cursor = null; this.prevCursors = []; this.load(); });
    }
    $("#h-trash")?.addEventListener("click", () => { this.trash = !this.trash; this.cursor = null; this.prevCursors = []; this.selectMode = false; this.selected.clear(); this.render(); this.load(); });
    $("#h-select")?.addEventListener("click", () => {
      this.selectMode = !this.selectMode;
      this.selected.clear();
      this.cursor = null;
      this.prevCursors = [];
      this.render();
      this.load();
    });
    if (this.selectMode) this.bindBatchBar();
  }

  /* ============ 批量选择 ============ */
  batchBar() {
    const n = this.selected.size;
    const trash = this.trash;
    const pageIds = this.selectableIds();
    const allSelected = pageIds.length > 0 && pageIds.every((id) => this.selected.has(id));
    return el("div", { class: "batch-bar", role: "toolbar", "aria-label": "批量操作" }, [
      el("span", { class: "bb-count" }, `已选 ${n} 项`),
      el("button", { class: "btn btn-sm", type: "button", id: "bb-all" }, [ic("check"), esc(allSelected ? "取消全选" : "全选")]),
      trash
        ? el("button", { class: "btn btn-sm", type: "button", id: "bb-restore" }, [ic("restore"), esc("恢复")])
        : el("button", { class: "btn btn-sm", type: "button", id: "bb-fav" }, [ic("favFill"), esc("收藏")]),
      trash
        ? el("button", { class: "btn btn-sm btn-danger", type: "button", id: "bb-purge" }, [ic("x"), esc("彻底删除")])
        : el("button", { class: "btn btn-sm", type: "button", id: "bb-unfav" }, [ic("fav"), esc("取消收藏")]),
      trash
        ? null
        : el("button", { class: "btn btn-sm btn-danger", type: "button", id: "bb-trash" }, [ic("trash"), esc("移入回收站")]),
      el("button", { class: "btn btn-sm btn-primary", type: "button", id: "bb-done" }, "完成"),
    ]);
  }

  bindBatchBar() {
    $("#bb-all", this.root)?.addEventListener("click", () => {
      const pageIds = this.selectableIds();
      const allSelected = pageIds.length && pageIds.every((id) => this.selected.has(id));
      if (allSelected) pageIds.forEach((id) => this.selected.delete(id));
      else pageIds.forEach((id) => this.selected.add(id));
      this.updateSelection();
    });
    $("#bb-fav", this.root)?.addEventListener("click", () => this.batchAction(async (id) => api.favoriteHistory(id), "已收藏"));
    $("#bb-unfav", this.root)?.addEventListener("click", () => this.batchAction(async (id) => api.unfavoriteHistory(id), "已取消收藏"));
    $("#bb-trash", this.root)?.addEventListener("click", () => this.batchAction(async (id) => api.trashHistory(id), "已移入回收站"));
    $("#bb-restore", this.root)?.addEventListener("click", () => this.batchAction(async (id) => api.restoreHistory(id), "已恢复"));
    $("#bb-purge", this.root)?.addEventListener("click", () => this.batchAction(async (id) => api.purgeHistory(id), "已彻底删除"));
    $("#bb-done", this.root)?.addEventListener("click", () => { this.selectMode = false; this.selected.clear(); this.render(); this.load(); });
  }

  async batchAction(fn, label) {
    if (this.batchBusy) return;
    const ids = [...this.selected];
    if (!ids.length) { toast("请先选择记录", "warn"); return; }
    this.batchBusy = true;
    $$(".batch-bar button", this.root).forEach((button) => { button.disabled = true; });
    let ok = 0;
    for (const id of ids) {
      try { await fn(id); ok++; } catch { /* 单项失败继续 */ }
    }
    toast(`${label} ${ok} 项`, ok === ids.length ? "ok" : "warn");
    store.set("favoritesDirty", true);
    this.selectMode = false;
    this.selected.clear();
    this.batchBusy = false;
    this.render();
    this.load();
  }

  toggleSelect(id) {
    if (!this.selectableIds().includes(id)) return;
    if (this.selected.has(id)) this.selected.delete(id);
    else this.selected.add(id);
    this.updateSelection();
  }

  selectableIds() {
    return this.pageItems.filter((it) => it.image?.url).map((it) => it.id);
  }

  updateSelection() {
    // 保留已经加载的页和滚动位置，仅更新选择状态。
    for (const cell of $$(".img-cell", this.root)) {
      const id = cell.dataset.id;
      cell.classList.toggle("selected", this.selected.has(id));
      const check = cell.querySelector(".cell-check");
      if (check) check.classList.toggle("on", this.selected.has(id));
    }
    const count = $(".bb-count", this.root);
    if (count) count.textContent = `已选 ${this.selected.size} 项`;
    const all = $("#bb-all", this.root);
    if (all) {
      const pageIds = this.selectableIds();
      all.textContent = pageIds.length && pageIds.every((id) => this.selected.has(id)) ? "取消全选" : "全选";
    }
  }

  async load(append = false, prefetched = null) {
    if (append && (this.loadingMore || !this.cursor)) return;
    const token = ++this.loadToken;
    const cursor = append ? this.cursor : null;
    this.loadingMore = append;
    if (!append) this.cursor = null;
    const grid = $("#h-grid", this.root);
    const body = $("#h-body", this.root);
    if (!grid) return;
    if (!append) {
      grid.innerHTML = "";
      const loader = $(".load-more", body);
      if (loader) loader.textContent = "加载中…";
    }
    try {
      const q = { limit: PAGE_SIZE };
      if (cursor) q.cursor = cursor;
      if (this.q) q.q = this.q;
      if (this.filter !== "all") q.status = this.filter;
      if (this.trash) q.trash = "1";
      const res = prefetched || await api.history(q);
      if (token !== this.loadToken) return;
      this.total = res.total ?? this.total;
      const items = res.items || [];
      if (!append) this.pageItems = items;
      else this.pageItems = this.pageItems.concat(items);
      const visibleIds = new Set(this.selectableIds());
      for (const id of this.selected) {
        if (!visibleIds.has(id)) this.selected.delete(id);
      }
      if (!append) this.prevCursors = [];
      else this.prevCursors.push(cursor);
      this.cursor = res.next_cursor || null;
      this.renderItems(items, append);
      if (this.selectMode) this.updateSelection();
      const loader = $(".load-more", body);
      if (loader) {
        if (res.next_cursor) {
          loader.textContent = "";
          const more = el("button", { class: "btn btn-sm", type: "button" }, "加载更多");
          more.addEventListener("click", () => this.load(true));
          loader.append(more);
        } else {
          loader.textContent = items.length ? "没有更多了" : "";
        }
      }
      this.loaded = true;
    } catch (e) {
      if (token !== this.loadToken) return;
      const loader = $(".load-more", body);
      if (loader) loader.textContent = "加载失败：" + e.message;
    } finally {
      if (token === this.loadToken) this.loadingMore = false;
    }
  }

  renderItems(items, append) {
    const grid = $("#h-grid");
    if (!grid) return;
    grid.classList.toggle("select-mode", this.selectMode);
    const cells = items.map((it) => this.cell(it));
    if (append) grid.append(...cells);
    else {
      grid.innerHTML = "";
      if (!items.length) {
        const empty = el("div", { class: "empty", style: "column-span: all" }, [
          ic(this.trash ? "trashOpen" : "image"),
          el("div", { class: "empty-title" }, this.trash ? "回收站是空的" : this.q ? "没有匹配的记录" : "还没有生成记录"),
          el("div", { class: "empty-desc" }, this.trash ? "删除的历史记录会出现在这里。" : "去「生成」页创建第一张图，或调整搜索条件。"),
        ]);
        grid.append(empty);
      } else {
        grid.append(...cells);
      }
    }
  }

  cell(it) {
    const hasImage = !!it.image?.url;
    const fav = !!it.favorite;
    const selected = this.selectMode && this.selected.has(it.id);
    // prompt 退化（null/undefined/"null" 字符串等）时回退到 id，绝不显示 "null"。
    const promptText = !isDegenerate(it.prompt)
      ? (it.prompt.length > 60 ? it.prompt.slice(0, 60) + "…" : it.prompt)
      : (isDegenerate(it.id) ? "" : it.id);
    const labelText = promptText || it.id || "历史记录";
    const cell = el("figure", {
      class: "img-cell" + (!hasImage ? " no-image" : "") + (this.trash ? " trashed" : "") + (selected ? " selected" : ""),
      tabindex: "0",
      role: "button",
      "aria-label": `预览记录：${labelText}`,
      dataset: { id: it.id },
    }, [
      this.selectMode && hasImage ? el("span", { class: "cell-check" + (selected ? " on" : ""), "aria-hidden": "true" }, [ic("check")]) : null,
      hasImage ? el("img", { src: it.image.url, alt: labelText, loading: "lazy", decoding: "async" })
        : el("div", { class: "history-failure" }, [ic("warn"), el("b", {}, "生成失败"), el("span", {}, esc(it.error || "点击查看详情"))]),
      fav ? el("span", { class: "fav-dot" }, [ic("fav")]) : null,
      el("figcaption", { class: "cell-meta" }, [
        el("span", {}, esc(promptText)),
        el("span", {}, esc(fmtRelative(it.created_at))),
      ]),
      hasImage ? el("div", { class: "cell-actions" }, [
        this.trash
          ? el("button", { class: "cell-btn", type: "button", "aria-label": "恢复", title: "恢复" }, [ic("restore")])
          : el("button", { class: "cell-btn" + (fav ? " on" : ""), type: "button", "aria-label": fav ? "取消收藏" : "收藏", title: fav ? "取消收藏" : "收藏" }, [ic("fav")]),
        this.trash
          ? el("button", { class: "cell-btn", type: "button", "aria-label": "彻底删除", title: "彻底删除" }, [ic("x")])
          : el("button", { class: "cell-btn", type: "button", "aria-label": "移入回收站", title: "移入回收站" }, [ic("trash")]),
      ]) : null,
    ]);
    // 点击卡片本身就是预览，悬浮操作只保留收藏/回收，减少视觉噪音。
    const btns = $$(".cell-btn", cell);
    if (hasImage && this.trash) {
      btns[0].addEventListener("click", async (e) => {
        e.stopPropagation();
        try { await api.restoreHistory(it.id); toast("已恢复", "ok"); this.load(); }
        catch (err) { toast("恢复失败：" + err.message, "err"); }
      });
      btns[1].addEventListener("click", async (e) => {
        e.stopPropagation();
        try { await api.purgeHistory(it.id); toast("已彻底删除", "ok"); this.load(); }
        catch (err) { toast("删除失败：" + err.message, "err"); }
      });
    } else if (hasImage) {
      btns[0].addEventListener("click", async (e) => {
        e.stopPropagation();
        try {
          if (fav) { await api.unfavoriteHistory(it.id); toast("已取消收藏", "ok"); }
          else { await api.favoriteHistory(it.id); toast("已收藏", "ok"); }
          store.set("favoritesDirty", true);
          this.load();
        } catch (err) { toast("操作失败：" + err.message, "err"); }
      });
      btns[1].addEventListener("click", async (e) => {
        e.stopPropagation();
        try { await api.trashHistory(it.id); toast("已移入回收站", "ok"); this.load(); }
        catch (err) { toast("操作失败：" + err.message, "err"); }
      });
    }
    cell.addEventListener("click", () => {
      if (this.selectMode && hasImage) this.toggleSelect(it.id);
      else this.preview(it);
    });
    cell.addEventListener("keydown", (e) => {
      if (e.target !== cell) return;
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        if (this.selectMode && hasImage) this.toggleSelect(it.id);
        else this.preview(it);
      }
    });
    return cell;
  }

  preview(it) {
    const overlay = el("div", { class: "overlay preview-overlay", role: "dialog", "aria-modal": "true", "aria-label": "记录预览" });
    const modal = el("div", { class: "modal" }, [
      el("div", { class: "modal-head" }, [
        el("h3", {}, "记录详情"),
        el("button", { class: "icon-btn", type: "button", "aria-label": "关闭" }, [ic("x")]),
      ]),
      el("div", { class: "preview-body" }, [
        it.image?.url ? el("img", { src: it.image.url, alt: isDegenerate(it.prompt) ? "历史图片" : it.prompt })
          : el("div", { class: "notice err", role: "alert" }, esc(it.error || "生成失败，未产生图片")),
        el("div", { class: "preview-meta" }, [
          el("div", { class: "pm-block" }, [el("span", { class: "pm-label" }, "提示词"), el("span", { class: "pm-val" }, esc(it.prompt || "—"))]),
          el("div", { class: "pm-block" }, [el("span", { class: "pm-label" }, "负面提示词"), el("span", { class: "pm-val" }, esc(it.negative_prompt || "—"))]),
          el("div", { class: "row wrap gap-3" }, [
            el("div", { class: "pm-block" }, [el("span", { class: "pm-label" }, "种子"), el("span", { class: "pm-val mono" }, esc(String(it.seed ?? "—")))]),
            el("div", { class: "pm-block" }, [el("span", { class: "pm-label" }, "尺寸"), el("span", { class: "pm-val mono" }, esc(it.width ? `${it.width}×${it.height}` : "—"))]),
            el("div", { class: "pm-block" }, [el("span", { class: "pm-label" }, "步数 / CFG"), el("span", { class: "pm-val mono" }, esc(`${it.steps ?? "—"} / ${it.cfg ?? "—"}`))]),
          ]),
          el("div", { class: "pm-block" }, [el("span", { class: "pm-label" }, "模型族 / 工作流"), el("span", { class: "pm-val" }, esc(`${it.family || "—"} / ${sanitizeName(it.workflow) || "—"}`))]),
          el("div", { class: "pm-block" }, [el("span", { class: "pm-label" }, "创建时间"), el("span", { class: "pm-val" }, esc(fmtRelative(it.created_at)))]),
          it.image?.url ? el("div", { class: "row gap-2" }, [
            el("button", { class: "btn btn-primary btn-sm", type: "button", id: "pv-save" }, [ic("download"), esc("保存")]),
            el("button", { class: "btn btn-sm", type: "button", id: "pv-fav" }, [ic(it.favorite ? "favFill" : "fav"), esc(it.favorite ? "取消收藏" : "收藏")]),
            this.trash ? null : el("button", { class: "btn btn-sm btn-danger", type: "button", id: "pv-trash" }, [ic("trash"), esc("删除")]),
          ]) : null,
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
    $("#pv-save", modal)?.addEventListener("click", async () => {
      try {
        const res = await saveImageDialog(it.image?.url || "", `winefox_${it.id}.png`);
        if (res.ok && res.downloaded) toast("已开始保存图片", "ok");
        else if (res.ok && res.path) toast("已保存到 " + res.path, "ok");
        else if (!res.ok && !res.cancelled) toast("保存失败，请重试", "err");
      } catch (e) { toast("保存失败：" + e.message, "err"); }
    });
    $("#pv-fav", modal)?.addEventListener("click", async () => {
      try {
        if (it.favorite) { await api.unfavoriteHistory(it.id); it.favorite = false; }
        else { await api.favoriteHistory(it.id); it.favorite = true; }
        store.set("favoritesDirty", true);
        const b = $("#pv-fav", modal);
        if (b) {
          b.replaceChildren(ic(it.favorite ? "favFill" : "fav"), esc(it.favorite ? "取消收藏" : "收藏"));
          const ico = b.querySelector("svg");
          if (ico && it.favorite) ico.style.color = "var(--danger)";
        }
        close(); this.load();
      } catch (e) { toast("操作失败：" + e.message, "err"); }
    });
    $("#pv-trash", modal)?.addEventListener("click", async () => {
      try { await api.trashHistory(it.id); close(); this.load(); toast("已移入回收站", "ok"); }
      catch (e) { toast("操作失败：" + e.message, "err"); }
    });
  }
}

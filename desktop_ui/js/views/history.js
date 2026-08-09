/* WineFox Desktop — 历史页
 * cursor 分页 + 搜索 + 预览 + 收藏 + 回收站（软删除）。
 */
import { el, $, $$, ic, esc, fmtRelative, sanitizeName, debouncedSearchInput, toast } from "../utils.js";
import { api } from "../api.js";

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
          el("button", { class: "btn btn-sm" + (this.trash ? " btn-danger" : ""), id: "h-trash", type: "button" }, [
            ic("trashOpen"), esc(this.trash ? "返回历史" : "回收站"),
          ]),
        ]),
      ]),
      el("div", { id: "h-body", class: "stack-3" }, [
        el("div", { class: "img-grid", id: "h-grid" }, []),
        el("div", { class: "load-more" }, "加载中…"),
      ]),
    );
    const filter = $("#h-filter");
    if (filter) {
      filter.value = this.filter;
      filter.addEventListener("change", (e) => { this.filter = e.target.value; this.cursor = null; this.prevCursors = []; this.load(); });
    }
    $("#h-trash")?.addEventListener("click", () => { this.trash = !this.trash; this.cursor = null; this.prevCursors = []; this.render(); this.load(); });
  }

  async load(append = false) {
    const grid = $("#h-grid");
    const body = $("#h-body");
    if (!grid) return;
    if (!append) {
      grid.innerHTML = "";
      const loader = $(".load-more", body);
      if (loader) loader.textContent = "加载中…";
    }
    try {
      const q = { limit: PAGE_SIZE };
      if (this.cursor) q.cursor = this.cursor;
      if (this.q) q.q = this.q;
      if (this.filter !== "all") q.status = this.filter;
      if (this.trash) q.trash = "1";
      const res = await api.history(q);
      this.total = res.total ?? this.total;
      const items = res.items || [];
      if (!append) this.prevCursors = [];
      else this.prevCursors.push(this.cursor);
      this.cursor = res.next_cursor || null;
      this.renderItems(items, append);
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
      const loader = $(".load-more", body);
      if (loader) loader.textContent = "加载失败：" + e.message;
    }
  }

  renderItems(items, append) {
    const grid = $("#h-grid");
    if (!grid) return;
    const cells = items.map((it) => this.cell(it));
    if (append) grid.append(...cells);
    else {
      grid.innerHTML = "";
      if (!items.length) {
        const empty = el("div", { class: "empty", style: "grid-column:1/-1" }, [
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
    const fav = !!it.favorite;
    const cell = el("figure", {
      class: "img-cell" + (this.trash ? " trashed" : ""),
      tabindex: "0",
      role: "button",
      "aria-label": `预览记录：${it.prompt || it.id}`,
      dataset: { id: it.id },
    }, [
      el("img", { src: it.image?.url || "", alt: "", loading: "lazy" }),
      fav ? el("span", { class: "fav-dot" }, [ic("fav")]) : null,
      el("figcaption", { class: "cell-meta" }, [
        el("span", {}, esc(it.prompt ? (it.prompt.length > 60 ? it.prompt.slice(0, 60) + "…" : it.prompt) : it.id)),
        el("span", {}, esc(fmtRelative(it.created_at))),
      ]),
      el("div", { class: "cell-actions" }, [
        el("button", { class: "cell-btn", type: "button", "aria-label": "预览", title: "预览" }, [ic("eye")]),
        this.trash
          ? el("button", { class: "cell-btn", type: "button", "aria-label": "恢复", title: "恢复" }, [ic("restore")])
          : el("button", { class: "cell-btn" + (fav ? " on" : ""), type: "button", "aria-label": fav ? "取消收藏" : "收藏", title: fav ? "取消收藏" : "收藏" }, [ic("fav")]),
        this.trash
          ? el("button", { class: "cell-btn", type: "button", "aria-label": "彻底删除", title: "彻底删除" }, [ic("x")])
          : el("button", { class: "cell-btn", type: "button", "aria-label": "移入回收站", title: "移入回收站" }, [ic("trash")]),
      ]),
    ]);
    const btns = $$(".cell-btn", cell);
    btns[0].addEventListener("click", (e) => { e.stopPropagation(); this.preview(it); });
    if (this.trash) {
      btns[1].addEventListener("click", async (e) => {
        e.stopPropagation();
        try { await api.restoreHistory(it.id); toast("已恢复", "ok"); this.load(); }
        catch (err) { toast("恢复失败：" + err.message, "err"); }
      });
      btns[2].addEventListener("click", async (e) => {
        e.stopPropagation();
        try { await api.purgeHistory(it.id); toast("已彻底删除", "ok"); this.load(); }
        catch (err) { toast("删除失败：" + err.message, "err"); }
      });
    } else {
      btns[1].addEventListener("click", async (e) => {
        e.stopPropagation();
        try {
          if (fav) { await api.unfavoriteHistory(it.id); toast("已取消收藏", "ok"); }
          else { await api.favoriteHistory(it.id); toast("已收藏", "ok"); }
          this.load();
        } catch (err) { toast("操作失败：" + err.message, "err"); }
      });
      btns[2].addEventListener("click", async (e) => {
        e.stopPropagation();
        try { await api.trashHistory(it.id); toast("已移入回收站", "ok"); this.load(); }
        catch (err) { toast("操作失败：" + err.message, "err"); }
      });
    }
    cell.addEventListener("click", () => this.preview(it));
    cell.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); this.preview(it); } });
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
        el("img", { src: it.image?.url || "", alt: it.prompt || "历史图片" }),
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
          el("div", { class: "row gap-2" }, [
            el("a", { class: "btn btn-primary btn-sm", href: it.image?.url || "#", download: `winefox_${it.id}.png`, target: "_blank", rel: "noopener" }, [ic("download"), esc("保存")]),
            el("button", { class: "btn btn-sm", type: "button", id: "pv-fav" }, [ic("fav"), esc(it.favorite ? "取消收藏" : "收藏")]),
            this.trash ? null : el("button", { class: "btn btn-sm btn-danger", type: "button", id: "pv-trash" }, [ic("trash"), esc("删除")]),
          ]),
        ]),
      ]),
    ]);
    overlay.append(modal);
    document.body.append(overlay);
    const close = () => overlay.remove();
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
    modal.querySelector(".modal-head .icon-btn").addEventListener("click", close);
    const onKey = (e) => { if (e.key === "Escape") { close(); document.removeEventListener("keydown", onKey); } };
    document.addEventListener("keydown", onKey);
    modal.querySelector(".modal-head .icon-btn").focus();
    $("#pv-fav", modal)?.addEventListener("click", async () => {
      try {
        if (it.favorite) { await api.unfavoriteHistory(it.id); it.favorite = false; }
        else { await api.favoriteHistory(it.id); it.favorite = true; }
        close(); this.load();
      } catch (e) { toast("操作失败：" + e.message, "err"); }
    });
    $("#pv-trash", modal)?.addEventListener("click", async () => {
      try { await api.trashHistory(it.id); close(); this.load(); toast("已移入回收站", "ok"); }
      catch (e) { toast("操作失败：" + e.message, "err"); }
    });
  }
}

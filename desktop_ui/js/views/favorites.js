/* WineFox Desktop — 收藏页
 * 分类筛选 + 备注编辑 + 新增收藏（从历史记录或直接创建）。
 */
import { el, $, $$, ic, esc, fmtRelative, debouncedSearchInput, toast } from "../utils.js";
import { api } from "../api.js";

export class FavoritesView {
  constructor() {
    this.q = "";
    this.category = "all";
    this.categories = [];
    this.loaded = false;
  }

  async mount(container) {
    this.root = container;
    this.render();
    await this.loadCats();
    await this.load();
  }

  render() {
    const root = this.root;
    root.innerHTML = "";
    root.append(
      el("div", { class: "view-head" }, [
        el("div", {}, [
          el("h1", { class: "h1" }, "收藏"),
          el("div", { class: "lead" }, "把喜欢的生成结果分类归档，随时回看。"),
        ]),
        el("div", { class: "toolbar" }, [
          debouncedSearchInput("搜索收藏…", (v) => { this.q = v; this.load(); }),
          el("button", { class: "btn btn-primary btn-sm", id: "f-add", type: "button" }, [ic("fav"), esc("新建收藏")]),
        ]),
      ]),
      el("div", { class: "fav-cats", id: "f-cats", role: "group", "aria-label": "收藏分类" }, []),
      el("div", { id: "f-body" }, [
        el("div", { class: "img-grid", id: "f-grid" }, []),
        el("div", { class: "load-more" }, "加载中…"),
      ]),
    );
    $("#f-add")?.addEventListener("click", () => this.addDialog());
    this.renderCats();
  }

  renderCats() {
    const wrap = $("#f-cats");
    if (!wrap) return;
    wrap.innerHTML = "";
    const all = el("button", { class: "cat-chip", type: "button", "aria-pressed": this.category === "all" ? "true" : "false" }, [
      esc("全部"),
      el("span", { class: "cnt" }, esc(String(this.total ?? ""))),
    ]);
    all.addEventListener("click", () => { this.category = "all"; this.renderCats(); this.load(); });
    wrap.append(all);
    for (const c of this.categories) {
      const chip = el("button", { class: "cat-chip", type: "button", "aria-pressed": this.category === c.id ? "true" : "false" }, [
        esc(c.label || c.id),
        el("span", { class: "cnt" }, esc(String(c.count ?? ""))),
      ]);
      chip.addEventListener("click", () => { this.category = c.id; this.renderCats(); this.load(); });
      wrap.append(chip);
    }
  }

  async loadCats() {
    try {
      const res = await api.categories();
      this.categories = res.items || res || [];
      this.renderCats();
    } catch { /* 离线静默 */ }
  }

  async load() {
    const grid = $("#f-grid");
    if (!grid) return;
    grid.innerHTML = "";
    $(".load-more", this.root).textContent = "加载中…";
    try {
      const q = {};
      if (this.q) q.q = this.q;
      if (this.category !== "all") q.category = this.category;
      const res = await api.favorites(q);
      this.total = res.total;
      const items = res.items || [];
      grid.innerHTML = "";
      if (!items.length) {
        grid.append(el("div", { class: "empty", style: "grid-column:1/-1" }, [
          ic("fav"),
          el("div", { class: "empty-title" }, this.q ? "没有匹配的收藏" : "还没有收藏"),
          el("div", { class: "empty-desc" }, this.q ? "换一个关键词试试。" : "在「历史」中点星标，或点击「新建收藏」。"),
        ]));
      } else {
        grid.append(...items.map((it) => this.cell(it)));
      }
      const loader = $(".load-more", this.root);
      loader.textContent = items.length ? `共 ${this.total ?? items.length} 条` : "";
      this.renderCats();
      this.loaded = true;
    } catch (e) {
      $(".load-more", this.root).textContent = "加载失败：" + e.message;
    }
  }

  cell(it) {
    const cell = el("div", { class: "fav-cell" }, [
      el("div", { class: "fav-thumb", tabindex: "0", role: "button", "aria-label": "预览收藏" }, [
        el("img", { src: it.image?.url || "", alt: it.prompt || "收藏图片", loading: "lazy" }),
        el("div", { class: "cell-actions" }, [
          el("button", { class: "cell-btn", type: "button", "aria-label": "预览", title: "预览" }, [ic("eye")]),
          el("button", { class: "cell-btn", type: "button", "aria-label": "编辑备注", title: "编辑备注" }, [ic("gear")]),
          el("button", { class: "cell-btn", type: "button", "aria-label": "删除收藏", title: "删除收藏" }, [ic("trash")]),
        ]),
      ]),
      el("div", { class: "fav-note" }, [
        el("b", {}, esc(it.category ? `${it.category} · ` : "") + (it.note || it.prompt || "未命名收藏")),
        el("span", {}, esc(fmtRelative(it.created_at))),
      ]),
    ]);
    const thumb = $(".fav-thumb", cell);
    const btns = $$(".cell-btn", cell);
    thumb.addEventListener("click", () => this.preview(it));
    thumb.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); this.preview(it); } });
    btns[0].addEventListener("click", (e) => { e.stopPropagation(); this.preview(it); });
    btns[1].addEventListener("click", (e) => { e.stopPropagation(); this.editDialog(it); });
    btns[2].addEventListener("click", async (e) => {
      e.stopPropagation();
      try { await api.deleteFavorite(it.id); toast("已删除收藏", "ok"); await this.loadCats(); this.load(); }
      catch (err) { toast("删除失败：" + err.message, "err"); }
    });
    return cell;
  }

  preview(it) {
    const overlay = el("div", { class: "overlay preview-overlay", role: "dialog", "aria-modal": "true", "aria-label": "收藏预览" });
    overlay.append(el("div", { class: "modal" }, [
      el("div", { class: "modal-head" }, [
        el("h3", {}, esc(it.category ? it.category : "收藏")),
        el("button", { class: "icon-btn", type: "button", "aria-label": "关闭" }, [ic("x")]),
      ]),
      el("div", { class: "preview-body" }, [
        el("img", { src: it.image?.url || "", alt: it.prompt || "收藏图片" }),
        el("div", { class: "preview-meta" }, [
          it.note ? el("div", { class: "pm-block" }, [el("span", { class: "pm-label" }, "备注"), el("span", { class: "pm-val" }, esc(it.note))]) : null,
          el("div", { class: "pm-block" }, [el("span", { class: "pm-label" }, "提示词"), el("span", { class: "pm-val" }, esc(it.prompt || "—"))]),
          el("div", { class: "pm-block" }, [el("span", { class: "pm-label" }, "分类"), el("span", { class: "pm-val" }, esc(it.category || "未分类"))]),
          el("div", { class: "row gap-2" }, [
            el("a", { class: "btn btn-primary btn-sm", href: it.image?.url || "#", download: `favorite_${it.id}.png` }, [ic("download"), esc("保存")]),
          ]),
        ]),
      ]),
    ]));
    document.body.append(overlay);
    const close = () => overlay.remove();
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
    overlay.querySelector(".modal-head .icon-btn").addEventListener("click", close);
    const onKey = (e) => { if (e.key === "Escape") { close(); document.removeEventListener("keydown", onKey); } };
    document.addEventListener("keydown", onKey);
    overlay.querySelector(".modal-head .icon-btn").focus();
  }

  addDialog() {
    this.formDialog(null);
  }

  editDialog(it) {
    this.formDialog(it);
  }

  formDialog(it) {
    const overlay = el("div", { class: "overlay", role: "dialog", "aria-modal": "true", "aria-label": it ? "编辑收藏" : "新建收藏" });
    const catVal = it?.category || (this.category !== "all" ? this.category : "");
    overlay.append(el("div", { class: "modal" }, [
      el("div", { class: "modal-head" }, [
        el("h3", {}, it ? "编辑收藏" : "新建收藏"),
        el("button", { class: "icon-btn", type: "button", "aria-label": "关闭" }, [ic("x")]),
      ]),
      el("div", { class: "modal-body" }, [
        el("div", { class: "field" }, [
          el("label", { for: "fl-note" }, "备注"),
          el("input", { id: "fl-note", class: "input", type: "text", placeholder: "例如：构图参考、配色方案…", value: it?.note || "" }),
        ]),
        el("div", { class: "field" }, [
          el("label", { for: "fl-cat" }, "分类"),
          el("input", { id: "fl-cat", class: "input", type: "text", list: "fl-cat-list", placeholder: "输入或选择已有分类", value: catVal || "" }),
          el("datalist", { id: "fl-cat-list" }, this.categories.map((c) => el("option", { value: c.label || c.id }))),
        ]),
        it ? null : el("div", { class: "field" }, [
          el("label", { for: "fl-prompt" }, "提示词（可选，用于检索）"),
          el("textarea", { id: "fl-prompt", class: "textarea", rows: 3, placeholder: "粘贴历史提示词…" }, it?.prompt || ""),
        ]),
      ]),
      el("div", { class: "modal-foot" }, [
        el("button", { class: "btn", type: "button", id: "fl-cancel" }, "取消"),
        el("button", { class: "btn btn-primary", type: "button", id: "fl-save" }, it ? "保存修改" : "创建"),
      ]),
    ]));
    document.body.append(overlay);
    const close = () => overlay.remove();
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
    overlay.querySelector(".modal-head .icon-btn").addEventListener("click", close);
    const onKey = (e) => { if (e.key === "Escape") { close(); document.removeEventListener("keydown", onKey); } };
    document.addEventListener("keydown", onKey);
    overlay.querySelector(".modal-head .icon-btn").focus();
    $("#fl-cancel", overlay)?.addEventListener("click", close);
    $("#fl-save", overlay)?.addEventListener("click", async () => {
      const note = $("#fl-note", overlay).value.trim();
      const category = $("#fl-cat", overlay).value.trim();
      const promptEl = $("#fl-prompt", overlay);
      try {
        if (it) await api.updateFavorite(it.id, { note, category });
        else await api.createFavorite({ note, category, prompt: promptEl ? promptEl.value.trim() || null : null });
        close();
        toast(it ? "已保存" : "已创建收藏", "ok");
        await this.loadCats();
        this.load();
      } catch (e) { toast("保存失败：" + e.message, "err"); }
    });
  }
}

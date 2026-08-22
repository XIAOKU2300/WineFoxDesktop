/* WineFox Desktop — 收藏页
 * 分类筛选 + 备注编辑 + 新增收藏（从历史记录或直接创建）。
 */
import { el, $, $$, ic, esc, fmtRelative, debouncedSearchInput, loadingState, toast, downloadUrl, saveImageDialog } from "../utils.js";
import { api } from "../api.js";
import { store } from "../store.js";

/* 分类字段兼容：真实后端 /categories 返回 {name, count}，mock 返回 {id, label, count}。
 * 统一在这里归一化，避免 chip 显示 "undefined" 且点击筛选失效。 */
const catId = (c) => String(c?.id ?? c?.name ?? c?.label ?? "");
const catLabel = (c) => String(c?.label ?? c?.name ?? c?.id ?? "");

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
    const dirtyFav = store.get("favoritesDirty");
    const prefetchedCategories = store.get("categories");
    const prefetchedFavorites = dirtyFav ? null : store.get("favorites");
    store.set("categories", null);
    store.set("favorites", null);
    store.set("favoritesDirty", false);
    await this.loadCats(prefetchedCategories);
    await this.load(prefetchedFavorites);
  }

  /** 视图切回时刷新：生成/收藏操作后无需重启即可看到最新收藏。 */
  activate() {
    if (!this.root) return;
    store.set("favoritesDirty", false);
    this.loadCats();
    this.load();
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
      el("div", { id: "f-body" }, [loadingState("正在整理收藏")]),
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
      const id = catId(c);
      if (!id) continue;
      const chip = el("button", { class: "cat-chip", type: "button", "aria-pressed": this.category === id ? "true" : "false" }, [
        esc(catLabel(c) || id),
        el("span", { class: "cnt" }, esc(String(c.count ?? ""))),
      ]);
      chip.addEventListener("click", () => { this.category = id; this.renderCats(); this.load(); });
      wrap.append(chip);
    }
  }

  async loadCats(prefetched = null) {
    try {
      const res = prefetched || await api.categories();
      this.categories = res.items || res || [];
      this.renderCats();
    } catch { /* 离线静默 */ }
  }

  async load(prefetched = null) {
    const body = $("#f-body");
    if (!body) return;
    body.replaceChildren(loadingState("正在整理收藏"));
    try {
      const q = {};
      if (this.q) q.q = this.q;
      if (this.category !== "all") q.category = this.category;
      const res = prefetched || await api.favorites(q);
      this.total = res.total;
      const items = res.items || [];
      const grid = el("div", { class: "img-grid", id: "f-grid" });
      if (!items.length) {
        grid.append(el("div", { class: "empty", style: "column-span: all" }, [
          ic("fav"),
          el("div", { class: "empty-title" }, this.q ? "没有匹配的收藏" : "还没有收藏"),
          el("div", { class: "empty-desc" }, this.q ? "换一个关键词试试。" : "在「历史」中点星标，或点击「新建收藏」。"),
        ]));
      } else {
        grid.append(...items.map((it) => this.cell(it)));
      }
      body.replaceChildren(
        grid,
        el("div", { class: "load-more" }, items.length ? `共 ${this.total ?? items.length} 条` : ""),
      );
      this.renderCats();
      this.loaded = true;
    } catch (e) {
      body.replaceChildren(el("div", { class: "empty" }, [
        ic("warn"),
        el("div", { class: "empty-title" }, "收藏加载失败"),
        el("div", { class: "empty-desc" }, esc(e.message)),
      ]));
    }
  }

  cell(it) {
    // 手动新建的提示词收藏没有图片，用占位徽记替代，避免破图图标。
    const hasImage = !!it.image?.url;
    const cell = el("div", { class: "fav-cell" }, [
      el("div", { class: "fav-thumb", tabindex: "0", role: "button", "aria-label": "预览收藏" }, [
        hasImage
          ? el("img", { src: it.image.url, alt: it.prompt || "收藏图片", loading: "lazy", decoding: "async" })
          : el("div", { class: "fav-thumb-placeholder", "aria-hidden": "true" }, [ic("fav")]),
        el("div", { class: "cell-actions" }, [
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
    // 点击缩略图即预览；悬浮操作只保留编辑/删除，与历史卡片保持同一设计语言。
    thumb.addEventListener("click", () => this.preview(it));
    thumb.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); this.preview(it); } });
    btns[0].addEventListener("click", (e) => { e.stopPropagation(); this.editDialog(it); });
    btns[1].addEventListener("click", async (e) => {
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
        it.image?.url
          ? el("img", { src: it.image.url, alt: it.prompt || "收藏图片" })
          : el("div", { class: "fav-thumb-placeholder is-large", "aria-hidden": "true" }, [ic("fav")]),
        el("div", { class: "preview-meta" }, [
          it.note ? el("div", { class: "pm-block" }, [el("span", { class: "pm-label" }, "备注"), el("span", { class: "pm-val" }, esc(it.note))]) : null,
          el("div", { class: "pm-block" }, [el("span", { class: "pm-label" }, "提示词"), el("span", { class: "pm-val" }, esc(it.prompt || "—"))]),
          el("div", { class: "pm-block" }, [el("span", { class: "pm-label" }, "分类"), el("span", { class: "pm-val" }, esc(it.category || "未分类"))]),
          el("div", { class: "row gap-2" }, [
            el("button", { class: "btn btn-primary btn-sm", type: "button", id: "pv-save" }, [ic("download"), esc("保存")]),
          ]),
        ]),
      ]),
    ]));
    document.body.append(overlay);
    requestAnimationFrame(() => requestAnimationFrame(() => overlay.classList.add("open")));
    const onKey = (e) => { if (e.key === "Escape") close(); };
    const close = () => {
      overlay.classList.add("closing");
      document.removeEventListener("keydown", onKey);
      setTimeout(() => overlay.remove(), 180);
    };
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
    overlay.querySelector(".modal-head .icon-btn").addEventListener("click", close);
    document.addEventListener("keydown", onKey);
    overlay.querySelector(".modal-head .icon-btn").focus();
    $("#pv-save", overlay)?.addEventListener("click", async () => {
      try {
        const res = await saveImageDialog(it.image?.url || "", `favorite_${it.id}.png`);
        if (res.ok && res.downloaded) toast("已开始保存图片", "ok");
        else if (res.ok && res.path) toast("已保存到 " + res.path, "ok");
        else if (!res.ok && !res.cancelled) toast("保存失败，请重试", "err");
      } catch (e) { toast("保存失败：" + e.message, "err"); }
    });
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
          el("datalist", { id: "fl-cat-list" }, this.categories.map((c) => el("option", { value: catLabel(c) || catId(c) }))),
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

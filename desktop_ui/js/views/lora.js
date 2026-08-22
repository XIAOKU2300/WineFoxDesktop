/* WineFox Desktop — LoRA 工作台。
 * 在线发现、本地档案与自动匹配集中在同一入口；文件管理仅支持隔离和恢复。
 */
import { el, $, $$, ic, esc, toast, loadingState, copyText } from "../utils.js";
import { api, loraPreviewUrl } from "../api.js";
import { store } from "../store.js";

function formatLoraDownloadError(error) {
  const message = String(error?.message || error || "LoRA 下载失败").trim();
  if (/HTTP\s*401|Unauthorized|requires you to be logged in|要求登录/i.test(message)) {
    return "Civitai 资源要求登录：请到设置 → 认证与密钥配置 Civitai 登录 Cookie，保存后重试；也可以换用公开资源。";
  }
  return message;
}
export class LoraLibraryView {
  constructor() {
    this.tab = "installed";
    this.family = store.get("settings")?.defaults?.family || "illustrious";
    this.items = [];
    this.loaded = false;
    this.loadError = "";
    this.loadToken = 0;
    this.showQuarantined = false;
    this.localQuery = "";
    this.onlineQuery = "";
    this.onlineFamily = this.family;
    this.onlineSources = ["civitai", "huggingface"];
    this.onlineResults = [];
    this.onlineErrors = [];
    this.onlineSearchedTerms = [];
    this.onlineHasSearched = false;
    this.onlinePage = 1;
    this.onlinePageSize = 20;
    this.onlineHasMore = false;
    this.onlineTotal = 0;
    this.onlineAdultConsent = false;
    this.onlineLoading = false;
    this.searchController = null;
    this.downloadPolls = new Map();
    this.downloadCapabilities = store.get("models")?.download || null;
    this.previewBusy = false;
    this.matchPrompt = "";
    this.matchResults = null;
    this.matchNotice = "";
    this.matchError = "";
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
      el("div", { class: "view-head lora-view-head" }, [
        el("div", {}, [
          el("h1", { class: "h1" }, "LoRA 工作台"),
          el("div", { class: "lead" }, "搜索、安装、整理与自动匹配都在这里完成。"),
        ]),
      ]),
      el("div", { class: "lora-workspace-tabs", role: "tablist", "aria-label": "LoRA 工作区" }, [
        this.workspaceTab("installed", "model", "已安装"),
        this.workspaceTab("discover", "search", "发现"),
        this.workspaceTab("match", "wand", "匹配测试"),
      ]),
      el("div", { class: "lora-workspace", id: "lora-workspace" }),
    );
    this.bindEvents();
    this.renderWorkspace();
  }

  workspaceTab(id, icon, label) {
    const children = [ic(icon), el("span", {}, label)];
    if (id === "installed") {
      children.push(el("span", { class: "lora-tab-count", id: "lora-installed-count" }, this.loaded ? String(this.items.length) : "…"));
    }
    return el("button", {
      class: "lora-workspace-tab",
      type: "button",
      role: "tab",
      "aria-selected": this.tab === id ? "true" : "false",
      dataset: { loraTab: id },
    }, children);
  }

  setTab(tab) {
    if (!["installed", "discover", "match"].includes(tab) || tab === this.tab) return;
    this.tab = tab;
    this.renderTabs();
    this.renderWorkspace();
    if (tab === "installed" && !this.loaded) void this.load();
  }

  renderTabs() {
    $$(".lora-workspace-tab", this.root).forEach((button) => {
      button.setAttribute("aria-selected", button.dataset.loraTab === this.tab ? "true" : "false");
    });
    const count = $("#lora-installed-count", this.root);
    if (count) count.textContent = this.loaded ? String(this.items.length) : "…";
  }

  renderWorkspace() {
    const body = $("#lora-workspace", this.root);
    if (!body) return;
    if (this.tab === "discover") body.replaceChildren(this.discoverPanel());
    else if (this.tab === "match") body.replaceChildren(this.matchPreviewPanel());
    else body.replaceChildren(this.libraryPanel());
  }

  familySelect(id, value = this.family) {
    return el("select", { class: "select", id, "aria-label": "模型族" }, [
      el("option", { value: "illustrious", selected: value === "illustrious" }, "Illustrious"),
      el("option", { value: "anima", selected: value === "anima" }, "Anima"),
    ]);
  }

  previewThumb(className, token, placeholderIcon = "image") {
    const thumb = el("div", {
      class: `${className}${token ? " is-loading" : " is-placeholder"}`,
      "aria-hidden": "true",
    });
    let retryCount = 0;
    let image = null;
    const baseUrl = token ? loraPreviewUrl(token) : "";
    const applyNaturalRatio = () => {
      if (!image?.naturalWidth || !image?.naturalHeight) return;
      // Keep the source composition instead of forcing every preview into a
      // portrait box. The small guard prevents pathological provider images
      // from creating an unusably tall card.
      const ratio = Math.max(0.55, Math.min(2.4, image.naturalWidth / image.naturalHeight));
      thumb.style.setProperty("--lora-preview-ratio", ratio.toFixed(4));
      thumb.dataset.previewRatio = `${image.naturalWidth}/${image.naturalHeight}`;
    };
    const showPlaceholder = () => {
      // Keep the historical handler name for the UI contract; once an image
      // exists, the same path renders a retryable error instead of a silent
      // blank placeholder.
      if (image) {
        showError();
        return;
      }
      thumb.classList.remove("is-loading", "is-loaded");
      thumb.classList.add("is-placeholder");
      thumb.replaceChildren(ic(placeholderIcon));
    };
    const showError = () => {
      thumb.classList.remove("is-loading", "is-loaded", "is-placeholder");
      thumb.classList.add("is-error");
      thumb.removeAttribute("aria-hidden");
      thumb.replaceChildren(
        image,
        el("button", {
          class: "lora-preview-retry",
          type: "button",
          title: "缩略图加载失败，点击重试",
          "aria-label": "缩略图加载失败，点击重试",
          onClick: (event) => {
            event.preventDefault();
            event.stopPropagation();
            retryCount += 1;
            beginLoad();
          },
        }, [ic("refresh"), el("span", { class: "lora-preview-error-label" }, "加载失败 · 重试")]),
      );
    };
    const beginLoad = () => {
      thumb.classList.remove("is-error", "is-placeholder", "is-loaded");
      thumb.classList.add("is-loading");
      thumb.setAttribute("aria-hidden", "true");
      thumb.replaceChildren(image);
      const suffix = retryCount ? `${baseUrl.includes("?") ? "&" : "?"}_retry=${retryCount}` : "";
      image.src = `${baseUrl}${suffix}`;
    };
    if (!token) {
      showPlaceholder();
      return thumb;
    }
    image = el("img", {
      alt: "",
      loading: "lazy",
      decoding: "async",
      referrerpolicy: "no-referrer",
      onload: () => {
        applyNaturalRatio();
        thumb.classList.remove("is-loading");
        thumb.classList.add("is-loaded");
        thumb.setAttribute("aria-hidden", "true");
      },
      onerror: showPlaceholder,
    });
    beginLoad();
    return thumb;
  }

  /* ---------- 本地库 ---------- */
  libraryPanel() {
    const active = this.items.filter((item) => item.state !== "quarantined" && !item.disabled).length;
    const disabled = this.items.filter((item) => item.disabled).length;
    const quarantined = this.items.filter((item) => item.state === "quarantined").length;
    return el("section", { class: "lora-pane", "aria-labelledby": "lora-lib-title" }, [
      el("div", { class: "lora-pane-heading" }, [
        el("div", {}, [
          el("h2", { id: "lora-lib-title" }, "已安装"),
          el("p", {}, "维护别名、触发词和自动匹配状态。隔离会移动真实文件，但不会永久删除。"),
        ]),
        el("div", { class: "lora-statline", "aria-label": "本地 LoRA 状态" }, [
          el("span", {}, [el("b", {}, String(this.items.length)), " 当前模型族"]),
          el("span", { class: "text-ok" }, [el("b", {}, String(active)), " 匹配启用"]),
          disabled ? el("span", { class: "text-warn" }, [el("b", {}, String(disabled)), " 已禁用"]) : null,
          quarantined ? el("span", {}, [el("b", {}, String(quarantined)), " 已隔离"]) : null,
        ]),
      ]),
      el("div", { class: "lora-toolbar" }, [
        el("div", { class: "lora-filter-search grow" }, [
          ic("search"),
          el("input", { class: "input", id: "lora-local-query", type: "search", value: this.localQuery, placeholder: "搜索名称、别名或触发词", autocomplete: "off" }),
        ]),
        this.familySelect("lora-lib-family"),
        el("label", { class: "check lora-quarantine-filter" }, [
          el("input", { type: "checkbox", id: "lora-lib-quarantined", checked: this.showQuarantined }),
          el("span", { class: "box" }, [ic("check")]),
          el("span", {}, "显示已隔离"),
        ]),
        el("button", { class: "icon-btn", type: "button", id: "lora-lib-refresh", title: "重新读取本地 LoRA", "aria-label": "重新读取本地 LoRA" }, [ic("refresh")]),
      ]),
      el("div", { id: "lora-lib-body", class: "lora-library-list", "aria-live": "polite" }, [this.libraryContent()]),
    ]);
  }

  libraryContent() {
    if (!this.loaded && !this.loadError) return loadingState("正在读取 LoRA 库", { compact: true });
    if (this.loadError) return this.empty("无法读取 LoRA 库", this.loadError);
    const items = this.filteredItems();
    if (!items.length && this.localQuery) return this.empty("没有匹配结果", "换一个名称、别名或触发词试试。");
    if (!items.length) return this.empty("当前模型族还没有 LoRA", "切换到“发现”搜索并安装，完成后会自动进入本地档案。", "去发现", "discover");
    return el("div", { class: "lora-library-rows" }, items.map((item) => this.libraryRow(item)));
  }

  filteredItems() {
    const query = this.localQuery.trim().toLowerCase();
    if (!query) return this.items;
    return this.items.filter((item) => {
      const values = [item.name, item.filename, item.character, item.role, ...(item.aliases || []), ...(item.triggers || [])];
      return values.some((value) => String(value || "").toLowerCase().includes(query));
    });
  }

  async load() {
    const body = $("#lora-lib-body", this.root);
    const token = ++this.loadToken;
    if (body) body.replaceChildren(loadingState("正在读取 LoRA 库", { compact: true }));
    try {
      const result = await api.loraLibraryList(this.family, { include_quarantined: this.showQuarantined });
      if (token !== this.loadToken || !this.root?.isConnected) return;
      this.items = Array.isArray(result?.items) ? result.items : [];
      this.loaded = true;
      this.loadError = "";
      this.renderTabs();
      if (this.tab === "installed") this.renderWorkspace();
    } catch (error) {
      if (token !== this.loadToken || !this.root?.isConnected) return;
      this.loaded = false;
      this.loadError = error?.message || "未知错误";
      if (this.tab === "installed") this.renderWorkspace();
      toast("LoRA 库读取失败：" + (error?.message || "未知错误"), "err");
    }
  }

  renderList() {
    const body = $("#lora-lib-body", this.root);
    if (!body) return;
    body.replaceChildren(this.libraryContent());
  }

  libraryRow(item) {
    const asList = (v) => Array.isArray(v) ? v : v ? [v] : [];
    const aliases = [...new Set([item.character, ...asList(item.aliases)].filter(Boolean))];
    const triggers = asList(item.triggers).filter(Boolean).slice(0, 6);
    const quarantined = item.state === "quarantined";
    const label = item.name || "未命名 LoRA";
    const thumb = this.previewThumb("lora-library-thumb", item.preview, "model");
    return el("article", { class: `lora-library-row${quarantined ? " is-quarantined" : ""}${item.disabled ? " is-disabled" : ""}` }, [
      thumb,
      el("div", { class: "lora-library-main" }, [
        el("div", { class: "lora-title-line" }, [
          el("h3", { title: label }, esc(label)),
          el("button", { class: "icon-btn lora-copy-name", type: "button", title: "复制完整名称", "aria-label": `复制 ${label}` }, [ic("copy")]),
        ]),
        el("div", { class: "lora-file-meta" }, esc([item.family || "通用", item.filename].filter(Boolean).join(" · "))),
        aliases.length ? el("div", { class: "lora-chipline" }, [el("span", { class: "lora-chip-label" }, "别名"), ...aliases.slice(0, 5).map((value) => el("span", { class: "tag" }, esc(value)))]) : null,
        triggers.length ? el("div", { class: "lora-trigger-preview" }, [el("span", { class: "hint" }, "触发词"), ...triggers.map((w) => el("span", { class: "tag" }, esc(w)))]) : null,
      ]),
      el("div", { class: "lora-library-state" }, [
        quarantined ? el("span", { class: "badge warn" }, "已隔离") : el("span", { class: "badge ok" }, "已安装"),
        item.disabled ? el("span", { class: "badge danger" }, "匹配关闭") : null,
      ]),
      el("div", { class: "lora-library-actions" }, [
        el("button", { class: "icon-btn", type: "button", "data-edit": item.id, title: "编辑档案", "aria-label": `编辑 ${label}` }, [ic("gear")]),
        quarantined
          ? el("button", { class: "icon-btn", type: "button", "data-restore": item.id, title: "恢复文件", "aria-label": `恢复 ${label}` }, [ic("restore")])
          : el("button", { class: "icon-btn", type: "button", "data-quarantine": item.id, title: "隔离文件", "aria-label": `隔离 ${label}` }, [ic("trashOpen")]),
        quarantined ? null : el("button", {
          class: "lora-enable-switch",
          type: "button",
          role: "switch",
          "aria-checked": item.disabled ? "false" : "true",
          "aria-label": item.disabled ? `启用 ${label} 的自动匹配` : `禁用 ${label} 的自动匹配`,
          title: item.disabled ? "启用自动匹配" : "禁用自动匹配",
          dataset: { toggle: item.id },
        }, [el("span", { class: "lora-switch-track", "aria-hidden": "true" }, [el("span")])]),
      ]),
    ]);
  }

  empty(title, desc, actionLabel = "", tab = "") {
    const node = el("div", { class: "empty lora-empty-state" }, [ic("model"), el("div", { class: "empty-title" }, title), el("div", { class: "empty-desc" }, desc)]);
    if (actionLabel && tab) node.append(el("button", { class: "btn btn-primary btn-sm", type: "button", dataset: { loraTab: tab } }, actionLabel));
    return node;
  }

  findItem(id) {
    return this.items.find((item) => String(item.id) === String(id)) || null;
  }

  bindEvents() {
    const root = this.root;
    root.addEventListener("click", (event) => {
      const tab = event.target.closest("[data-lora-tab]");
      if (tab) { this.setTab(tab.dataset.loraTab); return; }
      const refresh = event.target.closest("#lora-lib-refresh");
      if (refresh) { void this.load(); return; }
      const probe = event.target.closest("#lora-bridge-probe");
      if (probe) { void this.probeDownloadChannel(probe); return; }
      const download = event.target.closest("[data-lora-download]");
      if (download && !download.disabled) { void this.startOnlineDownload(download); return; }
      const detail = event.target.closest("[data-lora-detail]");
      if (detail) {
        const item = this.onlineResults.find((entry) => String(entry.id) === String(detail.dataset.loraDetail));
        if (item) this.openOnlineDetail(item);
        return;
      }
      const pageButton = event.target.closest("[data-lora-page]");
      if (pageButton && !pageButton.disabled) {
        void this.changeOnlinePage(Number(pageButton.dataset.loraPage || 1));
        return;
      }
      const edit = event.target.closest("[data-edit]");
      if (edit) { this.editDialog(this.findItem(edit.dataset.edit)); return; }
      const toggle = event.target.closest("[data-toggle]");
      if (toggle) { void this.toggleItem(toggle.dataset.toggle); return; }
      const quarantine = event.target.closest("[data-quarantine]");
      if (quarantine) { void this.quarantineItem(quarantine.dataset.quarantine); return; }
      const restore = event.target.closest("[data-restore]");
      if (restore) { void this.restoreItem(restore.dataset.restore); return; }
      const copy = event.target.closest(".lora-copy-name");
      if (copy) { void this.copyRowName(copy); }
    });
    root.addEventListener("keydown", (event) => {
      const detail = event.target.closest("[data-lora-detail]");
      if (!detail || event.target !== detail || !["Enter", " "].includes(event.key)) return;
      event.preventDefault();
      const item = this.onlineResults.find((entry) => String(entry.id) === String(detail.dataset.loraDetail));
      if (item) this.openOnlineDetail(item);
    });
    root.addEventListener("input", (event) => {
      if (event.target.id !== "lora-local-query") return;
      this.localQuery = event.target.value || "";
      this.renderList();
      $("#lora-local-query", this.root)?.focus();
    });
    root.addEventListener("change", (event) => {
      if (event.target.id === "lora-lib-quarantined") {
        this.showQuarantined = !!event.target.checked;
        void this.load();
        return;
      }
      if (["lora-lib-family", "lora-online-family", "lora-mp-family"].includes(event.target.id)) {
        this.family = event.target.value || "illustrious";
        this.onlineFamily = this.family;
        this.loaded = false;
        this.loadError = "";
        void this.load();
        return;
      }
      const source = event.target.closest("[data-lora-source]");
      if (!source) return;
      if (source.checked && !this.onlineSources.includes(source.value)) this.onlineSources.push(source.value);
      if (!source.checked) this.onlineSources = this.onlineSources.filter((value) => value !== source.value);
      if (!this.onlineSources.length) {
        this.onlineSources = ["civitai"];
        const civitai = $("[data-lora-source][value='civitai']", root);
        if (civitai) civitai.checked = true;
      }
    });
    root.addEventListener("submit", (event) => {
      if (event.target.id === "lora-search-form") {
        event.preventDefault();
        void this.searchOnlineLoras();
      } else if (event.target.id === "lora-mp-form") {
        event.preventDefault();
        void this.runMatchPreview();
      }
    });
  }

  async copyRowName(button) {
    const name = button.closest(".lora-library-row")?.querySelector(".lora-title-line h3")?.textContent || "";
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

  /* ---------- 在线发现与下载 ---------- */
  discoverPanel() {
    return el("section", { class: "lora-pane", "aria-labelledby": "lora-discover-title" }, [
      el("div", { class: "lora-pane-heading" }, [
        el("div", {}, [
          el("h2", { id: "lora-discover-title" }, "发现 LoRA"),
          el("p", {}, "跨来源搜索并安装到当前 ComfyUI；搜索结果不会自动下载。"),
        ]),
        el("button", { class: "btn btn-sm", type: "button", id: "lora-bridge-probe" }, [ic("refresh"), esc("检查安装通道")]),
      ]),
      el("form", { class: "lora-discover-form", id: "lora-search-form" }, [
        el("div", { class: "lora-discover-search" }, [
          el("div", { class: "field grow" }, [
            el("label", { for: "lora-online-query" }, "名称、角色或中文别称"),
            el("div", { class: "lora-input-with-icon" }, [
              ic("search"),
              el("input", { class: "input", id: "lora-online-query", type: "search", value: this.onlineQuery, maxlength: "120", placeholder: "例如：维里奈、Verina", autocomplete: "off" }),
            ]),
          ]),
          el("div", { class: "field lora-family-field" }, [el("label", { for: "lora-online-family" }, "模型族"), this.familySelect("lora-online-family", this.onlineFamily)]),
          el("button", { class: "btn btn-primary lora-search-submit", type: "submit", disabled: this.onlineLoading }, [ic("search"), esc(this.onlineLoading ? "搜索中…" : "搜索")]),
        ]),
        el("div", { class: "lora-source-row" }, [
          el("span", { class: "lora-source-label" }, "来源"),
          el("div", { class: "lora-source-select", id: "lora-source-select" }, [
            ...["civitai", "huggingface", "civitai_red"].map((key) => {
              const label = key === "civitai" ? "Civitai" : key === "huggingface" ? "Hugging Face" : "Civitai Red (R18+)";
              return el("label", { class: "lora-source-chip" }, [
                el("input", { type: "checkbox", value: key, checked: this.onlineSources.includes(key), dataset: { loraSource: key } }),
                el("span", {}, esc(label)),
              ]);
            }),
          ]),
          el("div", { class: "lora-channel-status", id: "lora-channel-status" }, this.downloadHint()),
        ]),
      ]),
      el("div", { class: "lora-market-grid", id: "lora-online-results", "aria-live": "polite" }, this.renderOnlineResults()),
    ]);
  }

  downloadHint() {
    const capabilities = this.downloadCapabilities || {};
    if (capabilities.mode === "local" || (capabilities.mode === "auto" && capabilities.local_configured)) {
      return "安装到本机 LoRA 目录";
    }
    if (capabilities.mode === "comfyui_bridge") return "由 ComfyUI Bridge 写入远端 LoRA 目录";
    return "按设置自动选择本机目录或 Bridge";
  }

  renderOnlineResults() {
    if (this.onlineLoading) return [el("div", { class: "lora-results-state" }, [loadingState("正在检索 LoRA 来源", { compact: true })])];
    const nodes = [];
    if (this.onlineErrors.length) {
      const firstDetail = this.onlineErrors[0] || "";
      const isProxyIssue = /代理|proxy|Clash|超时|timeout|连接/i.test(firstDetail);
      const isCredentialIssue = /401|Cookie|cookie|登录|Token 拒绝|拒访问/.test(firstDetail);
      let summary;
      let action = "";
      if (this.onlineResults.length) {
        summary = "部分来源连接失败，已展示其余可用结果。";
      } else if (isCredentialIssue) {
        summary = "搜索/下载需要 Civitai 登录凭证。";
        action = "到 设置 → 认证与密钥 配置 Civitai API Token（优先）或登录 Cookie。";
      } else if (isProxyIssue) {
        summary = "搜索失败：疑似代理或网络连接问题。";
        action = "到 设置 → LoRA 下载 填写代理（如 http://127.0.0.1:7890；留空自动用系统代理），或点“测试下载链路”诊断。";
      } else {
        summary = "搜索失败，来源无法连接。";
        action = "可到 设置 → LoRA 下载 点击“测试下载链路”定位问题。";
      }
      nodes.push(el("div", { class: "notice warn lora-search-diagnostics", role: "status" }, [
        el("b", {}, summary),
        el("span", { class: "lora-search-error-detail" }, esc(firstDetail)),
        action ? el("span", { class: "lora-search-error-detail" }, esc(action)) : null,
        (this.onlineErrors.length > 1)
          ? el("span", { class: "lora-search-error-more" }, `另有 ${this.onlineErrors.length - 1} 条错误信息` )
          : null,
      ]));
    } else if (this.onlineHasSearched && this.onlineSearchedTerms.length > 1) {
      nodes.push(el("div", { class: "lora-search-terms" }, [
        el("span", {}, "已尝试"),
        ...this.onlineSearchedTerms.slice(0, 6).map((term) => el("span", { class: "tag" }, esc(term))),
      ]));
    }
    if (!this.onlineResults.length) {
      const title = this.onlineHasSearched
        ? (this.onlineErrors.length ? "暂时无法完成搜索" : "没有找到匹配结果")
        : "从名称开始";
      const detail = this.onlineHasSearched
        ? (this.onlineErrors.length ? "至少一个来源请求失败；可重试或在设置中配置代理。" : "可换用英文角色名、LoRA 原名，或切换模型族后重试。")
        : "支持中文别称、英文角色名和 LoRA 原名。";
      nodes.push(el("div", { class: "lora-results-state" }, [ic("search"), el("b", {}, title), el("span", {}, detail)]));
      return nodes;
    }
    nodes.push(...this.onlineResults.map((item, index) => this.onlineResultRow(item, index)));
    const pagination = this.onlinePagination();
    if (pagination) nodes.push(pagination);
    return nodes;
  }

  onlinePagination() {
    if (!this.onlineHasSearched || (!this.onlineResults.length && this.onlinePage <= 1)) return null;
    const page = Math.max(1, Number(this.onlinePage) || 1);
    const total = Math.max(0, Number(this.onlineTotal) || 0);
    const first = (page - 1) * this.onlinePageSize + 1;
    const last = (page - 1) * this.onlinePageSize + this.onlineResults.length;
    const summary = total
      ? `第 ${page} 页 · ${first}–${last} / 约 ${total} 条`
      : `第 ${page} 页 · ${first}–${last} 条`;
    return el("nav", { class: "lora-pagination", "aria-label": "LoRA 搜索分页" }, [
      el("button", {
        class: "btn btn-sm",
        type: "button",
        disabled: this.onlineLoading || page <= 1,
        dataset: { loraPage: String(page - 1) },
      }, "上一页"),
      el("span", { class: "lora-pagination-summary" }, summary),
      el("button", {
        class: "btn btn-primary btn-sm",
        type: "button",
        disabled: this.onlineLoading || !this.onlineHasMore,
        dataset: { loraPage: String(page + 1) },
      }, "下一页"),
    ]);
  }

  onlineResultRow(item, index = 0) {
    const words = Array.isArray(item.trained_words) ? item.trained_words.filter(Boolean).slice(0, 5) : [];
    const source = item.source === "huggingface" ? "Hugging Face" : item.source === "civitai_red" ? "Civitai Red" : "Civitai";
    const meta = [item.base_model || "未知底模", item.version_name, item.download_count ? `${Number(item.download_count).toLocaleString()} 下载` : ""].filter(Boolean).join(" · ");
    const label = item.name || "未命名 LoRA";
    const thumb = this.previewThumb("lora-market-thumb", item.preview_token);
    const order = Math.min(Math.max(0, Number(index) || 0), 8);
    return el("article", {
      class: "lora-search-result lora-market-card",
      dataset: { resultId: item.id },
      style: `--lora-order: ${order}; animation-delay: ${order * 60}ms;`,
    }, [
      el("div", {
        class: "lora-card-open",
        role: "button",
        tabindex: "0",
        dataset: { loraDetail: item.id },
        "aria-label": `查看 ${label} 详情`,
      }, [
        thumb,
        el("div", { class: "lora-result-main" }, [
          el("div", { class: "lora-title-line" }, [
            el("h3", { title: label }, esc(label)),
            el("span", { class: "badge neutral" }, source),
            item.nsfw ? el("span", { class: "badge warn" }, "敏感") : null,
          ]),
          el("div", { class: "lora-file-meta" }, esc(meta)),
          words.length ? el("div", { class: "lora-trigger-preview" }, words.map((word) => el("span", { class: "tag" }, esc(word)))) : el("div", { class: "hint" }, "安装时提取触发词"),
        ]),
      ]),
      el("div", { class: "lora-result-actions" }, [
        el("input", { class: "input lora-alias-input", type: "text", maxlength: "80", placeholder: "中文别称（可选）", "aria-label": `${label} 的中文别称` }),
        el("button", { class: "btn btn-primary btn-sm", type: "button", dataset: { loraDownload: item.id } }, [ic("download"), esc("安装")]),
      ]),
      el("div", { class: "lora-download-state", "aria-live": "polite" }),
    ]);
  }

  openOnlineDetail(item) {
    if (!item) return;
    const label = item.name || "未命名 LoRA";
    const source = item.source === "huggingface" ? "Hugging Face" : item.source === "civitai_red" ? "Civitai Red" : "Civitai";
    const words = Array.isArray(item.trained_words) ? item.trained_words.filter(Boolean) : [];
    const tags = Array.isArray(item.tags) ? item.tags.filter(Boolean) : [];
    const sourceCard = [...(this.root?.querySelectorAll(".lora-search-result") || [])]
      .find((card) => String(card.dataset.resultId) === String(item.id));
    const sourceThumb = sourceCard?.querySelector(".lora-market-thumb");
    const sourceImage = sourceThumb?.querySelector("img");
    const canReuseImage = !!sourceImage && sourceThumb?.classList.contains("is-loaded");
    const sourceRatio = sourceThumb?.style.getPropertyValue("--lora-preview-ratio").trim();
    const activeElement = document.activeElement;
    const overlay = el("div", {
      class: "overlay lora-detail-overlay",
      role: "dialog",
      "aria-modal": "true",
      "aria-labelledby": "lora-detail-title",
      dataset: { loraDetailOverlay: item.id },
    });
    // When the card image is already loaded, the detail preview gets its own
    // clone of that image (src already cached, no second network fetch). The
    // flight layer animates another clone on top; on arrival both clones sit
    // pixel-identical, so the swap happens in a single frame — no cross-fade,
    // no double motion, and the real image never leaves the card.
    const detailPreview = canReuseImage
      ? el("div", { class: "lora-detail-preview lora-shared-target lora-shared-pending" }, [sourceImage.cloneNode(false)])
      : this.previewThumb("lora-detail-preview", item.preview_token);
    if (sourceRatio) detailPreview.style.setProperty("--lora-preview-ratio", sourceRatio);
    const modal = el("div", { class: "modal lora-detail-modal" }, [
      el("div", { class: "modal-head" }, [
        el("div", { class: "lora-detail-heading" }, [
          el("h3", { id: "lora-detail-title" }, esc(label)),
          el("div", { class: "lora-detail-badges" }, [
            el("span", { class: "badge neutral" }, source),
            item.nsfw ? el("span", { class: "badge warn" }, "敏感") : null,
          ]),
        ]),
        el("button", { class: "icon-btn", type: "button", "aria-label": "关闭详情" }, [ic("x")]),
      ]),
      el("div", { class: "modal-body lora-detail-body" }, [
        detailPreview,
        el("div", { class: "lora-detail-content" }, [
          el("div", { class: "lora-detail-facts" }, [
            this.detailFact("版本", item.version_name || "未提供"),
            this.detailFact("底模", item.base_model || "未知"),
            this.detailFact("下载", item.download_count ? Number(item.download_count).toLocaleString() : "暂无数据"),
            this.detailFact("收藏", item.heart_count ? Number(item.heart_count).toLocaleString() : "暂无数据"),
          ]),
          el("section", { class: "lora-detail-section" }, [
            el("h4", {}, "触发词"),
            words.length
              ? el("div", { class: "lora-detail-tags" }, words.map((word) => el("span", { class: "tag" }, esc(word))))
              : el("p", { class: "hint" }, "该来源未提供触发词，安装时会继续尝试提取。"),
          ]),
          tags.length ? el("section", { class: "lora-detail-section" }, [
            el("h4", {}, "标签"),
            el("div", { class: "lora-detail-tags" }, tags.map((tag) => el("span", { class: "tag" }, esc(tag)))),
          ]) : null,
        ]),
      ]),
      el("div", { class: "modal-foot" }, [
        el("button", { class: "btn btn-primary", type: "button", dataset: { loraDetailClose: "" } }, "关闭"),
      ]),
    ]);
    overlay.append(modal);
    document.body.append(overlay);

    let closed = false;
    const reduceMotion = window.__wfxReducedMotion?.() === true;
    // settled = 开场飞行已落地（或本就没有飞行），详情图已经就位。
    let settled = !canReuseImage || reduceMotion;
    let openFlight = null;
    const completeSharedPreview = () => {
      // is-shared-pending 故意保留：CSS 用它配合 is-shared-complete 做“同帧硬切”，
      // 目标图不走自身的 opacity/transform 过渡，避免与飞行层双重运动。
      detailPreview.classList.add("is-loaded", "is-shared-complete");
      overlay.classList.remove("is-shared-transfer");
    };
    const finishClose = () => {
      sourceCard?.classList.remove("is-opening");
      overlay.remove();
      if (activeElement?.isConnected && typeof activeElement.focus === "function") activeElement.focus();
    };
    const close = () => {
      if (closed) return;
      closed = true;
      document.removeEventListener("keydown", onKey);
      // 飞行中途关闭：先 cancel 触发 settle，就地把详情图交接到位，再从当前状态返航。
      if (!settled && openFlight) {
        openFlight.cancel();
        settled = true;
      }
      overlay.classList.remove("is-visible");
      overlay.classList.add("is-closing");
      sourceCard?.classList.remove("is-opening");
      const homeThumb = sourceThumb?.isConnected ? sourceThumb : null;
      const canReturn = settled && !!homeThumb
        && detailPreview.classList.contains("is-shared-complete")
        && !reduceMotion;
      if (canReturn) {
        // 对称返航：图片沿缓入曲线飘回卡片缩略图，末段随飞行层淡出融入；
        // 卡片压暗已同步解除，一来一回构成完整的“取用—归还”闭环。
        this.returnSharedPreview(detailPreview, homeThumb, {
          duration: 420,
          sourceImage,
          onfinish: finishClose,
        });
        return;
      }
      window.setTimeout(finishClose, reduceMotion ? 0 : 200);
    };
    const onKey = (event) => { if (event.key === "Escape") close(); };
    overlay.addEventListener("click", (event) => { if (event.target === overlay) close(); });
    modal.querySelector(".modal-head .icon-btn")?.addEventListener("click", close);
    modal.querySelector("[data-lora-detail-close]")?.addEventListener("click", close);
    document.addEventListener("keydown", onKey);
    modal.querySelector(".modal-head .icon-btn")?.focus();
    sourceCard?.classList.add("is-opening");
    if (canReuseImage && !reduceMotion) overlay.classList.add("is-shared-transfer");
    requestAnimationFrame(() => {
      if (closed) return;
      overlay.classList.add("is-visible");
      if (!canReuseImage) return;
      if (reduceMotion) {
        completeSharedPreview();
        return;
      }
      openFlight = this.animateSharedPreview(sourceThumb, detailPreview, {
        duration: 560,
        sourceImage,
        onfinish: () => {
          settled = true;
          completeSharedPreview();
        },
      });
    });
  }

  /* ---------- 共享元素飞行层 ----------
   * 飞行层只承载一张克隆图，只动画 transform（返航另加 opacity）。
   * 阴影、滤镜、描边全部退出动画：深度感交给遮罩渐暗与详情面板自身的
   * 静态阴影（随面板淡入），飞行层本身轻如纸页，全程交给合成器合成。
   * 圆角是唯一随行的插值属性——缩放会让半径视觉放大，需要按比例反补偿，
   * 保证落地瞬间与目标预览的圆角逐像素一致。 */
  buildFlightLayer(image, rect) {
    const flight = document.createElement("div");
    flight.className = "lora-detail-flight";
    Object.assign(flight.style, {
      position: "fixed",
      left: `${rect.left}px`,
      top: `${rect.top}px`,
      width: `${rect.width}px`,
      height: `${rect.height}px`,
      margin: "0",
      zIndex: "120",
      pointerEvents: "none",
      overflow: "hidden",
      transformOrigin: "top left",
      willChange: "transform",
    });
    Object.assign(image.style, {
      width: "100%",
      height: "100%",
      display: "block",
      opacity: "1",
      transform: "none",
      animation: "none",
      transition: "none",
      objectFit: "cover",
      objectPosition: "50% 50%",
    });
    flight.append(image);
    document.body.append(flight);
    return flight;
  }

  flightPlan(from, to, sourceImage) {
    const fromRect = from?.getBoundingClientRect?.();
    const toRect = to?.getBoundingClientRect?.();
    const src = sourceImage || from?.querySelector?.("img");
    const ok = !!fromRect && !!toRect && !!src
      && fromRect.width >= 1 && fromRect.height >= 1
      && toRect.width >= 1 && toRect.height >= 1;
    if (!ok) return null;
    const fromRadius = parseFloat(getComputedStyle(from).borderRadius) || 8;
    const toRadius = parseFloat(getComputedStyle(to).borderRadius) || 8;
    const sx = toRect.width / fromRect.width;
    const sy = toRect.height / fromRect.height;
    return {
      fromRect,
      toRect,
      image: src,
      fromRadius,
      // 元素被 scale 放大后，自身圆角的视觉半径同步放大；除以缩放比反补偿，
      // 落地时视觉半径恰好等于目标圆角。sx≠sy 时用 "rx / ry" 椭圆角语法。
      endRadius: `${(toRadius / sx).toFixed(2)}px / ${(toRadius / sy).toFixed(2)}px`,
      land: `translate3d(${toRect.left - fromRect.left}px, ${toRect.top - fromRect.top}px, 0) scale(${sx}, ${sy})`,
    };
  }

  /* 开场：小卡片 → 详情预览。一段 transform、一条缓动曲线走完全程——
   * 本站标准的指数减速 --ease-out：离手即动、长程滑翔、到点即静，
   * 无过冲、无回弹、无关键帧抽搐，如纸页被从容推放到位。
   * 交接采用“先显形、后移除、隔帧重叠”：落地时先让目标图在飞行层
   * 正下方显形（此刻被完全覆盖），下一帧才移除飞行层——克隆图与目标图
   * 同源同几何，重叠的一帧肉眼不可见，却保证任何亚像素误差都不会
   * 露出“消失再浮现”的缝隙，全程一镜到底。 */
  animateSharedPreview(from, to, { duration = 600, sourceImage = null, onfinish } = {}) {
    const plan = this.flightPlan(from, to, sourceImage);
    if (!plan || typeof Element.prototype.animate !== "function") {
      onfinish?.();
      return null;
    }
    const flight = this.buildFlightLayer(plan.image.cloneNode(false), plan.fromRect);
    flight.style.setProperty("--lora-flight-dur", `${duration}ms`);
    let settled = false;
    const settle = (interrupted = false) => {
      if (settled) return;
      settled = true;
      onfinish?.();
      // 被 cancel 时动画效果立即失效，必须同步移除，避免飞行层闪回起点；
      // 自然结束时 fill:forwards 仍把飞行层钉在终点，下一帧移除最稳。
      if (interrupted) flight.remove();
      else requestAnimationFrame(() => flight.remove());
    };
    const animation = flight.animate(
      [
        { transform: "translate3d(0, 0, 0) scale(1, 1)", borderRadius: `${plan.fromRadius}px` },
        { transform: plan.land, borderRadius: plan.endRadius },
      ],
      { duration, easing: "cubic-bezier(0.16, 1, 0.3, 1)", fill: "forwards" }
    );
    animation.addEventListener("finish", () => settle(), { once: true });
    animation.addEventListener("cancel", () => settle(true), { once: true });
    return animation;
  }

  /* 返航：详情预览 → 小卡片。轻起步、长滑翔回家，末段随飞行层整体淡出，
   * 融进缩略图，与开场构成对称的“取用—归还”闭环——同样无过冲无回弹。 */
  returnSharedPreview(from, to, { duration = 420, sourceImage = null, onfinish } = {}) {
    const plan = this.flightPlan(from, to, sourceImage);
    if (!plan || typeof Element.prototype.animate !== "function") {
      onfinish?.();
      return null;
    }
    const flight = this.buildFlightLayer(plan.image.cloneNode(false), plan.fromRect);
    flight.style.setProperty("--lora-flight-dur", `${duration}ms`);
    flight.style.willChange = "transform, opacity";
    let settled = false;
    const settle = (interrupted = false) => {
      if (settled) return;
      settled = true;
      onfinish?.();
      if (interrupted) flight.remove();
      else requestAnimationFrame(() => flight.remove());
    };
    const animation = flight.animate(
      [
        { transform: "translate3d(0, 0, 0) scale(1, 1)", borderRadius: `${plan.fromRadius}px`, opacity: 1 },
        { transform: plan.land, borderRadius: plan.endRadius, opacity: 1, offset: 0.7 },
        { transform: plan.land, borderRadius: plan.endRadius, opacity: 0 },
      ],
      { duration, easing: "cubic-bezier(0.4, 0.06, 0.18, 1)", fill: "forwards" }
    );
    animation.addEventListener("finish", () => settle(), { once: true });
    animation.addEventListener("cancel", () => settle(true), { once: true });
    return animation;
  }

  detailFact(label, value) {
    return el("div", { class: "lora-detail-fact" }, [
      el("span", {}, label),
      el("b", {}, esc(String(value || "—"))),
    ]);
  }

  async changeOnlinePage(page) {
    page = Math.max(1, Number(page) || 1);
    if (this.onlineLoading || page === this.onlinePage || (page > this.onlinePage && !this.onlineHasMore)) return;
    await this.searchOnlineLoras(page);
  }

  async searchOnlineLoras(page = 1) {
    const query = $("#lora-online-query", this.root)?.value?.trim() || "";
    const family = $("#lora-online-family", this.root)?.value || this.family;
    if (!query) { toast("先输入 LoRA 名称或角色别称", "warn"); return; }
    const requestedPage = Math.max(1, Number(page) || 1);
    this.onlineQuery = query;
    this.family = family;
    this.onlineFamily = family;
    this.onlineSources = [...($("#lora-source-select", this.root)?.querySelectorAll("[data-lora-source]:checked") || [])].map((box) => box.value);
    if (!this.onlineSources.length) this.onlineSources = ["civitai"];
    let adultConsent = this.onlineAdultConsent;
    if (this.onlineSources.includes("civitai_red")) {
      adultConsent = await ensureR18Consent();
      if (!adultConsent) return;
    }
    this.onlineAdultConsent = adultConsent;
    this.onlinePage = requestedPage;
    this.onlineLoading = true;
    this.onlineErrors = [];
    this.onlineSearchedTerms = [];
    this.searchController?.abort();
    const controller = new AbortController();
    this.searchController = controller;
    const timeout = setTimeout(() => controller.abort(), 45000);
    if (this.tab === "discover") this.renderWorkspace();
    try {
      const result = await api.searchLoras(query, family, {
        signal: controller.signal,
        sources: this.onlineSources,
        adult_consent: adultConsent,
        page: requestedPage,
        limit: this.onlinePageSize,
      });
      if (controller.signal.aborted) return;
      this.onlineResults = Array.isArray(result?.items) ? result.items : [];
      this.onlineErrors = Array.isArray(result?.errors) ? result.errors.filter(Boolean).slice(0, 5) : [];
      this.onlineSearchedTerms = Array.isArray(result?.searched_terms) ? result.searched_terms.filter(Boolean).slice(0, 8) : [];
      this.onlineHasSearched = true;
      this.onlineTotal = Math.max(0, Number(result?.total) || 0);
      const visibleEnd = (requestedPage - 1) * this.onlinePageSize + this.onlineResults.length;
      this.onlineHasMore = result?.has_more === true || this.onlineTotal > visibleEnd;
      if (result?.nsfw_hidden) toast(`已隐藏 ${result.nsfw_hidden} 条敏感结果`, "warn");
      if (!this.onlineResults.length && !this.onlineErrors.length) toast("没有找到匹配当前模型族的 LoRA", "warn");
      if (this.onlineResults.length && this.onlineErrors.length) toast("部分来源失败，已展示可用结果", "warn");
    } catch (error) {
      const message = error?.name !== "AbortError"
        ? (error?.message || "未知错误")
        : "搜索超时，请检查来源网络或代理";
      if (error?.name !== "AbortError") toast("LoRA 搜索失败：" + message, "err");
      else toast("LoRA 搜索超时，请检查来源网络或代理", "warn");
      this.onlineResults = [];
      this.onlineErrors = [message];
      this.onlineSearchedTerms = [query];
      this.onlineHasSearched = true;
    } finally {
      clearTimeout(timeout);
      if (this.searchController === controller) this.searchController = null;
      this.onlineLoading = false;
      if (this.root?.isConnected && this.tab === "discover") this.renderWorkspace();
    }
  }

  async probeDownloadChannel(button) {
    if (button.disabled) return;
    button.disabled = true;
    const status = $("#lora-channel-status", this.root);
    status?.replaceChildren(loadingState("正在检查安装通道", { compact: true }));
    try {
      const result = await api.probeLoraBridge();
      status?.replaceChildren(el("span", { class: result.ok ? "text-ok" : "text-warn" }, esc(result.message || (result.ok ? "安装通道已就绪" : "安装通道不可用"))));
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
    const item = this.onlineResults.find((entry) => String(entry.id) === button.dataset.loraDownload);
    if (!item) return;
    let adultConsent = false;
    if (item.nsfw) {
      if (!(await ensureR18Consent())) { state?.replaceChildren(); return; }
      adultConsent = true;
    }
    button.disabled = true;
    state?.replaceChildren(loadingState("正在创建下载任务", { compact: true }));
    try {
      const job = await api.startLoraDownload(item.id, alias, { adult_consent: adultConsent });
      this.pollLoraDownload(job.id, item.id);
    } catch (error) {
      button.disabled = false;
      state?.replaceChildren(el("div", { class: "notice err" }, esc(formatLoraDownloadError(error))));
    }
  }

  pollLoraDownload(jobId, resultId) {
    const tick = async () => {
      if (!this.root?.isConnected) { this.downloadPolls.delete(jobId); return; }
      const row = $$(".lora-search-result", this.root).find((node) => String(node.dataset.resultId) === String(resultId));
      const state = row ? $(".lora-download-state", row) : null;
      const button = row ? $("[data-lora-download]", row) : null;
      try {
        const job = await api.loraDownload(jobId);
        const progress = job.progress || {};
        const percent = Number.isFinite(progress.percent) ? progress.percent : null;
        state?.replaceChildren(el("div", { class: "lora-progress" }, [
          el("div", { class: "lora-progress-label" }, esc(job.message || "正在下载 LoRA")),
          el("div", { class: "lora-progress-track", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": percent ?? "" }, [el("span", { style: `width:${percent ?? 12}%` })]),
        ]));
        if (job.status === "completed") {
          state?.replaceChildren(el("div", { class: "notice ok" }, esc(`${job.result?.filename || "LoRA"} 已安装，触发词档案已更新。`)));
          toast("LoRA 安装完成", "ok");
          if (button) button.disabled = false;
          this.downloadPolls.delete(jobId);
          setTimeout(() => { if (this.root?.isConnected) void this.load(); }, 500);
          return;
        }
        if (["failed", "cancelled"].includes(job.status)) {
          state?.replaceChildren(el("div", { class: "notice err" }, esc(formatLoraDownloadError(job.error?.message || "LoRA 下载失败"))));
          if (button) button.disabled = false;
          this.downloadPolls.delete(jobId);
          return;
        }
        this.downloadPolls.set(jobId, setTimeout(tick, 1000));
      } catch (error) {
        state?.replaceChildren(el("div", { class: "notice err" }, esc("读取下载进度失败：" + formatLoraDownloadError(error))));
        if (button) button.disabled = false;
        this.downloadPolls.delete(jobId);
      }
    };
    void tick();
  }

  /* ---------- 自动匹配测试 ---------- */
  matchPreviewPanel() {
    return el("section", { class: "lora-pane", "aria-labelledby": "lora-mp-title" }, [
      el("div", { class: "lora-pane-heading" }, [
        el("div", {}, [el("h2", { id: "lora-mp-title" }, "匹配测试"), el("p", {}, "验证提示词会命中哪些本地 LoRA，不会修改档案或生成参数。")]),
      ]),
      el("div", { class: "lora-match-layout" }, [
        el("form", { class: "lora-match-controls", id: "lora-mp-form" }, [
          el("div", { class: "field" }, [
            el("label", { for: "lora-mp-prompt" }, "提示词"),
            el("textarea", { class: "textarea", id: "lora-mp-prompt", rows: 7, placeholder: "例如：维里奈，高马尾，少女……", autocomplete: "off" }, this.matchPrompt),
          ]),
          el("div", { class: "lora-match-submit-row" }, [
            this.familySelect("lora-mp-family"),
            el("button", { class: "btn btn-primary", type: "submit", id: "lora-mp-run", disabled: this.previewBusy }, [ic("wand"), esc(this.previewBusy ? "匹配中…" : "运行匹配")]),
          ]),
        ]),
        el("div", { class: "lora-match-results" }, [
          el("div", { class: "lora-match-results-head" }, [el("h3", {}, "命中结果"), this.matchResults ? el("span", { class: "badge neutral" }, `${this.matchResults.length} 项`) : null]),
          el("div", { id: "lora-mp-results", "aria-live": "polite" }, this.renderMatchResults()),
        ]),
      ]),
    ]);
  }

  renderMatchResults() {
    if (this.previewBusy) return [loadingState("正在匹配本地 LoRA", { compact: true })];
    if (this.matchError) return [this.empty("匹配失败", this.matchError)];
    if (this.matchResults === null) return [el("div", { class: "lora-results-state" }, [ic("wand"), el("b", {}, "等待测试"), el("span", {}, "输入一段实际提示词，查看别名、触发词和连接词是否正确命中。")])];
    const nodes = [];
    if (this.matchNotice) nodes.push(el("div", { class: "notice info" }, this.matchNotice));
    if (!this.matchResults.length) nodes.push(this.empty("没有命中 LoRA", "检查当前模型族、别名和触发词是否正确。"));
    else nodes.push(...this.matchResults.map((item) => this.matchRow(item)));
    return nodes;
  }

  async runMatchPreview() {
    const prompt = $("#lora-mp-prompt", this.root)?.value?.trim() || "";
    const family = $("#lora-mp-family", this.root)?.value || this.family;
    if (!prompt) { toast("先输入提示词再测试", "warn"); return; }
    if (this.previewBusy) return;
    this.matchPrompt = prompt;
    this.family = family;
    this.previewBusy = true;
    this.matchError = "";
    this.matchNotice = "";
    if (this.tab === "match") this.renderWorkspace();
    try {
      const result = await api.loraLibraryMatchPreview(prompt, family, 10);
      if (!this.root?.isConnected) return;
      const matches = Array.isArray(result?.matches) ? result.matches : [];
      const nsfw = matches.filter((item) => item.nsfw);
      const safe = matches.filter((item) => !item.nsfw);
      if (nsfw.length && !(await ensureR18Consent())) {
        this.matchResults = safe;
        this.matchNotice = `已隐藏 ${nsfw.length} 条成人/敏感匹配。`;
      } else {
        this.matchResults = matches;
      }
      if (!matches.length) toast("没有匹配到 LoRA", "warn");
    } catch (error) {
      this.matchResults = null;
      this.matchError = error?.message || "未知错误";
      toast("LoRA 匹配失败：" + this.matchError, "err");
    } finally {
      this.previewBusy = false;
      if (this.root?.isConnected && this.tab === "match") this.renderWorkspace();
    }
  }

  matchRow(item) {
    const asList = (value) => Array.isArray(value) ? value : value ? [value] : [];
    const aliases = asList(item.aliases).filter(Boolean).slice(0, 4);
    const triggers = asList(item.triggers).filter(Boolean).slice(0, 6);
    const score = Number(item.score);
    const scoreText = Number.isFinite(score) ? `${Math.round(score * 100)}%` : "命中";
    return el("article", { class: "lora-match-row" }, [
      el("div", { class: "lora-match-score" }, scoreText),
      el("div", { class: "lora-result-main" }, [
        el("div", { class: "lora-title-line" }, [
          el("h3", {}, esc(item.name || item.character || "未命名 LoRA")),
          item.disabled ? el("span", { class: "badge danger" }, "已禁用") : null,
        ]),
        el("div", { class: "lora-file-meta" }, esc([item.character, item.family, item.filename].filter(Boolean).join(" · "))),
        item.connector ? el("div", { class: "lora-chipline" }, [el("span", { class: "lora-chip-label" }, "连接词"), el("span", { class: "tag" }, esc(item.connector))]) : null,
        aliases.length ? el("div", { class: "lora-chipline" }, [el("span", { class: "lora-chip-label" }, "别名"), ...aliases.map((value) => el("span", { class: "tag" }, esc(value)))]) : null,
        triggers.length ? el("div", { class: "lora-trigger-preview" }, [el("span", { class: "hint" }, "触发词"), ...triggers.map((value) => el("span", { class: "tag" }, esc(value)))]) : null,
      ]),
    ]);
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
    this.searchController?.abort();
    this.searchController = null;
    for (const timer of this.downloadPolls.values()) clearTimeout(timer);
    this.downloadPolls.clear();
  }
}

/* ---------- R18 同意弹窗（仿 setSecretDialog 骨架） ----------
 * 首次渲染 nsfw 匹配前调用；同意后写入 localStorage，仅存本机。
 * 返回 Promise<boolean>：true 表示同意查看，false 表示拒绝。
 */
export function ensureR18Consent() {
  return new Promise((resolve) => {
    const consentKey = "winefox.r18_consent.v1";
    if (localStorage.getItem(consentKey) === "accepted") {
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
      localStorage.setItem(consentKey, "accepted");
      finish(true);
    });
    $("#r18-ok", overlay)?.focus();
  });
}

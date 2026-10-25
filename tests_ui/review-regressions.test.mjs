// Run: node --experimental-vm-modules --test tests_ui/review-regressions.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

class Element {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase(); this.nodeType = 1; this.children = [];
    this.attrs = {}; this.dataset = {}; this.style = { setProperty() {} }; this.listeners = {};
    this.classList = { add() {}, remove() {}, toggle() {} }; this.isConnected = true;
  }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return this.attrs[name] ?? null; }
  append(...nodes) { this.children.push(...nodes); }
  prepend(...nodes) { this.children.unshift(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  removeEventListener() {}
  focus() {}
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
  querySelectorAll(selector) {
    const found = [];
    const matches = (node) => selector.startsWith("#") ? node.attrs?.id === selector.slice(1)
      : selector.startsWith(".") ? (node.attrs?.class || node.className || "").split(" ").includes(selector.slice(1))
      : node.tagName?.toLowerCase() === selector;
    const walk = (node) => { for (const child of node.children || []) { if (matches(child)) found.push(child); walk(child); } };
    walk(this);
    return found;
  }
  set innerHTML(value) { this.children = []; }
  get textContent() { return this._text ?? this.children.map((node) => node.textContent || "").join(""); }
  set textContent(value) { this._text = String(value); this.children = []; }
}

async function workspace(settings = {}) {
  const timers = new Map(); const frames = new Map(); let nextTimer = 0; let clock = 1000;
  const document = {
    querySelector() { return null; }, querySelectorAll() { return []; },
    createElement: (tag) => new Element(tag), createElementNS: (_, tag) => new Element(tag),
    createTextNode: (text) => ({ nodeType: 3, textContent: String(text) }),
    addEventListener() {}, removeEventListener() {}, body: new Element("body"),
  };
  const window = { addEventListener() {}, removeEventListener() {} }; window.parent = window;
  const context = vm.createContext({
    console, document, window, navigator: {}, AbortController, FormData, URL, URLSearchParams,
    setTimeout: (fn) => { const id = ++nextTimer; timers.set(id, fn); return id; },
    clearTimeout: (id) => timers.delete(id),
    requestAnimationFrame: (fn) => { const id = ++nextTimer; frames.set(id, fn); return id; },
    cancelAnimationFrame: (id) => frames.delete(id), performance: { now: () => clock },
    localStorage: { getItem() { return null; }, setItem() {} },
  });
  const modules = new Map();
  async function load(filename) {
    filename = path.resolve(filename);
    if (modules.has(filename)) return modules.get(filename);
    const mod = new vm.SourceTextModule(await readFile(filename, "utf8"), { context, identifier: filename });
    modules.set(filename, mod);
    await mod.link((name, parent) => load(path.resolve(path.dirname(parent.identifier), name)));
    return mod;
  }
  const apiModule = await load(path.join(root, "desktop_ui/js/api.js")); await apiModule.evaluate();
  const storeModule = await load(path.join(root, "desktop_ui/js/store.js")); await storeModule.evaluate();
  storeModule.namespace.store.set("settings", settings);
  return {
    api: apiModule.namespace.api, JobPoller: apiModule.namespace.JobPoller,
    store: storeModule.namespace.store, timers,
    setTime(value) { clock = value; },
    flushFrames() { const callbacks = [...frames.values()]; frames.clear(); callbacks.forEach((fn) => fn()); },
    async module(relative) { const mod = await load(path.join(root, relative)); await mod.evaluate(); return mod.namespace; },
    async view(name) { const mod = await load(path.join(root, `desktop_ui/js/views/${name}.js`)); await mod.evaluate(); return mod.namespace; },
  };
}

test("poller ignores an in-flight response after stop and a new watch", async () => {
  const env = await workspace(); const old = deferred(); const newer = deferred(); let calls = 0;
  env.api.job = () => (++calls === 1 ? old.promise : newer.promise);
  const seen = []; const poller = new env.JobPoller((job) => seen.push(job.id));
  poller.watch("same"); poller.watch("same"); assert.equal(calls, 1);
  poller.stop("same"); poller.watch("same");
  old.resolve({ id: "obsolete", status: "running" }); await Promise.resolve(); await Promise.resolve();
  assert.deepEqual(seen, []);
  newer.resolve({ id: "current", status: "running" }); await Promise.resolve(); await Promise.resolve();
  assert.deepEqual(seen, ["current"]); poller.dispose(); assert.equal(env.timers.size, 0);
});

test("poller disposes all independent task timers", async () => {
  const env = await workspace(); env.api.job = async (id) => ({ id, status: "running" });
  const poller = new env.JobPoller(); poller.watch("a"); poller.watch("b");
  await new Promise(setImmediate); assert.equal(env.timers.size, 2);
  poller.dispose(); assert.equal(env.timers.size, 0);
});

test("generation honors configured dimensions and chooses a workflow of its family", async () => {
  const env = await workspace({ defaults: { family: "anima", width: 1536, height: 1216 } });
  const { GenerateView } = await env.view("generate"); const view = new GenerateView();
  env.store.set("catalog", { workflows: [{ id: "wrong", family: "illustrious" }, { id: "right", family: "anima" }] });
  assert.equal(view.syncWorkflow(), "right");
  const panel = view.rightPanel();
  for (const [id, expected] of [["g-w", 1536], ["g-h", 1216]]) {
    const selected = panel.querySelector(`#${id}`).children.find((node) => Object.hasOwn(node.attrs, "selected"));
    assert.equal(selected.attrs.value, String(expected));
  }
});

test("pending generation cannot be submitted twice", async () => {
  const env = await workspace(); const { GenerateView } = await env.view("generate");
  const view = new GenerateView(); view.root = new Element(); view.render = () => {};
  view.state.params.prompt = "test"; view.cancelAutoLoraPreview = () => {};
  const response = deferred(); let count = 0;
  env.api.submitJob = () => { count++; return response.promise; }; view.poller.watch = () => {};
  const first = view.submit(); const second = view.submit();
  assert.equal(count, 1); response.resolve({ id: "1", status: "queued" }); await first; await second;
  assert.equal(view.state.submitting, false); assert.equal(view.state.job.id, "1");
});

test("image generation without a reference is rejected before submission", async () => {
  const env = await workspace(); const { GenerateView } = await env.view("generate");
  const view = new GenerateView(); view.root = new Element(); view.state.mode = "img2img";
  let calls = 0; env.api.submitJob = async () => { calls++; };
  await view.submit(); assert.equal(calls, 0);
});

test("cancel failure preserves the active job and its polling", async () => {
  const env = await workspace(); const { GenerateView } = await env.view("generate");
  const view = new GenerateView(); view.root = new Element(); view.state.job = { id: "1" };
  env.api.cancelJob = async () => { throw new Error("offline"); };
  let stopped = false; view.poller.stop = () => { stopped = true; };
  await view.cancel(); assert.equal(view.state.job.id, "1"); assert.equal(stopped, false);
});

test("result favorite uses history identity instead of job identity", async () => {
  const env = await workspace(); const { GenerateView } = await env.view("generate");
  const view = new GenerateView(); view.root = new Element();
  view.state.result = { job: { id: "job-20", result: { history_id: "history-3" } } };
  let favoriteId; env.api.favoriteHistory = async (id) => { favoriteId = id; };
  await view.favoriteResult(); assert.equal(favoriteId, "history-3");
});

test("history refresh drops the next-page cursor and stale search responses", async () => {
  const env = await workspace(); const { HistoryView } = await env.view("history");
  const view = new HistoryView(); const body = new Element(); const grid = new Element();
  view.root = { querySelector: (selector) => selector === "#h-grid" ? grid : body };
  view.renderItems = (items) => { view.displayed = items; }; view.cursor = "old-cursor";
  const old = deferred(); const current = deferred(); const queries = [];
  env.api.history = (query) => { queries.push(query); return queries.length === 1 ? old.promise : current.promise; };
  const first = view.load(); view.q = "latest"; const second = view.load();
  assert.equal(queries[0].cursor, undefined);
  current.resolve({ items: [{ id: "new" }], next_cursor: "next" }); await second;
  old.resolve({ items: [{ id: "stale" }], next_cursor: "wrong" }); await first;
  assert.equal(view.displayed[0].id, "new"); assert.equal(view.cursor, "next");
});

test("history load-more is single flight", async () => {
  const env = await workspace(); const { HistoryView } = await env.view("history");
  const view = new HistoryView(); view.root = { querySelector: () => new Element() }; view.renderItems = () => {};
  view.cursor = "next"; const response = deferred(); let calls = 0;
  env.api.history = () => { calls++; return response.promise; };
  const first = view.load(true); const second = view.load(true); assert.equal(calls, 1);
  response.resolve({ items: [], next_cursor: null }); await first; await second;
});

test("history select-all skips failures and preserves loaded pages", async () => {
  const env = await workspace(); const { HistoryView } = await env.view("history");
  const view = new HistoryView(); view.root = new Element();
  view.pageItems = [{ id: "1", image: { url: "/1.png" } }, { id: "job-2", image: null }, { id: "3", image: { url: "/3.png" } }];
  view.root.append(view.batchBar()); view.bindBatchBar();
  view.render = () => { throw new Error("selection must preserve loaded pages"); };
  view.load = () => { throw new Error("selection must not refetch the first page"); };
  const all = view.root.querySelector("#bb-all");
  all.listeners.click();
  assert.deepEqual([...view.selected], ["1", "3"]); assert.equal(all.textContent, "取消全选");
  assert.equal(view.batchBar().querySelector("#bb-all").textContent, "取消全选");
  view.toggleSelect("job-2"); assert.equal(view.selected.has("job-2"), false);
  view.toggleSelect("3"); assert.equal(all.textContent, "全选");
  all.listeners.click(); assert.equal(all.textContent, "取消全选");
  all.listeners.click(); assert.equal(view.selected.size, 0);
});

test("aborted old LoRA search cannot clear a newer successful result", async () => {
  const env = await workspace(); const { LoraLibraryView } = await env.view("lora");
  const view = new LoraLibraryView(); let query = "first";
  view.root = { isConnected: true, querySelector: (selector) => selector === "#lora-online-query" ? { value: query } : null };
  view.renderWorkspace = () => {}; const firstResponse = deferred(); const secondResponse = deferred(); let count = 0;
  env.api.searchLoras = () => (++count === 1 ? firstResponse.promise : secondResponse.promise);
  const first = view.searchOnlineLoras(); query = "second"; const second = view.searchOnlineLoras();
  secondResponse.resolve({ items: [{ id: "current" }] }); await second;
  firstResponse.reject(Object.assign(new Error("cancelled"), { name: "AbortError" })); await first;
  assert.equal(view.onlineResults[0].id, "current"); assert.equal(view.onlineErrors.length, 0);
});

test("environment progress always flushes its trailing final state", async () => {
  const env = await workspace(); const { EnvSetupView } = await env.module("desktop_ui/js/setup.js");
  const view = new EnvSetupView(); view.root = new Element(); view.lastJobRender = 1000;
  let rendered = 0; view.render = () => { rendered++; };
  view._scheduleJobRender(); assert.equal(env.timers.size, 1);
  env.setTime(1300);
  const [timerId, callback] = [...env.timers.entries()][0]; env.timers.delete(timerId); callback();
  env.flushFrames(); assert.equal(rendered, 1);
});

test("environment ignores a previous job's late snapshot", async () => {
  const env = await workspace(); const { EnvSetupView } = await env.module("desktop_ui/js/setup.js");
  const view = new EnvSetupView(); view.activeJobId = "old"; const response = deferred();
  env.api.setupJob = () => response.promise;
  const pending = view.syncJob(); view.activeJobId = "new"; view.jobData = { id: "new" };
  response.resolve({ id: "old", status: "failed" }); await pending;
  assert.equal(view.jobData.id, "new");
});

test("settings binds the update button after its nodes are mounted", async () => {
  const env = await workspace(); const { SettingsView } = await env.view("settings");
  const view = new SettingsView(); view.settings = {}; view.section = "about";
  const body = new Element(); body.attrs.id = "s-body"; view.root = new Element(); view.root.append(body);
  view.renderBody();
  assert.equal(typeof body.querySelector("#up-check").listeners.click, "function");
});

test("overflowing numeric timestamps do not render NaN dates", async () => {
  const env = await workspace(); const { parseDate } = await env.module("desktop_ui/js/utils.js");
  assert.equal(parseDate("99999999999999999999999"), null);
});

test("LoRA progress reconnects after a temporary failure without starting another download", async () => {
  const env = await workspace(); const { LoraLibraryView } = await env.view("lora");
  const view = new LoraLibraryView(); view.root = new Element(); let calls = 0;
  env.api.loraDownload = async () => {
    if (++calls === 1) throw Object.assign(new Error("offline"), { status: 0 });
    return { status: "completed", result: { filename: "fixture.safetensors" } };
  };
  env.api.startLoraDownload = () => { throw new Error("must not enqueue again"); };
  view.pollLoraDownload("job", "result"); view.pollLoraDownload("job", "result");
  await new Promise(setImmediate); assert.equal(calls, 1); assert.equal(view.downloadPolls.size, 1);
  const [id, callback] = [...env.timers.entries()][0]; env.timers.delete(id); await callback();
  assert.equal(calls, 2); assert.equal(view.downloadPolls.size, 0); view.dispose();
});

test("disposed LoRA view cannot restart a progress timer from a late response", async () => {
  const env = await workspace(); const { LoraLibraryView } = await env.view("lora");
  const view = new LoraLibraryView(); view.root = new Element(); const response = deferred();
  env.api.loraDownload = () => response.promise;
  view.pollLoraDownload("job", "result"); view.dispose(); response.reject(new Error("offline"));
  await new Promise(setImmediate); assert.equal(view.downloadPolls.size, 0); assert.equal(env.timers.size, 0);
});

test("submitting cancels automatic LoRA preview and clears its busy indicator", async () => {
  const env = await workspace(); const { GenerateView } = await env.view("generate");
  const view = new GenerateView(); view.root = new Element(); view.refreshAutoLoraPanel = () => {};
  view.state.params.prompt = "beach"; view.scheduleAutoLoraPreview();
  assert.equal(view.state.autoLoraBusy, true);
  view.cancelAutoLoraPreview();
  assert.equal(view.state.autoLoraBusy, false); assert.equal(env.timers.size, 0);
});

test("manual LoRA selection skips irrelevant automatic matching requests", async () => {
  const env = await workspace(); const { GenerateView } = await env.view("generate");
  const view = new GenerateView(); view.root = new Element(); view.refreshAutoLoraPanel = () => {};
  view.state.params.prompt = "beach"; view.state.loras.set("selected", { strength: .8 });
  view.scheduleAutoLoraPreview();
  assert.equal(view.state.autoLoraBusy, false); assert.equal(env.timers.size, 0);
});

test("preview cancellation aborts the in-flight request and ignores its late response", async () => {
  const env = await workspace(); const { GenerateView } = await env.view("generate");
  const view = new GenerateView(); view.root = new Element(); view.refreshAutoLoraPanel = () => {};
  view.state.params.prompt = "beach"; const response = deferred(); let signal;
  env.api.loraMatchPreview = (_, options) => { signal = options.signal; return response.promise; };
  view.scheduleAutoLoraPreview(); const [id, callback] = [...env.timers.entries()][0]; env.timers.delete(id);
  const pending = callback(); assert.equal(signal.aborted, false);
  view.cancelAutoLoraPreview(); assert.equal(signal.aborted, true);
  response.resolve({ matches: [{ name: "stale" }] }); await pending;
  assert.equal(view.state.autoLoraMatches.length, 0); assert.equal(view.state.autoLoraBusy, false);
});

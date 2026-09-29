// Integrate the real page + Drive form + source rows with fake hooks/API.
// Downloading public responses deliberately omit private source_file metadata.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";
import * as settings from "../src/api/scanSettings.ts";
const ID = "a".repeat(32), API = "/api/local-video-experiment";
const account = { display_name: "Synthetic", email: "fake@example.test", account_id: "fake-account" };
const limits = { max_bytes: 16 * 1024**3, max_duration_seconds: 43200, max_files: 32, disk_reserve_bytes: 1024**3, containers: [".mkv"] };
const files = Array.from({ length: 7 }, (_, i) => ({ file_id: "video_0000" + (i + 1), filename: "cam0" + (i + 1) + ".mkv", bytes: 1024**3, supported: true, mime_type: "application/x-unknown" }));
function fixture() {
  return { id: ID, kind: "drive_batch", filename: "Seven synthetic cameras", status: "running", created_at: "2026-01-01T00:00:00Z",
    started_at: new Date().toISOString(), finished_at: null, error: null, media: null, model: null, scan_settings: { ...settings.DEFAULT_SCAN_SETTINGS },
    total_videos: 7, finished_videos: 0, completed_videos: 0, failed_videos: 0, temporary_downloads_cleaned: true,
    sampled_frames: 0, decoded_frames: 0, matched_face_detections: 0, unknown_detections: 0, frames_with_unknown: 0, detected_face_detections: 0,
    unique_matched_people: 0, processing_seconds: 0, progress_percent: 0, progress_is_estimate: true, verified_total_frames: null,
    sampling_hz: null, sampling_method: "configured-camera-v3", actual_video_detection_hz: null, processing_throughput_fps: null,
    decoded_frames_not_inferred: 0, max_scan_hz: 2.5, partial: true, can_start: false, can_cancel: true, can_delete: false, people: [], samples: [],
    videos: files.map((file, i) => ({ source_video_key: "video-00" + (i + 1), filename: file.filename, status: "pending", error: null,
      media: null, downloaded_bytes: 0, decoded_frames: 0, sampled_frames: 0, detected_face_detections: 0, matched_face_detections: 0,
      unknown_detections: 0, frames_with_unknown: 0, verified_total_frames: null, progress_percent: 0, processing_seconds: 0,
      actual_video_detection_hz: null, people: [], samples: [] })) };
}
function sharedApi() { return { job: null, starts: 0, requests: [], startError: null, malformed: false, pollError: false }; }
async function mount(shared = sharedApi(), query = "") {
  const contexts = new Map(), effects = [], timers = new Map(), exports = new Map();
  let current, dirty = true, tree, nextTimer = 0, visited, params = new URLSearchParams(query);
  const setParams = values => { params = new URLSearchParams(values); dirty = true; };
  const changed = (a, b) => !a || a.length !== b.length || b.some((value, i) => !Object.is(value, a[i]));
  const react = {
    useRef(value) { const i = current.cursor++; return current.hooks[i] ??= { current: value }; },
    useState(initial) { const c = current, i = c.cursor++; c.hooks[i] ??= { value: typeof initial === "function" ? initial() : initial };
      return [c.hooks[i].value, value => { c.hooks[i].value = typeof value === "function" ? value(c.hooks[i].value) : value; dirty = true; }]; },
    useCallback(fn, deps) { const i = current.cursor++, old = current.hooks[i];
      if (!old || changed(old.deps, deps)) current.hooks[i] = { value: fn, deps }; return current.hooks[i].value; },
    useEffect(fn, deps) { const c = current, i = c.cursor++, old = c.hooks[i];
      if (!old || changed(old.deps, deps)) { const value = { deps, cleanup: old?.cleanup }; c.hooks[i] = value;
        effects.push(() => { value.cleanup?.(); value.cleanup = fn(); }); } },
  };
  globalThis.window = { setTimeout: fn => { const id = ++nextTimer; timers.set(id, fn); return id; }, clearTimeout: id => timers.delete(id),
    clearInterval() {}, location: { origin: "http://localhost:5173" }, confirm: () => true };
  const api = {
    ApiError: class extends Error {},
    async apiGet(path) {
      shared.requests.push({ method: "GET", path });
      if (path.startsWith(API + "/drive/status")) return { connected: true, read_access: true, account, limits };
      if (path === API) return { experiments: shared.job ? [{ ...shared.job, videos: undefined }] : [] };
      if (path === API + "/" + ID) {
        if (shared.pollError) { shared.pollError = false; throw Error("Status request interrupted; retrying."); }
        return structuredClone(shared.job);
      }
      throw Error("Unexpected read: " + path);
    },
    async apiPostJson(path, body) {
      shared.requests.push({ method: "POST", path, body });
      if (path.endsWith("/drive/folder")) return { folder_id: "folder_12345", folder_name: "Seven cameras", account, files, limits, outcome: "videos", subfolder_count: 0, message: "Seven supported videos." };
      if (path.endsWith("/drive/batches")) {
        shared.starts++; if (shared.startError) throw Error(shared.startError);
        shared.job = fixture(); return shared.malformed ? { status: "running" } : structuredClone(shared.job);
      }
      throw Error("Unexpected write: " + path);
    },
    async apiDelete(path) { shared.requests.push({ method: "DELETE", path }); shared.job = null; },
    downloadFile() {},
  };
  const jsx = (type, props) => ({ type, props });
  function load(relative) {
    if (exports.has(relative)) return exports.get(relative);
    const compiled = ts.transpileModule(readFileSync(new URL("../src/" + relative, import.meta.url), "utf8"),
      { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText;
    const result = {}; exports.set(relative, result);
    new Function("require", "exports", compiled)(name => {
      if (name === "react") return react;
      if (name === "react/jsx-runtime") return { jsx, jsxs: jsx, Fragment: "Fragment" };
      if (name === "react-router-dom") return { useSearchParams: () => [params, setParams] };
      if (name.endsWith("/api/client")) return api;
      if (name.endsWith("/api/scanSettings")) return settings;
      if (name.endsWith("/ui")) return { Button: "button", Card: "section", PageHeader: ({ title }) => jsx("h1", { children: title }) };
      if (name.endsWith("/ScanSettingsFields")) return { __esModule: true, default: "Fields" };
      if (name.endsWith("/VideoMatchPreview")) return { __esModule: true, default: "Preview" };
      if (name.endsWith("/DriveVideoBatchPanel")) return load("components/DriveVideoBatchPanel.tsx");
      if (name.endsWith("/VideoBatchSources")) return load("components/VideoBatchSources.tsx");
      throw Error(name);
    }, result);
    return result;
  }
  const Page = load("pages/LocalVideoExperiment.tsx").default;
  function resolve(node, key = "root") {
    if (Array.isArray(node)) return node.map((child, i) => resolve(child, key + "/" + i));
    if (!node?.props) return node;
    if (typeof node.type === "function") {
      visited.add(key); const context = contexts.get(key) ?? { cursor: 0, hooks: [] }; contexts.set(key, context);
      const prior = current; current = context; context.cursor = 0;
      const result = node.type(node.props); current = prior;
      return resolve(result, key + "/component");
    }
    return { ...node, props: { ...node.props, children: resolve(node.props.children, key + "/children") } };
  }
  const nodes = (node, out = []) => { if (Array.isArray(node)) node.forEach(child => nodes(child, out)); else if (node?.props) { out.push(node); nodes(node.props.children, out); } return out; };
  const text = node => Array.isArray(node) ? node.map(text).join(" ") : node?.props ? text(node.props.children) : typeof node === "string" || typeof node === "number" ? String(node) : "";
  const dispose = () => { for (const c of contexts.values()) for (const hook of c.hooks) hook?.cleanup?.(); contexts.clear(); timers.clear(); };
  async function flush() {
    for (let i = 0; i < 12; i++) {
      if (dirty) {
        dirty = false; visited = new Set(); tree = resolve(jsx(Page, {}));
        for (const [key, c] of contexts) if (!visited.has(key)) { for (const hook of c.hooks) hook?.cleanup?.(); contexts.delete(key); }
        while (effects.length) effects.shift()();
      }
      await new Promise(setImmediate);
    }
  }
  await flush();
  const find = label => nodes(tree).find(n => n.type === "button" && text(n).includes(label));
  async function click(label) { const button = find(label); assert.ok(button, label); assert.ok(!button.props.disabled, label + " disabled"); await button.props.onClick(); await flush(); }
  async function input(label, value) { const node = nodes(tree).find(n => n.props["aria-label"] === label); assert.ok(node, label); node.props.onChange({ target: { value } }); await flush(); }
  async function start() { await click("Google Drive batch"); await input("Google Drive folder link", "folder_12345"); await click("List videos");
    assert.equal(nodes(tree).filter(n => n.props.type === "checkbox").length, 7);
    await click("Select all supported"); await input("Video batch name", "Seven synthetic cameras"); await click("Start selected videos"); }
  return { shared, start, click, input, dispose, flush, query: () => params.toString(),
    text: () => text(tree).replace(/\s+/g, " "), nodes: () => nodes(tree),
    async tick() { const first = timers.entries().next().value; assert.ok(first, "Polling timer"); timers.delete(first[0]); await first[1](); await flush(); } };
}

test("seven-video Start shows pending progress immediately; download poll without private metadata never blanks", async () => {
  const ui = await mount(); await ui.start();
  assert.equal(ui.shared.starts, 1); assert.match(ui.text(), /Status: running/); assert.match(ui.text(), /Results by source video/);
  assert.match(ui.text(), /pending/); assert.equal(ui.query(), "experiment=" + ID);
  Object.assign(ui.shared.job.videos[0], { status: "downloading", downloaded_bytes: 4 * 1024**2 });
  await ui.tick(); assert.match(ui.text(), /4.0 MiB downloaded/); assert.match(ui.text(), /total unavailable until validation/);
  assert.equal(ui.shared.job.videos[0].source_file, undefined);
  assert.deepEqual(ui.shared.requests.find(r => r.path.endsWith("/drive/batches")).body.scan_settings, settings.DEFAULT_SCAN_SETTINGS);
  const query = ui.query(), shared = ui.shared; ui.dispose();
  const reload = await mount(shared, query); assert.match(reload.text(), /Status: running/); assert.match(reload.text(), /4.0 MiB downloaded/);
  assert.equal(shared.starts, 1); reload.dispose();
  const reopen = await mount(shared); assert.match(reopen.text(), /Status: running/); assert.equal(shared.starts, 1); reopen.dispose();
});
test("poll failures keep visible progress and retry; processing and partial errors render", async () => {
  const ui = await mount(); await ui.start(); ui.shared.pollError = true; await ui.tick();
  assert.match(ui.text(), /Status request interrupted/); assert.match(ui.text(), /Status: running/);
  Object.assign(ui.shared.job.videos[0], { status: "failed", error: "Synthetic unsupported codec; partial results kept." });
  Object.assign(ui.shared.job.videos[1], { status: "running", progress_percent: 25, media: { fps: 15, frame_count: 100, duration_seconds: 6.667 }, sampled_frames: 2 });
  await ui.tick(); assert.match(ui.text(), /Synthetic unsupported codec/); assert.match(ui.text(), /25\.0\s*% estimated decode progress/);
  assert.doesNotMatch(ui.text(), /Status request interrupted/); assert.equal(ui.shared.starts, 1); ui.dispose();
});
test("Start rejected before creation displays an actionable error with no progress job or duplicate", async () => {
  const shared = sharedApi(); shared.startError = "Drive permission expired. Reconnect Google Drive, then list the folder again.";
  const ui = await mount(shared); await ui.start(); assert.match(ui.text(), /Drive permission expired/);
  assert.equal(shared.job, null); assert.equal(shared.starts, 1); assert.doesNotMatch(ui.text(), /Status: running/);
  assert.ok(ui.nodes().some(n => n.props.role === "alert")); ui.dispose();
});
test("malformed Start response remains visible and list refresh recovers created job without another Start", async () => {
  const shared = sharedApi(); shared.malformed = true; const ui = await mount(shared); await ui.start();
  assert.match(ui.text(), /incomplete experiment response/); assert.match(ui.text(), /before trying again/);
  await ui.click("Refresh list"); assert.match(ui.text(), /Status: running/); assert.equal(shared.starts, 1); ui.dispose();
});
test("explicit failed batch URL recovers partial error state; Delete does not recover stale list", async () => {
  const shared = sharedApi(); shared.job = fixture(); Object.assign(shared.job, { status: "failed", can_cancel: false, can_delete: true, error: "Synthetic download interrupted; partial results retained." });
  const ui = await mount(shared, "experiment=" + ID); assert.match(ui.text(), /Synthetic download interrupted/);
  assert.equal(shared.starts, 0); await ui.click("Delete"); assert.equal(shared.job, null); assert.doesNotMatch(ui.text(), /Status: failed/);
  assert.equal(shared.starts, 0); ui.dispose();
});

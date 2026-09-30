// Integrate the real page + Drive form + source rows with fake hooks/API.
// Downloading public responses deliberately omit private source_file metadata.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";
import * as settings from "../src/api/scanSettings.ts";
const ID = "a".repeat(32), API = "/api/local-video-experiment";
const account = { display_name: "Synthetic", email: "fake@example.test", account_id: "fake-account" };
const limits = { max_duration_seconds: 43200, max_files: 32, disk_reserve_bytes: 1024**3, containers: [".mkv"] };
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
function sharedApi() { return { job: null, jobs: [], starts: 0, requests: [], startError: null, malformed: false, pollError: false }; }
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
      if (path === API + "/execution") return shared.execution ?? { options: [{ value: "auto", label: "Auto" }, { value: "cpu", label: "CPU" }], max_workers: 2, active_workers: 0, inference_limit: 1 };
      if (path.startsWith(API + "/drive/status")) return { connected: true, read_access: true, account, limits };
      if (path === API) return { experiments: (shared.jobs.length ? shared.jobs : shared.job ? [shared.job] : []).map(job => ({ ...job, videos: undefined })) };
      if (path === API + "/" + shared.job?.id || shared.jobs.some(job => path === API + "/" + job.id)) {
        if (shared.pollError) { shared.pollError = false; throw Error("Status request interrupted; retrying."); }
        return structuredClone(shared.jobs.find(job => path === API + "/" + job.id) ?? shared.job);
      }
      throw Error("Unexpected read: " + path);
    },
    async apiPostJson(path, body) {
      shared.requests.push({ method: "POST", path, body });
      if (path === API + "/execution") return { max_workers: body.max_workers };
      if (path.endsWith("/drive/folder")) return { folder_id: "folder_12345", folder_name: "Seven cameras", account, files, limits, outcome: "videos", subfolder_count: 0, message: "Seven supported videos." };
      if (path.endsWith("/drive/batches")) {
        shared.starts++; if (shared.startError) throw Error(shared.startError);
        shared.job = fixture(); shared.job.id = shared.starts === 1 ? ID : "b".repeat(32); shared.jobs.push(shared.job);
        return shared.malformed ? { status: "running" } : structuredClone(shared.job);
      }
      throw Error("Unexpected write: " + path);
    },
    async apiDelete(path) { shared.requests.push({ method: "DELETE", path }); shared.jobs = shared.jobs.filter(job => path !== API + "/" + job.id); shared.job = null; },
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
  await ui.tick(); await ui.tick(); assert.match(ui.text(), /Synthetic unsupported codec/); assert.match(ui.text(), /25\.0\s*% estimated decode progress/);
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

test("worker ceiling comes from the backend (not a fixed 4) and work is shown by stage", async () => {
  const shared = sharedApi();
  shared.execution = { options: [{ value: "auto", label: "Auto" }], max_workers: 2, max_workers_limit: 12, default_workers: 2,
    active_workers: 5, downloading_workers: 2, processing_workers: 3, inference_in_flight: 1, inference_limit: 1,
    queued_sources: 9, queued_batches: 1, reserved_download_bytes: 20 * 1024 ** 3 };
  const ui = await mount(shared);
  const field = () => ui.nodes().find(n => n.props["aria-label"] === "Simultaneous video workers");
  assert.equal(field().props.max, 12);
  await ui.input("Simultaneous video workers", "8"); await ui.click("Apply worker limit");
  assert.equal(shared.requests.find(r => r.path.endsWith("/execution") && r.method === "POST").body.max_workers, 8);
  const status = ui.text();
  assert.match(status, /1–\s*12/); assert.match(status, /2 downloading, 3 decoding\/recognizing/);
  assert.match(status, /GPU inference: 1 of 1 at a time/); assert.match(status, /Queued: 9 videos, 1 batches waiting/);
  assert.match(status, /20\.00 GiB/); assert.match(status, /measured default 2/);
  await ui.input("Simultaneous video workers", "13");
  assert.equal(ui.nodes().find(n => n.type === "button" && n.props.children === "Apply worker limit").props.disabled, true);
  ui.dispose();
  // An older backend that reports no ceiling keeps the former limit of 4.
  const legacy = await mount();
  assert.equal(legacy.nodes().find(n => n.props["aria-label"] === "Simultaneous video workers").props.max, 4);
  legacy.dispose();
});

test("Clear all after a batch started only clears the draft; the running batch continues", async () => {
  const ui = await mount(); await ui.start();
  assert.match(ui.text(), /Status: running/);
  const before = ui.shared.requests.length;
  await ui.click("Select all supported"); await ui.click("Clear all");
  assert.match(ui.text(), /0 selected/);
  assert.equal(ui.nodes().find(n => n.type === "button" && /Start selected/.test(n.props.children?.toString?.() ?? "")) !== undefined, true);
  assert.equal(ui.shared.requests.slice(before).some(r => r.method !== "GET" || /cancel|delete|folder/.test(r.path)), false,
    "no Drive listing, cancel or delete request");
  assert.equal(ui.shared.starts, 1); assert.match(ui.text(), /Status: running/);
  ui.dispose();
});

test("device and bounded worker controls allow a second seven-video batch while first runs; both links survive reload", async () => {
  const ui = await mount();
  await ui.input("Video processing device", "cpu");
  await ui.input("Simultaneous video workers", "3"); await ui.click("Apply worker limit");
  assert.equal(ui.shared.requests.find(r => r.path.endsWith('/execution') && r.method === 'POST').body.max_workers, 3);
  await ui.start(); await ui.start();
  assert.equal(ui.shared.starts, 2);
  assert.equal(ui.shared.jobs.length, 2);
  assert.notEqual(ui.shared.jobs[0].id, ui.shared.jobs[1].id);
  assert.ok(ui.shared.requests.filter(r => r.path.endsWith('/drive/batches')).every(r => r.body.device === 'cpu'));
  ui.shared.job.status = 'queued'; ui.shared.job.queue_position = 2;
  const shared = ui.shared, query = ui.query(); ui.dispose();
  const reload = await mount(shared, query);
  assert.match(reload.text(), /Status: queued/);
  assert.match(reload.text(), /Queue turn 2/);
  assert.equal(shared.starts, 2); reload.dispose();
});

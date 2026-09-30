// Real TSX controls with fake hooks and authenticated Drive metadata. No Google.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";
import * as settingsModule from "../src/api/scanSettings.ts";
const compiled = ts.transpileModule(readFileSync(new URL("../src/components/DriveVideoBatchPanel.tsx", import.meta.url), "utf8"),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText;
const account = { display_name: "Synthetic Account", email: "synthetic@example.test", account_id: "fake-account" };
const limits = { max_duration_seconds: 12 * 3600, max_files: 32, disk_reserve_bytes: 1024 ** 3, containers: [".mkv", ".avi"] };
const files = Array.from({ length: 7 }, (_, i) => ({ file_id: "video_0000" + (i + 1), filename: "cam0" + (i + 1) + (i ? ".avi" : ".mkv"),
  bytes: i ? 1024 : 2 * 1024 ** 3, supported: true, duration_seconds: i ? 1.2 : 3600, mime_type: i ? "video/x-msvideo" : "application/x-unexpected" }));
async function setup({ connected = true, readAccess = true, scanSettings = settingsModule.DEFAULT_SCAN_SETTINGS,
  entries = files, listingError = "", outcome = "videos", popupAllowed = true } = {}) {
  let cursor = 0, dirty = true, tree, timer;
  const hooks = [], effects = [], requests = [], started = [];
  const status = { connected, read_access: readAccess, account: connected ? account : null, limits, message: "Reconnect once and allow read-only access." };
  const popup = { closed: false, location: { href: "about:blank", origin: "null", search: "" }, close() { this.closed = true; }, focus() {} };
  globalThis.window = { location: { origin: "http://localhost:5173" }, open: () => popupAllowed ? popup : null,
    setInterval: callback => { timer = callback; return 1; }, clearInterval() { timer = undefined; } };
  const react = { useRef: initial => { const i = cursor++; hooks[i] ??= { current: initial }; return hooks[i]; },
    useState: initial => { const i = cursor++; hooks[i] ??= { value: initial }; return [hooks[i].value, value => { hooks[i].value = typeof value === "function" ? value(hooks[i].value) : value; dirty = true; }]; },
    useEffect: (fn, deps) => { const i = cursor++, prior = hooks[i]; if (!prior || deps.some((v, j) => v !== prior.deps[j])) effects.push(fn); hooks[i] = { deps }; } };
  const jsx = (type, props) => ({ type, props }), exports = {};
  const state = { folder_id: "folder_12345", folder_name: "Synthetic cameras", account, files: entries, limits, outcome, subfolder_count: 0,
    message: outcome === "empty" ? "This accessible folder has no direct files or subfolders." : outcome === "unsupported"
      ? "Files found, but none pass the video validation/limits." : "Seven selectable videos. Subfolders are not scanned." };
  const api = { apiGet: async path => { requests.push({ path }); return path.endsWith("/oauth/start") ? { auth_url: "https://accounts.google.com/synthetic-consent" } : { ...status }; },
    apiPostJson: async (path, body) => { requests.push({ path, body }); if (path.endsWith("/folder") && listingError) throw Error(listingError);
      return path.endsWith("/folder") ? state : { id: "synthetic-batch", kind: "drive_batch" }; } };
  new Function("require", "exports", compiled)(name => {
    if (name === "react") return react;
    if (name === "react/jsx-runtime") return { jsx, jsxs: jsx };
    if (name === "../api/client") return api;
    if (name === "../api/scanSettings") return settingsModule;
    if (name === "./ScanSettingsFields") return { default: "Fields" };
    if (name === "./ui") return { Button: "Button" };
    throw Error(name);
  }, exports);
  const props = { settings: { ...scanSettings }, onSettings() {}, disabled: false, onStarted: state => started.push(state) };
  const nodes = (node, out = []) => { if (Array.isArray(node)) node.forEach(n => nodes(n, out)); else if (node?.props) { out.push(node); nodes(node.props.children, out); } return out; };
  const text = node => Array.isArray(node) ? node.map(text).join(" ") : node?.props ? text(node.props.children) : typeof node === "string" || typeof node === "number" ? String(node) : "";
  const render = () => { cursor = 0; dirty = false; tree = exports.default(props); while (effects.length) effects.shift()(); };
  async function flush() { for (let i = 0; i < 8; i++) { if (dirty) render(); await new Promise(setImmediate); } }
  await flush();
  const all = () => nodes(tree);
  const button = value => all().find(n => n.type === "Button" && text(n).includes(value));
  async function input(label, value) { all().find(n => n.props["aria-label"] === label).props.onChange({ target: { value } }); await flush(); }
  async function click(label) { const node = button(label); assert.ok(node); assert.equal(node.props.disabled, false); await node.props.onClick(); await flush(); }
  return { all, button, click, input, flush, requests, started, popup, status, tick: () => timer?.(), text: () => text(tree), props };
}
test("link-only seven checkboxes include generic-MIME MKV and larger file; Start freezes defaults once", async () => {
  const ui = await setup();
  assert.match(ui.text(), /synthetic@example.test/);
  assert.doesNotMatch(ui.text(), /16\s+GiB/); assert.match(ui.text(), /No fixed file-size limit/); assert.match(ui.text(), /12\s+hours/);
  await ui.input("Google Drive folder link", "https://drive.google.com/drive/folders/folder_12345");
  await ui.click("List videos");
  assert.equal(ui.all().filter(n => n.props.type === "checkbox").length, 7);
  assert.match(ui.text(), /cam01.mkv/); assert.match(ui.text(), /application\/x-unexpected/);
  assert.equal(ui.requests.filter(r => r.path.endsWith("/folder")).length, 1);
  assert.equal(ui.requests.some(r => r.path.includes("picker")), false);
  await ui.click("Select all supported videos"); await ui.input("Video batch name", "Seven cameras");
  assert.equal(ui.requests.some(r => r.path.endsWith("/batches")), false);
  await ui.click("Start selected videos as one batch");
  const posts = ui.requests.filter(r => r.path.endsWith("/batches")); assert.equal(posts.length, 1);
  assert.deepEqual(posts[0].body.file_ids, files.map(f => f.file_id));
  assert.equal(posts[0].body.name, "Seven cameras");
  assert.deepEqual(posts[0].body.scan_settings, settingsModule.DEFAULT_SCAN_SETTINGS);
  assert.equal(ui.started[0].kind, "drive_batch");
});
test("old scope requires one-time reconnect; consent callback refreshes and enables link listing", async () => {
  const ui = await setup({ readAccess: false });
  await ui.input("Google Drive folder link", "folder_12345");
  assert.equal(ui.button("List videos").props.disabled, true);
  assert.match(ui.text(), /Reconnect once/);
  await ui.click("Reconnect with read-only access");
  assert.match(ui.popup.location.href, /accounts.google.com/);
  ui.status.read_access = true;
  ui.popup.location = { origin: "http://localhost:5173", search: "?drive_connected=1" };
  ui.tick(); await ui.flush();
  assert.equal(ui.popup.closed, true);
  assert.equal(ui.button("List videos").props.disabled, false);
  assert.equal(ui.requests.some(r => r.path.includes("picker")), false);
  const offline = await setup({ connected: false, readAccess: false });
  assert.equal(offline.button("List videos").props.disabled, true);
});
test("mixed supported/rejected files remain visible with exact reason; Select all excludes rejected", async () => {
  const rejected = { file_id: "bad_video_01", filename: "too-long.mkv", supported: false, bytes: 17 * 1024 ** 3, error: "Video duration is 13.00 hours; the per-file Drive limit is 12 hours." };
  const ui = await setup({ entries: [...files, rejected] });
  await ui.input("Google Drive folder link", "folder_12345"); await ui.click("List videos");
  const boxes = ui.all().filter(n => n.props.type === "checkbox");
  assert.equal(boxes.length, 8); assert.equal(boxes.at(-1).props.disabled, true);
  assert.match(ui.text(), /per-file Drive limit is 12 hours/);
  assert.match(ui.text(), /17\.00 GiB/, "large sizes are shown exactly, in GiB");
  await ui.click("Select all supported");
  assert.equal(ui.all().filter(n => n.props.type === "checkbox" && n.props.checked).length, 7);
});
test("Select all, Clear all, then individual picks start only those; listing is reused and a started batch is untouched", async () => {
  const ui = await setup();
  const checked = () => ui.all().filter(n => n.props.type === "checkbox" && n.props.checked).map(n => n.props["aria-label"]);
  const count = () => ui.all().find(n => n.props["aria-label"] === "Selected video count").props.children[0];
  const toggle = async (name, value) => { ui.all().find(n => n.props["aria-label"] === "Select " + name).props.onChange({ target: { checked: value } }); await ui.flush(); };
  await ui.input("Google Drive folder link", "https://drive.google.com/drive/folders/folder_12345");
  await ui.click("List videos");
  await ui.input("Video batch name", "Picked cameras");
  assert.equal(ui.button("Clear all").props.disabled, true, "nothing selected yet");
  await ui.click("Select all supported videos");
  assert.equal(checked().length, 7); assert.equal(count(), 7);
  assert.equal(ui.button("Start selected").props.disabled, false);
  await ui.click("Clear all");
  assert.deepEqual(checked(), []); assert.equal(count(), 0);
  assert.equal(ui.button("Start selected").props.disabled, true, "Start needs at least one video");
  assert.equal(ui.button("Clear all").props.disabled, true);
  // Link and listing stay: select again without another Drive request.
  assert.equal(ui.all().find(n => n.props["aria-label"] === "Google Drive folder link").props.value, "https://drive.google.com/drive/folders/folder_12345");
  assert.equal(ui.all().filter(n => n.props.type === "checkbox").length, 7);
  await toggle("cam02.avi", true); await toggle("cam05.avi", true); await toggle("cam07.avi", true); await toggle("cam05.avi", false);
  assert.equal(count(), 2);
  await ui.click("Start selected videos as one batch");
  const posts = ui.requests.filter(r => r.path.endsWith("/batches"));
  assert.equal(posts.length, 1);
  assert.deepEqual(posts[0].body.file_ids, ["video_00002", "video_00007"]);
  assert.equal(ui.requests.filter(r => r.path.endsWith("/folder")).length, 1, "Drive was listed once");
  // After Start, clearing the draft sends nothing: no cancel, no delete, no Drive call.
  const before = ui.requests.length;
  await ui.click("Clear all");
  assert.equal(ui.requests.length, before);
  assert.equal(ui.requests.some(r => /cancel|delete/i.test(r.path)), false);
  assert.equal(ui.started.length, 1);
});
test("genuinely empty, unsupported-only and no-access results differ and cannot start", async () => {
  const empty = await setup({ entries: [], outcome: "empty" });
  await empty.input("Google Drive folder link", "folder_12345"); await empty.click("List videos");
  assert.match(empty.text(), /accessible folder has no direct files/); assert.equal(empty.button("Start selected").props.disabled, true);
  const unsupported = await setup({ entries: [{ file_id: "text_file_123", filename: "notes.txt", supported: false, error: "Supported containers: MP4, MOV, AVI, MKV, WebM, M4V." }], outcome: "unsupported" });
  await unsupported.input("Google Drive folder link", "folder_12345"); await unsupported.click("List videos");
  assert.match(unsupported.text(), /Files found, but none pass/); assert.match(unsupported.text(), /notes.txt/);
  const denied = await setup({ listingError: "No access to this folder. Check sharing with the connected account." });
  await denied.input("Google Drive folder link", "folder_12345"); await denied.click("List videos");
  assert.match(denied.text(), /No access/); assert.equal(denied.button("Start selected").props.disabled, true);
});
test("custom settings validated; folder/account changes clear old selection", async () => {
  const custom = { camera_fps: 10, post_scan_delay_seconds: .7, target_detections_per_second: 1 };
  const ui = await setup({ scanSettings: custom }); await ui.input("Google Drive folder link", "folder_12345"); await ui.click("List videos");
  await ui.click("Select all supported"); await ui.input("Video batch name", "Custom batch"); await ui.click("Start selected");
  assert.deepEqual(ui.requests.find(r => r.path.endsWith("/batches")).body.scan_settings, custom);
  ui.status.account = { ...account, account_id: "changed" }; await ui.click("Refresh account");
  assert.equal(ui.button("Start selected").props.disabled, true);
  await ui.input("Google Drive folder link", "other_folder"); assert.equal(ui.button("Start selected").props.disabled, true);
  const invalid = await setup({ scanSettings: { ...custom, camera_fps: 0 } });
  await invalid.input("Google Drive folder link", "folder_12345"); await invalid.click("List videos");
  await invalid.click("Select all supported"); await invalid.input("Video batch name", "Invalid");
  assert.equal(invalid.button("Start selected").props.disabled, true);
});
test("blocked popup and declined consent have actionable errors", async () => {
  const blocked = await setup({ readAccess: false, popupAllowed: false }); await blocked.click("Reconnect with read-only access");
  assert.match(blocked.text(), /Allow this site's connection popup/);
  const declined = await setup({ readAccess: false }); await declined.click("Reconnect with read-only access");
  declined.popup.location = { origin: "http://localhost:5173", search: "?drive_error=access_denied" };
  declined.tick(); await declined.flush();
  assert.match(declined.text(), /consent did not complete/);
  assert.equal(declined.button("List videos").props.disabled, true);
});

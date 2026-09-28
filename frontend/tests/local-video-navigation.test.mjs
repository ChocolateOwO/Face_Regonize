// Real Main Layout with fake auth/API/hooks; no credentials or real data.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const source = readFileSync(new URL("../src/components/Layout.tsx", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
}).outputText;
const videoPath = "/local-video-experiment";
const nodes = (node, out = []) => {
  if (Array.isArray(node)) node.forEach(value => nodes(value, out));
  else if (node?.props) { out.push(node); nodes(node.props.children, out); }
  return out;
};
const text = node => Array.isArray(node) ? node.map(text).join(" ")
  : node?.props ? text(node.props.children) : typeof node === "string" ? node : "";

function harness(response, pathname = "/") {
  let cursor = 0, dirty = true, tree, username = "Synthetic Admin";
  const hooks = [], effects = [], requests = [];
  const react = {
    useState: initial => {
      const index = cursor++; hooks[index] ??= { value: initial };
      return [hooks[index].value, value => { hooks[index].value = value; dirty = true; }];
    },
    useEffect: (effect, deps) => {
      const index = cursor++, prior = hooks[index];
      if (!prior || deps.some((value, i) => value !== prior.deps[i])) {
        prior?.cleanup?.(); effects.push(() => { hooks[index].cleanup = effect(); });
      }
      hooks[index] = { deps, cleanup: prior?.cleanup };
    },
  };
  const jsx = (type, props) => ({ type, props }), exports = {};
  new Function("require", "exports", compiled)(name => {
    if (name === "react") return react;
    if (name === "react/jsx-runtime") return { jsx, jsxs: jsx };
    if (name === "react-router-dom") return {
      Link: "Link", NavLink: "NavLink", useLocation: () => ({ pathname }), useNavigate: () => () => {},
    };
    if (name === "../auth/AuthContext") return { useAuth: () => ({ username, logout: () => {} }) };
    if (name === "../api/client") return { apiGet: path => { requests.push(path); return response; } };
    throw new Error(name);
  }, exports);
  const render = () => {
    cursor = 0; dirty = false; tree = exports.default({ children: "Synthetic page" });
    while (effects.length) effects.shift()();
    return tree;
  };
  const flush = async () => {
    for (let i = 0; i < 5; i++) { if (dirty) render(); await new Promise(setImmediate); }
    return tree;
  };
  return { render, flush, requests, links: () => nodes(tree).filter(node => node.type === "NavLink"),
    unmount: () => hooks.forEach(hook => hook.cleanup?.()),
    changeUser: value => { username = value; dirty = true; }, tree: () => tree };
}

test("verified admin sees clearly labelled video experiment near CCTV", async () => {
  const app = harness(Promise.resolve({ role: "admin" })); await app.flush();
  const links = app.links(), video = links.find(link => link.props.to === videoPath);
  assert.ok(video); assert.match(text(video), /Local Video Experiment.*Admin experiment/);
  assert.ok(links.indexOf(video) < links.findIndex(link => link.props.to === "/connect"));
  assert.ok(links.some(link => link.props.to === "/photo-batches"));
  assert.doesNotMatch(text(app.tree()), /Legacy.*Experiment/);
  assert.deepEqual(app.requests, ["/api/auth/me"]);
});

test("non-admin does not see video entry; existing navigation stays", async () => {
  const app = harness(Promise.resolve({ role: "viewer" })); await app.flush();
  assert.ok(!app.links().some(link => link.props.to === videoPath));
  assert.ok(app.links().some(link => link.props.to === "/"));
  assert.ok(app.links().some(link => link.props.to === "/photo-batches"));
});

test("unverified or failed role check never assumes admin from username", async () => {
  let resolve; const response = new Promise(done => { resolve = done; });
  const app = harness(response); app.render();
  assert.ok(!app.links().some(link => link.props.to === videoPath));
  resolve({ role: "admin" }); await app.flush();
  assert.ok(app.links().some(link => link.props.to === videoPath));
  const failed = harness(Promise.reject(new Error("Synthetic denied"))); await failed.flush();
  assert.ok(!failed.links().some(link => link.props.to === videoPath));
});

test("late role response is ignored after unmount", async () => {
  let resolve; const app = harness(new Promise(done => { resolve = done; }));
  app.render(); app.unmount(); resolve({ role: "admin" }); await app.flush();
  assert.ok(!app.links().some(link => link.props.to === videoPath));
});

test("direct video route keeps sidebar; recognition kiosk keeps no sidebar", async () => {
  const video = harness(Promise.resolve({ role: "admin" }), videoPath); await video.flush();
  assert.ok(video.links().some(link => link.props.to === videoPath));
  const kiosk = harness(Promise.resolve({ role: "admin" }), "/recognition"); await kiosk.flush();
  assert.deepEqual(kiosk.links(), []);
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(app, /path="\/local-video-experiment" element=\{<LocalVideoExperiment \/>\}/);
});

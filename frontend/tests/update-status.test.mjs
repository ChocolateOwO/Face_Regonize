// Settings → Application Updates status badge: "No published release" is a
// neutral status, "Up to Date" only after a real comparison, and "Update Now"
// still depends only on update_available. Real UpdatePanel render with fake
// hooks and API (no browser, no network, no credentials).
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
import ts from "typescript";

function compile(rel){
 return ts.transpileModule(readFileSync(new URL(rel,import.meta.url),"utf8"),
  {compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
}
function load(rel,deps){const exports={};new Function("require","exports",compile(rel))(n=>{if(n in deps)return deps[n];throw Error(n);},exports);return exports;}
const helper=load("../src/updateStatus.ts",{});

const BASE={current_version:"1.0.1",latest_version:null,update_available:false,release_notes:"",release_url:"",release_name:"",
 last_checked:"2026-11-26T14:52:43",github_configured:true,error:"",maintenance_active:false,abandoned_update:false,abandoned_log_path:""};
const NO_RELEASE={...BASE,no_published_release:true,
 error:"No published release found for 'o/r'. Check GITHUB_REPO, and that the repository is public and has a published release."};
const UP_TO_DATE={...BASE,latest_version:"1.0.1",no_published_release:false};
const AVAILABLE={...BASE,latest_version:"1.0.2",update_available:true,no_published_release:false};
const FAILED=(error)=>({...BASE,check_failed:true,no_published_release:false,error});
const RATE="GitHub's API rate limit was reached. The application is unaffected; the next check will run automatically later.";
const NETWORK="Could not reach GitHub: no route to host";
const HTTP="Could not check for updates — GitHub returned HTTP 502.";

async function panel(status){
 let cursor=0,dirty=true,tree;const hooks=[],effects=[];
 const react={useState:i=>{const k=cursor++;hooks[k]??={v:typeof i==="function"?i():i};return [hooks[k].v,v=>{hooks[k].v=typeof v==="function"?v(hooks[k].v):v;dirty=true;}];},
  useEffect:fn=>{const k=cursor++;if(!hooks[k]){effects.push(fn);hooks[k]={};}},
  useRef:i=>{const k=cursor++;hooks[k]??={current:i};return hooks[k];}};
 const jsx=(type,props)=>({type,props});
 const mod=load("../src/components/UpdatePanel.tsx",{"react":react,"react/jsx-runtime":{jsx,jsxs:jsx,Fragment:"Fragment"},
  "../api/client":{apiGet:async p=>{assert.equal(p,"/api/update/status");return {...status};},apiPostJson:async()=>({}),API_BASE:"",ApiError:class extends Error{}},
  "./ui":{Badge:"Badge",Button:"Button",Card:"Card"},"../updateStatus":helper});
 const nodes=(n,out=[])=>{if(Array.isArray(n))n.forEach(x=>nodes(x,out));else if(n?.props){out.push(n);nodes(n.props.children,out);}return out;};
 const text=n=>Array.isArray(n)?n.map(text).join(""):n?.props?text(n.props.children):typeof n==="string"||typeof n==="number"?String(n):"";
 const render=()=>{cursor=0;dirty=false;tree=mod.default();while(effects.length)effects.shift()();};
 for(let i=0;i<6;i++){if(dirty)render();await new Promise(setImmediate);}
 const badges=nodes(tree).filter(n=>n.type==="Badge").map(n=>({tone:n.props.tone,label:text(n)}));
 const buttons=nodes(tree).filter(n=>n.type==="Button").map(text);
 return {badges,buttons,text:text(tree)};
}

test("helper: no published release is a neutral status, never Up to Date",()=>{
 assert.deepEqual(helper.updateStatusBadge(NO_RELEASE),{label:"● No published release",tone:"default"});
});
test("helper: a confirmed check with nothing newer is Up to Date",()=>{
 assert.deepEqual(helper.updateStatusBadge(UP_TO_DATE),{label:"● Up to Date",tone:"good"});
 assert.deepEqual(helper.updateStatusBadge({...UP_TO_DATE,no_published_release:undefined}),{label:"● Up to Date",tone:"good"},"older backend without the flag");
});
test("helper: a newer release is Update Available; unconfigured shows no badge",()=>{
 assert.deepEqual(helper.updateStatusBadge(AVAILABLE),{label:"● Update Available — v1.0.2",tone:"warn"});
 assert.equal(helper.updateStatusBadge({...BASE,github_configured:false}),null);
});

test("panel: no published release shows the neutral badge and the message, no Up to Date, no Update Now",async()=>{
 const p=await panel(NO_RELEASE);
 assert.deepEqual(p.badges,[{tone:"default",label:"● No published release"}]);
 assert.doesNotMatch(p.text,/Up to Date/);
 assert.match(p.text,/No published release found for 'o\/r'/);
 assert.equal(p.buttons.some(b=>/Update Now/.test(b)),false);
});
test("panel: confirmed up to date shows the green Up to Date badge and no Update Now",async()=>{
 const p=await panel(UP_TO_DATE);
 assert.deepEqual(p.badges,[{tone:"good",label:"● Up to Date"}]);
 assert.doesNotMatch(p.text,/No published release/);
 assert.equal(p.buttons.some(b=>/Update Now/.test(b)),false);
});
test("panel: update available keeps the warn badge and the Update Now button",async()=>{
 const p=await panel(AVAILABLE);
 assert.deepEqual(p.badges,[{tone:"warn",label:"● Update Available — v1.0.2"}]);
 assert.equal(p.buttons.some(b=>/Update Now/.test(b)),true);
 assert.doesNotMatch(p.text,/Up to Date|No published release/);
});

test("helper: every failed check is a neutral Check failed, never Up to Date",()=>{
 for(const e of [NETWORK,RATE,HTTP,"GitHub refused the request. If the repository is private, this build expects a public one."])
  assert.deepEqual(helper.updateStatusBadge(FAILED(e)),{label:"● Check failed",tone:"default"},e);
});
test("helper: an older backend's failed result (error, no flag, no release) is still not Up to Date",()=>{
 assert.deepEqual(helper.updateStatusBadge({...BASE,error:RATE}),{label:"● Check failed",tone:"default"});
 assert.deepEqual(helper.updateStatusBadge({...BASE}),{label:"● Check failed",tone:"default"},"nothing was compared");
});
test("helper: a failed flag wins over a stale latest_version",()=>{
 assert.deepEqual(helper.updateStatusBadge({...UP_TO_DATE,check_failed:true,error:NETWORK}),{label:"● Check failed",tone:"default"});
});

for(const [name,error] of [["network failure",NETWORK],["rate limit",RATE],["HTTP error",HTTP]]){
 test(`panel: ${name} shows Check failed with the actionable message, no Up to Date, no Update Now`,async()=>{
  const p=await panel(FAILED(error));
  assert.deepEqual(p.badges,[{tone:"default",label:"● Check failed"}]);
  assert.ok(p.text.includes(error),"the error message is kept");
  assert.doesNotMatch(p.text,/Up to Date|No published release/);
  assert.equal(p.buttons.some(b=>/Update Now/.test(b)),false);
  assert.equal(p.buttons.some(b=>/Check for Updates/.test(b)),true,"the admin can retry");
 });
}

test("panel: unconfigured keeps its note and shows no status badge",async()=>{
 const p=await panel({...BASE,github_configured:false,error:"Updates are not configured — set GITHUB_REPO in .env (e.g. owner/Reconize)."});
 assert.deepEqual(p.badges,[]);
 assert.match(p.text,/Updates are not configured on this machine/);
 assert.doesNotMatch(p.text,/Up to Date|Check failed|No published release/);
});

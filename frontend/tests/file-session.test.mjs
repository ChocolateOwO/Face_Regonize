// File-read cookie: requested once per page load before protected pages
// render (so first-render <img> tags are authorized), never blocks on
// failure, and is cleared on sign-out.
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
import ts from "typescript";

function load(rel, deps){
 const source=readFileSync(new URL(rel,import.meta.url),"utf8");
 const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
 const exports={};new Function("require","exports",compiled)(name=>{if(name in deps)return deps[name];throw Error(name);},exports);return exports;
}

function fileSession({fail=false}={}){
 const posts=[],fetches=[];
 globalThis.fetch=async(url,init)=>{fetches.push({url,method:init?.method});return {ok:true};};
 const mod=load("../src/auth/fileSession.ts",{"../api/client":{API_BASE:"",apiPostJson:async(path,body)=>{posts.push({path,body});if(fail)throw new Error("401");return new Blob();}}});
 return {mod,posts,fetches};
}

test("file session is requested once per page load and failures never block",async()=>{
 const {mod,posts}=fileSession();
 await Promise.all([mod.ensureFileSession(),mod.ensureFileSession()]);await mod.ensureFileSession();
 assert.deepEqual(posts,[{path:"/api/auth/file-session",body:{}}]);
 const failing=fileSession({fail:true});
 await assert.doesNotReject(failing.mod.ensureFileSession());
});

test("sign-out clears the cookie and the next sign-in requests a new one",async()=>{
 const {mod,posts,fetches}=fileSession();
 await mod.ensureFileSession();mod.endFileSession();await mod.ensureFileSession();
 assert.deepEqual(fetches,[{url:"/api/auth/file-session",method:"DELETE"}]);
 assert.equal(posts.length,2);
});

test("ProtectedRoute renders children only after the file session settles",async()=>{
 let release;const gate=new Promise(r=>release=r);let cursor=0;const hooks=[],effects=[];
 const react={useState:i=>{const k=cursor++;hooks[k]??={v:i};return [hooks[k].v,v=>{hooks[k].v=v;}];},
  useEffect:(fn,deps)=>{const k=cursor++;const prior=hooks[k];if(!prior||deps.some((d,j)=>d!==prior.deps[j]))effects.push(fn);hooks[k]={deps};}};
 const jsx=(type,props)=>({type,props});
 const mod=load("../src/components/ProtectedRoute.tsx",{"react":react,"react/jsx-runtime":{jsx,jsxs:jsx,Fragment:"Fragment"},
  "react-router-dom":{Navigate:"Navigate"},"../auth/AuthContext":{useAuth:()=>({username:"admin"})},
  "../auth/fileSession":{ensureFileSession:()=>gate}});
 const render=()=>{cursor=0;const out=mod.default({children:"PAGE"});while(effects.length)effects.shift()();return out;};
 assert.equal(render(),null,"nothing (no <img>) before the cookie is set");
 release();await new Promise(setImmediate);
 assert.equal(render().props.children,"PAGE");
});

test("signed-out users are still redirected to login",()=>{
 const react={useState:i=>[i,()=>{}],useEffect:()=>{}};const jsx=(type,props)=>({type,props});
 const mod=load("../src/components/ProtectedRoute.tsx",{"react":react,"react/jsx-runtime":{jsx,jsxs:jsx,Fragment:"Fragment"},
  "react-router-dom":{Navigate:"Navigate"},"../auth/AuthContext":{useAuth:()=>({username:null})},
  "../auth/fileSession":{ensureFileSession:()=>{throw Error("must not be called");}}});
 const out=mod.default({children:"PAGE"});
 assert.equal(out.type,"Navigate");assert.equal(out.props.to,"/login");
});

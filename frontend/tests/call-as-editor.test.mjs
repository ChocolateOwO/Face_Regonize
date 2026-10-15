// CallAsEditor: real component render with fake React hooks and fake API.
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
import ts from "typescript";
const source=readFileSync(new URL("../src/components/CallAsEditor.tsx",import.meta.url),"utf8");
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;

async function setup({value=null,fail=null}={}){
 let cursor=0,dirty=true,tree;const hooks=[],effects=[],puts=[],saved=[];
 const react={useState:initial=>{const i=cursor++;hooks[i]??={value:initial};return [hooks[i].value,v=>{hooks[i].value=v;dirty=true;}];},
  useEffect:(fn,deps)=>{const i=cursor++;const prior=hooks[i];if(!prior||deps.some((d,j)=>d!==prior.deps[j]))effects.push(fn);hooks[i]={deps};}};
 const jsx=(type,props)=>({type,props});const exports={};
 new Function("require","exports",compiled)(name=>{
  if(name==="react")return react;
  if(name==="react/jsx-runtime")return {jsx,jsxs:jsx,Fragment:"Fragment"};
  if(name==="./ui")return {Button:"Button",Input:"Input"};
  if(name==="../api/client")return {apiPutJson:async(path,body)=>{puts.push({path,body});if(fail)throw new Error(fail);
   const v=body.call_as.split(/\s+/).filter(Boolean).join(" ");return {call_as:v||null};}};
  throw Error(name);
 },exports);
 const props={personId:"p1",value,onSaved:v=>saved.push(v)};
 const nodes=(n,out=[])=>{if(Array.isArray(n))n.forEach(x=>nodes(x,out));else if(n?.props){out.push(n);nodes(n.props.children,out);}return out;};
 const text=n=>Array.isArray(n)?n.map(text).join(" "):n?.props?text(n.props.children):typeof n==="string"?n:"";
 const render=()=>{cursor=0;dirty=false;tree=exports.default(props);while(effects.length)effects.shift()();};
 async function flush(){for(let i=0;i<6;i++){if(dirty)render();await new Promise(setImmediate);}}
 await flush();
 return {flush,puts,saved,input:()=>nodes(tree).find(n=>n.type==="Input"),button:()=>nodes(tree).find(n=>n.type==="Button"),text:()=>text(tree)};
}

test("shows the current value; Save is disabled until it changes",async()=>{
 const app=await setup({value:"พี่บอส"});
 assert.equal(app.input().props.value,"พี่บอส");
 assert.equal(app.input().props.maxLength,40);
 assert.equal(app.button().props.disabled,true);
 app.input().props.onChange({target:{value:"พี่บอส "}});await app.flush();
 assert.equal(app.button().props.disabled,true,"whitespace-only change is not a change");
});

test("saves through the dedicated call-as endpoint only",async()=>{
 const app=await setup();
 app.input().props.onChange({target:{value:" พี่  แอน "}});await app.flush();
 assert.equal(app.button().props.disabled,false);
 await app.button().props.onClick();await app.flush();
 assert.deepEqual(app.puts,[{path:"/api/people/p1/call-as",body:{call_as:" พี่  แอน "}}]);
 assert.deepEqual(app.saved,["พี่ แอน"]);
});

test("clearing sends empty and reports null; errors are shown",async()=>{
 const app=await setup({value:"Boss"});
 app.input().props.onChange({target:{value:""}});await app.flush();
 await app.button().props.onClick();await app.flush();
 assert.deepEqual(app.saved,[null]);
 const bad=await setup({fail:"Call-as is limited to 40 characters."});
 bad.input().props.onChange({target:{value:"x"}});await bad.flush();
 await bad.button().props.onClick();await bad.flush();
 assert.match(bad.text(),/limited to 40/);assert.deepEqual(bad.saved,[]);
});

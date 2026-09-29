// Real modal render/behavior with synthetic image Blob, fake API and hooks.
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
import ts from "typescript";
const source=readFileSync(new URL("../src/components/VideoMatchPreview.tsx",import.meta.url),"utf8");
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
const person={identity_key:"a",name:"Synthetic Alpha",detection_count:8,manual_verdict:"Not reviewed",preview_available:true,
 preview:{frame_index:2,frame_number:3,timestamp_seconds:.4,source_timestamp_seconds:.433,match_score:.9,
  selection_rule:"strongest valid cosine similarity; earliest tie",source_filename:"synthetic_cam07.avi",example_key:"1".repeat(32),width:80,height:30,bbox:[0,4.4,39.8,30]}};
async function setup({old=false,active=false}={}){
 let cursor=0,dirty=true,tree;const hooks=[],effects=[],requests=[],posts=[],revoked=[],reviewed=[];
 globalThis.document={activeElement:{focus:()=>{}},addEventListener:()=>{},removeEventListener:()=>{}};
 const originalCreate=URL.createObjectURL,originalRevoke=URL.revokeObjectURL;
 URL.createObjectURL=()=>"blob:synthetic-authenticated";URL.revokeObjectURL=url=>revoked.push(url);
 const react={useRef:initial=>{const i=cursor++;hooks[i]??={current:initial};return hooks[i];},
 useState:initial=>{const i=cursor++;hooks[i]??={value:initial};return [hooks[i].value,value=>{hooks[i].value=value;dirty=true;}];},
 useEffect:(fn,deps)=>{const i=cursor++;const prior=hooks[i];if(!prior||deps.some((value,j)=>value!==prior.deps[j]))effects.push(()=>{prior?.cleanup?.();hooks[i].cleanup=fn();});hooks[i]={...prior,deps};}};
 const jsx=(type,props)=>{if(props.ref)props.ref.current={focus:()=>{},closest:()=>null};return {type,props};};
 const exports={};const chosen=old?{...person,preview:null,preview_available:false}:person;
 new Function("require","exports",compiled)(name=>{
  if(name==="react")return react;
  if(name==="react/jsx-runtime")return {jsx,jsxs:jsx,Fragment:"Fragment"};
  if(name==="./ui")return {Button:"Button"};
  if(name==="../api/client")return {apiGet:async(path,signal)=>{requests.push({path,signal});return new Blob(["synthetic-image"]);},
   apiPostJson:async(path,body)=>{posts.push({path,body});return {people:[{...person,manual_verdict:body.verdict}]};}};
  throw Error(name);
 },exports);
 const props={jobId:"synthetic-job",person:chosen,active,onClose:()=>{},onReviewed:state=>reviewed.push(state)};
 const nodes=(node,out=[])=>{if(Array.isArray(node))node.forEach(n=>nodes(n,out));else if(node?.props){out.push(node);nodes(node.props.children,out);}return out;};
 const text=node=>Array.isArray(node)?node.map(text).join(" "):node?.props?text(node.props.children):typeof node==="string"||typeof node==="number"?String(node):"";
 const render=()=>{cursor=0;dirty=false;tree=exports.default(props);while(effects.length)effects.shift()();};
 async function flush(){for(let i=0;i<8;i++){if(dirty)render();await new Promise(setImmediate);}}
 await flush();const image=nodes(tree).find(n=>n.type==="img");if(image){image.props.onLoad();await flush();}return {flush,requests,posts,revoked,reviewed,nodes:()=>nodes(tree),text:()=>text(tree).replace(/\s+/g," "),
  cleanup:()=>{hooks.forEach(h=>h?.cleanup?.());URL.createObjectURL=originalCreate;URL.revokeObjectURL=originalRevoke;}};
}
test("modal uses authenticated Blob endpoint, boxed whole frame and proportional zoom",async()=>{
 const app=await setup();try{
  assert.match(app.requests[0].path,/people\/a\/preview\?example_key=/);
  const image=app.nodes().find(n=>n.type==="img");assert.equal(image.props.src,"blob:synthetic-authenticated");
  const svg=app.nodes().find(n=>n.type==="svg");assert.equal(svg.props.viewBox,"0 0 80 30");
  const rectangles=app.nodes().filter(n=>n.type==="rect");assert.equal(rectangles.length,2,"Two contrast strokes for same one face");
  for(const rect of rectangles){assert.equal(rect.props.x,0);assert.equal(rect.props.y,4.4);assert.equal(rect.props.width,39.8);assert.equal(rect.props.height,25.6);}
  const zoom=app.nodes().find(n=>n.props["aria-label"]==="Match preview zoom");zoom.props.onChange({target:{value:"2"}});await app.flush();
  assert.match(app.text(),/Zoom 2.0\s*x/);assert.ok(app.nodes().some(n=>n.props.style?.width==="200%"));
  assert.match(app.text(),/0.433 s/);assert.match(app.text(),/Frame 3/);assert.match(app.text(),/Cosine similarity: 0.9000/);
  assert.match(app.text(),/Source video:\s*synthetic_cam07.avi/);
 }finally{app.cleanup();}assert.deepEqual(app.revoked,["blob:synthetic-authenticated"]);
});
test("manual verdict sends exact example key, does not send counts or accuracy",async()=>{
 const app=await setup();try{
  const select=app.nodes().find(n=>n.type==="select");select.props.onChange({target:{value:"Incorrect"}});await app.flush();
  const save=app.nodes().find(n=>n.type==="Button");assert.equal(save.props.disabled,false);save.props.onClick();await app.flush();
  assert.deepEqual(app.posts[0].body,{verdict:"Incorrect",example_key:"1".repeat(32)});
  assert.equal(app.reviewed.length,1);assert.match(app.text(),/does not verify every appearance/);
 }finally{app.cleanup();}
});
test("older unavailable example makes no request; active run cannot save a verdict",async()=>{
 const old=await setup({old:true});try{assert.equal(old.requests.length,0);assert.match(old.text(),/Preview unavailable for this older run/);}finally{old.cleanup();}
 const active=await setup({active:true});try{assert.equal(active.nodes().find(n=>n.type==="Button").props.disabled,true);assert.match(active.text(),/Wait for processing to stop/);}finally{active.cleanup();}
});

test("failed image display disables verdict submission",async()=>{
 const app=await setup();try{
  app.nodes().find(n=>n.type==="img").props.onError();await app.flush();
  assert.equal(app.nodes().find(n=>n.type==="Button").props.disabled,true);
  assert.match(app.text(),/No review saved/);assert.equal(app.posts.length,0);
 }finally{app.cleanup();}
});

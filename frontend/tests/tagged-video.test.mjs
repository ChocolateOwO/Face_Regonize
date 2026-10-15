// Tagged-video controls: real component render with fake hooks and API.
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
import ts from "typescript";

const compiled=ts.transpileModule(readFileSync(new URL("../src/components/VideoRenderControls.tsx",import.meta.url),"utf8"),
 {compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;

function load(calls){
 let cursor=0;const hooks=[];let dirty=false;
 const react={useState:i=>{const k=cursor++;hooks[k]??={v:i};return [hooks[k].v,v=>{hooks[k].v=v;dirty=true;}];}};
 const jsx=(type,props)=>({type,props});const exports={};
 new Function("require","exports",compiled)(name=>{
  if(name==="react")return react;
  if(name==="react/jsx-runtime")return {jsx,jsxs:jsx,Fragment:"Fragment"};
  if(name==="./ui")return {Button:"Button"};
  if(name==="../api/client")return {API_BASE:"",ApiError:class extends Error{},
   apiPostJson:async(path,body)=>{calls.push({method:"POST",path,body});return {id:"job",refreshed:true};},
   apiDelete:async path=>{calls.push({method:"DELETE",path});return {id:"job",refreshed:true};}};
  throw Error(name);
 },exports);
 const nodes=(n,out=[])=>{if(Array.isArray(n))n.forEach(x=>nodes(x,out));else if(n?.props){out.push(n);nodes(n.props.children,out);}return out;};
 const text=n=>Array.isArray(n)?n.map(text).join(" "):n?.props?text(n.props.children):typeof n==="string"||typeof n==="number"?String(n):"";
 const render=(component,props)=>{cursor=0;dirty=false;return exports[component](props);};
 return {exports,nodes,text:n=>text(n).replace(/\s+/g," "),render,isDirty:()=>dirty,reset:()=>{hooks.length=0;}};
}

test("render is off by default; turning it on reveals Plain/Tiled and the Tiled GPU warning",()=>{
 const ui=load([]);const changes=[];
 const off=ui.render("RenderOptions",{value:ui.exports.DEFAULT_RENDER_CHOICE,onChange:v=>changes.push(v),disabled:false,drive:true});
 assert.deepEqual(ui.exports.DEFAULT_RENDER_CHOICE,{enabled:false,keep_audio:true,detector:"plain",decoder:"auto",encoder:"auto"});
 assert.equal(ui.nodes(off).filter(n=>n.type==="select").length,0);
 assert.match(ui.text(off),/cannot be rendered later/);
 assert.match(ui.text(off),/recognition counts, previews and the CSV are not changed/);
 ui.nodes(off).find(n=>n.props["aria-label"]==="Render tagged video").props.onChange({target:{checked:true}});
 assert.deepEqual(changes.at(-1),{enabled:true,keep_audio:true,detector:"plain",decoder:"auto",encoder:"auto"});
 const on=ui.render("RenderOptions",{value:changes.at(-1),onChange:v=>changes.push(v),disabled:false});
 const select=ui.nodes(on).find(n=>n.type==="select");
 assert.equal(select.props.value,"plain");
 assert.deepEqual(select.props.children.map(o=>o.props.value),["plain","tiled"]);
 assert.doesNotMatch(ui.text(on),/9 detector calls/);
 select.props.onChange({target:{value:"tiled"}});
 const tiled=ui.render("RenderOptions",{value:changes.at(-1),onChange:()=>{},disabled:false});
 assert.match(ui.text(tiled),/9 detector calls per 1080p frame/);
 assert.match(ui.text(tiled),/several times more GPU time/);
});

test("the request body carries render only when it is enabled",()=>{
 const ui=load([]);
 assert.deepEqual(ui.exports.renderBody({enabled:false,keep_audio:true,detector:"tiled",decoder:"gpu",encoder:"cpu"}),{});
 assert.deepEqual(ui.exports.renderBody({enabled:true,keep_audio:false,detector:"tiled",decoder:"gpu",encoder:"cpu"}),
  {render:{enabled:true,keep_audio:false,detector:"tiled",decoder:"gpu",encoder:"cpu"}});
 assert.equal(ui.exports.renderIsActive({status:"running"}),true);
 assert.equal(ui.exports.renderIsActive({status:"completed"}),false);
 assert.equal(ui.exports.renderIsActive(null),false);
});

test("a running render shows progress and the actual decoder, detector and encoder; Cancel targets that source",async()=>{
 const calls=[];const ui=load(calls);const changed=[];
 const record={status:"running",detector:"tiled",keep_audio:true,progress_percent:42.5,frames_written:340,frames_expected:800,
  decoder_mode:"cpu",encoder_mode:"auto",
  decoder:"OpenCV FFmpeg (CPU)",encoder:"libx264",encoder_label:"libx264 (CPU)",encoder_note:"h264_nvenc unavailable at 4280x2160: width",audio:"copied"};
 const tree=ui.render("RenderStatus",{jobId:"abc",sourceKey:"video-003",filename:"cam03.mkv",record,onChanged:s=>changed.push(s)});
 const text=ui.text(tree);
 assert.match(text,/Tagged video: rendering 42\.5 ?%/);assert.match(text,/340 ?\/ ?~800 frames/);
 assert.match(text,/Detector: Tiled \(higher GPU cost\)/);assert.match(text,/Decoder: OpenCV FFmpeg \(CPU\) \( ?CPU ?\)/);
 assert.match(text,/Encoder: libx264 \(CPU\) \( ?Auto ?\)/);assert.match(text,/Encoder fallback: h264_nvenc unavailable at 4280x2160/);
 const buttons=ui.nodes(tree).filter(n=>n.type==="Button");
 assert.deepEqual(buttons.map(b=>ui.text(b)),["Cancel render"]);
 await buttons[0].props.onClick();await new Promise(setImmediate);
 assert.deepEqual(calls,[{method:"POST",path:"/api/local-video-experiment/abc/sources/video-003/render/cancel",body:{}}]);
 assert.equal(changed.length,1);
});

test("a finished render offers Download and Delete for that source only and reports timing honestly",async()=>{
 const calls=[];const ui=load(calls);
 globalThis.window={confirm:()=>true};
 const record={status:"completed",detector:"plain",keep_audio:true,output_bytes:5*1024**2,width:2360,height:1080,decoder:"OpenCV FFmpeg (CPU)",
  encoder:"h264_nvenc",encoder_label:"h264_nvenc (NVIDIA GPU)",audio:"transcoded",timing:"source timestamps preserved (no frames duplicated or dropped)",
  source_vfr:true,timestamp_repairs:0,padded_pixels:[0,0]};
 const tree=ui.render("RenderStatus",{jobId:"abc",sourceKey:"local",filename:"clip.mp4",record,onChanged:()=>{}});
 const text=ui.text(tree);
 assert.match(text,/Tagged video: done — 5\.0 MiB ?, 2360×1080/);assert.match(text,/source audio converted to AAC/);
 assert.match(text,/source timestamps preserved \(no frames duplicated or dropped\) \(variable frame rate source\)/);
 const buttons=ui.nodes(tree).filter(n=>n.type==="Button");
 assert.deepEqual(buttons.map(b=>ui.text(b)),["Download tagged video","Delete tagged video"]);
 buttons[1].props.onClick();await new Promise(setImmediate);
 assert.deepEqual(calls,[{method:"DELETE",path:"/api/local-video-experiment/abc/sources/local/render"}]);
});

test("failed, not-rendered and absent renders are stated plainly",()=>{
 const ui=load([]);
 assert.equal(ui.render("RenderStatus",{jobId:"a",sourceKey:"local",filename:"x.mp4",record:null,onChanged:()=>{}}),null);
 const failed=ui.render("RenderStatus",{jobId:"a",sourceKey:"local",filename:"x.mp4",onChanged:()=>{},
  record:{status:"failed",detector:"plain",keep_audio:true,error:"Not enough free disk space for the tagged video of x.mp4"}});
 assert.match(ui.text(failed),/Tagged video: failed/);assert.match(ui.text(failed),/Not enough free disk space/);
 assert.deepEqual(ui.nodes(failed).filter(n=>n.type==="Button").map(b=>ui.text(b)),["Clear"]);
 const skipped=ui.render("RenderStatus",{jobId:"a",sourceKey:"video-002",filename:"y.mp4",onChanged:()=>{},
  record:{status:"not_rendered",detector:"plain",keep_audio:true,error:"Recognition did not complete for this source, so no tagged video was made."}});
 assert.match(ui.text(skipped),/not rendered/);
 assert.equal(ui.nodes(skipped).filter(n=>n.type==="Button").length,0);
});

test("a retained local upload can be rendered later with its own choices",async()=>{
 const calls=[];const ui=load(calls);const changed=[];
 const tree=ui.render("RenderLater",{jobId:"abc",disabled:false,onChanged:s=>changed.push(s)});
 assert.match(ui.text(tree),/still stored/);
 await ui.nodes(tree).find(n=>n.type==="Button").props.onClick();await new Promise(setImmediate);
 assert.deepEqual(calls,[{method:"POST",path:"/api/local-video-experiment/abc/render",body:{keep_audio:true,detector:"plain",decoder:"auto",encoder:"auto"}}]);
 assert.equal(changed.length,1);
});

// ModeFields is a component of its own: render it the way React would.
const modeFields=(ui,tree)=>{const node=ui.nodes(tree).find(n=>typeof n.type==="function"&&n.type.name==="ModeFields");return {node,tree:node.type(node.props)};};

test("decoder and encoder are chosen independently: Auto, GPU or CPU each",()=>{
 const ui=load([]);const changes=[];
 const value={...ui.exports.DEFAULT_RENDER_CHOICE,enabled:true};
 const fields=modeFields(ui,ui.render("RenderOptions",{value,onChange:v=>changes.push(v),disabled:false}));
 const selects=ui.nodes(fields.tree).filter(n=>n.type==="select");
 assert.deepEqual(selects.map(s=>s.props["aria-label"]),["Tagged video decoder","Tagged video encoder"]);
 for(const select of selects){assert.equal(select.props.value,"auto");assert.deepEqual(select.props.children.map(o=>o.props.value),["auto","gpu","cpu"]);}
 assert.doesNotMatch(ui.text(fields.tree),/fails with the reason/);
 selects[0].props.onChange({target:{value:"gpu"}});
 assert.deepEqual(changes.at(-1),{...value,decoder:"gpu"});            // the encoder did not move
 selects[1].props.onChange({target:{value:"cpu"}});
 assert.deepEqual(changes.at(-1),{...value,encoder:"cpu"});            // the decoder did not move
 const gpu=modeFields(ui,ui.render("RenderOptions",{value:{...value,decoder:"cpu",encoder:"gpu"},onChange:()=>{},disabled:false}));
 assert.match(ui.text(gpu.tree),/cannot encode a video on the GPU, that tagged video fails with the reason instead of quietly using the CPU/);
 assert.match(ui.text(gpu.tree),/4K source is wider than the GPU's H\.264 encoder accepts/);
 const both=modeFields(ui,ui.render("RenderOptions",{value:{...value,decoder:"gpu",encoder:"gpu"},onChange:()=>{},disabled:false}));
 assert.match(ui.text(both.tree),/cannot decode or encode a video on the GPU/);
 assert.equal(ui.nodes(ui.render("RenderOptions",{value:ui.exports.DEFAULT_RENDER_CHOICE,onChange:()=>{},disabled:false})).some(n=>typeof n.type==="function"),false);
});

test("a later render sends its own decoder and encoder choice",async()=>{
 const calls=[];const ui=load(calls);
 let tree=ui.render("RenderLater",{jobId:"abc",disabled:false,onChanged:()=>{}});
 const fields=modeFields(ui,tree);
 ui.nodes(fields.tree).find(n=>n.props["aria-label"]==="Tagged video encoder").props.onChange({target:{value:"gpu"}});
 tree=ui.render("RenderLater",{jobId:"abc",disabled:false,onChanged:()=>{}});
 const again=modeFields(ui,tree);
 assert.equal(again.node.props.encoder,"gpu");assert.equal(again.node.props.decoder,"auto");
 await ui.nodes(tree).find(n=>n.type==="Button").props.onClick();await new Promise(setImmediate);
 assert.deepEqual(calls.at(-1).body,{keep_audio:true,detector:"plain",decoder:"auto",encoder:"gpu"});
});

test("a failed explicit-GPU render says which mode was asked for and why it stopped",()=>{
 const ui=load([]);
 const failed=ui.render("RenderStatus",{jobId:"a",sourceKey:"local",filename:"x.mp4",onChanged:()=>{},
  record:{status:"failed",detector:"plain",keep_audio:true,decoder_mode:"cpu",encoder_mode:"gpu",decoder:"OpenCV FFmpeg (CPU)",
   error:"GPU encoding (h264_nvenc) cannot write 4280x2160 on this computer: width. Choose Auto or CPU encoding and render again."}});
 const text=ui.text(failed);
 assert.match(text,/Encoder: GPU requested/);assert.match(text,/Decoder: OpenCV FFmpeg \(CPU\) \( ?CPU ?\)/);
 assert.match(text,/cannot write 4280x2160/);
});

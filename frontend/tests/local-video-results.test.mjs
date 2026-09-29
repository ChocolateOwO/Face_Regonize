// Render real result page with fake API/hooks; synthetic state only.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";
import * as settings from "../src/api/scanSettings.ts";
const source=readFileSync(new URL("../src/pages/LocalVideoExperiment.tsx",import.meta.url),"utf8");
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
const base={id:"synthetic",filename:"synthetic.mkv",status:"failed",created_at:"",started_at:null,finished_at:null,
 error:"Synthetic decoder error",media:{duration_seconds:23.2,fps:15,frame_count:348,expected_samples:null,max_samples:58},
 model:{name:"buffalo_l",provider:"Fake",threshold:.45,detection_threshold:.5,detection_size:[320,320],enrolled_identities:114},
 sampled_frames:44,decoded_frames:301,unknown_detections:19,frames_with_unknown:19,matched_face_detections:0,
 detected_face_detections:19,unique_matched_people:0,processing_seconds:9.4,progress_percent:86.49,progress_is_estimate:true,
 verified_total_frames:null,sampling_hz:null,sampling_method:"configured-camera-v3",actual_video_detection_hz:2.1,
 processing_throughput_fps:4.68,max_scan_hz:2.5,decoded_frames_not_inferred:257,scan_settings:{...settings.DEFAULT_SCAN_SETTINGS},
 partial:true,can_start:false,can_cancel:false,can_delete:true,people:[],samples:[]};
async function renderSelected(state){
 let cursor=0,dirty=true,tree;const hooks=[],effects=[];
 globalThis.window={setTimeout:()=>1,clearTimeout:()=>{}};
 const react={useRef:initial=>{const i=cursor++;hooks[i]??={current:initial};return hooks[i];},
 useState:initial=>{const i=cursor++;hooks[i]??={value:initial};return [hooks[i].value,value=>{hooks[i].value=value;dirty=true;}];},
 useCallback:fn=>fn,
 useEffect:(fn,deps)=>{const i=cursor++;const prior=hooks[i];if(!prior||deps.some((value,j)=>value!==prior.deps[j]))effects.push(fn);hooks[i]={deps};}};
 const jsx=(type,props)=>({type,props});const exports={};
 new Function("require","exports",compiled)(name=>{
  if(name==="react")return react;
  if(name==="react-router-dom")return {useSearchParams:()=>[new URLSearchParams(),()=>{}]};
  if(name==="react/jsx-runtime")return {jsx,jsxs:jsx,Fragment:"Fragment"};
  if(name==="../components/ui")return {Button:"Button",Card:"Card",PageHeader:"PageHeader"};
  if(name==="../components/VideoMatchPreview")return {__esModule:true,default:"VideoMatchPreview"};
  if(name==="../components/ScanSettingsFields")return {__esModule:true,default:"Fields"};
  if(name==="../components/DriveVideoBatchPanel")return {__esModule:true,default:"DriveVideoBatchPanel"};
  if(name==="../components/VideoBatchSources")return {__esModule:true,default:"VideoBatchSources"};
  if(name==="../api/scanSettings")return settings;
  if(name==="../api/client")return {apiGet:async path=>path.endsWith(state.id)?state:{experiments:[state]},ApiError:class extends Error{}};
  throw Error(name);
 },exports);
 const nodes=(node,out=[])=>{if(Array.isArray(node))node.forEach(n=>nodes(n,out));else if(node?.props){out.push(node);if(node.props.children!==undefined)nodes(node.props.children,out);}return out;};
 const text=node=>Array.isArray(node)?node.map(text).join(" "):node?.props?text(node.props.children):typeof node==="string"||typeof node==="number"?String(node):"";
 const render=()=>{cursor=0;dirty=false;tree=exports.default();while(effects.length)effects.shift()();};
 async function flush(){for(let i=0;i<8;i++){if(dirty)render();await new Promise(setImmediate);}}
 await flush();const select=nodes(tree).find(n=>n.type==="button"&&text(n).includes(state.filename));assert.ok(select);
 await select.props.onClick();await flush();return {text:text(tree).replace(/\s+/g," "),nodes:nodes(tree)};
}
test("failed partial matches remain visible with CSV control",async()=>{
 const state={...base,matched_face_detections:3,detected_face_detections:22,unique_matched_people:1,
  people:[{identity_key:"a",name:"Synthetic Person",detection_count:3,first_timestamp_seconds:0,last_timestamp_seconds:2}]};
 const result=await renderSelected(state);
 assert.match(result.text,/Partial results only/);assert.match(result.text,/Synthetic Person/);
 assert.match(result.text,/Download CSV/);assert.match(result.text,/View match/);assert.match(result.text,/Preview unavailable for this older run/);assert.match(result.text,/301 decoded frames/);
 assert.match(result.text,/estimate against reported metadata/);
});
test("failed unknown faces explained separately from zero faces",async()=>{
 const unknown=await renderSelected({...base});assert.match(unknown.text,/Faces were detected, but no enrolled identity matched/);
 assert.match(unknown.text,/Enrolled identities frozen at start\s+114/);
 const zero=await renderSelected({...base,unknown_detections:0,detected_face_detections:0});
 assert.match(zero.text,/No faces detected in sampled frames/);
});
test("clean EOF shows actual decoded total and metadata estimate separately",async()=>{
 const result=await renderSelected({...base,status:"completed",error:null,partial:false,progress_percent:100,
  progress_is_estimate:false,verified_total_frames:301,decode_audit:{outcome:"normal_eof",verified_frame_count:301},
  decoded_timestamp_first_seconds:0,decoded_timestamp_last_seconds:23.133,actual_max_samples_zero_work:51});
 assert.match(result.text,/301 decoded frames\s+\/ 301 verified total/);
 assert.match(result.text,/Reported frame count \(estimate\)\s+348/);
 assert.match(result.text,/Clean EOF confirmed/);assert.match(result.text,/23.133 s/);
 assert.doesNotMatch(result.text,/Partial results only/);
});
test("Drive batch displays combined names, source breakdown, partial errors and CSV without local FPS fiction", async()=>{
 const result=await renderSelected({...base,kind:"drive_batch",filename:"Seven cameras",media:null,status:"completed_with_errors",
  total_videos:7,finished_videos:7,completed_videos:6,failed_videos:1,temporary_downloads_cleaned:true,
  people:[{identity_key:"a",name:"Synthetic Batch Person",detection_count:18,first_timestamp_seconds:0,last_timestamp_seconds:.8}],videos:[]});
 assert.match(result.text,/Combined matched people/);assert.match(result.text,/Synthetic Batch Person/);
 assert.match(result.text,/7\s*\/\s*7 videos stopped/);assert.match(result.text,/6 completed,\s*1 failed/);
 assert.match(result.text,/Temporary downloads removed/);assert.match(result.text,/Do not sum person and video_person rows together/);
 assert.match(result.text,/Download CSV/);assert.ok(result.nodes.some(node=>node.type==="VideoBatchSources"));
 assert.doesNotMatch(result.text,/Source FPS\s*—/);
});

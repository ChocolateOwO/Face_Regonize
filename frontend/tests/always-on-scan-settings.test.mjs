// Component behavior under fake media/API/hooks. No browser, real camera or DB.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";
import * as timing from "../src/api/scanSettings.ts";
import { videoConstraints } from "../src/api/cameras.ts";
const source=readFileSync(new URL("../src/pages/Recognition.tsx",import.meta.url),"utf8");
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
function setup({admin=true,mode="always",actualFps=15}={}) {
  let cursor=0,dirty=true,tree,clock=0;
  const hooks=[],effects=[],requests=[],waiters=[],cameraCalls=[],validations=[],posts=[],tracks=[];
  const fakeVideo={videoWidth:64,videoHeight:64,srcObject:null};
  const storage=new Map();
  globalThis.localStorage={getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)};
  globalThis.window={location:{search:"?mode="+mode},setTimeout:()=>1,clearTimeout:()=>{}};
  globalThis.document={fullscreenElement:null,addEventListener:()=>{},removeEventListener:()=>{},
    createElement:()=>({width:0,height:0,getContext:()=>({drawImage:()=>{}}),toBlob:callback=>callback(new Blob(["synthetic"]))})};
  Object.defineProperty(globalThis,"navigator",{configurable:true,value:{mediaDevices:{getUserMedia:async constraints=>{
    cameraCalls.push(constraints);
    const track={stop:()=>tracks.push("stopped"),getSettings:()=>({frameRate:actualFps})};
    return {getTracks:()=>[track],getVideoTracks:()=>[track]};
  }}}});
  const react={
    useRef:initial=>{const index=cursor++;hooks[index]??={current:initial};return hooks[index];},
    useState:initial=>{const index=cursor++;hooks[index]??={value:typeof initial==="function"?initial():initial};
      return [hooks[index].value,value=>{hooks[index].value=typeof value==="function"?value(hooks[index].value):value;dirty=true;}];},
    useEffect:(fn,deps)=>{const index=cursor++;const previous=hooks[index];
      if(!previous||!deps||deps.some((value,i)=>value!==previous.deps?.[i])){
        effects.push(()=>{previous?.cleanup?.();hooks[index].cleanup=fn();});
      }
      hooks[index]={...previous,deps};
    },
  };
  const jsx=(type,props)=>{
    if(type==="video")props.ref.current=fakeVideo;
    return {type,props};
  };
  class ApiError extends Error {}
  const exports={};
  new Function("require","exports",compiled)(name=>{
    if(name==="react")return react;
    if(name==="react/jsx-runtime")return {jsx,jsxs:jsx,Fragment:"Fragment"};
    if(name==="react-router-dom")return {Link:"Link",useSearchParams:()=>[new URLSearchParams("?mode="+mode+"&camera=chosen")]};
    if(name==="../components/ScanSettingsFields")return {__esModule:true,default:"ScanSettingsFields"};
    if(name==="../api/scanSettings")return {...timing,runSerialScans:options=>timing.runSerialScans({...options,now:()=>clock,
      wait:seconds=>new Promise(resolve=>waiters.push({seconds,resolve}))})};
    if(name==="../api/cameras")return {listCameras:async()=>[{deviceId:"chosen",label:"Synthetic camera"}],
      getKioskOverride:()=>null,setKioskOverride:()=>{},resolveCameraDeviceId:()=>null,videoConstraints};
    if(name==="../api/client")return {ApiError,
      apiGet:async path=>{
        if(path==="/api/scan-settings")return {defaults:{...timing.DEFAULT_SCAN_SETTINGS},can_configure:admin};
        if(path==="/api/settings")return {kiosk_mode:mode,debug_mode:"false"};
        if(path==="/api/activities")return {activities:[],current_activity_id:null};
        throw Error("Unexpected GET "+path);
      },
      apiPostJson:async(path,body)=>{
        posts.push({path,body});
        if(path==="/api/scan-settings/validate"){assert.equal(admin,true);validations.push({...body});return {...body};}
        return {};
      },
      apiPostForm:(path,body)=>new Promise(resolve=>requests.push({path,body,resolve})),
    };
    throw Error(name);
  },exports);
  function render(){cursor=0;dirty=false;tree=exports.default();while(effects.length)effects.shift()();}
  async function flush(){for(let i=0;i<8;i++){if(dirty)render();await new Promise(setImmediate);}}
  function elements(node=tree,result=[]){
    if(Array.isArray(node)){for(const child of node)elements(child,result);return result;}
    if(node&&typeof node==="object"&&node.props){result.push(node);if(node.props.children!==undefined)elements(node.props.children,result);}
    return result;
  }
  function text(node=tree){
    if(Array.isArray(node))return node.map(text).join(" ");
    if(node&&typeof node==="object"&&node.props)return node.props.children===undefined?"":text(node.props.children);
    return typeof node==="string"||typeof node==="number"?String(node):"";
  }
  function button(label){return elements().find(node=>node.type==="button"&&text(node).includes(label));}
  function fields(){return elements().find(node=>node.type==="ScanSettingsFields");}
  async function nextScan(){
    const count=requests.length;
    for(let i=0;i<12&&requests.length===count;i++){
      const waiter=waiters.shift();assert.ok(waiter);clock+=waiter.seconds;waiter.resolve();await flush();
    }
    assert.equal(requests.length,count+1);
  }
  async function cleanup(){
    for(const item of hooks)item?.cleanup?.();
    for(const request of requests)request.resolve({results:[],processing_duration_ms:0});
    while(waiters.length)waiters.shift().resolve();
    await flush();
  }
  render();
  return {flush,button,fields,text,requests,cameraCalls,validations,posts,nextScan,cleanup,tracks};
}
const blank={results:[],processing_duration_ms:0};
test("admin chooses before Start, selected constraints and negotiated actual FPS displayed",async()=>{
  const app=setup();await app.flush();
  try{
    assert.equal(app.cameraCalls.length,0);assert.ok(app.button("Start Always On"));
    app.fields().props.onChange({camera_fps:12,post_scan_delay_seconds:0.8,target_detections_per_second:1});
    await app.flush();app.button("Start Always On").props.onClick();await app.flush();
    assert.deepEqual(app.validations,[{camera_fps:12,post_scan_delay_seconds:0.8,target_detections_per_second:1}]);
    assert.deepEqual(app.cameraCalls[0].video.frameRate,{ideal:12,max:12});
    assert.match(app.text(),/negotiated actual:\s+15.000/);
    assert.match(app.text(),/Wait after completed scan:\s+0.8/);
    assert.match(app.text(),/Target detections\/s:\s+1/);
    assert.equal(app.requests.length,1);
  }finally{await app.cleanup();}
});
test("Stop drains inference before restart and freezes each new run separately",async()=>{
  const app=setup();await app.flush();
  try{
    app.button("Start Always On").props.onClick();await app.flush();
    app.requests[0].resolve(blank);await app.flush();await app.nextScan();
    app.button("Stop to change settings").props.onClick();await app.flush();
    assert.equal(app.fields().props.disabled,true);
    app.button("Waiting for completed scan").props.onClick();await app.flush();
    assert.equal(app.cameraCalls.length,1);assert.equal(app.requests.length,2);
    app.requests[1].resolve(blank);await app.flush();
    assert.equal(app.button("Start Always On").props.disabled,false);
    app.fields().props.onChange({camera_fps:8,post_scan_delay_seconds:1,target_detections_per_second:0.5});
    await app.flush();app.button("Start Always On").props.onClick();await app.flush();
    assert.equal(app.cameraCalls.length,2);assert.equal(app.validations[1].camera_fps,8);
  }finally{await app.cleanup();}
});
test("non-admin default auto-start has no editable controls or validation mutation",async()=>{
  const app=setup({admin:false,actualFps:null});await app.flush();
  try{
    assert.equal(app.cameraCalls.length,1);assert.equal(app.fields(),undefined);
    assert.equal(app.validations.length,0);
    assert.deepEqual(app.cameraCalls[0].video.frameRate,{ideal:30,max:30});
    assert.match(app.text(),/Camera FPS requested:\s+30/);
    assert.match(app.text(),/negotiated actual:\s+unavailable/);
  }finally{await app.cleanup();}
});
test("Tap mode preserves consent, camera constraints and existing matched check-in result",async()=>{
  const app=setup({mode:"tap"});await app.flush();
  try{
    app.button("TAP TO SCAN").props.onClick();await app.flush();
    app.button("DO NOT CONSENT").props.onClick();await app.flush();
    assert.equal(app.cameraCalls.length,1);assert.equal(app.validations.length,0);
    assert.deepEqual(app.cameraCalls[0].video,{deviceId:{exact:"chosen"}});
    app.requests[0].resolve({processing_duration_ms:0,results:[{status:"matched",person_id:"fake-person",first_name:"Synthetic",checkin_status:"already_checked_in"}]});
    await app.flush();
    assert.match(app.text(),/YOU ALREADY CHECKED IN/);assert.match(app.text(),/SYNTHETIC/);
    assert.equal(app.posts.at(-1).path,"/api/pdpa/record");
    assert.equal(app.posts.at(-1).body.choice,"declined");
    assert.equal(app.tracks.length,1);
  }finally{await app.cleanup();}
});

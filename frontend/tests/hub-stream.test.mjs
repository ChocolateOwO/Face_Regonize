// Mice Hub cameras in the CCTV station (src/hubStream.ts): MJPEG parsing,
// first frame, drop + reconnect with backoff, busy Hub (Retry-After),
// refused sign-in, stop, two cameras at once, and the camera list.
// No browser, no network: fetch, canvas and image decoding are doubles.
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
import ts from "typescript";

function load(rel){
 const src=readFileSync(new URL(rel,import.meta.url),"utf8");
 const out=ts.transpileModule(src,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 const exports={};new Function("require","exports",out)(n=>{throw Error(n);},exports);return exports;
}
const hub=load("../src/hubStream.ts");
const enc=new TextEncoder();
const jpeg=(n)=>Uint8Array.from([0xff,0xd8,n,n,n,0xff,0xd9]);
const part=(f,len=true)=>{const head=enc.encode(`\r\n--mice\r\nContent-Type: image/jpeg\r\n${len?`Content-Length: ${f.length}\r\n`:""}\r\n`);const o=new Uint8Array(head.length+f.length);o.set(head);o.set(f,head.length);return o;};
const cat=(...a)=>{const o=new Uint8Array(a.reduce((n,x)=>n+x.length,0));let k=0;for(const x of a){o.set(x,k);k+=x.length;}return o;};
const F=[1,2,3,4].map(jpeg);

test("parser: byte by byte with Content-Length",()=>{
 const p=new hub.MjpegParser();const data=cat(...F.map(f=>part(f)));const out=[];
 for(let i=0;i<data.length;i++)out.push(...p.push(data.slice(i,i+1)));
 assert.deepEqual(out.map(f=>[...f]),F.map(f=>[...f]));
});
test("parser: JPEG markers without lengths",()=>{
 const p=new hub.MjpegParser();
 assert.deepEqual(p.push(cat(...F.map(f=>part(f,false)))).map(f=>[...f]),F.map(f=>[...f]));
});
test("parser: invalid and oversize parts are dropped; the buffer stays bounded",()=>{
 const p=new hub.MjpegParser(16,256);
 const out=p.push(cat(part(enc.encode("not a jpeg")),part(Uint8Array.from([0xff,0xd8,...new Array(40).fill(1),0xff,0xd9])),part(F[0])));
 assert.deepEqual(out.map(f=>[...f]),[[...F[0]]]);assert.ok(p.invalid>=2);
 p.push(new Uint8Array(4000));assert.ok(p.buf.length<=256);
});

function body(chunks,{endAfter=true}={}){
 return new ReadableStream({start(c){for(const ch of chunks)c.enqueue(ch);if(endAfter)c.close();}});
}
function env(responses){
 const calls=[],sleeps=[],states=[];let drawn=0,t=0;
 const tracks=[{stopped:false,stop(){this.stopped=true;}}];
 const deps={
  fetchImpl:async(url,init)=>{calls.push({url,auth:init.headers.Authorization,signal:init.signal});
   const r=responses.shift();if(!r)return new Promise((_,rej)=>init.signal.addEventListener("abort",()=>rej(new Error("aborted"))));
   if(r instanceof Error)throw r;return r;},
  token:()=>"tok",
  decode:async(f)=>({width:320,height:240,frame:[...f]}),
  makeCanvas:()=>({width:0,height:0,getContext:()=>({drawImage:()=>{drawn++;}}),captureStream:()=>({getTracks:()=>tracks})}),
  now:()=>t,
  sleep:async(ms)=>{sleeps.push(ms);},
  onState:(s,d)=>states.push(s),
 };
 return {deps,calls,sleeps,states,tracks,get drawn(){return drawn;},tick:(ms)=>{t+=ms;}};
}
const ok=(chunks,opts)=>new Response(body(chunks,opts),{status:200,headers:{"Content-Type":"multipart/x-mixed-replace; boundary=reconizehub"}});
const wait=()=>new Promise(r=>setTimeout(r,20));

test("valid stream: resolves on the first frame, draws it, is fresh, authenticated same-origin URL",async()=>{
 const e=env([ok([part(F[0]),part(F[1])],{endAfter:false})]);
 const h=await hub.openHubCamera("pc:Desk Cam",e.deps);
 await wait();
 assert.ok(e.drawn>=1);assert.equal(h.fresh(),true);
 assert.equal(e.calls[0].url,"/api/hub/cam.stream?dev=pc%3ADesk%20Cam");assert.equal(e.calls[0].auth,"Bearer tok");
 e.tick(5000);assert.equal(h.fresh(),false,"no frame for 5 s = not fresh: the scan loop skips the frozen picture");
 h.stop();
});

test("dropped connection reconnects with backoff and resumes",async()=>{
 const e=env([ok([part(F[0])]),new Error("network"),ok([part(F[2])],{endAfter:false})]);
 const h=await hub.openHubCamera("pc:Cam",e.deps);
 await wait();
 assert.equal(e.calls.length,3);
 assert.deepEqual(e.sleeps,[1000,2000],"stream ended after a frame: 1 s; then a failure doubles it");
 assert.ok(e.states.includes("reconnecting"));assert.equal(e.states.at(-1),"live");
 h.stop();
});

test("busy Hub: Retry-After is honoured; repeated failures back off up to 15 s",async()=>{
 const busy=()=>new Response("{}",{status:503,headers:{"Retry-After":"4"}});
 const down=()=>new Error("refused");
 const e=env([ok([part(F[0])]),busy(),down(),down(),down(),down(),down(),ok([part(F[1])],{endAfter:false})]);
 const h=await hub.openHubCamera("pc:Cam",e.deps);
 await wait();
 assert.deepEqual(e.sleeps,[1000,4000,4000,8000,15000,15000,15000],"Retry-After 4 s, then doubling, capped at 15 s");
 h.stop();
});

test("refused sign-in stops at once: no retry loop",async()=>{
 const e=env([new Response("",{status:403})]);
 await assert.rejects(hub.openHubCamera("pc:Cam",e.deps),/Not allowed/);
 await wait();
 assert.equal(e.calls.length,1);assert.deepEqual(e.sleeps,[]);
});

test("stopping the camera's track closes the Hub connection and ends reconnects",async()=>{
 const e=env([ok([part(F[0])],{endAfter:false})]);
 const h=await hub.openHubCamera("pc:Cam",e.deps);
 h.stream.getTracks()[0].stop();
 assert.equal(e.calls[0].signal.aborted,true);assert.equal(e.tracks[0].stopped,true);
 await wait();
 assert.equal(e.calls.length,1);assert.equal(e.states.at(-1),"stopped");
});

test("two Hub cameras are independent: stopping one leaves the other live",async()=>{
 const a=env([ok([part(F[0])],{endAfter:false})]);const b=env([ok([part(F[1])],{endAfter:false})]);
 const ha=await hub.openHubCamera("pc:A",a.deps);const hb=await hub.openHubCamera("cam:7",b.deps);
 ha.stop();
 assert.equal(a.calls[0].signal.aborted,true);assert.equal(b.calls[0].signal.aborted,false);assert.equal(hb.fresh(),true);
 hb.stop();
});

test("camera list: Hub cameras become CCTV devices; no Hub = none",async()=>{
 const list=await hub.listHubCameras(async()=>({enabled:true,cameras:[{dev:"pc:Desk Cam",name:"Desk Cam"}]}));
 assert.deepEqual(list,[{deviceId:"hub:pc:Desk Cam",label:"Hub · Desk Cam"}]);
 assert.equal(hub.isHubDevice(list[0].deviceId),true);assert.equal(hub.hubDevOf(list[0].deviceId),"pc:Desk Cam");
 assert.deepEqual(await hub.listHubCameras(async()=>({enabled:false,cameras:[]})),[]);
 assert.deepEqual(await hub.listHubCameras(async()=>{throw new Error("401");}),[]);
 assert.equal(hub.isHubDevice("a1b2c3-local-device"),false);
});

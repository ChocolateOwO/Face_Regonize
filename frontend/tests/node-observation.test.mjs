// CCTV page -> Central live presence: built from the Local Agent's scan,
// compatible with older agents, and never able to hold up the scan loop.
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
import ts from "typescript";

const read=path=>readFileSync(new URL(path,import.meta.url),"utf8");
const compiled=ts.transpileModule(read("../src/nodeObservation.ts"),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText;
// A fresh copy of the module = a newly loaded page; `storage` is that tab's sessionStorage.
function load(storage){
 const exports={};
 new Function("exports","sessionStorage",compiled)(exports,storage);
 return exports;
}
const tab=()=>{const data=new Map();return {getItem:k=>data.has(k)?data.get(k):null,setItem:(k,v)=>{data.set(k,String(v));}};};
const {observationBody,reportObservation,reporterId}=load(tab());

const scan={faces_total:2,matches:[{participant_id:"0001",name:"Ann Alpha",confidence:0.71}],image:{w:1280,h:720},
 faces:[{bbox:[10,20,110,160],participant_id:"0001",confidence:0.71},{bbox:[600,80,660,150],participant_id:null,confidence:null}]};

test("the body carries the camera's node id, the frame size, every face and this tab's reporter id",()=>{
 assert.deepEqual(observationBody("STATION-A-cam2",scan),{node_id:"STATION-A-cam2",image_w:1280,image_h:720,faces:scan.faces,reporter_id:reporterId()});
 assert.deepEqual(observationBody("CAM-01",{...scan,faces_total:0,matches:[],faces:[]}).faces,[]);
 assert.match(reporterId(),/^[0-9a-z-]{10,64}$/);
});

test("one tab keeps its reporter id across a refresh; another tab or station gets a different one",()=>{
 const first=tab();
 const page=load(first);
 const id=page.reporterId();
 assert.equal(page.reporterId(),id);
 assert.equal(load(first).reporterId(),id);            // refresh: same sessionStorage, new page
 assert.notEqual(load(tab()).reporterId(),id);         // a second tab / another PC
 const blocked={getItem(){throw new Error("denied");},setItem(){throw new Error("denied");}};
 const noStorage=load(blocked);
 assert.match(noStorage.reporterId(),/^[0-9a-z-]{10,64}$/);   // storage refused: still reports, id kept in memory
 assert.equal(noStorage.reporterId(),noStorage.reporterId());
});

test("an older agent (no image/faces) and a missing node id report nothing",()=>{
 const calls=[];const post=async(...a)=>{calls.push(a);};
 const old={faces_total:1,matches:scan.matches};
 assert.equal(observationBody("CAM-01",old),null);
 assert.equal(observationBody("",scan),null);
 assert.equal(observationBody("CAM-01",{...scan,image:{w:0,h:720}}),null);
 reportObservation(post,"CAM-01",old);reportObservation(post,"",scan);
 assert.deepEqual(calls,[]);
});

test("reporting returns at once and swallows every failure, including a refusal from Central",async()=>{
 const calls=[];let release;
 const slow=(path,body)=>{calls.push([path,body]);return new Promise(resolve=>{release=resolve;});};
 assert.equal(reportObservation(slow,"CAM-01",scan),undefined);   // did not wait for Central
 assert.deepEqual(calls,[["/api/node/observation",observationBody("CAM-01",scan)]]);
 release();
 const unhandled=[];const onUnhandled=reason=>unhandled.push(reason);
 process.on("unhandledRejection",onUnhandled);
 reportObservation(()=>Promise.reject(new Error("Central is down")),"CAM-01",scan);
 reportObservation(()=>Promise.reject(Object.assign(new Error("Another station is already reporting for this node."),{status:409})),"CAM-01",scan);
 reportObservation(()=>{throw new Error("sync failure");},"CAM-01",scan);
 await new Promise(resolve=>setTimeout(resolve,20));
 process.off("unhandledRejection",onUnhandled);
 assert.deepEqual(unhandled,[]);
});

test("the CCTV scan loop reports presence without awaiting it and leaves attendance as it was",()=>{
 const page=read("../src/pages/CameraNode.tsx");
 assert.match(page,/import \{ reportObservation, type AgentScan \} from "\.\.\/nodeObservation";/);
 const scanOnce=page.slice(page.indexOf("async function scanOnce(cameraId: string, internal: CameraInstanceInternal)"),page.indexOf("// ---- Face count + stale-timeout"));
 assert.equal((scanOnce.match(/reportObservation\(/g)||[]).length,1);
 assert.doesNotMatch(scanOnce,/await\s+reportObservation/);
 assert.match(scanOnce,/reportObservation\(apiPostJson, internal\.cameraId, data\);/);
 // order: face count, presence, then the unchanged attendance report
 const at=name=>scanOnce.indexOf(name);
 assert.ok(at("updateFaceCount(cameraId, internal, data.faces_total)")<at("reportObservation(")&&at("reportObservation(")<at("recordRecognition(cameraId, recognized)"));
 assert.match(scanOnce,/node_id: internal\.cameraId, camera_label: internal\.label,\s+activity_id: selectedActivityIdRef\.current \|\| null, matches: recognized,/);
 assert.match(scanOnce,/await apiPostForm\("\/api\/node\/event", form\);/);
});

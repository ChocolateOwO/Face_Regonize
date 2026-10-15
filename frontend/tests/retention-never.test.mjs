// Event Photo retention "Never / ไม่ลบอัตโนมัติ": the shared choice list and
// the Settings card (real Settings page render, fake hooks and API).
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
import ts from "typescript";

function compile(rel){
 return ts.transpileModule(readFileSync(new URL(rel,import.meta.url),"utf8"),
  {compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
}
function load(rel,deps){const exports={};new Function("require","exports",compile(rel))(n=>{if(n in deps)return deps[n];throw Error(n);},exports);return exports;}
const retention=load("../src/retention.ts",{});

test("choices are 1-7 days plus an explicit Never, in Thai and English",()=>{
 assert.deepEqual(retention.RETENTION_CHOICES.map(c=>c.value),["1","2","3","4","5","6","7","never"]);
 assert.equal(retention.RETENTION_CHOICES.at(-1).label,"Never / ไม่ลบอัตโนมัติ");
 assert.equal(retention.RETENTION_CHOICES[0].label,"1 Day");assert.equal(retention.RETENTION_CHOICES[6].label,"7 Days");
 assert.equal(retention.isRetentionChoice("never"),true);assert.equal(retention.isRetentionChoice("8"),false);assert.equal(retention.isRetentionChoice(7),false);
});

test("Never is sent as a policy, never as a number of days",()=>{
 assert.deepEqual(retention.retentionPayload("never"),{retention_policy:"never"});
 assert.deepEqual(retention.retentionPayload("3"),{retention_policy:"days",retention_days:3});
});

test("a saved batch with no deadline reads as Never",()=>{
 assert.equal(retention.retentionChoiceOf({retention_policy:"never",retention_days:0,delete_at:null}),"never");
 assert.equal(retention.retentionChoiceOf({retention_policy:"days",retention_days:5,delete_at:"2026-10-20T00:00:00"}),"5");
 assert.equal(retention.retentionChoiceOf({retention_days:7,delete_at:null}),"never","no deadline means kept, even from an older API");
 assert.equal(retention.retentionLabel("never"),"Never / ไม่ลบอัตโนมัติ");
});

async function settingsPage(stored){
 let cursor=0,dirty=true,tree;const hooks=[],effects=[],puts=[];
 const react={useState:i=>{const k=cursor++;hooks[k]??={v:typeof i==="function"?i():i};return [hooks[k].v,v=>{hooks[k].v=typeof v==="function"?v(hooks[k].v):v;dirty=true;}];},
  useEffect:(fn,deps)=>{const k=cursor++;if(!hooks[k]){effects.push(fn);hooks[k]={deps};}}};
 const jsx=(type,props)=>({type,props});
 const mod=load("../src/pages/Settings.tsx",{"react":react,"react/jsx-runtime":{jsx,jsxs:jsx,Fragment:"Fragment"},
  "../api/client":{apiGet:async p=>p==="/api/settings"?{...stored}:{attendance_count:0,recognition_history_count:0,upload_count:0},
   apiPostJson:async()=>({}),apiPutJson:async(path,body)=>{puts.push({path,body});return {};}},
  "../api/cameras":{listCamerasWithPermission:async()=>[]},
  "../components/ui":{Button:"Button",Card:"Card",Input:"Input",PageHeader:"PageHeader"},
  "../components/UpdatePanel":{default:"UpdatePanel"},"../retention":retention});
 const nodes=(n,out=[])=>{if(Array.isArray(n))n.forEach(x=>nodes(x,out));else if(n?.props){out.push(n);nodes(n.props.children,out);}return out;};
 const text=n=>Array.isArray(n)?n.map(text).join(" "):n?.props?text(n.props.children):typeof n==="string"?n:"";
 const render=()=>{cursor=0;dirty=false;tree=mod.default();while(effects.length)effects.shift()();};
 async function flush(){for(let i=0;i<6;i++){if(dirty)render();await new Promise(setImmediate);}}
 await flush();
 const select=()=>nodes(tree).find(n=>n.type==="select"&&n.props["aria-label"]==="Automatic deletion for new photo batches");
 const save=()=>nodes(tree).find(n=>n.type==="Button"&&text(n).includes("Save Settings"));
 return {flush,puts,select,save,text:()=>text(tree).replace(/\s+/g," ")};
}

test("Settings shows Never beside the finite choices and reflects the saved value",async()=>{
 const page=await settingsPage({face_match_threshold:"0.45",face_detection_confidence:"0.5",event_name:"E",event_date:"",event_location:"",
  app_name:"Reconize",storage_path:"x",debug_mode:"false",camera_label:"",kiosk_mode:"tap",timezone:"UTC",photo_retention_default:"never"});
 const select=page.select();
 assert.equal(select.props.value,"never");
 const options=select.props.children.map(o=>[o.props.value,[].concat(o.props.children).join("")]);
 assert.deepEqual(options.map(o=>o[0]),["1","2","3","4","5","6","7","never"]);
 assert.equal(options.at(-1)[1],"Never / ไม่ลบอัตโนมัติ");
 assert.match(page.text(),/kept until deleted manually/);
 assert.match(page.text(),/Existing batches keep their own retention/);
});

test("Settings saves the chosen retention default; an older backend falls back to 7 days",async()=>{
 const page=await settingsPage({face_match_threshold:"0.45",face_detection_confidence:"0.5",event_name:"E",event_date:"",event_location:"",
  app_name:"Reconize",storage_path:"x",debug_mode:"false",camera_label:"",kiosk_mode:"tap",timezone:"UTC"});
 assert.equal(page.select().props.value,"7");
 page.select().props.onChange({target:{value:"never"}});await page.flush();
 assert.equal(page.select().props.value,"never");
 await page.save().props.onClick();await page.flush();
 assert.equal(page.puts.length,1);assert.equal(page.puts[0].path,"/api/settings");
 assert.equal(page.puts[0].body.values.photo_retention_default,"never");
 assert.equal("storage_path" in page.puts[0].body.values,false);
});

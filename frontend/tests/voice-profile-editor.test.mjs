// VoiceProfileEditor + LanguageComboBox: the real components rendered together
// with fake React hooks and a fake API that applies the backend's rules
// (person_address.normalize_language / normalize_title). "Reload" = mount the
// editor again with what the fake backend stored.
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
import ts from "typescript";

const compile=file=>ts.transpileModule(readFileSync(new URL(`../src/components/${file}`,import.meta.url),"utf8"),
 {compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
const EDITOR=compile("VoiceProfileEditor.tsx"),COMBO=compile("LanguageComboBox.tsx");
const LANGUAGE_ERROR="Language must be a language tag such as th, en or en-US.";

// Backend rules, mirrored for the fake server (tests/test_inference_lanes.py covers the real ones).
function backendLanguage(v){
 if(v==null||!v.trim())return null;
 const tag=v.trim().replace(/_/g,"-");
 if(tag.length>35||!/^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8}){0,3}$/.test(tag))throw new Error(LANGUAGE_ERROR);
 const [first,...rest]=tag.split("-");
 return [first.toLowerCase(),...rest.map(p=>p.length===2&&/^[A-Za-z]+$/.test(p)?p.toUpperCase():p.length===4&&/^[A-Za-z]+$/.test(p)?p[0].toUpperCase()+p.slice(1).toLowerCase():p.toLowerCase())].join("-");
}
function backendTitle(v){const t=(v??"").split(/\s+/).filter(Boolean).join(" ");return t||null;}

function makeServer(person={call_as:"พี่แอน",title:"ดร.",language:null},fail=null){
 const puts=[];
 return {person,puts,api:{apiPutJson:async(path,body)=>{
  puts.push({path,body});
  if(fail)throw new Error(fail);
  const next={...person};
  if("title" in body)next.title=backendTitle(body.title);
  if("language" in body)next.language=backendLanguage(body.language);   // throws -> nothing stored
  Object.assign(person,next);
  return {...person};
 }}};
}

async function mount(server){
 let cursor=0,dirty=true,tree,ids=0;const hooks=[],effects=[],saved=[];
 const react={
  useState:initial=>{const i=cursor++;hooks[i]??={value:initial};return [hooks[i].value,v=>{hooks[i].value=typeof v==="function"?v(hooks[i].value):v;dirty=true;}];},
  useEffect:(fn,deps)=>{const i=cursor++;const prior=hooks[i];if(!prior||deps.some((d,j)=>d!==prior.deps[j]))effects.push(fn);hooks[i]={deps};},
  useRef:initial=>{const i=cursor++;hooks[i]??={current:initial};return hooks[i];},
  useId:()=>{const i=cursor++;hooks[i]??={id:`:r${ids++}:`};return hooks[i].id;},
 };
 // Function components render inline so the whole tree (editor + combobox) is real.
 const jsx=(type,props)=>typeof type==="function"?type(props):({type,props});
 const load=(code,extra)=>{const exports={};new Function("require","exports",code)(name=>{
  if(name==="react")return react;
  if(name==="react/jsx-runtime")return {jsx,jsxs:jsx,Fragment:"Fragment"};
  if(name in extra)return extra[name];
  throw Error(name);},exports);return exports;};
 const combo=load(COMBO,{});
 const editor=load(EDITOR,{"../api/client":server.api,"./ui":{Button:"Button",Input:"Input"},"./LanguageComboBox":combo});
 const props={personId:"p1",title:server.person.title,language:server.person.language,onSaved:(t,l)=>saved.push([t,l])};
 const nodes=(n,out=[])=>{if(Array.isArray(n))n.forEach(x=>nodes(x,out));else if(n?.props){out.push(n);nodes(n.props.children,out);}return out;};
 const text=n=>Array.isArray(n)?n.map(text).join(" "):n?.props?text(n.props.children):typeof n==="string"?n:"";
 const render=()=>{cursor=0;dirty=false;tree=editor.default(props);while(effects.length)effects.shift()();};
 async function flush(){for(let i=0;i<8;i++){if(dirty)render();await new Promise(setImmediate);}}
 await flush();
 const all=()=>nodes(tree);
 const ev=extra=>({preventDefault(){},...extra});
 const app={
  combo,saved,flush,text:()=>text(tree),all,
  lang:()=>all().find(n=>n.props.role==="combobox"),
  title:()=>all().find(n=>n.type==="Input"&&n.props["aria-label"]==="Title"),
  save:()=>all().find(n=>n.type==="Button"),
  list:()=>all().find(n=>n.props.role==="listbox"),
  options:()=>all().filter(n=>n.props.role==="option"),
  option:label=>app.options().find(n=>text(n).includes(label)),
  async focus(){app.lang().props.onFocus();await flush();},
  async type(value){app.lang().props.onChange({target:{value}});await flush();},
  async key(key){app.lang().props.onKeyDown(ev({key}));await flush();},
  async pick(label,pointerType="mouse"){const o=app.option(label);assert.ok(o,`option ${label}`);
   o.props.onPointerDown(ev({pointerType}));o.props.onClick();await flush();},
  async blur(){app.lang().props.onBlur();await flush();},
  async clickSave(){await app.save().props.onClick();await flush();},
 };
 return app;
}

const OPTION_LABELS=["ไทย","English","中文 (จีน)","日本語 (ญี่ปุ่น)","한국어 (เกาหลี)","Français (ฝรั่งเศส)"];

async function saveAndReload(server,app){
 await app.clickSave();
 return mount(server);
}

test("TEST 1: opening the list shows the 6 languages and 'not set'",async()=>{
 const app=await mount(makeServer());
 assert.equal(app.list(),undefined,"closed until used");
 await app.focus();
 assert.equal(app.lang().props["aria-expanded"],true);
 const labels=app.options().map(o=>o.props.children[0].props.children);
 assert.deepEqual(labels,["ไม่ระบุภาษา",...OPTION_LABELS]);
 assert.deepEqual(app.combo.LANGUAGE_OPTIONS.map(o=>o.code),["th","en","zh","ja","ko","fr"]);
});

for(const [n,label,code] of [[2,"ไทย","th"],[3,"English","en"],[4,"Français (ฝรั่งเศส)","fr"]]){
 test(`TEST ${n}: pick ${label} -> saved as ${code}, shown as its name after reload`,async()=>{
  const server=makeServer();
  const app=await mount(server);
  await app.focus();await app.pick(label);
  assert.equal(app.lang().props.value,label,"shows the name");
  assert.equal(app.list(),undefined,"closes after a pick");
  const again=await saveAndReload(server,app);
  assert.deepEqual(server.puts.at(-1).body.language,code,"sends the code");
  assert.equal(server.person.language,code);
  assert.equal(again.lang().props.value,label);
 });
}

for(const [n,typed] of [[5,"de-DE"],[6,"es-MX"]]){
 test(`TEST ${n}: typed ${typed} (not in the list) is saved and shown after reload`,async()=>{
  const server=makeServer();
  const app=await mount(server);
  await app.focus();await app.type(typed);
  assert.equal(app.lang().props.value,typed);
  assert.ok(app.options().some(o=>o.props.children[0].props.children.includes(typed)),"offers the typed code");
  const again=await saveAndReload(server,app);
  assert.equal(server.person.language,typed);
  assert.equal(again.lang().props.value,typed);
 });
}

test("TEST 7: typing TH saves the normalised code th",async()=>{
 const server=makeServer({call_as:null,title:null,language:"en"});
 const app=await mount(server);
 await app.focus();await app.type("TH");
 const again=await saveAndReload(server,app);
 assert.equal(server.person.language,"th");
 assert.equal(again.lang().props.value,"ไทย");
 // A free code is normalised by the backend the same way.
 const s2=makeServer({call_as:null,title:null,language:null});
 const a2=await mount(s2);await a2.focus();await a2.type("pt_br");
 const r2=await saveAndReload(s2,a2);
 assert.equal(s2.person.language,"pt-BR");assert.equal(r2.lang().props.value,"pt-BR");
});

test("TEST 8: invalid text shows an error under Language and is never sent",async()=>{
 for(const bad of ["thai language","hello world","12345"]){
  const server=makeServer({call_as:null,title:null,language:"fr"});
  const app=await mount(server);
  await app.focus();await app.type(bad);await app.blur();
  await app.clickSave();
  assert.equal(server.puts.length,0,`${bad} not sent`);
  assert.equal(server.person.language,"fr","stored value unchanged");
  const alert=app.all().find(n=>n.props.role==="alert");
  assert.ok(alert,"error shown");assert.match(app.text(),/language tag/);
  assert.equal(alert.props.id,app.lang().props["aria-describedby"],"error is tied to the Language field");
  assert.equal(app.lang().props["aria-invalid"],true);
  assert.deepEqual(app.saved,[]);
  await app.type("de");
  assert.equal(app.all().find(n=>n.props.role==="alert"),undefined,"error clears when the value changes");
 }
});

test("TEST 9: clearing (× or 'not set') saves null",async()=>{
 for(const how of ["x","none"]){
  const server=makeServer({call_as:null,title:null,language:"ja"});
  const app=await mount(server);
  if(how==="x"){const clear=app.all().find(n=>n.props["aria-label"]==="Clear language");clear.props.onClick();await app.flush();}
  else{await app.focus();await app.pick("ไม่ระบุภาษา");}
  assert.equal(app.lang().props.value,"");
  const again=await saveAndReload(server,app);
  assert.equal(server.person.language,null);
  assert.equal(again.lang().props.value,"");
 }
});

test("TEST 10/11: changing Language leaves Call as and Title alone",async()=>{
 const server=makeServer({call_as:"พี่แอน",title:"ดร.",language:null});
 const app=await mount(server);
 await app.focus();await app.pick("English");
 await saveAndReload(server,app);
 assert.equal(server.person.call_as,"พี่แอน");
 assert.equal(server.person.title,"ดร.");
 assert.equal(server.puts.length,1);
 assert.equal(server.puts[0].path,"/api/people/p1/voice-profile");
 assert.equal(server.puts[0].body.title,"ดร.","title sent back unchanged");
 assert.ok(!("call_as" in server.puts[0].body));
});

test("TEST 12: a stored code that is not in the list is shown and editable",async()=>{
 for(const code of ["pt-BR","de-DE","zh-Hant-TW"]){
  const app=await mount(makeServer({call_as:null,title:null,language:code}));
  assert.equal(app.lang().props.value,code);
  assert.equal(app.save().props.disabled,true,"nothing changed yet");
  await app.focus();await app.type(code.slice(0,2));
  assert.equal(app.save().props.disabled,false);
 }
});

test("TEST 13: keyboard — arrows move, Enter picks, Escape closes, Home/End",async()=>{
 const server=makeServer({call_as:null,title:null,language:null});
 const app=await mount(server);
 await app.key("ArrowDown");
 assert.equal(app.lang().props["aria-expanded"],true,"ArrowDown opens");
 assert.equal(app.lang().props["aria-activedescendant"],app.options()[0].props.id);
 await app.key("ArrowDown");await app.key("ArrowDown");          // none -> th -> en
 assert.equal(app.lang().props["aria-activedescendant"],app.options()[2].props.id);
 await app.key("End");
 assert.equal(app.lang().props["aria-activedescendant"],app.options()[6].props.id);
 await app.key("ArrowDown");                                      // wraps
 assert.equal(app.lang().props["aria-activedescendant"],app.options()[0].props.id);
 await app.key("Home");await app.key("ArrowDown");await app.key("ArrowDown");
 await app.key("Enter");
 assert.equal(app.lang().props.value,"English");
 assert.equal(app.list(),undefined,"Enter closes");
 await app.key("ArrowDown");assert.ok(app.list());
 await app.key("Escape");assert.equal(app.list(),undefined,"Escape closes");
 await app.clickSave();
 assert.equal(server.person.language,"en");
});

test("TEST 14: touch — tapping an option selects it without losing focus first",async()=>{
 const app=await mount(makeServer({call_as:null,title:null,language:null}));
 await app.focus();
 let prevented=false;
 const o=app.option("한국어");
 o.props.onPointerDown({pointerType:"touch",preventDefault(){prevented=true;}});
 o.props.onClick();await app.flush();
 assert.ok(prevented,"pointerdown keeps focus in the box (no blur before the tap lands)");
 assert.equal(app.lang().props.value,"한국어 (เกาหลี)");
 const toggle=app.all().find(n=>n.props["aria-label"]==="Show languages");
 toggle.props.onClick();await app.flush();assert.ok(app.list(),"the ▾ button opens the list by tap/click");
});

test("TEST 15: explicit page palette; does not follow an OS dark scheme on its own",()=>{
 // The app has no dark theme: the box and list use the page's own light colours
 // and color-scheme: light, so they never turn dark while the page stays light.
 const src=readFileSync(new URL("../src/components/LanguageComboBox.tsx",import.meta.url),"utf8");
 assert.match(src,/colorScheme: "light"/);
 assert.match(src,/bg-white text-gray-900 border rounded-lg/);
 assert.match(src,/role="listbox"[\s\S]*bg-white text-gray-900/);
 assert.doesNotMatch(src,/<datalist/);
});

test("TEST 16: a failed save shows the error and reports nothing as saved",async()=>{
 const server=makeServer({call_as:null,title:null,language:null},"Network error");
 const app=await mount(server);
 await app.focus();await app.pick("ไทย");
 await app.clickSave();
 assert.match(app.text(),/Network error/);
 assert.deepEqual(app.saved,[]);
 assert.equal(app.save().props.children,"Save","not stuck in Saving...");
 assert.equal(server.person.language,null);
 // A backend language error lands under the Language field.
 const s2=makeServer({call_as:null,title:null,language:null},LANGUAGE_ERROR);
 const a2=await mount(s2);await a2.focus();await a2.pick("ไทย");await a2.clickSave();
 assert.ok(a2.all().find(n=>n.props.role==="alert"));
});

test("search: typing filters by name, code or Thai/English name; exact name resolves",async()=>{
 const app=await mount(makeServer({call_as:null,title:null,language:null}));
 await app.focus();
 await app.type("fra");
 // "fra" is itself a valid ISO 639-2 tag, so it is also offered as typed.
 assert.deepEqual(app.options().map(o=>o.props.children[0].props.children),["Français (ฝรั่งเศส)",'ใช้ "fra" (พิมพ์เอง)']);
 await app.type("frenc");
 assert.deepEqual(app.options().map(o=>o.props.children[0].props.children),["Français (ฝรั่งเศส)"],"not a tag: no typed row");
 await app.type("ญี่");
 assert.deepEqual(app.options().map(o=>o.props.children[0].props.children),["日本語 (ญี่ปุ่น)"]);
 await app.type("ko");
 assert.ok(app.options().some(o=>o.props.children[0].props.children==="한국어 (เกาหลี)"));
 await app.type("ไทย");await app.clickSave();
 const r=app.combo;
 assert.equal(r.resolveLanguage("ไทย"),"th");assert.equal(r.resolveLanguage(" English "),"en");
 assert.equal(r.resolveLanguage("french"),"fr");assert.equal(r.resolveLanguage("de-DE"),"de-DE");
 assert.equal(r.resolveLanguage(""),"");assert.equal(r.displayLanguage("es-MX"),"es-MX");
 assert.equal(r.displayLanguage("FR"),"Français (ฝรั่งเศส)");
});

test("nothing is saved while typing or picking — only on Save",async()=>{
 const server=makeServer({call_as:null,title:null,language:null});
 const app=await mount(server);
 await app.focus();await app.type("d");await app.type("de");await app.type("de-D");
 assert.equal(app.option("ไทย"),undefined,"list is filtered while typing");
 const toggle=()=>app.all().find(n=>n.props["aria-label"]==="Show languages");
 toggle().props.onClick();await app.flush();                       // close
 toggle().props.onClick();await app.flush();                       // reopen: full list again
 await app.pick("ไทย");
 await app.blur();
 assert.equal(server.puts.length,0);
 assert.equal(app.save().props.disabled,false);
});

test("title still saves on its own (unchanged behaviour)",async()=>{
 const server=makeServer({call_as:"Boss",title:null,language:"th"});
 const app=await mount(server);
 app.title().props.onChange({target:{value:" อาจารย์ "}});await app.flush();
 const again=await saveAndReload(server,app);
 assert.equal(server.person.title,"อาจารย์");assert.equal(server.person.language,"th");assert.equal(server.person.call_as,"Boss");
 assert.equal(again.title().props.value,"อาจารย์");
});

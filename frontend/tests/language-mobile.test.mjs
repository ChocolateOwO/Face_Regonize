// ?lang handoff from the Mice Hub, the route-not-found page, and the phone
// menu (Layout below 768 px). Node-only: fake hooks, fake router, fake storage.
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
import ts from "typescript";

function compile(rel){
 return ts.transpileModule(readFileSync(new URL(rel,import.meta.url),"utf8"),
  {compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
}
function load(rel,deps){const exports={};new Function("require","exports",compile(rel))(n=>{if(n in deps)return deps[n];throw Error(n);},exports);return exports;}
const lang=load("../src/language.ts",{});
function store(init={}){const m=new Map(Object.entries(init));return {getItem:k=>m.has(k)?m.get(k):null,setItem:(k,v)=>m.set(k,String(v)),m};}

test("?lang=th|en|zh|ja is used and remembered; refresh without it keeps it",()=>{
 for(const l of ["th","en","zh","ja"]){
  const s=store();
  assert.equal(lang.resolveLanguage(`?lang=${l}`,s),l);
  assert.equal(lang.resolveLanguage("",s),l,"a later page without ?lang keeps the choice");
 }
 const s=store();assert.equal(lang.resolveLanguage("?lang=TH",s),"th","case-insensitive");
});

test("unsupported or missing ?lang keeps the saved choice or the default",()=>{
 assert.equal(lang.resolveLanguage("",store()),"en");
 assert.equal(lang.resolveLanguage("?lang=fr",store()),"en");
 const s=store({reconize_lang:"ja"});
 assert.equal(lang.resolveLanguage("?lang=klingon",s),"ja");
 assert.equal(s.m.get("reconize_lang"),"ja","an unsupported value is never saved");
 assert.equal(lang.resolveLanguage("",store({reconize_lang:"xx"})),"en","a corrupt saved value falls back");
 const broken={getItem(){throw new Error("denied");},setItem(){throw new Error("denied");}};
 assert.equal(lang.resolveLanguage("?lang=zh",broken),"zh","private mode: used, just not remembered");
 assert.equal(lang.resolveLanguage("",broken),"en");
});

test("Help opens the guide in the chosen language",()=>{
 assert.equal(lang.helpUrl("en"),"/help/index.html");
 for(const l of ["th","zh","ja"])assert.equal(lang.helpUrl(l),`/help/guide-${l}.html`);
 const built=readFileSync(new URL("../vite.help.ts",import.meta.url),"utf8");
 assert.match(built,/guide-\$\{language\}\.html/,"the Help build writes guide-<lang>.html");
});

test("unknown addresses get the not-found page inside the app",()=>{
 const app=readFileSync(new URL("../src/App.tsx",import.meta.url),"utf8");
 assert.match(app,/<Route path="\*" element={<NotFound \/>} \/>/);
});

function layout({path="/people",search="",role="admin",security={jwt_secret_default:false,admin_password_default:false,fix:[]}}={}){
 const st={cursor:0,dirty:true,hooks:[],effects:[]};let tree,root=null,rootProps=null;
 const react={useState:i=>{const k=st.cursor++;st.hooks[k]??={v:typeof i==="function"?i():i};return [st.hooks[k].v,v=>{st.hooks[k].v=typeof v==="function"?v(st.hooks[k].v):v;st.dirty=true;}];},
  useEffect:(fn,deps)=>{const k=st.cursor++;const prev=st.hooks[k];const changed=!prev||!deps||deps.some((d,i)=>d!==prev.deps[i]);if(changed){st.effects.push(fn);st.hooks[k]={deps};}}};
 const jsx=(type,props)=>({type,props});
 globalThis.document={documentElement:{lang:""}};
 globalThis.window={localStorage:store()};
 const mod=load("../src/components/Layout.tsx",{"react":react,"react/jsx-runtime":{jsx,jsxs:jsx,Fragment:"Fragment"},
  "react-router-dom":{Link:"Link",NavLink:"NavLink",useLocation:()=>({pathname:path,search}),useNavigate:()=>()=>{}},
  "../auth/AuthContext":{useAuth:()=>({username:"u",logout(){}})},
  "../api/client":{apiGet:async p=>p==="/api/auth/me"?{role}:p==="/api/system/security"?security:{}},
  "../language":lang});
 const nodes=(n,out=[])=>{if(Array.isArray(n))n.forEach(x=>nodes(x,out));else if(n?.props){out.push(n);nodes(n.props.children,out);}return out;};
 const text=n=>Array.isArray(n)?n.map(text).join(""):n?.props?text(n.props.children):typeof n==="string"?n:"";
 const render=async()=>{for(let i=0;i<6;i++){if(st.dirty){st.cursor=0;st.dirty=false;tree=(root||mod.default)(rootProps||{children:"PAGE"});while(st.effects.length)st.effects.shift()();}await new Promise(setImmediate);}};
 // Render one child component (e.g. the security banner) on its own.
 const mount=async(fn,props)=>{root=fn;rootProps=props;st.hooks=[];st.effects=[];st.dirty=true;await render();};
 return {render,mount,all:()=>nodes(tree),text:()=>text(tree)};
}

test("phone menu: a top-bar button opens and closes the drawer; desktop keeps the sidebar",async()=>{
 const l=layout();await l.render();
 const aside=()=>l.all().find(n=>n.type==="aside");
 const button=()=>l.all().find(n=>n.type==="button"&&n.props["aria-controls"]==="reconize-nav");
 assert.match(aside().props.className,/-translate-x-full/,"closed on a phone");
 assert.match(aside().props.className,/md:static md:translate-x-0/,"always visible from 768 px");
 assert.equal(button().props["aria-expanded"],false);
 button().props.onClick();await l.render();
 assert.match(aside().props.className,/ translate-x-0/);assert.equal(button().props["aria-expanded"],true);
 const overlay=l.all().find(n=>n.type==="div"&&n.props.onClick&&n.props["aria-hidden"]==="true");
 overlay.props.onClick();await l.render();
 assert.match(aside().props.className,/-translate-x-full/,"tapping outside closes it");
});

test("Help link and <html lang> follow ?lang",async()=>{
 const l=layout({search:"?lang=ja"});await l.render();
 const help=l.all().find(n=>n.type==="a"&&String(n.props.href).startsWith("/help/"));
 assert.equal(help.props.href,"/help/guide-ja.html");assert.equal(globalThis.document.documentElement.lang,"ja");
});

test("security banner: admins see public defaults, without values; others never ask",async()=>{
 async function banner(opts,isAdmin){
  const l=layout(opts);await l.render();
  const el=l.all().find(n=>typeof n.type==="function"&&n.type.name==="SecurityBanner");
  assert.ok(el,"Layout renders the security banner slot");
  await l.mount(el.type,{isAdmin});return l.text();
 }
 const shown=await banner({security:{jwt_secret_default:true,admin_password_default:true,fix:["Stop Reconize."]}},true);
 assert.match(shown,/Security: public default sign-in secret \(JWT_SECRET\) and admin password/);
 assert.match(shown,/Stop Reconize\./);
 assert.doesNotMatch(shown,/admin123|dev-secret/);
 assert.equal(await banner({},true),"","a clean install shows nothing");
 assert.equal(await banner({security:{jwt_secret_default:true,admin_password_default:false,fix:["x"]}},false),"","non-admins never see it");
});

test("mobile CSS: wide tables scroll inside their own box below 768 px",()=>{
 const css=readFileSync(new URL("../src/index.css",import.meta.url),"utf8");
 assert.match(css,/@media \(max-width: 767px\)\s*{\s*main table\s*{[^}]*overflow-x: auto/);
});

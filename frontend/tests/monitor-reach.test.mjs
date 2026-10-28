import assert from "node:assert/strict";
import {createRequire, Module} from "node:module";
import {fileURLToPath} from "node:url";
import fs from "node:fs";
import test from "node:test";
const require=createRequire(new URL("../package.json",import.meta.url));
function load(name){const entry=fileURLToPath(new URL("../src/"+name,import.meta.url)),mod=new Module(entry);mod.paths=require.resolve.paths("react");mod._compile(require("esbuild").buildSync({entryPoints:[entry],bundle:true,write:false,platform:"node",format:"cjs",packages:"external",loader:{".css":"empty"}}).outputFiles[0].text,entry);return mod.exports;}
const {monitorReachModel}=load("monitorReachModel.ts"),{MonitorReach}=load("monitorReach.tsx");
const node=(id,parent_id="",fields={})=>({id,parent_id,scope_kind:"relation",status:"active",section:"peers",hop:"99",text:"SECRET_NAME",reason:"SECRET_REASON",...fields});
const plan=(items,extra={})=>({items,framework:[],...extra});
const render=(p,props={})=>require("react-dom/server").renderToStaticMarkup(require("react").createElement(MonitorReach,{company:"合成公司",plan:p,...props}));
test("four trunks; valid parentage overrides stored hop, counts and research",()=>{
 const p=plan([node("a"),node("b","a"),node("c","b",{section:"rules"}),node("q","c",{scope_kind:"question"}),node("old","",{scope_kind:"",hop:"99"})]);
 const before=JSON.stringify(p),m=monitorReachModel(p);
 assert.deepEqual(m.rows.map(r=>r.depth),[0,3,0,0]);assert.equal(JSON.stringify(p),before);assert.equal(m.excluded,0);
});
test("framework ring wins, standard sections align with backend, type is last fallback",()=>{
 const m=monitorReachModel(plan([node("a","",{section:"custom",relation_type:"competitor"}),node("b","",{section:"science",relation_type:"product"}),node("c","",{section:"partners"}),node("d","",{section:"unknown",relation_type:"competitor"})],{framework:[{key:"custom",ring:"company"}]}));
 assert.deepEqual(m.rows.map(r=>r.depth),[1,1,1,1]);
});
test("retired leaf excluded; closed ancestor preserves active descendant provenance",()=>{
 assert.equal(monitorReachModel(plan([node("a","",{status:"retired"})])).depth,0);
 assert.equal(monitorReachModel(plan([node("a","",{status:"retired"}),node("b","a")])).depth,2);
});
test("malformed parentage, duplicates and unknown roots fail closed with notice",()=>{
 const m=monitorReachModel(plan([node("orphan","missing"),node("a","b"),node("b","a"),node("dup"),node("dup"),node("child","dup"),node("unknown","",{section:"unknown"}),node("legacy","",{scope_kind:""}),node("legacy-child","legacy")]));
 assert.equal(m.depth,0);assert.equal(m.excluded,8);
});
test("empty and legacy-only plans keep four gray trunks",()=>{
 const html=render(plan([node("legacy","",{scope_kind:""})]));
 assert.equal((html.match(/class="mr-trunk"/g)||[]).length,4);
 assert.doesNotMatch(html,/class="mr-lit"/);assert.match(html,/原有关注点仍保留/);
});
test("markup contains no object details or interactive tree; paused is not running",()=>{
 const html=render(plan([node("a"),node("b","a")]),{paused:true,company:"<script>alert(1)</script>"});
 assert.doesNotMatch(html,/SECRET_NAME|SECRET_REASON|<button|role="button"|<script>/);
 assert.match(html,/已暂停/);assert.match(html,/不表示全面覆盖/);assert.match(html,/data-direction="competition" data-depth="2"/);
});
test("new plan response updates reach and retiring endpoint reduces it",()=>{
 const a=node("a"),b=node("b","a");
 assert.match(render(plan([a])),/data-depth="1"/);
 assert.match(render(plan([a,b])),/data-depth="2"/);
 assert.doesNotMatch(render(plan([a,{...b,status:"retired"}])),/data-depth="2"/);
});
test("large graphs use four trunks; deep chains are not capped or recursively traversed",()=>{
 const items=Array.from({length:1200},(_,i)=>node("r"+i,i?"r"+(i-1):"")),p=plan(items),m=monitorReachModel(p);
 assert.equal(m.depth,1200);const html=render(p);assert.equal((html.match(/class="mr-trunk"/g)||[]).length,4);assert.match(html,/已至第 1200 层/);assert.match(html,/…/);
});
test("theme variables, scoped styles and horizontal scroll preserved",()=>{
 const css=fs.readFileSync(new URL("../src/monitorReach.css",import.meta.url),"utf8");
 assert.doesNotMatch(css,/#[0-9a-f]{3,8}\b/i);assert.match(css,/overflow-x:auto/);assert.match(css,/focus-visible/);
 const source=fs.readFileSync(new URL("../src/plan.tsx",import.meta.url),"utf8");
 assert.match(source,/workRevision\(detail\)/);
 assert.doesNotMatch(source,/<MonitorReach /); // Removed by the approved scope redesign.
 assert.ok(source.indexOf("<MonitorLoop ")>=0);
 assert.ok(source.indexOf("<MonitorLoop ")<source.indexOf("<MonitorScope "));
});

test("compact strip keeps readable geometry and avoids stacked card margins",()=>{
 const html=render(plan([node("a"),node("b","a"),node("c","b")]));
 assert.match(html,/viewBox="0 0 980 134"/);assert.match(html,/min-width:980px/);
 const css=fs.readFileSync(new URL("../src/monitorReach.css",import.meta.url),"utf8");
 assert.match(css,/margin:0;min-width:0/);assert.match(css,/monitor-reach\+\.relation-scope\{margin-top:0\}/);
 assert.match(css,/\.mr-map\{[^}]*height:auto/);assert.doesNotMatch(css,/monitor-reach>footer/);
});

test("tier panels fill the entire canvas with aligned dividers and no edge shadow",()=>{
 const html=render(plan([]));
 assert.match(html,/class="mr-tier-backgrounds" viewBox="0 0 980 1" preserveAspectRatio="none"/);
 assert.equal((html.match(/class="mr-panel-divider"/g)||[]).length,3);
 assert.doesNotMatch(html,/tier-wash|mr-tier-zone/);
 const css=fs.readFileSync(new URL("../src/monitorReach.css",import.meta.url),"utf8");
 assert.match(css,/\.mr-tier-backgrounds\{[^}]*inset:0/);
 assert.doesNotMatch(css,/box-shadow:(?!none)/);
});

test("each visible tier has a decorative background without changing depth or height",()=>{
 const html=render(plan([]));
 assert.equal((html.match(/class="mr-tier-band"/g)||[]).length,3);
 assert.ok(html.indexOf('class="mr-tier-backgrounds"')<html.indexOf('class="mr-trunk"'));
 assert.doesNotMatch(html,/class="mr-lit"/);assert.match(html,/viewBox="0 0 980 134"/);
 const bands=[...html.matchAll(/class="mr-tier-band"[^>]*opacity="([^"]+)"/g)].map(m=>Number(m[1]));
 assert.equal(bands.length,3);assert.ok(bands[0]>bands[1] && bands[1]>bands[2]);assert.ok(bands[2]>0);
});

test("hover inspection remains read-only with keyboard equivalents and reduced motion",()=>{
 const html=render(plan([node("a")]));
 assert.equal((html.match(/tabindex="0" role="img"/g)||[]).length,4);
 assert.equal((html.match(/class="mr-hit"/g)||[]).length,4);
 assert.match(html,/aria-label="竞争 · 已至第 1 层"/);
 assert.match(html,/aria-label="公司本体 · 尚未建立关联层级"/);
 const css=fs.readFileSync(new URL("../src/monitorReach.css",import.meta.url),"utf8");
 assert.match(css,/prefers-reduced-motion:reduce/);assert.match(css,/:focus-visible \.mr-focus-ring/);
 assert.match(css,/mr-base\{stroke:var\(--muted\)\}/);
});

function depthPlan(depths) {
 return plan(depths.flatMap((depth, direction) => Array.from({length:depth}, (_, i) =>
  node(`${direction}-${i}`, i ? `${direction}-${i-1}` : "", {section:["asset","peers","customer","rules"][direction]}))));
}
function trunks(html) {
 return [...html.matchAll(/<g class="mr-trunk"[^>]*data-depth="(\d+)"[^>]*>([\s\S]*?)<\/g>/g)]
  .map(([,depth,body]) => ({depth:Number(depth),body}));
}

test("deepest-node halo and gold endpoint agree across mixed and compressed depths",()=>{
 for (const depths of [[0,1,2,3],[3,2,1,0],[6,5,4,3],[7,5,2,0],[1200,11,4,1]]) {
  const rows=trunks(render(depthPlan(depths)));
  assert.deepEqual(rows.map(r=>r.depth),depths);
  for (const {depth,body} of rows) {
   if (!depth) { assert.doesNotMatch(body,/class="mr-lit"|class="mr-front"|class="mr-node reached"/);continue; }
   const front=body.match(/class="mr-front" cx="([^"]+)" cy="([^"]+)"/);
   const lit=body.match(/class="mr-lit" d="([^"]+)"/);
   assert.ok(front && lit);
   assert.equal(Number(lit[1].match(/H ([\d.]+)$/)[1]),Number(front[1]));
   const endpoint=[...body.matchAll(/class="mr-node reached" cx="([^"]+)" cy="([^"]+)" r="([^"]+)"/g)]
    .find(m=>m[1]===front[1] && m[2]===front[2] && m[3]==="5");
   assert.ok(endpoint,"halo must surround the actual deepest reached node");
   assert.match(body,new RegExp(`已至第 ${depth} 层`));
  }
 }
});

test("direction labels occupy a line gap before the first tier, not the line or tier heading",()=>{
 for(const {body} of trunks(render(depthPlan([3,2,1,0])))) {
  const label=body.match(/class="mr-direction" x="([^"]+)" y="([^"]+)">([^<]+)<\/text>/);
  const base=body.match(/class="mr-base" d="M 91 75 C 140 75, 146 ([\d.]+), ([\d.]+) ([\d.]+) M ([\d.]+) ([\d.]+) H/);
  assert.ok(label && base);
  const left=Number(label[1]),estimatedRight=left+label[3].length*13;
  assert.ok(Number(base[2])<=left-10,"curve ends before label");
  assert.ok(Number(base[4])>=estimatedRight+10,"track resumes after label");
  assert.ok(Number(base[4])<300-10,"label clears the first tier node");
  assert.equal(Number(label[2]),Number(base[1])+4.5);
  assert.ok(Number(label[2])-13>12,"label clears tier heading baseline");
 }
});

test("arrival conclusions share a right-aligned column after the final tier",()=>{
 for(const depths of [[0,1,2,3],[7,5,2,0]]) {
  const html=render(depthPlan(depths));
  const width=Number(html.match(/class="mr-map" viewBox="0 0 (\d+) 134"/)[1]);
  for(const {depth,body} of trunks(html)) {
   const arrival=body.match(/class="mr-(?:arrival|empty)" x="([^"]+)" y="[^"]+" text-anchor="end">([^<]+)<\/text>/);
   assert.ok(arrival);assert.equal(Number(arrival[1]),width-26);
   assert.equal(arrival[2],depth?`已至第 ${depth} 层`:"尚未延伸");
  }
 }
});

test("reached tracks remain solid and frontier halo remains static with focus feedback",()=>{
 const css=fs.readFileSync(new URL("../src/monitorReach.css",import.meta.url),"utf8");
 assert.match(css,/\.mr-base\{[^}]*stroke-dasharray:2 4/);
 assert.doesNotMatch(css.match(/\.mr-lit\{[^}]*\}/)[0],/stroke-dasharray/);
 assert.match(css,/\.mr-front\{[^}]*opacity:\.16;pointer-events:none/);
 assert.match(css,/\.mr-trunk\[data-active=true\] \.mr-front\{opacity:\.26\}/);
 assert.doesNotMatch(css,/@keyframes|animation:/);
});

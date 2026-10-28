import assert from 'node:assert/strict';
import { createRequire, Module } from 'node:module';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import fs from 'node:fs';
const require = createRequire(new URL('../package.json', import.meta.url));
const entry = fileURLToPath(new URL('../src/planTreeModel.ts', import.meta.url));
const mod = new Module(entry);
mod.paths = require.resolve.paths('react');
mod._compile(require('esbuild').buildSync({entryPoints:[entry],bundle:true,write:false,platform:'node',format:'cjs',packages:'external',loader:{'.css':'empty'}}).outputFiles[0].text,entry);
const {treeModel} = mod.exports;
const item = (id,fields={}) => ({id,text:id,section:'science',status:'active',hits:99,...fields});
const r = item('r',{scope_kind:'relation',relation_target:'合成技术'});
const q = item('q',{scope_kind:'question',parent_id:'r',hop:'2'});
test('research depth is not another object relation hop',()=>{
 const m=treeModel([r,q],{},{});
 assert.equal(m.items[0].hop,'1'); assert.equal(m.items[1].hop,'1');
 assert.equal(m.items[0].name,'合成技术');
 assert.equal(m.items[1].role,'question');
 assert.equal(m.items[0].evidence,false); // raw historical hits never override accepted signal
});
test('stopped, unknown recovery and active are not presented as running',()=>{
 assert.equal(treeModel([r,q],{q:{state:'stopped',reason:'answered'}},{}).items[1].stateLabel,'已结束');
 assert.equal(treeModel([r,q],{q:{state:'waiting',reason:'waiting_recovery'}},{}).items[1].stateLabel,'待确认恢复');
 assert.equal(treeModel([r,q],{q:{state:'active'}},{}).items[1].stateLabel,'可继续探索');
});
test('closed ancestor is kept, while unrelated closed leaves remain hidden',()=>{
 const m=treeModel([{...r,status:'retired'},q,item('old',{status:'retired'})],{},{});
 assert.deepEqual(m.items.map(i=>i.id),['r','q']);
 assert.equal(m.items[1].parent_id,'r'); assert.equal(m.items[0].contextOnly,true);
});
test('evidence and recent flags come only from saved signals',()=>{
 const m=treeModel([r,q],{},{r:{count:2,fresh:true},q:{count:0,fresh:true}});
 assert.equal(m.items[0].evidence,true); assert.equal(m.items[0].fresh,true);
 assert.equal(m.items[1].fresh,false);
});
test('unknown parents and cycles remain disclosed, never recurse forever',()=>{
 const a={...r,parent_id:'q'}; const m=treeModel([a,q,item('missing',{parent_id:'gone'})],{},{});
 assert.ok(m.warnings.length>=2); assert.ok(m.items.every(i=>Number.isFinite(Number(i.hop))));
});
test('legacy item is not silently promoted to a relation',()=>{
 assert.equal(treeModel([item('old')],{},{}).items[0].role,'legacy');
});

test('detail uses full research question instead of truncated short label',()=>{
 assert.equal(treeModel([{...q,label:'摘要…',text:'完整的研究问题与结束边界'}],{},{}).items[0].name,'完整的研究问题与结束边界');
});

test('dense sector aggregates SVG nodes but retains every entry in the list',()=>{
 const entry=fileURLToPath(new URL('../src/planTree.tsx',import.meta.url));
 const component=new Module(entry); component.paths=mod.paths;
 component._compile(require('esbuild').buildSync({entryPoints:[entry],bundle:true,write:false,platform:'node',format:'cjs',packages:'external',loader:{'.css':'empty'}}).outputFiles[0].text,entry);
 const items=Array.from({length:40},(_,n)=>({...r,id:`r${n}`,relation_target:`合成路线${n}`}));
 const html=require('react-dom/server').renderToStaticMarkup(require('react').createElement(component.exports.PlanChainTree,{company:'合成公司',paused:true,plan:{items,rings:[],framework:[{key:'science',label:'技术路线'}],tree_signals:{}}}));
 assert.match(html,/pt-bundle/); assert.match(html,/40 个节点/);
 assert.doesNotMatch(html,/class="pt-node /);
 for(let n=0;n<40;n++) assert.ok(html.includes(`合成路线${n}`));
 assert.match(html,/项目已暂停/);
});

test('overview exposes stored reasons, escapes text and keeps help collapsed',()=>{
 const entry=fileURLToPath(new URL('../src/planTree.tsx',import.meta.url));
 const component=new Module(entry); component.paths=mod.paths;
 component._compile(require('esbuild').buildSync({entryPoints:[entry],bundle:true,write:false,platform:'node',format:'cjs',packages:'external',loader:{'.css':'empty'}}).outputFiles[0].text,entry);
 const html=require('react-dom/server').renderToStaticMarkup(require('react').createElement(component.exports.PlanChainTree,{company:'合成公司',plan:{items:[{...r,reason:'采用该技术路线 <script>unsafe()</script>'}],rings:[],framework:[{key:'science',label:'技术路线'}],tree_signals:{}}}));
 assert.match(html,/采用该技术路线 &lt;script&gt;/);
 assert.doesNotMatch(html,/<script>/);
 assert.match(html,/<details class="pt-help small"><summary>图例与说明/);
 assert.match(html,/aria-label="返回全部关联"/);
 assert.match(html,/aria-expanded="false"[^>]*>展开关联图/);
});

test('hover highlights independently from pinned details; all reset paths remain',()=>{
 const src=fs.readFileSync(new URL('../src/planTree.tsx',import.meta.url),'utf8');
 assert.match(src,/const focus = selected \? byId.get\(selected\)/);
 assert.match(src,/const highlight = hovered \?\? focus/);
 assert.match(src,/onMouseLeave=\{\(\)=>setHover\(null\)\}/);
 assert.match(src,/e.key==='Escape'/);
 assert.match(src,/onClick=\{reset\}>返回全部/);
 assert.match(src,/aria-pressed=\{effectiveDirection===d.key\}/);
 assert.match(src,/这个方向尚未纳入关联对象/);
 assert.match(src,/查看另 \{named.length-1\} 个对象/);
 assert.ok(src.indexOf('<dt>当前认识')<src.indexOf('<dt>纳入原因'));
 assert.match(src,/<details key=\{`research-\$\{focus.id\}`\}/);
});

test('narrow list is default, diagram stays opt-in and category labels remain legible on focus',()=>{
 const css=fs.readFileSync(new URL('../src/planTree.css',import.meta.url),'utf8');
 assert.match(css,/@container \(max-width:600px\)/);
 assert.match(css,/\.pt-figure:not\(\.expanded\) \{ display:none/);
 assert.match(css,/g.off > text \{ fill:var\(--muted\);opacity:.85/);
 assert.match(css,/prefers-reduced-motion:reduce/);
});

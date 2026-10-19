import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
function load(relative){const text=fs.readFileSync(new URL(relative,import.meta.url),'utf8');const js=ts.transpileModule(text,{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText;const exports={};new Function('exports',js)(exports);return exports;}
const {tabs,readRoute,normalizePath}=load('./navigation.ts');
const {projects,events,analyses}=load('../prototype/data.ts');
test('8933 primary five pages retain their order',()=>assert.deepEqual(tabs.slice(0,5).map(x=>x[1]),['公司概览','经营情况','公司动态','竞争对手','行业环境']));
test('every project and page routes independently',()=>{for(const p of projects)for(const [tab] of tabs){const r=readRoute(`#/project/${p.id}/${tab}`);assert.equal(r.projectId,p.id);assert.equal(r.tab,tab);assert.equal(r.portfolio,false)}});
test('source analysis selection survives navigation',()=>assert.equal(readRoute('#/project/chengyao/analysis?id=chengyao-position').params.get('id'),'chengyao-position'));
test('8945 external links use the correct separate page',()=>{assert.equal(normalizePath('/project/chengyao/external?category=peers'),'/project/chengyao/competition?category=peers');assert.equal(normalizePath('/project/hengchuan/external?category=policy'),'/project/hengchuan/industry?category=policy')});
test('unknown routes recover to safe portfolio or company overview',()=>{assert.equal(readRoute('#/broken').portfolio,true);assert.equal(readRoute('#/project/chengyao/broken').tab,'overview')});
test('both fictional projects support peer, industry and source evidence',()=>{for(const p of projects){for(const suffix of ['position','environment','technology'])assert.ok(analyses.some(a=>a.id===`${p.id}-${suffix}`));assert.ok(events.filter(e=>e.projectId===p.id).length>=18);assert.ok(p.docs.some(d=>d.id.endsWith('financial-pack')))}});
test('new application is isolated from network services and file contents',()=>{for(const file of ['App.tsx','Pages.tsx']){const text=fs.readFileSync(new URL(file,import.meta.url),'utf8');assert.doesNotMatch(text,/\bfetch\s*\(|\baxios\b|FileReader|\.arrayBuffer\(|\.text\(/);assert.doesNotMatch(text,/from ['"].*\/api['"]/)}const vite=fs.readFileSync(new URL('vite.config.ts',import.meta.url),'utf8');assert.match(vite,/port:8946/);assert.match(vite,/outDir:'dist'/)});
test('financial statements and source links remain reachable',()=>{const text=fs.readFileSync(new URL('Pages.tsx',import.meta.url),'utf8');for(const feature of ['InternalFinance','InternalRecords','TechnologyMap','CompactNewsRow','AnalysisVisual'])assert.ok(text.includes(feature));assert.match(text,/e\.subject===project\.name/)});

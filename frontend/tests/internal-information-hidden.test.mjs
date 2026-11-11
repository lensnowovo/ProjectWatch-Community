import assert from 'node:assert/strict';
import test from 'node:test';
import {createRequire,Module} from 'node:module';
import {fileURLToPath} from 'node:url';
import {readFileSync} from 'node:fs';
const require=createRequire(new URL('../package.json',import.meta.url));
const entry=fileURLToPath(new URL('../src/internalInformation.ts',import.meta.url)),mod=new Module(entry);
mod._compile(require('esbuild').buildSync({entryPoints:[entry],bundle:true,write:false,platform:'node',format:'cjs'}).outputFiles[0].text,entry);
const {INTERNAL_INFORMATION_VISIBLE,INTERNAL_TABS,visibleProjectTab}=mod.exports;
test('hidden internal tabs redirect while public deep links remain usable',()=>{
 assert.equal(INTERNAL_INFORMATION_VISIBLE,false);
 for(const t of INTERNAL_TABS)assert.equal(visibleProjectTab(t,'brief'),'brief');
 for(const t of ['brief','external','competition','chain','industry','overview','plan'])assert.equal(visibleProjectTab(t,'brief'),t);
});
test('navigation and both upload entry points use the same visibility policy',()=>{
 const project=readFileSync(new URL('../src/pages/Project.tsx',import.meta.url),'utf8');
 assert.match(project,/VISIBLE_SECTIONS.map/);assert.match(project,/raw && VISIBLE_SECTIONS.some/);
 assert.match(project,/INTERNAL_INFORMATION_VISIBLE && <button className="btn project-upload"/);
 const create=readFileSync(new URL('../src/pages/NewProject.tsx',import.meta.url),'utf8');
 assert.match(create,/INTERNAL_INFORMATION_VISIBLE && <div\s+className=\{`dropzone/);
});
test('public monitoring controls stay reachable on 监控范围 while internal pages are hidden',()=>{
 const project=readFileSync(new URL('../src/pages/Project.tsx',import.meta.url),'utf8');
 assert.match(project,/<PlanView[^>]*\n?\s*reference=\{monitoringReference\(data\)\}/);
 const reference=project.slice(project.indexOf('function monitoringReference'));
 for(const part of ['<Coverage ','<BodyCoverageCard ','<QueryTerms ','<MonitorRules ','<MonitorSources ','<MonitorCadence '])
  assert.ok(reference.includes(part),part);
 const documents=project.slice(project.indexOf('function DocumentsTab'),project.indexOf('function monitoringReference'));
 assert.doesNotMatch(documents,/MonitorRules|MonitorSources|MonitorCadence|<Coverage /);
 assert.match(readFileSync(new URL('../src/plan.tsx',import.meta.url),'utf8'),/\.\.\.reference,/);
});

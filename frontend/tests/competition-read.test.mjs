import assert from 'node:assert/strict';
import {createRequire, Module} from 'node:module';
import {fileURLToPath} from 'node:url';
import {readFileSync} from 'node:fs';
import test from 'node:test';
const require=createRequire(new URL('../package.json',import.meta.url));
const {buildSync}=require('esbuild'),React=require('react'),{renderToStaticMarkup}=require('react-dom/server');
function load(path){const entry=fileURLToPath(new URL(path,import.meta.url));const mod=new Module(entry);mod.paths=require.resolve.paths('react');mod._compile(buildSync({entryPoints:[entry],bundle:true,write:false,platform:'node',format:'cjs',packages:'external',loader:{'.css':'empty'}}).outputFiles[0].text,entry);return mod.exports}
const {createReadResource}=load('../src/readResource.ts');
const {CompetitionFallback}=load('../src/questionAnalysis.tsx');
test('switches and concurrent consumers reuse the same GET',async()=>{
 let count=0,release; const resource=createReadResource(()=>{count++;return new Promise(r=>release=r)});
 const a=resource.load('project-a:1'),b=resource.load('project-a:1');assert.equal(a,b);
 await Promise.resolve();assert.equal(count,1);release({rows:[]});await a;
 await resource.load('project-a:1');assert.equal(count,1);assert.equal(resource.snapshot('project-a:1').loading,false);
});
test('projects and revisions never share results',async()=>{
 const resource=createReadResource(async key=>({key}));
 await resource.load('A:1');await resource.load('A:2');await resource.load('B:1');
 assert.equal(resource.snapshot('A:1').data.key,'A:1');assert.equal(resource.snapshot('B:1').data.key,'B:1');
});
test('failed refresh retains content and explicit retry recovers',async()=>{
 let fail=false,count=0;const resource=createReadResource(async()=>{count++;if(fail)throw Error('offline');return {ok:count}});
 await resource.load('A');fail=true;await resource.load('A',true);
 assert.equal(resource.snapshot('A').data.ok,1);assert.match(resource.snapshot('A').error.message,/offline/);
 fail=false;await resource.load('A',true);assert.equal(resource.snapshot('A').error,null);assert.equal(count,3);
});
test('timeout aborts read and never publishes late data',async()=>{
 let signal,release;const resource=createReadResource((_,s)=>{signal=s;return new Promise(r=>release=r)},60000,5);
 await resource.load('A');assert.equal(signal.aborted,true);assert.match(resource.snapshot('A').error.message,/超时/);
 release({late:true});await Promise.resolve();assert.equal(resource.snapshot('A').data,null);
});
test('expired read refreshes but fresh navigation does not',async()=>{
 let n=0;const r=createReadResource(async()=>++n,0);
 await r.load('A');await r.load('A');assert.equal(n,2);
});
test('stage-empty view shows sourced comparison without invented stages',()=>{
 const view={record:{analysis:{position:{for:[],against:[{text:'合成对手交付规模更大。',refs:[]}]}}}};
 const html=renderToStaticMarkup(React.createElement(CompetitionFallback,{data:{rows:[{name:'合成竞对',is_self:false}]},view}));
 assert.match(html,/合成竞对/);assert.match(html,/合成对手交付规模更大/);assert.match(html,/阶段记录不足/);assert.doesNotMatch(html,/跑得最快|qa-stage-dot/);
});
test('question and evidence state survive details navigation',()=>{
 const question=readFileSync(new URL('../src/questionAnalysis.tsx',import.meta.url),'utf8');
 const page=readFileSync(new URL('../src/pages/Project.tsx',import.meta.url),'utf8');
 assert.match(question,/params.get\('question'\)/);assert.match(question,/params.get\('evidence'\)/);
 assert.match(page,/new URLSearchParams\(previous\)/);assert.doesNotMatch(question,/api\.race\(projectId\)\.catch/);assert.doesNotMatch(question,/跑得最快的是/);
});

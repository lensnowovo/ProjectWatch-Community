import assert from 'node:assert/strict';
import {createRequire, Module} from 'node:module';
import {fileURLToPath} from 'node:url';
import test from 'node:test';
const require=createRequire(new URL('../package.json',import.meta.url));
const {buildSync}=require('esbuild');
function load(path){const entry=fileURLToPath(new URL(path,import.meta.url));const m=new Module(entry);m.paths=require.resolve.paths('react');m._compile(buildSync({entryPoints:[entry],bundle:true,write:false,platform:'node',format:'cjs',packages:'external',loader:{'.css':'empty'}}).outputFiles[0].text,entry);return m.exports;}
const {dossierSnapshot,observeDossier,emptyObserver,dossierRail}=load('../src/dossierMotionState.ts');
const base=()=>({project:{id:'synthetic',activated:true,status:'active'},profile:null,public_profile:null,jobs:[],documents:[],execution:{analysis:'running',parsing:'running'}});
const fact=(text,kind='product')=>({kind,label:'合成产品',text,date:'2026-10',refs:[]});
const snap=d=>dossierSnapshot(d);
test('first load is a baseline, persisted products are not new arrivals',()=>{
 const d={...base(),public_profile:{facts:[fact('已有产品')]}};
 const result=observeDossier(emptyObserver(),snap(d));assert.equal(result.arrivals.length,0);
 assert.equal(observeDossier(result,snap(d)).arrivals.length,0);
});
test('new persisted content arrives once, same-count replacement remains visible',()=>{
 let state=observeDossier(emptyObserver(),snap(base()));
 const d={...base(),public_profile:{facts:[fact('已保存产品')]}};
 state=observeDossier(state,snap(d));assert.equal(state.arrivals[0].group,'business');assert.equal(state.arrivals[0].added,1);
 state=observeDossier(state,snap(d));assert.equal(state.arrivals.length,0);
 d.public_profile.facts[0].text='更正产品用途';state=observeDossier(state,snap(d));assert.equal(state.arrivals[0].kind,'updated');
});
test('timestamps, source counts, object-key order and array order do not create receipts',()=>{
 const d={...base(),public_profile:{updated_at:'one',sources:1,facts:[fact('A'),fact('B','person')]}};
 const state=observeDossier(emptyObserver(),snap(d));
 const next={...d,public_profile:{...d.public_profile,updated_at:'two',sources:4,facts:[...d.public_profile.facts].reverse().map(f=>({refs:f.refs,date:f.date,text:f.text,label:f.label,kind:f.kind}))}};
 assert.deepEqual(snap(d),snap(next));assert.equal(observeDossier(state,snap(next)).arrivals.length,0);
});
test('removal, project switch, stale and hidden gaps never replay history',()=>{
 const d={...base(),public_profile:{facts:[fact('A'),fact('B')]}};
 let state=observeDossier(emptyObserver(),snap(d));
 state=observeDossier(state,snap({...d,public_profile:{facts:[fact('A')]}}));assert.equal(state.arrivals.length,0);
 state=observeDossier(state,snap({...d,project:{...d.project,id:'another'}}));assert.equal(state.arrivals.length,0);
 state=observeDossier(state,null);state=observeDossier(state,snap(d));assert.equal(state.arrivals.length,0);
});
test('saved relations and judgment revisions, not successful jobs, cause arrivals',()=>{
 let state=observeDossier(emptyObserver(),snap(base()));
 const d={...base(),jobs:[{id:'done',kind:'insight',status:'done'}]};
 state=observeDossier(state,snap(d));assert.equal(state.arrivals.length,0);
 d.dossier_saved={version:1,relations:{revision:'r',count:2},judgment:{revision:'j',count:1}};
 state=observeDossier(state,snap(d));assert.deepEqual(state.arrivals.map(x=>x.group),['relations','judgment']);
});
test('running animation requires a backend state; waiting has no travelling beam',()=>{
 for(const state of ['queued','paused','offline','partial','failed','needs_confirmation','stalled','unavailable','waiting_response','recovering']){
  const d={...base(),jobs:[{id:'j',kind:'external',status:'running',runtime:{state,label:state,phase:'read'}}]};
  assert.equal(dossierRail(d).flowing,false,state);
 }
 const d={...base(),jobs:[{id:'j',kind:'external',status:'running',runtime:{state:'running',label:'整理中',phase:'read'}}]};
 assert.equal(dossierRail(d).flowing,true);assert.equal(dossierRail({...d,status_stale:true}).flowing,false);
 assert.equal(dossierRail({...d,project:{...d.project,status:'paused'}}).flowing,true,'in-flight work is not cancelled by pause');
});

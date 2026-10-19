import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source=readFileSync(new URL('../src/project_watch/static/app.js',import.meta.url),'utf8');
const select=source.slice(source.indexOf('async function selectProject('),source.indexOf('function closeMemberDialogs('));
const refresh=source.slice(source.indexOf('async function refreshProject('),source.indexOf('function jobFor('));
function harness(code,extras={}) {
  const state={projects:[{id:'a',name:'Alpha'},{id:'b',name:'Beta'}],current:{id:'a'},selectedCandidates:new Set(),batchDecisions:new Map(),teamMode:true};
  const nodes=new Map(),rendered=[],notices=[];
  const context=vm.createContext({state,sessionStorage:{setItem(){},removeItem(){}},clearTimeout(){},
    $:id=>{if(!nodes.has(id))nodes.set(id,{open:false,replaceChildren(){}});return nodes.get(id);},
    reportNode(){},closeMemberDialogs(){},closeIdentityDialog(){},renderProjects(){},renderView(){},applyCapabilities(){},
    renderProject(){rendered.push({...state});},schedulePoll(){},toast:m=>notices.push(m),...extras});
  vm.runInContext(code,context);
  return {state,context,rendered,notices};
}
test('switch clears project content and renders new identity before awaiting network',async()=>{
  let finish;
  const h=harness(select,{refreshProject:()=>new Promise(resolve=>{finish=resolve;})});
  const old={project_id:'a'};
  for(const key of ['external','briefing','competition','watchReview','watchChange','watchChangeReview','publicSearchPreviews','review','impactReview'])h.state[key]=old;
  h.state.watchItems=[old];h.state.watchChanges=[old];
  const pending=h.context.selectProject('b');
  assert.equal(h.rendered.length,1);
  assert.equal(h.rendered[0].current.name,'Beta');
  for(const key of ['external','briefing','competition','watchReview','watchChange','watchChangeReview','publicSearchPreviews','review','impactReview'])assert.equal(h.state[key],null,key);
  assert.equal(h.state.watchItems.length,0);assert.equal(h.state.watchChanges.length,0);
  assert.equal(h.state.capabilities.propose,false);
  finish();await pending;
});
test('late failure from a previous selection cannot remove the current project',async()=>{
  let rejectSummary;
  const h=harness(refresh,{request:path=>path.endsWith('/summary')?new Promise((_,reject)=>{rejectSummary=reject;}):Promise.resolve([]),loadProjects:async()=>{throw Error('must not reload');}});
  const pending=h.context.refreshProject();
  h.state.current={id:'b'};
  rejectSummary(Object.assign(Error('gone'),{status:404}));
  await pending;
  assert.equal(h.state.current.id,'b');assert.equal(h.notices.length,0);
});
for(const fail of [false,true])test(`A to B to A discards first A response (${fail?'failure':'success'})`,async()=>{
  let complete;
  const h=harness(refresh,{request:path=>path.endsWith('/summary')?new Promise((resolve,reject)=>{complete=()=>fail?reject(Object.assign(Error('gone'),{status:404})):resolve({project:{id:'a',name:'obsolete'}});}):Promise.resolve([]),loadProjects:async()=>{throw Error('must not reload');}});
  h.state.projectRevision=1;const pending=h.context.refreshProject();
  h.state.projectRevision=3;h.state.current={id:'a',name:'current'};
  complete();await pending;
  assert.equal(h.state.current.name,'current');assert.equal(h.rendered.length,0);assert.equal(h.notices.length,0);
});

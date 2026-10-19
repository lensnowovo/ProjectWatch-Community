import test from 'node:test';
import assert from 'node:assert/strict';
import {externalNavigation,materialSelection,internalReceiptSummary,compactStoryText} from '../src/project_watch/static/page_polish.mjs';

test('four external directions remain visible, with industry scopes combined only for navigation',()=>{
  const state={current:{id:'p'},external:{project_id:'p',categories:[
    {key:'sector',coverage:'checked',plans:[{last_success_at:1791288000}],items:[{id:'a',project_id:'p'}]},
    {key:'industry',coverage:'not_configured',plans:[],items:[]},
    {key:'company',coverage:'paused',plans:[],items:[]},
    {key:'competitors',coverage:'failed',plans:[],items:[]},
    {key:'policy',coverage:'checked',plans:[],items:[]}
  ]}};
  const before=JSON.stringify(state),groups=externalNavigation(state);
  assert.deepEqual(groups.map(g=>g.key),['company','industry','competitors','policy']);
  assert.deepEqual(groups[1].keys,['sector','industry']);
  assert.equal(groups[1].coverage,'partial');assert.match(groups[1].status,/部分/);
  assert.match(groups[0].status,/暂停/);assert.match(groups[2].status,/失败/);
  assert.match(groups[3].status,/未命中/);assert.ok(groups[1].lastSuccess.startsWith('2026-'));
  assert.equal(JSON.stringify(state),before);
  state.external.project_id='other';assert.ok(externalNavigation(state).every(g=>g.items.length===0&&g.coverage==='unavailable'));
});

test('material directory search is project-scoped, newest-first and topic-aware',()=>{
  const sources=[{id:'old',project_id:'p',reference:'旧 BP.pptx',topic:'产品与技术',created_at:'2026-01-01'},
    {id:'new',project_id:'p',reference:'月报.xlsx',topic:'财务表现',created_at:'2026-02-01'},
    {id:'foreign',project_id:'q',reference:'月报.xlsx',topic:'财务表现',created_at:'2026-03-01'}];
  assert.deepEqual(materialSelection(sources,'p').map(s=>s.id),['new','old']);
  assert.deepEqual(materialSelection(sources,'p',{query:'BP'}).map(s=>s.id),['old']);
  assert.deepEqual(materialSelection(sources,'p',{topic:'财务表现'}).map(s=>s.id),['new']);
  assert.deepEqual(materialSelection(sources,'p',{query:'没有'}),[]);
});

test('intake summary distinguishes parsing, failure and an initial readable document',()=>{
  const state={current:{id:'p'},sources:[{id:'s',project_id:'p'},{id:'other',project_id:'q'}],jobs:[{source_id:'s',project_id:'q',status:'failed'}]};
  assert.equal(internalReceiptSummary(state).total,1);
  state.jobs.push({source_id:'s',project_id:'p',status:'completed',created_at:'2026-01-01'});
  assert.match(internalReceiptSummary(state).copy,/第一份/);
  state.jobs.push({source_id:'s',project_id:'p',status:'running',created_at:'2026-02-01'});
  assert.equal(internalReceiptSummary(state).processing,1);
  state.jobs.push({source_id:'s',project_id:'p',status:'failed',created_at:'2026-03-01'});
  assert.equal(internalReceiptSummary(state).failed,1);
  assert.match(internalReceiptSummary(state).copy,/未完成/);
});

test('compact story keeps AI attribution and does not convert a hypothesis to a fact',()=>{
  const saved={analysis:{relationship:{assessment:'potentially_relevant',text:'若客户重合，可能竞争。'}}};
  assert.deepEqual(compactStoryText(saved,'摘要'),{label:'AI 关联 · 待核实',text:'若客户重合，可能竞争。'});
  assert.deepEqual(compactStoryText(null,'原消息摘要'),{label:'公开线索',text:'原消息摘要'});
});

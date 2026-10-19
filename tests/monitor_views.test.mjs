import test from 'node:test';
import assert from 'node:assert/strict';
import {companyReading} from '../src/project_watch/static/monitor_views.mjs';
const base={observations:[],sources:[],signals:[]};
test('reading draft never leaks from the previously selected project',()=>{
  const reading={project_id:'a',sections:[]};
  assert.equal(companyReading({...base,current:{id:'b'},reading}).reading,null);
  assert.equal(companyReading({...base,current:{id:'a'},reading}).reading,reading);
});
test('retained reading is hidden when its source or observation is withdrawn',()=>{
 const item={id:'o',text:'Saved excerpt',source_refs:[{source_id:'s'}]};
 const reading={project_id:'a',sections:[{title:'业务',items:[item]}]};
 const state={...base,current:{id:'a'},reading,sources:[{id:'s',created_at:'2026-10-04'}],observations:[{id:'o',statement:item.text,status:'candidate'}]};
 assert.equal(companyReading(state).reading,reading);
 assert.equal(companyReading({...state,sources:[]}).reading,null);
 assert.equal(companyReading({...state,observations:[{...state.observations[0],status:'rejected'}]}).reading,null);
});
test('unreviewed extraction never becomes the company introduction or business change',()=>{
  const data=companyReading({...base,observations:[{status:'candidate',subject:'经营情况',statement:'Do not present this as fact'}]});
  assert.equal(data.facts.length,0);assert.equal(data.pending,1);assert.equal(data.changes.length,0);
});
test('same-day materials are grouped; original names and source objects survive',()=>{
  const sources=[{id:'a',reference:'first.txt',created_at:'2026-10-03T01:00:00Z'},{id:'b',reference:'second.txt',created_at:'2026-10-03T02:00:00Z'}];
  const data=companyReading({...base,sources});
  assert.equal(data.changes.length,1);assert.equal(data.changes[0].kind,'materials');assert.equal(data.changes[0].sources.length,2);assert.equal(data.sources[0].id,'b');assert.equal(sources[0].id,'a');
});
test('public signals keep their distinct provenance and are sorted with material arrivals',()=>{
  const data=companyReading({...base,sources:[{id:'a',created_at:'2026-10-03'}],signals:[{title:'Public report',first_seen_at:'2026-10-04'}]});
  assert.equal(data.changes[0].kind,'news');assert.equal(data.changes[1].kind,'materials');
});

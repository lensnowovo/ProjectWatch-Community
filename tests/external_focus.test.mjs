import test from 'node:test';
import assert from 'node:assert/strict';
import {coverageText,focusPayload,categories} from '../src/project_watch/static/external_focus.mjs';
test('five public directions and truthful coverage states',()=>{
  assert.equal(Object.keys(categories).length,5);
  const states=['not_configured','paused','failed','pending','checked'];
  assert.equal(new Set(states.map(coverage=>coverageText({coverage}))).size,5);
  assert.match(coverageText({coverage:'failed'}),/失败/);
});
test('changing public term invalidates preview consent',()=>{
  const selection={category:'competitors',provider:'gdelt',term:'Synthetic Peer'};
  const preview={...selection,eligible:true,consent_version:'public-focus-v1'};
  assert.throws(()=>focusPayload(preview,selection,false));
  assert.throws(()=>focusPayload(preview,{...selection,term:'Other Peer'},true));
  assert.throws(()=>focusPayload({...preview,eligible:false},selection,true));
  assert.deepEqual(focusPayload(preview,selection,true),{...selection,consent_version:'public-focus-v1'});
});

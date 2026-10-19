import test from 'node:test';
import assert from 'node:assert/strict';
import {waitForBp} from '../src/project_watch/static/bp_intake.mjs';

test('waits for parsing before reading fragments; never calls model',async()=>{
  const calls=[];let reads=0;
  const request=async path=>{calls.push(path);if(path.endsWith('/intake'))return [{source_id:'s',status:++reads===1?'processing':'completed'}];return [{locator:'1',text:'Synthetic company'}];};
  assert.equal((await waitForBp({projectId:'p',sourceId:'s',request,sleep:async()=>{}})).length,1);
  assert.deepEqual(calls,['/api/projects/p/intake','/api/projects/p/intake','/api/projects/p/sources/s/fragments']);
});
test('failed/OCR parse, cancellation and timeout do not become ready',async()=>{
  for(const status of ['failed','needs_ocr'])await assert.rejects(waitForBp({projectId:'p',sourceId:'s',request:async()=>[{source_id:'s',status}]}));
  assert.equal(await waitForBp({cancelled:()=>true}),null);
  await assert.rejects(waitForBp({projectId:'p',sourceId:'s',request:async()=>[],sleep:async()=>{},attempts:2}),/后台/);
});

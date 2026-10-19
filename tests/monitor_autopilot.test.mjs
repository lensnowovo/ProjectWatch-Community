import test from 'node:test';
import assert from 'node:assert/strict';
import {selectedPacket,workflowCopy} from '../src/project_watch/static/monitor_autopilot.mjs';
test('only selected excerpts sent, no file name used as keyword',()=>{
 const source={id:'s',reference:'Private project name.pptx',content_sha256:'a'.repeat(64)};
 const packet=selectedPacket('p',source,[{selected:true,locator:'1',text:'Public product'},{selected:false,locator:'2',text:'Private customer finance'}]);
 assert.equal(packet.excerpts.length,1);assert.equal(packet.excerpts[0].text,'Public product');assert.equal(packet.keywords,undefined);
 assert.throws(()=>selectedPacket('p',source,[]));
 assert.throws(()=>selectedPacket('p',source,[{selected:true,locator:'1',text:'x'.repeat(6001)}]));
});
test('no results, partial failure and uncertain model response distinguished',()=>{
 assert.notEqual(workflowCopy('checked'),workflowCopy('partial_failure'));
 assert.match(workflowCopy('uncertain'),/未自动重复/);
 assert.match(workflowCopy('material_changed'),/换版/);
 assert.match(workflowCopy('strategy_changed'),/重新确认 BP/);
 assert.notEqual(workflowCopy('strategy_changed'),workflowCopy('queued'));
});

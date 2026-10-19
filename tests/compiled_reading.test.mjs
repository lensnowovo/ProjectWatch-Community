import test from 'node:test';
import assert from 'node:assert/strict';
import {compiledReading,readingStatusCopy} from '../src/project_watch/static/compiled_reading.mjs';
import {consentPayload} from '../src/project_watch/static/reading_consent.mjs';
const source={id:'s',project_id:'p',content_sha256:'hash'};
const item={topic:'business',title:'业务',text:'合成业务',as_of:null,origin:'ai_synthesis',review_status:'unreviewed',source_refs:[{source_id:'s',version:'hash',locator:'page:1',quote:'合成业务'}]};
const reading={project_id:'p',compiled:{project_id:'p',batches:[{source_id:'s',source_version:'hash',id:'b',recorded_at:'2026-10-04',items:[item]}]}};
test('compiled text stays attributed; version/project mismatch never leaks',()=>{
  assert.equal(compiledReading(reading,'p',[source])[0].items[0].review_status,'unreviewed');
  assert.deepEqual(compiledReading(reading,'other',[source]),[]);
  assert.deepEqual(compiledReading(reading,'p',[{...source,project_id:'other'}]),[]);
  assert.deepEqual(compiledReading(reading,'p',[{...source,content_sha256:'changed'}]),[]);
});
test('duplicate statements merge citations without modifying response or losing disagreement',()=>{
  const copy=structuredClone(reading);copy.compiled.batches[0].items.push(item,{...item,text:'不同描述'});
  const result=compiledReading(copy,'p',[source]);assert.equal(result[0].items.length,2);assert.equal(result[0].items[0].source_refs.length,1);
  assert.equal(copy.compiled.batches[0].items.length,3);
});
test('authorization requires explicit consent, scope and finite bound',()=>{
  const status={automatic_reading_available:true,rule_version:'company-reading-v2',model:{config_id:'config',max_output_tokens:3072}};
  const choice={hashes:['b','a','a'],includeFuture:false,maxRequests:2,consent:true,requestKey:'fixed'};
  assert.deepEqual(consentPayload(status,choice).source_hashes,['a','b']);
  assert.equal(consentPayload(status,choice).rule_version,status.rule_version);
  assert.equal(consentPayload(status,choice).max_output_tokens,3072);
  for(const change of [{consent:false},{hashes:[]},{maxRequests:0},{maxRequests:1.5},{maxRequests:Infinity}])assert.throws(()=>consentPayload(status,{...choice,...change}));
  assert.equal(consentPayload(status,{...choice,hashes:[],includeFuture:true}).include_future,true);
  assert.throws(()=>consentPayload({...status,model:null},choice));
  assert.throws(()=>consentPayload({...status,rule_version:null},choice));
  assert.throws(()=>consentPayload({...status,model:{config_id:'config'}},choice));
});
test('empty monitoring result is not used to describe an unavailable model',()=>{
  assert.match(readingStatusCopy({reason:'model_missing'}),/尚未连接 AI/);
  assert.match(readingStatusCopy({reason:'ready',counts:{uncertain:1}}),/未自动重复调用/);
  assert.match(readingStatusCopy({reason:'paused',counts:{queued:1}}),/暂停/);
});

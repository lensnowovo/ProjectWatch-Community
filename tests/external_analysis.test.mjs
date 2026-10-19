import test from 'node:test';
import assert from 'node:assert/strict';
import {analysisFor, analysisStatusCopy, externalConsentPayload,evidenceCoverage} from '../src/project_watch/static/external_analysis.mjs';

function fixture(){
  const item={id:'n',project_id:'p',title:'公开产品试验',summary:'公开报道摘要',published_at:'2026-10-04T00:00:00Z',url:'https://example.org/news',match_term:'合成竞品',category:'competitors'};
  const record={id:'r',recorded_at:1,packet:{project_id:'p',signal_id:'n',published_at:item.published_at,term:item.match_term,category:item.category,source_url:item.url,references:[
    {ref_id:'news:title',kind:'external',text:item.title}, {ref_id:'news:summary',kind:'external',text:item.summary},
    {ref_id:'material:0',kind:'internal',source_id:'s',source_version:'sha',text:'材料内容',locator:'第 1 段'}]},analysis:{project_id:'p',signal_id:'n',origin:'ai_synthesis',review_status:'unreviewed',relationship_verified:false,policy_applicability_verified:false,content_scope:'headline_and_feed_summary_only',support_status:'quotes_located_not_semantic_verification'}};
  return {item,record,state:{current:{id:'p'},sources:[{id:'s',project_id:'p',content_sha256:'sha'}],externalAnalysis:{project_id:'p',items:[record]}}};
}
test('only current project/story/material evidence can display analysis',()=>{
  const f=fixture();assert.equal(analysisFor(f.item,f.state).id,'r');
  for(const change of [f=>f.state.current.id='other',f=>f.item.title='更正后的标题',f=>f.item.summary='更正后的摘要',f=>f.item.url='https://other.test',f=>f.state.sources[0].content_sha256='new',f=>f.record.analysis.relationship_verified=true,f=>f.record.analysis.project_id='other',f=>f.state.externalAnalysisError=Error('offline')]){
    const x=fixture();change(x);assert.equal(analysisFor(x.item,x.state),null);
  }
});
test('latest valid analysis wins without mutating saved order',()=>{
  const f=fixture();f.state.externalAnalysis.items.push({...structuredClone(f.record),id:'new',recorded_at:2});
  assert.equal(analysisFor(f.item,f.state).id,'new');assert.equal(f.state.externalAnalysis.items[0].id,'r');
});

test('changed publication time cannot reuse an old interpretation',()=>{
  const f=fixture();f.record.packet.published_at='2026-10-04T00:00:00Z';
  f.item.published_at='2026-10-04T08:00:00+08:00';
  assert.equal(analysisFor(f.item,f.state).id,'r');
  f.item.published_at='2026-09-04T00:00:00Z';
  assert.equal(analysisFor(f.item,f.state),null);
});

test('newly available summary or missing date invalidates the displayed packet',()=>{
  const f=fixture();f.record.packet.references=f.record.packet.references.filter(r=>r.ref_id!=='news:summary');
  assert.equal(analysisFor(f.item,f.state),null);
  f.item.summary='';assert.equal(analysisFor(f.item,f.state).id,'r');
  delete f.record.packet.published_at;assert.equal(analysisFor(f.item,f.state),null);
});
test('explanation does not confuse source coverage, pause or unknown billing',()=>{
  assert.match(analysisStatusCopy({processing:{reason:'model_missing'}}),/连接 AI/);
  assert.match(analysisStatusCopy({processing:{reason:'scope_changed'}}),/背景材料|范围/);
  assert.match(analysisStatusCopy({processing:{reason:'paused',counts:{}}}),/暂停/);
  assert.match(analysisStatusCopy({processing:{reason:'ready',counts:{uncertain:1}}}),/未自动重试/);
  assert.match(analysisStatusCopy({processing:{reason:'ready'},automatic_analysis_available:false}),/未启动/);
});
test('consent binds the draft actually previewed and never includes secrets',()=>{
  const draft={plan_ids:['a'],selections:[],include_future_news:false,max_requests:2};
  const viewed={draft:JSON.stringify(draft),disclosure_digest:'d'.repeat(64)};
  assert.equal(externalConsentPayload(draft,viewed,true,'request-key').disclosure_digest,viewed.disclosure_digest);
  assert.throws(()=>externalConsentPayload({...draft,max_requests:20},viewed,true,'key'));
  assert.throws(()=>externalConsentPayload(draft,viewed,false,'key'));
  assert.throws(()=>externalConsentPayload(draft,null,true,'key'));
});

test('body analysis retains snapshot attribution and rejects changed paragraph version',()=>{
  const f=fixture(),p=f.record.packet,a=f.record.analysis;
  p.rule_version=a.rule_version='external-analysis-v2';p.content_scope=a.content_scope='article_excerpts';
  p.article={status:'partial_body',snapshot_digest:'b'.repeat(64)};
  p.references=p.references.filter(r=>r.ref_id!=='news:summary');
  p.references.push({kind:'external',ref_id:'article:p1:0',source_version:p.article.snapshot_digest,text:'Public report details'});
  assert.equal(analysisFor(f.item,f.state).id,'r');
  assert.match(evidenceCoverage(p),/部分正文/);
  p.references.at(-1).source_version='wrong';assert.equal(analysisFor(f.item,f.state),null);
  assert.match(evidenceCoverage({article:{status:'unavailable'}}),/正文暂不可用/);
});

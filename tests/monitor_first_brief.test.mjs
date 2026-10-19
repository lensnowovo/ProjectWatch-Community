import test from 'node:test';
import assert from 'node:assert/strict';
import {firstBriefItems,briefCoverageText,partitionFirstBrief,briefAnalysisStory} from '../src/project_watch/static/monitor_first_brief.mjs';

function fixture(){
  const news={id:'n',project_id:'p',title:'Company plans product launch',summary:'Not completed.',url:'https://example.org/news',category:'sector',published_at:'2026-10-06T00:00:00Z'};
  const item={...news,statement:news.title,content_scope:'headline_and_feed_summary_only',origin:'source_bound_projection',relationship:{kind:'monitoring_scope',verified:false},impact:null,evidence_refs:[{source_id:'s',version:'sha'}]};
  return {current:{id:'p'},sources:[{id:'s',project_id:'p',content_sha256:'sha'}],external:{project_id:'p',categories:[{items:[news]}],briefing:{project_id:'p',rule_version:'external-first-brief-v1',items:[item]}}};
}
test('brief only shows current source-bound stories, never mismatched evidence',()=>{
  assert.equal(firstBriefItems(fixture()).length,1);
  for(const change of [s=>s.current.id='other',s=>s.externalError=Error('offline'),s=>s.external.categories[0].items[0].title='Corrected',s=>s.external.briefing.items[0].impact='Guaranteed revenue',s=>s.sources[0].content_sha256='new',s=>s.external.briefing.items[0].relationship.verified=true]){
    const s=fixture();change(s);assert.deepEqual(firstBriefItems(s),[]);
  }
});
test('coverage distinguishes failed, empty, paused and unconfigured',()=>{
  const copy=['failed','pending','paused','not_configured','checked'].map(state=>briefCoverageText({state,item_count:0}));
  assert.equal(new Set(copy).size,5);
  assert.match(copy[0],/失败/);assert.match(copy[4],/未命中/);
});

function rankingFixture(){
  const state=fixture(),item=state.external.briefing.items[0],news=state.external.categories[0].items[0];
  news.match_term='Company';
  item.attention={version:'headline-topic-v1',basis:'headline_topic_only',event_verified:false,impact:null,level:'read_first',topic:'product',label:'产品与技术'};
  const other={...news,id:'other',title:'Company annual gathering',published_at:'2026-10-06T01:00:00Z'};
  state.external.categories[0].items.push(other);
  state.external.briefing.items.push({...item,...other,statement:other.title,attention:{...item.attention,level:'all_news',topic:'other'}});
  const record={id:'a',recorded_at:1,packet:{project_id:'p',signal_id:'n',term:'Company',category:news.category,source_url:news.url,published_at:news.published_at,references:[
    {ref_id:'news:title',kind:'external',text:news.title},
    {ref_id:'news:summary',kind:'external',text:news.summary},
    {ref_id:'material:0',kind:'internal',source_id:'s',source_version:'sha',text:'Material',locator:'Page 1'}]},
    analysis:{project_id:'p',signal_id:'n',origin:'ai_synthesis',review_status:'unreviewed',relationship_verified:false,policy_applicability_verified:false,content_scope:'headline_and_feed_summary_only',support_status:'quotes_located_not_semantic_verification',relationship:{assessment:'unclear'}}};
  state.externalAnalysis={project_id:'p',items:[record]};
  return state;
}

test('product lead precedes newer peripheral news; all items survive unchanged',()=>{
  const s=rankingFixture(),before=JSON.stringify(s),result=partitionFirstBrief(s);
  assert.deepEqual(result.primary.map(i=>i.id),['n']);
  assert.deepEqual(result.secondary.map(i=>i.id),['other']);
  assert.equal(JSON.stringify(s),before);
});
test('valid negative AI relationship folds but never removes a topic; stale AI cannot demote',()=>{
  const s=rankingFixture();s.externalAnalysis.items[0].analysis.relationship.assessment='not_relevant';
  assert.equal(partitionFirstBrief(s).primary.length,0);
  assert.equal(partitionFirstBrief(s).secondary.length,2);
  s.externalAnalysis.items[0].packet.references[2].source_version='old';
  assert.equal(partitionFirstBrief(s).primary.length,1);
  s.externalAnalysis.project_id='another';
  assert.equal(partitionFirstBrief(s).primary.length,1);
});
test('broad potential relationship alone cannot promote non-topic news',()=>{
  const s=rankingFixture();s.external.briefing.items[0].attention.level='all_news';
  s.externalAnalysis.items[0].analysis.relationship.assessment='potentially_relevant';
  assert.equal(partitionFirstBrief(s).primary.length,0);
  s.externalAnalysis.items[0].analysis.implications=[{hypothesis:'A supported mechanism',condition:'If the shared product is established',evidence:[{ref_id:'material:0'}]}];
  assert.equal(partitionFirstBrief(s).primary.length,1);
  s.externalAnalysisError=Error('unavailable');
  assert.equal(partitionFirstBrief(s).primary.length,0);
  assert.equal(partitionFirstBrief(s).secondary.length,2);
});

test('priority cap applies to groups; overflow and grouped reports are retained',()=>{
  const s=rankingFixture(),item=s.external.briefing.items[0],news=s.external.categories[0].items[0];
  for(let n=0;n<7;n++){
    s.external.categories[0].items.push({...news,id:`extra-${n}`});
    s.external.briefing.items.push({...item,id:`extra-${n}`});
  }
  s.external.briefing.report_groups=[{version:'report-groups-v1',status:'candidate',event_verified:false,member_ids:['n','extra-0','extra-1']}];
  const result=partitionFirstBrief(s);
  assert.equal(result.primary.length,5);
  assert.equal(result.secondary.length,2);
  const ids=[...result.primary,...result.secondary].flatMap(g=>g.reports.map(r=>r.id));
  assert.equal(ids.length,9);assert.equal(new Set(ids).size,9);
  assert.equal(result.primary.find(g=>g.reports.length===3).reports.length,3);
  s.external.briefing.report_groups[0].member_ids.push('foreign');
  assert.equal(partitionFirstBrief(s).primary.some(g=>g.reports.length>1),false);
});

test('cross-scope duplicate retains its own valid latest analysis without crossing content boundaries',()=>{
  const s=rankingFixture(),item=s.external.briefing.items[0],story=s.external.categories[0].items[0];
  const other={...story,id:'peer-signal',category:'competitors'};
  s.external.categories.push({items:[other]});
  const record=structuredClone(s.externalAnalysis.items[0]);
  record.packet.signal_id=record.analysis.signal_id='peer-signal';record.packet.category='competitors';record.recorded_at=2;
  s.externalAnalysis.items.push(record);
  assert.equal(briefAnalysisStory(item,s).id,'peer-signal');
  other.summary='Changed summary';
  assert.equal(briefAnalysisStory(item,s).id,'n');
  other.summary=story.summary;other.project_id='foreign';
  assert.equal(briefAnalysisStory(item,s).id,'n');
});

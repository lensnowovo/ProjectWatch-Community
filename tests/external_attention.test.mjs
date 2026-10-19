import test from 'node:test';
import assert from 'node:assert/strict';
import {readingNews, attentionFor, newsMode} from '../src/project_watch/static/external_attention.mjs';

const attention={version:'headline-topic-v1',basis:'headline_topic_only',level:'read_first',topic:'capital',label:'融资与资本',reason:'查看交易条款与资金安排。',event_verified:false,impact:null};
const make=(id,title,time,extra={})=>({id,project_id:'p',title,published_at:time,url:'https://example.org/news',...extra});
const a=make('a','Scholarship','2026-10-04');
const b=make('b','Denies fundraising','2026-10-03',{attention});
const data={project_id:'p',categories:[{items:[a,b]},{items:[b]}]};

test('reading priority keeps distinct stories on the same URL and does not mutate chronology',()=>{
  assert.deepEqual(readingNews(data,'p').map(i=>i.id),['b','a']);
  assert.deepEqual(readingNews(data,'p','all').map(i=>i.id),['a','b']);
  assert.deepEqual(data.categories[0].items.map(i=>i.id),['a','b']);
});
test('unknown rules and cross-project items are never promoted',()=>{
  assert.deepEqual(readingNews(data,'other'),[]);
  assert.equal(attentionFor({...b,attention:{...attention,version:'unrecognized'}}),null);
  assert.equal(attentionFor({...b,attention:{...attention,event_verified:true}}),null);
  assert.deepEqual(readingNews({project_id:'p',categories:[{items:[{...b,project_id:'other'}]}]},'p'),[]);
});
test('all-news mode survives same-project rerender but not a different project',()=>{
  assert.equal(newsMode({focusProject:'p',newsMode:'all'},'p'),'all');
  assert.equal(newsMode({focusProject:'p',newsMode:'all'},'other'),'priority');
  assert.equal(newsMode({},'p'),'priority');
});

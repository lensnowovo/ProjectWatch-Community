import test from 'node:test';
import assert from 'node:assert/strict';
import {parseRoute, primaryView, contentView, reduceNavigation} from '../src/project_watch/static/navigation.mjs';
import {companyReading} from '../src/project_watch/static/monitor_views.mjs';

test('four primary views retain existing deep links and internal chapters', () => {
  for (const tab of ['overview','internal','external','business','finance','materials','review','report','changes']) {
    assert.equal(parseRoute(`#project/p/${tab}`).tab, tab);
  }
  for (const tab of ['internal','business','finance','review','report','monitoring']) assert.equal(primaryView(tab),'internal');
  assert.equal(primaryView('materials'),'materials');
  assert.equal(primaryView('changes'),'external');
  assert.equal(primaryView('external'),'external');
  assert.equal(primaryView('overview'),'overview');
  assert.equal(contentView('external'),'changes');
  assert.equal(contentView('monitoring'),'business');
});

test('closing source preview preserves each project and reading perspective', () => {
  for (const projectId of ['a','b']) for (const tab of ['internal','external','finance']) {
    const state={route:'project',projectId,tab,panel:'source'};
    assert.deepEqual(reduceNavigation(state,{type:'close-panel'}),{...state,panel:null});
  }
});

test('material receipts are not external news or business changes', () => {
  const reading=companyReading({current:{id:'p'},observations:[],sources:[{id:'s',created_at:'2026-10-04'}],signals:[{title:'Public report',first_seen_at:'2026-10-03',url:'https://example.org/news'}]});
  assert.equal(reading.materialUpdates.length,1);
  assert.equal(reading.externalUpdates.length,1);
  assert.equal(reading.externalUpdates[0].kind,'news');
  assert.ok(reading.externalUpdates.every(item=>!item.sources));
});

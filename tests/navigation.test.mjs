import test from 'node:test';
import assert from 'node:assert/strict';
import { reduceNavigation, parseRoute, routeHash,primaryView,contentView } from '../src/project_watch/static/navigation.mjs';
test('materials is a primary view and changes keeps its existing external route',()=>{
 assert.equal(primaryView('materials'),'materials');assert.equal(primaryView('finance'),'internal');assert.equal(primaryView('business'),'internal');assert.equal(primaryView('internal'),'internal');assert.equal(primaryView('changes'),'external');assert.equal(contentView('external'),'changes');
});
test('closing a panel preserves project and tab', () => {
  const s = { route:'project', projectId:'a', tab:'materials', panel:'source' };
  assert.deepEqual(reduceNavigation(s, { type:'close-panel' }), {...s, panel:null});
});
test('project switch clears source and does not select an executor page', () => {
  const s = { route:'project', projectId:'a', tab:'materials', panel:'source' };
  assert.deepEqual(reduceNavigation(s, {type:'project', id:'b'}), {route:'project',projectId:'b',tab:'overview',panel:null});
});
test('routes round trip and reject unrecognized tabs', () => {
  assert.equal(parseRoute(routeHash({route:'project',projectId:'a',tab:'materials'})).tab, 'materials');
  assert.equal(parseRoute('#project/a/unknown').tab, 'overview');
  assert.equal(parseRoute('#settings').route, 'settings');
  assert.equal(parseRoute('#bad').route, 'home');
  for (const tab of ['changes','business','finance']) assert.equal(parseRoute(`#project/a/${tab}`).tab,tab);
});

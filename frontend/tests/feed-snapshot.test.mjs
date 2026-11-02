import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

const source = readFileSync(new URL('../src/feedSnapshot.ts', import.meta.url), 'utf8');
const js = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.ESNext}}).outputText;
const {feedChanged} = await import('data:text/javascript;base64,' + Buffer.from(js).toString('base64'));
const event = {id:'same', title:'Synthetic', status:'new', published_at:null, importance:'routine',
  importance_status:'pending', sources: [{id:'source',url:'https://example.test/1'}]};

test('same-ID date, rating, correction, evidence and read status produce staged updates', () => {
  for (const patch of [{published_at:'2026-10-29'}, {importance:'major'}, {importance_status:'assessed'},
    {title:'Corrected'}, {status:'read'}, {sources:[{id:'source',url:'https://example.test/corrected'}]}]) {
    assert.equal(feedChanged([event], [{...event,...patch}]), true);
  }
});
test('removed and added events are detected, equivalent snapshots stay quiet', () => {
  assert.equal(feedChanged([event], []), true);
  assert.equal(feedChanged([], [event]), true);
  assert.equal(feedChanged([event], [{...event}]), false);
  const reordered = Object.fromEntries(Object.entries(event).reverse());
  assert.equal(feedChanged([event], [reordered]), false);
  assert.equal(feedChanged([event,{...event,id:'two'}], [{...event,id:'two'},event]), false);
});

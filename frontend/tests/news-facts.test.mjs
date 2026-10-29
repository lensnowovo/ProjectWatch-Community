import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

const source = readFileSync(new URL('../src/newsFacts.ts', import.meta.url), 'utf8');
const js = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.ESNext}}).outputText;
const {newsDate, inNewsWindow, newsSummary} = await import('data:text/javascript;base64,' + Buffer.from(js).toString('base64'));
const now = Date.parse('2026-10-29T12:00:00Z');
const item = (published_at, importance = 'routine', importance_status = 'assessed') => ({published_at, importance, importance_status, collected_at: '2026-10-29T11:00:00Z'});

test('collection time cannot make unknown, old, invalid or future dates recent', () => {
  for (const date of [null, '', 'invalid', '2026-02-30', '2025-01-01', '2026-10-30']) {
    assert.equal(inNewsWindow(item(date), 30, now), false, String(date));
  }
  assert.equal(newsDate(item('2026-02-30')), null);
  assert.equal(inNewsWindow(item('2026-10-28T16:00:00+08:00'), 30, now), true);
  assert.equal(inNewsWindow(item('2026-09-29T12:00:00Z'), 30, now), true);
  assert.equal(inNewsWindow(item('2026-09-29T11:59:59Z'), 30, now), false);
});

test('pending is distinct from routine, with date and assessment coverage separate', () => {
  const data = [item(null, 'routine', 'pending'), item('2026-10-28','routine','pending'),
    item('2026-10-28','major'), item('2026-10-27','notable'), item('2026-10-25'), item('2025-01-01','major')];
  const result = newsSummary(data, now);
  assert.equal(result.recent.length, 4);
  assert.deepEqual([result.major,result.notable,result.pending,result.recentPending,result.undated], [1,1,2,1,1]);
});

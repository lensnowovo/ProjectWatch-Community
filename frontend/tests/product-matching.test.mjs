import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

test('competitor refresh keeps search/read budgets without a model-work completion ceiling', () => {
  const view = readFileSync(new URL('../src/landscape.tsx', import.meta.url), 'utf8');
  const backend = readFileSync(new URL('../../src/project_watch/studio/pipeline.py', import.meta.url), 'utf8');
  assert.match(view, /最多24次搜索、21次公开页面读取、6次公开临床登记查询；每家公司最多3页/);
  assert.match(backend, /'competition': \{'search': 24, 'model': None, 'open_data': 6, 'page': 21\}/);
  assert.doesNotMatch(view, /12次公开页面读取/);
});

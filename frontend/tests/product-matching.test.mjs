import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

test('competitor refresh explains scope and cost recording without application quotas', () => {
  const view = readFileSync(new URL('../src/landscape.tsx', import.meta.url), 'utf8');
  const backend = readFileSync(new URL('../../src/project_watch/studio/pipeline.py', import.meta.url), 'utf8');
  assert.doesNotMatch(view, /最多24次搜索|每家公司最多3页/);
  assert.match(view, /费用/);
  assert.doesNotMatch(backend, /budgets\.get\(job\['kind'\]\)/);
  assert.match(backend, /execution\.allowed_call_kinds/);
  assert.doesNotMatch(view, /12次公开页面读取/);
});

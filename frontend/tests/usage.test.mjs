import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const settings = readFileSync(new URL('../src/pages/Settings.tsx', import.meta.url), 'utf8');
test('usage labels HTTP attempts and partial/legacy accounting without implying zero actual use', () => {
  assert.match(settings, /模型请求次数/);
  assert.match(settings, /已记录输入 tokens/);
  assert.match(settings, /已记录输出 tokens/);
  assert.match(settings, /用量不全/);
  assert.match(settings, /旧版汇总/);
  assert.match(settings, /缓存读取/);
  assert.doesNotMatch(settings, /模型调用次数/);
});

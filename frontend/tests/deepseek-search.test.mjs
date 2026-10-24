import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const settings = readFileSync(new URL('../src/pages/Settings.tsx', import.meta.url), 'utf8');
const setup = readFileSync(new URL('../src/pages/Setup.tsx', import.meta.url), 'utf8');
test('DeepSeek is added without removing any existing search provider', () => {
  for (const source of [settings, setup]) {
    for (const provider of ['deepseek', 'bocha', 'tavily', 'brave'])
      assert.match(source, new RegExp(`id: "${provider}"`));
  }
  assert.match(settings, /search_deepseek: "DeepSeek Search"/);
  assert.match(settings, /结果不明则停止/);
  assert.match(settings, /已启用的 DeepSeek 优先/);
});
test('new setup recommends DeepSeek without overwriting saved provider selection', () => {
  assert.match(setup, /useState\("deepseek"\)/);
  assert.match(setup, /s\.search\.providers\?\.\[0\]/);
  assert.match(setup, /searchKey, setSearchKey/);
});

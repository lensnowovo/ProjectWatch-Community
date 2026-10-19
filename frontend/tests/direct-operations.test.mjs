import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const api = readFileSync(new URL('../src/api.ts', import.meta.url), 'utf8');
const ui = readFileSync(new URL('../src/ui.tsx', import.meta.url), 'utf8');
const tasks = readFileSync(new URL('../src/pages/Tasks.tsx', import.meta.url), 'utf8');
const settings = readFileSync(new URL('../src/pages/Settings.tsx', import.meta.url), 'utf8');

test('interactive errors keep operation identity and require explicit cost consent', () => {
  assert.match(api, /data\?\.code === "operation_held"/);
  assert.match(api, /error\.operationId = data\.operation_id/);
  assert.match(ui, /window\.confirm\("上次请求可能已经计费/);
  assert.match(ui, /await api\.retryDirectOperation\(held\.operationId\)/);
  assert.match(ui, /已允许重试，请再次点击原来的操作按钮/);
  assert.doesNotMatch(ui, /await api\.(?:testSettings|resolve)\(/);
  assert.match(settings, /const test = async \(\) => \{[\s\S]*?setSaveError\(null\);[\s\S]*?setTesting\(true\)/);
});

test('partial results are neither labelled success nor counted as successful filter rows', () => {
  assert.match(tasks, /j\.runtime\?\.state !== "partial"/);
  assert.match(tasks, /title=\{j\.runtime\.label\}>部分完成/);
  assert.match(tasks, /服务请求待确认/);
});

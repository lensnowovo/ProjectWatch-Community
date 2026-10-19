import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const monitor = readFileSync(new URL('../src/monitor.tsx', import.meta.url), 'utf8');
const overview = readFileSync(new URL('../src/overview.tsx', import.meta.url), 'utf8');
const api = readFileSync(new URL('../src/api.ts', import.meta.url), 'utf8');

test('tracked questions keep system, human and Agent judgements apart', () => {
  assert.match(monitor, /system: "系统判断", human: "你的判断", agent: "外部助手"/);
  assert.match(monitor, /不会覆盖你的判断/);
  assert.match(monitor, /q\.origin === "ai" \? "AI 建议" : "你添加"/);
});

test('a person can write a judgement and see the history', () => {
  assert.match(api, /judgeQuestion: \(id: string, watchId: string/);
  assert.match(api, /\/questions\/\$\{watchId\}\/assessments/);
  assert.match(monitor, /写下你的判断/);
  assert.match(monitor, /questionHistory/);
});

test('the overview shows the questions card', () => {
  assert.match(overview, /<QuestionsCard projectId=\{project\.id\}/);
});

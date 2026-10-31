import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const project = readFileSync(new URL('../src/pages/Project.tsx', import.meta.url), 'utf8');
const round = readFileSync(new URL('../src/roundView.tsx', import.meta.url), 'utf8');
const ui = readFileSync(new URL('../src/ui.tsx', import.meta.url), 'utf8');
test('the project page shows the round, not cross-task recovery', () => {
  assert.doesNotMatch(project, /RecoveryDialog|api\.recover/);
  assert.match(project, /<RoundProgress/);
  assert.doesNotMatch(ui, /明天会自动继续/);
});

test('re-analysis reuses saved material and is confirmed before any possible duplicate charge', () => {
  assert.match(round, /只使用已收录的材料，不重新检索/);
  assert.match(round, /可能重复计费/);
  assert.match(round, /setConfirming\(true\)/);
  assert.match(round, /onClick=\{run\}/);
  assert.match(round, /data\.can_reanalyze &&/);
  assert.match(round, /已收录的内容保留|已收录材料保留/);
});

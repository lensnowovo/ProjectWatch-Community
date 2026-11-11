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

test('a settled round folds to one line; running, failed or redo-able rounds stay open', () => {
  assert.match(round, /if \(!data\.collect\) return null;/);
  assert.match(round, /if \(data\.running \|\| data\.can_reanalyze \|\| failed\)/);
  assert.match(round, /部分来源暂不可用/);
  assert.match(round, /<details className="round-view-fold"/);
  const overview = readFileSync(new URL('../src/overview.tsx', import.meta.url), 'utf8');
  assert.doesNotMatch(overview, /title="本期最重要的变化"/);
  const operational = readFileSync(new URL('../src/Operational.tsx', import.meta.url), 'utf8');
  assert.match(operational, /\(isMoving\(work\.state\) \|\| needsAttention\(work\.state\)\) && <TaskActivity/);
});

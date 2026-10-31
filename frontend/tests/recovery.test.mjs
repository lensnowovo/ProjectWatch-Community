import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const dialog = readFileSync(new URL('../src/RecoveryDialog.tsx', import.meta.url), 'utf8');
const project = readFileSync(new URL('../src/pages/Project.tsx', import.meta.url), 'utf8');
const round = readFileSync(new URL('../src/roundView.tsx', import.meta.url), 'utf8');
const ui = readFileSync(new URL('../src/ui.tsx', import.meta.url), 'utf8');
test('recovery is an explicit centered cost confirmation, not a read side effect', () => {
  assert.match(dialog, /恢复可能再次计费/);
  assert.match(dialog, /确认重新请求/);
  assert.match(dialog, /role="dialog" aria-modal="true"/);
  assert.match(dialog, /onClick=\{submit\}/);
  assert.match(dialog, /api\.recoveryPreview/);
  assert.match(dialog, /actionKey\.current/);
  assert.match(dialog, /previous\.focus\(\)/);
  // The project page no longer offers cross-task recovery; it shows the round and a confirmed re-analysis.
  assert.doesNotMatch(project, /RecoveryDialog|api\.recover/);
  assert.match(project, /<RoundProgress/);
  assert.doesNotMatch(project, /api\.refresh\(detail\.project\.id, failed\.kind\)/);
  assert.doesNotMatch(ui, /明天会自动继续/);
});

test('recovery locates receipt owners rather than only the latest failed task', () => {
  assert.match(dialog, /api\.recoveryProject/);
  assert.match(dialog, /api\.recover\(projectId, state\.job_id/);
  assert.match(dialog, /历史任务已完成，仍有待处理请求/);
  assert.match(dialog, /不是原 HTTP 请求的逐字重放/);
});

test('re-analysis reuses saved material and is confirmed before any possible duplicate charge', () => {
  assert.match(round, /只使用已收录的材料，不重新检索/);
  assert.match(round, /可能重复计费/);
  assert.match(round, /setConfirming\(true\)/);
  assert.match(round, /onClick=\{run\}/);
  assert.match(round, /data\.can_reanalyze &&/);
  assert.match(round, /已收录的内容保留|已收录材料保留/);
});

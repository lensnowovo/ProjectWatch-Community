import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';
import test from 'node:test';

test('runtime UI distinguishes stalled work and provider waits without an invented percentage', () => {
  const tasks = readFileSync(new URL('../src/pages/Tasks.tsx', import.meta.url), 'utf8');
  const project = readFileSync(new URL('../src/pages/Project.tsx', import.meta.url), 'utf8');
  assert.match(tasks, /runtime\?\.restart_required/);
  assert.match(tasks, /runtime\?\.label/);
  assert.match(tasks, /runtime\?\.progress_at/);
  assert.match(tasks, /runtime\?\.state === "stalled"/);
  assert.match(project, /health\?\.restart_required/);
  assert.match(project, /查看运行状态/);
});

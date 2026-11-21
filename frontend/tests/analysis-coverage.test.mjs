import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const round = readFileSync(new URL('../src/roundView.tsx', import.meta.url), 'utf8');
const bundle = readFileSync(new URL('../../src/project_watch/web/assets/app.js', import.meta.url), 'utf8');

test('an incremental round says what it carried forward and what it read anew', () => {
  assert.match(round, /c\.carried_fragments == null \|\| c\.new_fragments == null/);
  assert.match(round, /已处理 \$\{c\.processed_fragments\} \/ \$\{c\.fragments\} 段/);  // a full traversal reads as before
  assert.match(round, /沿用已发布判断 \$\{c\.carried_fragments\} 段 · 新读 \$\{c\.drafted_fragments \?\? 0\} \/ \$\{c\.new_fragments\} 段/);
  assert.match(bundle, /沿用已发布判断/);  // the shipped interface was rebuilt from this source
});

test('a round total with missing usage receipts reads as a lower bound, never as complete', () => {
  assert.match(round, /f\.model_usage_missing\s*\?\s*`至少 \$\{f\.model_input_tokens\.toLocaleString\(\)\}（\$\{f\.model_usage_missing\} 次无用量回执）`/);
  assert.match(bundle, /次无用量回执/);  // the shipped interface was rebuilt from this source
});

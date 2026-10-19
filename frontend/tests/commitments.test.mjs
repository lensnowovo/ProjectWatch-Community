import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

test('public promises use stored evidence with honest absence and source links', () => {
  const text = fs.readFileSync(new URL('../src/commitments.tsx', import.meta.url), 'utf8');
  assert.match(text, /api\.commitments/);
  assert.match(text, /原话出处/);
  assert.match(text, /后续出处/);
  // No sourced promise: the card is hidden rather than showing a placeholder; read failures still surface.
  assert.match(text, /if \(!error && !data\?\.truncated && !data\?\.items\.length\) return null/);
  assert.match(text, /公开承诺读取失败/);
  assert.doesNotMatch(text, /待核实|待核验|请核实/);
});

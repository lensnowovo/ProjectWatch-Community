import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

test('monitoring scope shares the project tab width, without a legacy inner cap', () => {
  const css = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8');
  const plan = readFileSync(new URL('../src/plan.tsx', import.meta.url), 'utf8');
  assert.match(plan, /className="plan-view outline tab-frame"/);
  const rules = [...css.matchAll(/\.plan-view\.outline(?:\.tab-frame)?\s*\{([^}]+)\}/g)].map(m => m[1]);
  assert.ok(rules.length);
  for (const rule of rules) assert.doesNotMatch(rule, /max-width:\s*\d+px/);
  assert.ok(rules.some(rule => /grid-template-columns:\s*minmax\(0,\s*1fr\)/.test(rule)));
  assert.match(css, /\.tab-frame\s*\{[^}]*width:\s*100%[^}]*min-width:\s*0/);
  assert.match(css, /\.table-wrap\s*\{[^}]*overflow-x:\s*auto/);
});

import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

test('judgment tiles keep individual glows and crossfade opacity in place',()=>{
  const view=readFileSync(new URL('../src/questionAnalysis.tsx',import.meta.url),'utf8');
  const css=readFileSync(new URL('../src/styles.css',import.meta.url),'utf8');
  assert.match(view,/<i className="rj-tile-glow" aria-hidden="true" \/>/);
  assert.doesNotMatch(view,/layoutId="rj-tile-glow"/);
  assert.match(css,/\.rj-tile-glow \{[^}]*opacity: 0; transition: opacity \.24s ease;/);
  assert.match(css,/\.rj-tile\[aria-selected="true"\] > \.rj-tile-glow \{ opacity: 1; \}/);
  assert.match(css,/@media \(prefers-reduced-motion: reduce\) \{ \.rj-tile-glow \{ transition: none;/);
});

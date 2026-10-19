import { readFileSync } from 'node:fs';
import { strict as assert } from 'node:assert';
import test from 'node:test';

const read = (name) => readFileSync(new URL(`../src/${name}`, import.meta.url), 'utf8');
test('external Agent results keep attribution across ordinary and deep reading', () => {
  assert.match(read('overview.tsx'), /brief\.origin\?\.producer/);
  assert.match(read('overview.tsx'), /profile\?\.origin/);
  assert.match(read('judgment.tsx'), /record\?\.origin\?\.producer/);
  assert.match(read('overview.tsx'), /record\?\.origin\?\.producer/);
  assert.match(read('items.tsx'), /item\.extra\.agent_result/);
  assert.match(read('api.ts'), /extra: \{ \.\.\.e\.sources\[0\]\?\.extra/);
  assert.match(read('api.ts'), /date_basis/);
  assert.match(read('pages/Project.tsx'), /本批由外部 Agent 写回/);
});
test('public profile sources link to the original, not an empty private file ID', () => {
  assert.match(read('ui.tsx'), /r\.url \? <a/);
  assert.match(read('ui.tsx'), /搜索摘录/);
});

test('company news renders an Agent summary without legacy outlook fields', () => {
  assert.match(read('landscape.tsx'), /detail\.brief\.origin\?\.producer/);
  assert.match(read('landscape.tsx'), /!stale && detail\.brief\?\.summary/);
  assert.match(read('landscape.tsx'), /<p className="side-text">\{detail\.brief\.summary\}<\/p>/);
});

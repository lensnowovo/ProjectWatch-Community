import assert from 'node:assert/strict';
import { createRequire, Module } from 'node:module';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
const require = createRequire(new URL('../package.json', import.meta.url));
const entry = fileURLToPath(new URL('../src/TopicResearch.tsx', import.meta.url)), mod = new Module(entry);
mod.paths = require.resolve.paths('react');
mod._compile(require('esbuild').buildSync({ entryPoints: [entry], bundle: true, write: false,
  platform: 'node', format: 'cjs', packages: 'external', loader: { '.css': 'empty' } }).outputFiles[0].text, entry);
const render = view => require('react-dom/server').renderToStaticMarkup(
  require('react').createElement(mod.exports.TopicResult, { view }));
const base = { job: { id: 'synthetic', status: 'done', created_at: '2026-10-26T00:00:00Z', finished_at: null },
  question: { text: 'Previous question' }, scope_changed: true, result: { status: 'partial', answer_status: 'insufficient',
    text: '', gaps: ['One source unavailable'], sources: [] } };
test('partial and changed question remain visible without claiming an answer or offering retries', () => {
  const html = render(base);
  assert.match(html, /暂未形成回答/); assert.match(html, /本轮部分完成/);
  assert.match(html, /Previous question/); assert.match(html, /<details><summary>查看未完成项/);
  assert.doesNotMatch(html, /重新运行|自动重试|已回答/);
});
test('references are explicit expandable evidence and unsafe URLs are never links', () => {
  const html = render({ ...base, result: { ...base.result, answer_status: 'answered', text: 'Synthetic answer', sources: [
    { ref: 'N1', title: 'Original', url: 'https://example.org/evidence', context: 'Original excerpt', context_kind: '搜索摘要' },
    { ref: 'N2', title: 'Unsafe', url: 'javascript:alert(1)', context: '<script>synthetic</script>', context_kind: '搜索摘要' }] } });
  assert.match(html, /<details><summary>查看依据（2）/); assert.match(html, /https:\/\/example.org\/evidence/);
  assert.doesNotMatch(html, /javascript:|<script>|onmouseover/); assert.match(html, /&lt;script&gt;/);
});
test('no job and failed job are distinct, and failure links to existing task review', () => {
  assert.match(render({ ...base, job: null, result: null }), /尚未单独研究/);
  const html = render({ ...base, job: { ...base.job, status: 'failed' }, result: null });
  assert.match(html, /本轮未完成/); assert.match(html, /href="\/tasks"/);
  assert.doesNotMatch(html, /本题研究结果/);
});

test('separate assessment pending and stale knowledge preserve the previous answer', () => {
  const html = render({...base, scope_changed: false, assessment_job: {...base.job, status:'queued'},
    result: {...base.result, status:'pending', answer_status:'answered', knowledge_state:'needs_recheck',
      text:'Previous answer remains', closure:'owner_confirmation_required'}});
  assert.match(html, /问题评估：已排队/);
  assert.match(html, /旧回答待复核/);
  assert.match(html, /Previous answer remains/);
  assert.match(html, /评估尚未完成/);
  assert.match(html, /等待所有者确认/);
});

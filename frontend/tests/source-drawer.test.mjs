import assert from 'node:assert/strict';
import { createRequire, Module } from 'node:module';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
const require = createRequire(new URL('../package.json', import.meta.url));
const { buildSync } = require('esbuild'), React = require('react'), { renderToStaticMarkup } = require('react-dom/server');
function load(path) {
  const entry = fileURLToPath(new URL(path, import.meta.url));
  const mod = new Module(entry);
  mod.paths = require.resolve.paths('react');
  mod._compile(buildSync({ entryPoints: [entry], bundle: true, write: false, platform: 'node', format: 'cjs', packages: 'external', loader: { '.css': 'empty' } }).outputFiles[0].text, entry);
  return mod.exports;
}
const { createLatest } = load('../src/latestRequest.ts');
const { AnnotatedSummary, CompetitionReading, TechnologyJudgment } = load('../src/judgment.tsx');

test('technology detail keeps the same annotation and history status as judgment', () => {
  const html = renderToStaticMarkup(React.createElement(TechnologyJudgment, {view:{record:{total:1,generated_at:'2026-11-11',analysis:{technology:{route:'合成路线',maturity:'advanced',consensus:'forming',summary:'收入待核实',annotations:[{span:'收入待核实',code:'income'}],state:'stale',academic:[],industry:[],risks:[]}}},labels:{maturity:{advanced:'先进'},consensus:{forming:'形成中'}}}}));
  assert.match(html,/<mark/); assert.match(html,/本轮未更新，显示历史版本/);
});

test('distinct annotation spans are all focusable and retain the sentence', () => {
  const html = renderToStaticMarkup(React.createElement(AnnotatedSummary, {
    text: '收入待核实，订单待核实，收入待核实。',
    notes: [{span:'收入待核实',code:'income'}, {span:'订单待核实',code:'orders'}],
  }));
  assert.equal((html.match(/<mark/g)||[]).length,3);
  assert.equal((html.match(/tabindex="0"/g)||[]).length,3);
  assert.match(html,/核验标注/);
});

test('a qualitative competition window does not invent a shrinking trend', () => {
  const html = renderToStaticMarkup(React.createElement(CompetitionReading, {position:{competition_window:{months:null,basis:'双方仍处于验证阶段'},competition_signals:[]}}));
  assert.match(html,/双方仍处于验证阶段/);assert.doesNotMatch(html,/正在收窄|个月/);
});

test('a slow earlier reply cannot overwrite a newer source', async () => {
  const latest = createLatest();
  let shown = null;
  const open = (name, promise) => {
    const seq = latest.next();
    shown = name; // the title always follows the newest click
    promise.then((text) => { if (latest.isCurrent(seq)) shown = text; });
  };
  let releaseA, releaseB;
  const a = new Promise((r) => releaseA = r), b = new Promise((r) => releaseB = r);
  open('A', a);
  open('B', b);
  releaseB('B-text');
  await b;
  releaseA('A-text'); // late, and must be dropped
  await a;
  await Promise.resolve();
  assert.equal(shown, 'B-text');
});

test('a failed earlier request does not replace the newer error state', async () => {
  const latest = createLatest();
  let error = null;
  const open = (promise) => {
    const seq = latest.next();
    error = null;
    promise.catch((cause) => { if (latest.isCurrent(seq)) error = cause.message; });
  };
  const a = Promise.reject(new Error('older failure'));
  open(a);
  open(new Promise(() => {})); // newer request still pending
  await a.catch(() => {});
  await Promise.resolve();
  assert.equal(error, null);
});

test('every occurrence of a neutralised clause is marked, not just the first', () => {
  const notes = [{ span: '收入确认情况待核实', code: 'recognition_not_supported', original: '由于没有确认收入' }];
  const html = renderToStaticMarkup(React.createElement(AnnotatedSummary, {
    text: '收入确认情况待核实，订单仍在交付；收入确认情况待核实，不改判断。', notes,
  }));
  assert.equal(html.split('<mark').length - 1, 2);
  assert.match(html, /订单仍在交付/);
});

test('an unannotated summary stays plain text', () => {
  const html = renderToStaticMarkup(React.createElement(AnnotatedSummary, { text: '公司领先。', notes: [] }));
  assert.doesNotMatch(html, /<mark/);
  assert.match(html, /公司领先。/);
});

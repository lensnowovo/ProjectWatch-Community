import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';
import test from 'node:test';
import { createRequire, Module } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(new URL('../package.json', import.meta.url));
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { StaticRouter } = require('react-router-dom');
const entry = fileURLToPath(new URL('../src/questionAnalysis.tsx', import.meta.url));
const component = new Module(entry);
component.paths = require.resolve.paths('react');
component._compile(require('esbuild').buildSync({ entryPoints: [entry], bundle: true, write: false,
  platform: 'node', format: 'cjs', packages: 'external', loader: { '.css': 'empty' } }).outputFiles[0].text, entry);
const render = view => renderToStaticMarkup(React.createElement(StaticRouter, null,
  React.createElement(component.exports.QuestionAnalysis, {
    detail: { project: { id: 'synthetic' }, capabilities: { insight_generation: false, insight_projections: false } },
    view, error: null, onStart() {}, goto() {},
  })));

const read = name => readFileSync(new URL(`../src/${name}`, import.meta.url), 'utf8');
test('missing generator cannot offer start; saved analysis remains readable', () => {
  const src = read('questionAnalysis.tsx');
  assert.match(src, /generation_available === true/);
  assert.match(src, /generation_available === false && !analysis/);
  assert.match(src, /onStart && canGenerate && view\?\.model_ready/);
  assert.match(src, /insight_projections !== true/);
});

test('rendered Core empty state offers no generator even with a configured model', () => {
  const html = render({ generation_available: false, model_ready: true, record: null });
  assert.match(html, /暂无判断记录/);
  assert.doesNotMatch(html, /开始 AI 交叉分析|选择问题|下次 AI 交叉分析/);
});

test('rendered historical Agent judgment keeps evidence without private projections', () => {
  const html = render({ generation_available: false, model_ready: true,
    labels: { position: { on_par: '相当' } },
    record: { analysis: { position: { verdict: 'on_par', summary: '合成历史结论', confidence: 'low',
      for: [{ text: '合成证据', refs: [] }], against: [], change_if: [] } } },
  });
  assert.match(html, /合成历史结论/);
  assert.match(html, /已保存的判断/);
  assert.match(html, /查看全部依据/);
  assert.doesNotMatch(html, /开始 AI 交叉分析|下次 AI 交叉分析时生成/);
});

test('full installation can still offer generation', () => {
  assert.match(render({ generation_available: true, model_ready: true, record: null }), /开始 AI 交叉分析/);
});
test('Core opens overview and does not mount private remote projections', () => {
  const src = read('pages/Project.tsx');
  assert.match(src, /insight_generation === true \? "brief" : "overview"/);
  assert.match(src, /insight_projections === true && <RaceBoard/);
  assert.match(src, /const projections=detail\.capabilities\?\.insight_projections===true/);
  assert.match(src, /projections\?<IndustryFrame/);
  assert.match(read('overview.tsx'), /insight_projections === true\s*\? <PositionLine/);
  assert.match(read('landscape.tsx'), /insight_projections === true && <RedFlags/);
});

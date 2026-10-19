import test from 'node:test';
import assert from 'node:assert/strict';
import {renderDashboardCharts, selectDashboardSeries} from '../src/project_watch/static/dashboard_charts.mjs';

const HASH = 'a'.repeat(64);
const source = (id = 's1', project_id = 'p1', hash = HASH) => ({id, project_id, reference: `${id}.xlsx`, content_sha256: hash});
const point = (id, period, number, patch = {}) => ({
  observation_id: id, observation_version: 2, period, number,
  source_id: 's1', source_reference: 'ignored.xlsx', source_sha256: HASH,
  source_locator: `sheet!${id}`, same_period_conflict: false, ...patch,
});
const series = (name, points, patch = {}) => ({
  subject: '财务表现', name, unit: '万元', scope: '合并', basis: 'actual', cadence: 'year',
  total_points: points.length, truncated_points: false, points, ...patch,
});
const observation = (id, period, number, patch = {}) => ({
  id, project_id: 'p1', source_id: 's1', subject: '财务表现', statement: '营业收入', number,
  unit: '万元', period, scope: '合并', source_locator: `sheet!${id}`,
  status: 'confirmed', version: 2, basis: 'actual', ...patch,
});
const fixture = () => ({
  current: {id: 'p1'}, sources: [source()], observations: [], metricError: null,
  metricSeries: {series: [series('营业收入', [point('o1', '2024', '10'), point('o2', '2025', '-3.50')])], total_series: 1},
});

test('selects only active-project ACTUAL finance points with a live source version', () => {
  const state = fixture();
  state.metricSeries.series.push(series('预测', [point('f', '2026', '20')], {basis: 'forecast'}));
  state.metricSeries.series.push(series('客户数', [point('c', '2025', '2')], {subject: '经营情况'}));
  state.metricSeries.series.push(series('过期', [point('x', '2025', '9', {source_sha256: 'b'.repeat(64)})]));
  state.sources.push(source('foreign', 'p2'));
  state.metricSeries.series.push(series('跨项目', [point('z', '2025', '1', {source_id: 'foreign'})]));
  const selected = selectDashboardSeries(state);
  assert.equal(selected.length, 1);
  assert.equal(selected[0].name, '营业收入');
  assert.deepEqual(selected[0].points.map(row => row.valueText), ['10', '-3.50']);
});

test('rechecks observation confirmation and exact series metadata when available', () => {
  const state = fixture();
  state.observations = [observation('o1', '2024', '10'), observation('o2', '2025', '-3.50', {status: 'candidate'})];
  assert.deepEqual(selectDashboardSeries(state)[0].points.map(row => row.observationId), ['o1']);
  state.observations[0].unit = '元';
  assert.equal(selectDashboardSeries(state).length, 0);
});

test('keeps duplicate periods as conflicts and never manufactures missing periods or zero values', () => {
  const state = fixture();
  state.metricSeries.series[0].points = [point('a', '2023', '8'), point('b', '2025', '0'), point('c', '2025', '2')];
  const rows = selectDashboardSeries(state)[0].points;
  assert.deepEqual(rows.map(row => [row.period, row.value]), [['2023', 8], ['2025', 0], ['2025', 2]]);
  assert.deepEqual(rows.map(row => row.conflict), [false, true, true]);
});

class Node {
  constructor(tag, ownerDocument) { this.tagName = tag; this.ownerDocument = ownerDocument; this.children = []; this.attributes = {}; this.listeners = {}; this.textContent = ''; this.className = ''; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; if (name === 'class') this.className = value; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
}
class Document {
  createElement(tag) { return new Node(tag, this); }
  createElementNS(_namespace, tag) { return new Node(tag, this); }
}
const walk = node => [node, ...node.children.flatMap(walk)];

test('renders no more than two accessible bar charts, sourced tables, and finance navigation', () => {
  const state = fixture();
  state.metricSeries.series.push(series('净利润', [point('n', '2025', '4')]));
  state.metricSeries.series.push(series('总资产', [point('a', '2025', '30')]));
  const document = new Document(); const root = new Node('root', document);
  const opened = []; const navigated = [];
  renderDashboardCharts(root, state, {openSource: (...args) => opened.push(args), navigate: tab => navigated.push(tab)});
  const nodes = walk(root);
  const svgs = nodes.filter(node => node.tagName === 'svg');
  assert.equal(svgs.length, 2);
  assert.ok(svgs.every(svg => svg.attributes.role === 'img' && svg.attributes['aria-label'].includes('单位')));
  assert.equal(nodes.filter(node => node.className === 'dashboard-table').length, 2);
  assert.ok(nodes.some(node => node.tagName === 'line'));
  const sourceButton = nodes.find(node => node.tagName === 'button' && node.textContent.includes('sheet!o1'));
  sourceButton.listeners.click();
  assert.equal(opened[0][0].id, 's1'); assert.equal(opened[0][1], 'sheet!o1');
  const more = nodes.find(node => node.tagName === 'button' && node.textContent.includes('全部财务'));
  more.listeners.click(); assert.deepEqual(navigated, ['finance']);
});

test('renders an honest empty state with finance and materials actions', () => {
  const state = fixture(); state.metricSeries.series = [];
  const document = new Document(); const root = new Node('root', document); const tabs = [];
  renderDashboardCharts(root, state, {navigate: tab => tabs.push(tab)});
  const nodes = walk(root);
  assert.equal(nodes.filter(node => node.tagName === 'svg').length, 0);
  const buttons = nodes.filter(node => node.tagName === 'button');
  buttons.forEach(button => button.listeners.click());
  assert.deepEqual(tabs, ['finance', 'materials']);
  assert.ok(nodes.some(node => node.textContent.includes('还没有')));
});

const SVG_NS = 'http://www.w3.org/2000/svg';
const CADENCE_LABELS = {year: '年度', quarter: '季度', month: '月度'};
const PERIOD_PATTERNS = {
  year: /^(\d{4})$/,
  quarter: /^(\d{4})-Q([1-4])$/,
  month: /^(\d{4})-(0[1-9]|1[0-2])$/,
};

const text = value => typeof value === 'string' ? value.trim() : '';
const compact = value => text(value).replace(/\s+/g, ' ');
const sameNullableText = (left, right) => text(left) === text(right);

function periodOrder(period, cadence) {
  const match = PERIOD_PATTERNS[cadence]?.exec(period);
  return match ? Number(match[1]) * 100 + Number(match[2] || 0) : null;
}

function numericValue(value) {
  if ((typeof value !== 'string' && typeof value !== 'number') || text(String(value)) === '') return null;
  const display = String(value).trim();
  const number = Number(display);
  return Number.isFinite(number) ? {number, display} : null;
}

function observationMatches(observation, point, series, projectId) {
  if (!observation) return true;
  const observationNumber = numericValue(observation.number);
  return observation.project_id === projectId
    && observation.status === 'confirmed'
    && observation.source_id === point.sourceId
    && observation.version === point.observationVersion
    && observation.period === point.period
    && observation.source_locator === point.sourceLocator
    && observation.basis === 'actual'
    && observation.subject === series.subject
    && compact(observation.statement) === series.name
    && observation.unit === series.unit
    && sameNullableText(observation.scope, series.scope)
    && observationNumber?.number === point.value;
}

/**
 * Return chart-safe, evidence-linked ACTUAL finance series for the active project.
 * The function deliberately keeps duplicate periods and never combines series.
 */
export function selectDashboardSeries(state) {
  const projectId = text(state?.current?.id);
  if (!projectId || state?.metricError || !Array.isArray(state?.metricSeries?.series)) return [];
  const liveSources = new Map(
    (Array.isArray(state.sources) ? state.sources : [])
      .filter(source => source?.project_id === projectId && text(source.id))
      .map(source => [source.id, source]),
  );
  const observations = new Map(
    (Array.isArray(state.observations) ? state.observations : [])
      .filter(observation => text(observation?.id))
      .map(observation => [observation.id, observation]),
  );

  const selected = [];
  for (const raw of state.metricSeries.series) {
    const subject = text(raw?.subject);
    const name = compact(raw?.name);
    const unit = text(raw?.unit);
    const scope = text(raw?.scope) || null;
    const cadence = text(raw?.cadence);
    if (raw?.basis !== 'actual'
      || !['财务表现', 'financials'].includes(subject)
      || !name || !unit || !PERIOD_PATTERNS[cadence]
      || !Array.isArray(raw.points) || raw.points.length === 0) continue;

    const series = {subject, name, unit, scope, basis: 'actual', cadence};
    const points = [];
    for (const rawPoint of raw.points) {
      const period = text(rawPoint?.period);
      const order = periodOrder(period, cadence);
      const value = numericValue(rawPoint?.number);
      const sourceId = text(rawPoint?.source_id);
      const sourceSha256 = text(rawPoint?.source_sha256).toLowerCase();
      const sourceLocator = text(rawPoint?.source_locator);
      const observationId = text(rawPoint?.observation_id);
      const observationVersion = rawPoint?.observation_version;
      const source = liveSources.get(sourceId);
      if (order === null || !value || !source || !observationId
        || !Number.isInteger(observationVersion) || observationVersion < 1
        || !sourceLocator || !/^[0-9a-f]{64}$/.test(sourceSha256)
        || text(source.content_sha256).toLowerCase() !== sourceSha256) continue;
      const point = {
        observationId, observationVersion, period, order,
        value: value.number, valueText: value.display,
        sourceId, sourceSha256, sourceLocator,
        sourceReference: text(source.reference) || text(rawPoint.source_reference) || '已登记原件',
        conflict: rawPoint.same_period_conflict === true,
        source,
      };
      if (!observationMatches(observations.get(observationId), point, series, projectId)) continue;
      points.push(point);
    }
    if (!points.length) continue;
    points.sort((left, right) => left.order - right.order || left.observationId.localeCompare(right.observationId));
    const periodCounts = new Map();
    for (const point of points) periodCounts.set(point.period, (periodCounts.get(point.period) || 0) + 1);
    for (const point of points) point.conflict ||= periodCounts.get(point.period) > 1;
    selected.push({
      ...series,
      totalPoints: Number.isInteger(raw.total_points) ? raw.total_points : points.length,
      truncatedPoints: raw.truncated_points === true,
      points,
    });
  }
  return selected;
}

function element(document, tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = content;
  return node;
}

function svgElement(document, tag, attributes = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, String(value));
  return node;
}

function action(document, label, callback) {
  const button = element(document, 'button', 'text-button', label);
  button.type = 'button';
  button.addEventListener('click', callback);
  return button;
}

function appendChart(document, panel, series) {
  const width = 640; const height = 240;
  const left = 54; const right = 18; const top = 20; const bottom = 48;
  const plotWidth = width - left - right; const plotHeight = height - top - bottom;
  const minimum = Math.min(0, ...series.points.map(point => point.value));
  const maximum = Math.max(0, ...series.points.map(point => point.value));
  const range = maximum - minimum || 1;
  const y = value => top + ((maximum - value) / range) * plotHeight;
  const baseline = y(0);
  const slot = plotWidth / series.points.length;
  const barWidth = Math.max(2, Math.min(44, slot * 0.62));

  const svg = svgElement(document, 'svg', {
    class: 'dashboard-chart', viewBox: `0 0 ${width} ${height}`,
    role: 'img', 'aria-label': `${series.name}，${CADENCE_LABELS[series.cadence]}实际值，单位 ${series.unit}`,
  });
  const title = svgElement(document, 'title');
  title.textContent = `${series.name} · 实际 · ${series.scope || '未指定口径'} · ${CADENCE_LABELS[series.cadence]} · ${series.unit}`;
  svg.append(title, svgElement(document, 'line', {x1: left, x2: width - right, y1: baseline, y2: baseline, class: 'dashboard-chart-baseline'}));
  const zero = svgElement(document, 'text', {x: left - 8, y: baseline + 4, 'text-anchor': 'end'});
  zero.textContent = '0'; svg.append(zero);
  series.points.forEach((point, index) => {
    const x = left + slot * index + (slot - barWidth) / 2;
    const valueY = y(point.value);
    const rect = svgElement(document, 'rect', {
      x, y: Math.min(valueY, baseline), width: barWidth,
      height: Math.max(1, Math.abs(valueY - baseline)),
      class: point.conflict ? 'dashboard-chart-bar is-conflict' : 'dashboard-chart-bar',
    });
    const tooltip = svgElement(document, 'title');
    tooltip.textContent = `${point.period}：${point.valueText} ${series.unit}${point.conflict ? '（同期存在多值）' : ''} · ${point.sourceReference} · ${point.sourceLocator}`;
    rect.append(tooltip); svg.append(rect);
    const label = svgElement(document, 'text', {x: left + slot * (index + 0.5), y: height - 22, 'text-anchor': 'middle'});
    label.textContent = point.period; svg.append(label);
  });
  panel.append(svg);
}

function appendTable(document, panel, series, openSource) {
  const table = element(document, 'table', 'dashboard-table');
  const head = element(document, 'thead'); const headRow = element(document, 'tr');
  for (const label of ['期间', `数值（${series.unit}）`, '来源与定位']) headRow.append(element(document, 'th', '', label));
  head.append(headRow); table.append(head);
  const body = element(document, 'tbody');
  for (const point of series.points) {
    const row = element(document, 'tr');
    row.append(element(document, 'td', '', `${point.period}${point.conflict ? ' · 同期待核对' : ''}`));
    row.append(element(document, 'td', '', point.valueText));
    const sourceCell = element(document, 'td');
    sourceCell.append(action(document, `${point.sourceReference} · ${point.sourceLocator} ↗`, () => openSource(point.source, point.sourceLocator)));
    row.append(sourceCell); body.append(row);
  }
  table.append(body); panel.append(table);
}

/** Render at most two evidence-linked charts and their underlying data tables. */
export function renderDashboardCharts(root, state, {openSource = () => {}, navigate = () => {}} = {}) {
  if (!root?.replaceChildren) return [];
  const document = root.ownerDocument || globalThis.document;
  const series = selectDashboardSeries(state);
  const layout = element(document, 'div', 'dashboard-charts grid2');
  root.replaceChildren(layout);
  if (!series.length) {
    const panel = element(document, 'section', 'dashboard-panel');
    panel.append(element(document, 'h3', '', '暂无可绘制的实际财务指标'));
    panel.append(element(document, 'p', 'empty', state?.metricError
      ? '指标记录暂时无法读取；可继续查看财务记录或阅读原始材料。'
      : '当前项目还没有来源有效、已确认的实际财务时序数据。'));
    panel.append(action(document, '查看财务记录 →', () => navigate('finance')));
    panel.append(action(document, '阅读项目材料 →', () => navigate('materials')));
    layout.append(panel); return series;
  }
  for (const item of series.slice(0, 2)) {
    const panel = element(document, 'section', 'dashboard-panel metric-chart');
    panel.append(element(document, 'h3', '', item.name));
    panel.append(element(document, 'p', 'dashboard-chart-caption',
      `实际 · ${item.scope || '未指定口径'} · ${CADENCE_LABELS[item.cadence]} · 单位 ${item.unit}`));
    appendChart(document, panel, item);
    appendTable(document, panel, item, openSource);
    layout.append(panel);
  }
  if (series.length > 2) {
    const panel = element(document, 'section', 'dashboard-panel');
    panel.append(element(document, 'p', '', `还有 ${series.length - 2} 组已确认实际财务指标。`));
    panel.append(action(document, '查看全部财务记录 →', () => navigate('finance')));
    layout.append(panel);
  }
  return series;
}

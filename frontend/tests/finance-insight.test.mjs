import assert from "node:assert/strict";
import { createRequire, Module } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";

const require = createRequire(new URL("../package.json", import.meta.url));
const { buildSync } = require("esbuild");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const entry = fileURLToPath(new URL("../src/financeInsight.tsx", import.meta.url));
const mod = new Module(entry);
mod.paths = require.resolve.paths("react");
mod._compile(buildSync({ entryPoints: [entry], bundle: true, write: false, platform: "node", format: "cjs",
  packages: "external", loader: { ".css": "empty" } }).outputFiles[0].text, entry);
const { FinanceInsight, financeReading, priorPeriod } = mod.exports;

let n = 0;
const metric = (name, period, value, basis = "actual") => ({ id: `m${n++}`, name, std_name: name, period, period_key: period,
  value, value_norm: value, unit: "万元", norm_unit: "万元", basis, category: "financial", scope: "", locator: "page 1",
  quote: "", source_id: "synthetic", source_name: "合成资料.pdf", created_at: "2026-01-01" });
const row = (name, values) => ({ name, std: name, unit: "万元", category: "financial", ids: [],
  cells: new Map(values.map((m) => [m.period_key, [m]])) });
const render = (rows, finance) => renderToStaticMarkup(React.createElement(FinanceInsight, { dims: financeReading(rows, finance).dims }));

test("prior period keeps the period length", () => {
  assert.equal(priorPeriod("2026H1"), "2025H1");
  assert.equal(priorPeriod("2025"), "2024");
});

test("compares like-for-like periods and explains the change", () => {
  const html = render([
    row("营业收入", [metric("营业收入", "2025H1", 10000), metric("营业收入", "2025", 30000), metric("营业收入", "2026H1", 9000)]),
    row("净利润", [metric("净利润", "2025H1", -4000), metric("净利润", "2026H1", -2000)]),
  ]);
  assert.match(html, /同比 -10\.0%/);
  assert.match(html, /收入下滑/);
  assert.match(html, /亏损收窄 50%/);
  assert.doesNotMatch(html, /2025 全年.*同比/); // No prior full year: no annual growth claim.
});

test("forecasts never enter a comparison and missing periods are stated, not invented", () => {
  const html = render([row("营业收入", [metric("营业收入", "2025", 8000, "forecast"), metric("营业收入", "2026", 9000)])]);
  assert.match(html, /没有上年同期数字/);
  assert.match(html, /缺少同期数据/);
  assert.doesNotMatch(html, /同比 [+-]/);
});

test("renders nothing without statement numbers", () => {
  assert.equal(render([], undefined), "");
});

test("waterfall uses reported subtotals and labels the expense step as a difference", () => {
  const charts = fileURLToPath(new URL("../src/financeCharts.tsx", import.meta.url));
  const cm = new Module(charts);
  cm.paths = require.resolve.paths("react");
  cm._compile(buildSync({ entryPoints: [charts], bundle: true, write: false, platform: "node", format: "cjs",
    packages: "external", loader: { ".css": "empty" } }).outputFiles[0].text, charts);
  const steps = cm.exports.waterfallSteps({
    revenue: row("营业收入", [metric("营业收入", "2026H1", 11140)]),
    cost: row("营业成本", [metric("营业成本", "2026H1", 5364.7)]),
    gross: row("毛利", [metric("毛利", "2026H1", 5770)]),
    profit: row("净利润", [metric("净利润", "2026H1", -5040)]),
  }, "2026H1");
  assert.deepEqual(steps.map((s) => [s.label, s.to, !!s.computed]),
    [["营业收入", 11140, false], ["营业成本", 5770, false], ["毛利", 5770, false], ["费用及其他", -5040, true], ["净利润", -5040, false]]);
  assert.equal(cm.exports.waterfallSteps({ revenue: row("营业收入", [metric("营业收入", "2026H1", 1, "forecast")]) }, "2026H1"), null);
});

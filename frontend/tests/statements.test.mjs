import assert from "node:assert/strict";
import { createRequire, Module } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";

const require = createRequire(new URL("../package.json", import.meta.url));
const { buildSync } = require("esbuild");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const entry = fileURLToPath(new URL("../src/statements.tsx", import.meta.url));
const mod = new Module(entry);
mod.paths = require.resolve.paths("react");
mod._compile(buildSync({ entryPoints: [entry], bundle: true, write: false, platform: "node", format: "cjs",
  packages: "external", loader: { ".css": "empty" } }).outputFiles[0].text, entry);
const { Statements } = mod.exports;

let n = 0;
const metric = (raw, period, value, extra = {}) => ({ id: `m${n++}`, name: raw, period, period_key: period, value, value_norm: value,
  unit: "万元", norm_unit: "万元", basis: "actual", category: "financial", scope: "", locator: "page 1", quote: "",
  source_id: "s", source_name: "合成资料.pdf", created_at: "2026-01-01", ...extra });
const row = (std, name, values) => ({ name, std, unit: "万元", category: "financial", ids: values.map((v) => v.id),
  aliases: [...new Set(values.map((v) => v.name))], cells: new Map(values.map((m) => [m.period_key, [m]])) });
const layout = [
  { key: "income", title: "利润表", groups: [{ title: "", items: [{ name: "营业收入", label: "营业收入", total: true }] },
    { title: "期间费用", items: [{ name: "管理费用", label: "管理费用", total: false }] }] },
  { key: "balance", title: "资产负债表", groups: [{ title: "流动资产", items: [{ name: "货币资金", label: "货币资金", total: false }] }] },
  { key: "cashflow", title: "现金流量表", groups: [] },
];
const sections = {};
const rows = [
  row("营业收入", "营业收入", [metric("收入", "2024", 100), metric("收入", "2025", 120)]),
  row("管理费用", "管理费用", [metric("一般及行政开支", "2024", 30), metric("一般及行政开支", "2025", 27)]),
  row("货币资金", "货币资金", [metric("银行结余及现金", "2025M12", 500), metric("货币资金", "2024", 650)]),
  row("货币资金", "货币资金（母公司）", [metric("银行结余及现金", "2025M12", 80, { scope: "母公司" })]),
];
for (const r of rows) for (const id of r.ids) sections[id] = r.std === "货币资金" ? "balance" : "income";
const html = renderToStaticMarkup(React.createElement(Statements, { rows, finance: { layout, sections, ratios: [], key_numbers: {} } }));

test("a few disclosed lines render flat, with YoY and the original HK wording on hover", () => {
  assert.doesNotMatch(html, /scope="colgroup"/); // 2 income lines: no group headings
  assert.match(html, /cas-flat/);
  assert.match(html, /\+20\.0%/); // 营业收入 2025 vs 2024
  assert.match(html, /资料原文科目：一般及行政开支/);
  assert.match(html, /资料中暂无现金流量表数字/);
  assert.doesNotMatch(html, /主要科目/); // nothing to condense
});

test("balance dates merge annual and month-end keys; non-consolidated rows stay out", () => {
  assert.match(html, /2024-12/);
  assert.match(html, /2025-12/);
  assert.doesNotMatch(html, /货币资金（母公司）/);
  assert.doesNotMatch(html, />80</);
});

test("rich statements keep CAS groups and open on key lines with a full view available", () => {
  const items = Array.from({ length: 50 }, (_, i) => ({ name: `明细${i}`, label: `明细科目${i}`, total: false, key: false }));
  const rich = [{ key: "income", title: "利润表", groups: [
    { title: "", items: [{ name: "营业收入", label: "营业收入", total: true, key: true }] },
    { title: "期间费用", items: [{ name: "管理费用", label: "管理费用", total: false, key: true }, ...items] },
  ] }, { key: "balance", title: "资产负债表", groups: [] }, { key: "cashflow", title: "现金流量表", groups: [] }];
  const richRows = [rows[0], rows[1], ...items.map((it) => row(it.name, it.name, [metric(it.name, "2024", 1), metric(it.name, "2025", 2)]))];
  const secs = Object.fromEntries(richRows.flatMap((r) => r.ids.map((id) => [id, "income"])));
  const out = renderToStaticMarkup(React.createElement(Statements, { rows: richRows, finance: { layout: rich, sections: secs, ratios: [], key_numbers: {} } }));
  assert.match(out, /scope="colgroup">期间费用/);
  assert.match(out, /主要科目/);
  assert.match(out, /全部科目（52）/);
  assert.match(out, /另有 50 个明细科目/);
  assert.doesNotMatch(out, /明细科目3</);
});

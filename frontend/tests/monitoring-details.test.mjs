import assert from "node:assert/strict";
import { createRequire, Module } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";
const require = createRequire(new URL("../package.json", import.meta.url));
const { buildSync } = require("esbuild");
const React = require("react"), { renderToStaticMarkup } = require("react-dom/server");
const entry = fileURLToPath(new URL("../src/MonitoringDetails.tsx", import.meta.url));
const component = new Module(entry);
component.paths = require.resolve.paths("react");
component._compile(buildSync({ entryPoints: [entry], bundle: true, write: false, platform: "node", format: "cjs",
  packages: "external", loader: { ".css": "empty" } }).outputFiles[0].text, entry);
const render = detail => renderToStaticMarkup(React.createElement(component.exports.default, { detail, onReload() {} }));
test("no empty monitoring panels", () => { assert.equal(render({ project: { id: "synthetic" } }), ""); });
test("dated plans stay separate and exact money strings never round", () => {
  const html = render({ project: { id: "synthetic", config_version: 1 },
    milestones: { items: [{ subject_id: "s", name: "Line One", phase: "adjusted", support: "source_assertion",
      original_dates: ["2026-11-01"], adjusted_dates: ["2026-12-01"], actual_dates: [], history: [] }] },
    observations: { items: [{ kind: "funding_fact", fact_id: "f", measure: "funding", stage: "plan", basis: "forecast",
      raw_value: "123456789012345.6789", raw_unit: "元", currency: "CNY", period: "2026Q3", consolidation_scope: "unknown", refs: [] }] },
    concerns: { items: [{ id: "c", title: "Line One · 资料中的延期", status: "observation_insufficient", refs: [], feedback_history: [] }] },
  });
  assert.match(html, /原定/); assert.match(html, /调整/); assert.match(html, /2026-11-01/); assert.match(html, /2026-12-01/);
  assert.match(html, /123456789012345.6789/); assert.match(html, /计划 · 预测/); assert.match(html, /口径未明/);
  assert.match(html, /依据待补/); assert.match(html, /记录处理/); assert.doesNotMatch(html, /资金缺口|承压|投资建议/);
});

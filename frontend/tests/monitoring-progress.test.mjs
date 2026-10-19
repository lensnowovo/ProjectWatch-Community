import assert from "node:assert/strict";
import { createRequire, Module } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";

const require = createRequire(new URL("../package.json", import.meta.url));
const { buildSync } = require("esbuild");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const { StaticRouter } = require("react-router-dom");
const entry = fileURLToPath(new URL("../src/MonitoringProgress.tsx", import.meta.url));
const mod = new Module(entry);
mod.paths = require.resolve.paths("react");
mod._compile(buildSync({ entryPoints: [entry], bundle: true, write: false, platform: "node",
  format: "cjs", packages: "external" }).outputFiles[0].text, entry);
const { MonitoringEmpty, DigestProgress, showStoredPanel, monitoringLabel } = mod.exports;
const render = (Component, props) => renderToStaticMarkup(React.createElement(StaticRouter, null, React.createElement(Component, props)));

test("idle header uses saved monitoring state instead of claiming automatic monitoring", () => {
  assert.equal(monitoringLabel({state: "inactive", label: "尚未启用监控"}), "尚未启用监控");
  assert.equal(monitoringLabel({state: "failed", label: "更新未完成"}), "更新未完成");
  assert.equal(monitoringLabel(), "监控状态尚未更新");
  assert.equal(monitoringLabel(undefined, true), "监控已暂停");
});

test("empty changes show stored progress, not silence inferred from an empty feed", () => {
  for (const [state, empty_text] of [["queued", "正在等待更新，尚未完成本轮监控。"], ["running", "正在检索和整理，已有结果保留。"],
    ["failed", "本轮更新未完成，已有结果保留。"], ["partial", "部分信息尚未整理完成。"]]) {
    const html = render(MonitoringEmpty, { progress: { state, empty_text } });
    assert.match(html, new RegExp(empty_text));
    assert.doesNotMatch(html, /未见重要变化|无明显变化/);
  }
});

test("failed feed and old backend response never display no changes", () => {
  assert.match(render(MonitoringEmpty, { error: "synthetic failure", progress: { empty_text: "已收录信息未见重要变化" } }), /暂时未能加载/);
  assert.match(render(MonitoringEmpty, {}), /监控状态尚未更新/);
});

test("digest progress links incomplete projects without dropping saved events", () => {
  const html = render(DigestProgress, { incomplete: [
    { id: "queued", name: "合成项目甲", state: "queued", label: "等待更新" },
    { id: "failed", name: "合成项目乙", state: "failed", label: "更新未完成" },
  ] });
  assert.match(html, /aria-label="监控进度"/);
  assert.match(html, /href="\/projects\/queued"/);
  assert.match(html, /合成项目甲/); assert.match(html, /更新未完成/);
  assert.doesNotMatch(html, /无明显变化/);
  assert.equal(render(DigestProgress, { incomplete: [] }), "");
});

test("incomplete monitoring hides only empty risk/commitment panels, never actual records", () => {
  for (const state of ["queued", "running", "failed", "partial", "unobserved", "stale", "inactive", "paused"]) {
    assert.equal(showStoredPanel({ state }, 0), false);
    assert.equal(showStoredPanel({ state }, 1), true);
  }
  assert.equal(showStoredPanel({ state: "observed" }, 0), true);
  assert.equal(showStoredPanel(undefined, 0), false);
});

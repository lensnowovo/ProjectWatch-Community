import assert from "node:assert/strict";
import { createRequire, Module } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";

const require = createRequire(new URL("../package.json", import.meta.url));
const { buildSync } = require("esbuild");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const entry = fileURLToPath(new URL("../src/race.tsx", import.meta.url));
const mod = new Module(entry);
mod.paths = require.resolve.paths("react");
mod._compile(buildSync({ entryPoints: [entry], bundle: true, write: false, platform: "node",
  format: "cjs", packages: "external", loader: { ".css": "empty" } }).outputFiles[0].text, entry);
const { StageScope, PeerRelation } = mod.exports;
const render = (Component, props) => renderToStaticMarkup(React.createElement(Component, props));

test("stage row shows the actual product, indication and regional scope", () => {
  assert.match(render(StageScope, { stage: { scope: "AB-123", context: { indication: ["肝癌"], region: ["CN"] } } }),
    /AB-123 · 肝癌 · 中国/);
  assert.equal(render(StageScope, { stage: {} }), "");
  assert.match(render(StageScope, { stage: { scope: "", title: "合成药业甲药获批", context: { region: ["CN"] } } }),
    /合成药业甲药获批 · 中国/);
});

test("typed peer roles remain distinct, with a neutral label for an untyped name", () => {
  for (const [relation_type, label] of [["direct", "直接竞品"], ["substitute", "替代方案"],
    ["adjacent", "产业关联"], [undefined, "关注同行"], ["unrecognized", "关注同行"]]) {
    assert.match(render(PeerRelation, { row: { is_self: false, relation_type } }), new RegExp(label));
  }
  assert.equal(render(PeerRelation, { row: { is_self: true, relation_type: "direct" } }), "");
});

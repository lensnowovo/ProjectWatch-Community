import assert from "node:assert/strict";
import { createRequire, Module } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";

const require = createRequire(new URL("../package.json", import.meta.url));
const { buildSync } = require("esbuild");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const entry = fileURLToPath(new URL("../src/signals.tsx", import.meta.url));
const mod = new Module(entry);
mod.paths = require.resolve.paths("react");
mod._compile(buildSync({entryPoints:[entry], bundle:true, write:false, platform:"node",
  format:"cjs", packages:"external", loader:{".css":"empty"}}).outputFiles[0].text, entry);
const { SignalsPanel } = mod.exports;

function render(severity, question) {
  return renderToStaticMarkup(React.createElement(SignalsPanel, {onChange:()=>{},
    data:{silence:[], crosschecks:[{id:"synthetic",kind:"mismatch",kind_label:"数字或事实不一致",
      severity,question,topic:"收入",public_says:"公开金额",internal_says:"内部金额",
      public_refs:[],internal_refs:[]}]}}));
}
test("unrated factual comparison has both sides without a severity badge or question", () => {
  const html = render("unrated", "");
  assert.match(html,/公开金额/);
  assert.match(html,/内部金额/);
  assert.doesNotMatch(html,/title="重要程度"|class="xc-q"|投后|追问/);
});
test("existing investment result retains its severity and question", () => {
  const html = render("high", "差异是什么原因？");
  assert.match(html,/title="重要程度">高/);
  assert.match(html,/差异是什么原因/);
});

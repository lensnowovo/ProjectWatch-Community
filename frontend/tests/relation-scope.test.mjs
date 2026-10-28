import assert from "node:assert/strict";
import { createRequire, Module } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";
const require = createRequire(new URL("../package.json", import.meta.url));
const { buildSync } = require("esbuild");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const entry = fileURLToPath(new URL("../src/relationScope.tsx", import.meta.url));
const loaded = new Module(entry);
loaded.paths = require.resolve.paths("react");
loaded._compile(buildSync({ entryPoints: [entry], bundle: true, write: false, platform: "node",
  format: "cjs", packages: "external", loader: { ".css": "empty" } }).outputFiles[0].text, entry);
const { RelationScope, branchState, publicLink } = loaded.exports;
test("research state is separate from an active monitoring row", () => {
  assert.equal(branchState({ status: "active" }, { state: "stopped" }), "已结束");
  assert.equal(branchState({ status: "active" }, { state: "active" }), "可继续探索");
  assert.equal(branchState({ status: "active" }), "待运行");
  assert.equal(branchState({ status: "retired" }, { state: "active" }), "已关闭");
  assert.equal(branchState({ status: "active" }, { state: "waiting", reason: "source_failure" }), "来源待恢复");
  assert.equal(branchState({ status: "active" }, { state: "waiting", reason: "waiting_recovery" }), "待确认恢复");
  assert.equal(branchState({ status: "active" }, { state: "waiting", reason: "request_budget" }), "本轮额度已到");
});
test("four-column relation table retains branches and honest empty matches", () => {
  const items = [
    { id: "r", scope_kind: "relation", status: "active", relation_type: "product",
      relation_target: "SYN-ONE", relation_aliases: ["合成产品"], reason: "公司开发的产品" },
    { id: "q", parent_id: "r", scope_kind: "question", status: "active", text: "获批了吗？" },
  ];
  const html = renderToStaticMarkup(React.createElement(RelationScope, { items,
    explorations: { q: { state: "stopped" } }, renderState: () => "持续关注", renderDetail: () => "" }));
  for (const label of ["关注对象", "纳入原因", "最近变化", "研究分支", "合成产品", "已结束", "暂无匹配动态"])
    assert.ok(html.includes(label), label);
  assert.ok(!html.includes("未检索"));
});
test("source links reject executable/local protocols", () => {
  for (const url of ["javascript:alert(1)", "file:///private", "data:text/html,x", "not-a-url"])
    assert.equal(publicLink(url), undefined);
  assert.equal(publicLink("https://example.com"), "https://example.com");
});

test("search excerpt matches are not labelled full body", () => {
  const html = renderToStaticMarkup(React.createElement(RelationScope, {
    items: [{ id: "r", scope_kind: "relation", status: "active", relation_target: "SYN-ONE" }],
    updates: { r: { count: 1, items: [{ event_id: "e", title: "合成报道", url: "https://example.com",
      basis: "search_summary", matched_term: "SYN-ONE", excerpt: "SYN-ONE 的搜索摘要" }] } },
    renderState: () => "持续关注", renderDetail: () => "",
  }));
  assert.ok(html.includes("搜索摘要关联"));
  assert.ok(!html.includes("正文关联"));
});

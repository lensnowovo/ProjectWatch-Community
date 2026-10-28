import assert from "node:assert/strict";
import { createRequire, Module } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";
const require = createRequire(new URL("../package.json", import.meta.url));
const { buildSync } = require("esbuild");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const entry = fileURLToPath(new URL("../src/RoundOutcome.tsx", import.meta.url));
const loaded = new Module(entry);
loaded.paths = require.resolve.paths("react");
loaded._compile(buildSync({ entryPoints:[entry], bundle:true, write:false, platform:"node", format:"cjs",
  packages:"external", loader:{".css":"empty"} }).outputFiles[0].text,entry);
const { RoundOutcome } = loaded.exports;
const round = {job_id:"synthetic", completed_at:"2026-01-01T10:00:00Z", coverage:"partial", assessed_events:2,
  evidence_only:1, object_changes:0, judgment_change:"unchanged", headline:"仅有计划，实际支付仍待确认",
  stopped:1, active:0, waiting:0, standing:1, unfulfilled:0, pending:0, gaps:["省级原件未取得"]};
const render = props => renderToStaticMarkup(React.createElement(RoundOutcome,props));
test("settled branch and incomplete coverage coexist without success/failure confusion",()=>{
  const html=render({round});
  for(const text of ["最近一轮结果","本轮信息","判断方向未变","部分完成","长期关联继续保留","研究分支","省级原件未取得"])
    assert.ok(html.includes(text),text);
  assert.ok(!html.includes("公司取得进展"));
  assert.ok(!html.includes("<details open"));
});
test("legacy and stale receipts do not claim live success",()=>{
  assert.equal(render({round:null}),"");
  assert.ok(render({round,stale:true}).includes("状态暂不可读"));
  assert.ok(render({round:{...round,unfulfilled:1,pending:2}}).includes("结束修订未落实"));
});
test("untrusted generated text is escaped",()=>{
  const html=render({round:{...round,headline:"<script>alert(1)</script>"}});
  assert.ok(!html.includes("<script>"));
});

test("outputs, stop reason and coverage remain available in closed click-to-open details",()=>{
  const html=render({round:{...round,stop_reason:"本轮评估已收口，仍有缺口。",
    stop_details:[{reason:"本次问题已回答",count:1}],
    outputs:{saved_entries:4,revision_applied:2,revision_added:1,revision_proposed:1,revision_dropped:1}}});
  for(const text of ["本轮保留 4 条记录","已执行 2","新增分支 1","待确认 1","未执行 1","本轮评估已收口","本次问题已回答"])
    assert.ok(html.includes(text),text);
  assert.ok(html.indexOf("省级原件未取得") > html.indexOf('<details class="round-outcome-details"'));
  assert.match(html, /<summary>查看本轮详情<\/summary>/);
  assert.doesNotMatch(html, /<details[^>]*\bopen\b/);
});

test("legacy receipts do not invent output counts or natural stop reasons",()=>{
  const html=render({round});
  assert.ok(!html.includes("本轮保留 0"));
  assert.match(html, /结束原因<\/strong><p>未记录<\/p>/);
});

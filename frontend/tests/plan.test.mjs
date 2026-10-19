import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

test("监控范围 is the fourth section and reads as one relation table: why, object, question, latest, state; details on demand", () => {
  const project = fs.readFileSync(new URL("../src/pages/Project.tsx", import.meta.url), "utf8");
  assert.match(project, /\{ id: "scope", label: "监控范围", pages: \[\{ id: "plan", label: "监控方案" \}\] \}/);
  assert.match(project, /tab === "plan" && <PlanView projectId=\{id\} company=/);
  const plan = fs.readFileSync(new URL("../src/plan.tsx", import.meta.url), "utf8");
  for (const text of ["需要你看", "方案概要", "跟踪对象", "方向 / 纳入理由", "关系对象", "关注事项", "最新", "状态",
    "出现什么就提醒", "何时关闭或降频", "方案变更", "撤销", "确认保留", "升为核心", "降频", "关闭", "已关闭"]) {
    assert.match(plan, new RegExp(text));
  }
  assert.match(plan, /data\.latest/);  // the latest per focus point comes from the server
  assert.match(plan, /aria-expanded=\{open\}/);  // details only when the reader asks
  assert.match(plan, /role="switch" aria-checked=\{on\}/);  // tracking ↔ reduced is one switch in the row
  assert.doesNotMatch(plan, /PlanMap|框架总览|RingMatrix|po-tree/);  // one structure, not several views of the same items
  assert.match(plan, /status === "trial"/);
});

test("each focus point renders its display module from a fixed library; templates are data", () => {
  const modules = fs.readFileSync(new URL("../src/planModules.tsx", import.meta.url), "utf8");
  for (const view of ["timeline", "peer_table", "metric_series", "commitments"]) assert.match(modules, new RegExp(`case "${view}"`));
  assert.match(modules, /pm-target/);  // target line for metric series
  assert.match(modules, /暂无公开证据/);  // commitments never invent progress
  const plan = fs.readFileSync(new URL("../src/plan.tsx", import.meta.url), "utf8");
  assert.match(plan, /<PlanModuleView watchId=\{item\.id\}/);
  assert.match(plan, /行业模板：/); assert.match(plan, /模板建议：/);
  assert.match(plan, /请先把｛｝里的内容换成本项目的具体对象/);
});

test("system suggestions wait for the owner; system changes show what they rest on", () => {
  const plan = fs.readFileSync(new URL("../src/plan.tsx", import.meta.url), "utf8");
  for (const text of ["待你确认的建议", "接受前方案不变", "接受", "拒绝", "依据："]) assert.match(plan, new RegExp(text));
  assert.match(plan, /api\.acceptPlanChange/); assert.match(plan, /api\.rejectPlanChange/);
});

test("a system-written first plan asks for one review; extended points name their source", () => {
  const plan = fs.readFileSync(new URL("../src/plan.tsx", import.meta.url), "utf8");
  for (const text of ["待你审阅", "确认方案", "直接相关", "二阶相关", "延伸"]) assert.match(plan, new RegExp(text));
  assert.match(plan, /api\.confirmPlanReview/); assert.match(plan, /phase-\$\{data\.state\.phase\}/);
  assert.match(plan, /由「\$\{parent\.label \|\| parent\.text\.slice\(0, 10\)\}」延伸/);
});

test("routine collection stays visible beside the focus questions — the plan is not only the questions", () => {
  const plan = fs.readFileSync(new URL("../src/plan.tsx", import.meta.url), "utf8");
  for (const text of ["常规采集", "专项问题", "未开启", "上轮失败", "只靠常规采集覆盖"]) assert.match(plan, new RegExp(text));
  assert.match(plan, /data\.coverage/);
  assert.match(plan, /<RoutineChannels framework=/);
});

test("every focus point says why (its link to the company) before what is new", () => {
  const plan = fs.readFileSync(new URL("../src/plan.tsx", import.meta.url), "utf8");
  assert.match(plan, /className="po-why"><b>因为<\/b>/);
  assert.ok(plan.indexOf('className="po-why"') < plan.indexOf("po-latest ${"));  // why first, then latest
});

test("new, system-added, trial and reduced focus points are marked in the table", () => {
  const plan = fs.readFileSync(new URL("../src/plan.tsx", import.meta.url), "utf8");
  const css = fs.readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
  assert.match(plan, /po-tag new">新增/);
  assert.match(plan, /系统加的/); assert.match(plan, /试运行/); assert.match(plan, /有建议/);
  assert.match(plan, /added > firstVersion/);  // a brand-new plan is not all "新增"
  assert.match(css, /\.scope-row\.reduced td/);
  assert.match(css, /\.sc-fresh \{/);
});

test("points are grouped by dimension with its ring; a misplaced point can be moved", () => {
  const plan = fs.readFileSync(new URL("../src/plan.tsx", import.meta.url), "utf8");
  assert.match(plan, /data\.rings/);
  assert.match(plan, /ringLabel\.get\(section\?\.ring/);
  assert.match(plan, /aria-label="按方向筛选"/);
  assert.match(plan, /function MoveTo/);
  assert.match(plan, /c\.action !== "regroup" && <button/);  // the framework migration is not undone item by item
});

test("the plan opens with the shared conclusion band; the table is the main card, the rest is folded", () => {
  const plan = fs.readFileSync(new URL("../src/plan.tsx", import.meta.url), "utf8");
  const css = fs.readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
  assert.match(plan, /<VerdictBand/);
  assert.match(plan, /<ScopeTable projectId=/);
  assert.match(plan, /<Reference items=/);
  assert.match(css, /\.scope \{ background: var\(--card-bg/);
  assert.ok(plan.indexOf('className="po-why"') > plan.indexOf("function ItemDetail"));  // 因为 in full lives in the details
});

test("dates ahead of today are labelled as scheduled, never shown as the latest news", () => {
  const ui = fs.readFileSync(new URL("../src/ui.tsx", import.meta.url), "utf8");
  assert.match(ui, /预定 \$\{date\.toLocaleDateString/);
});

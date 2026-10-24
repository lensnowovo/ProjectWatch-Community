import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const component = readFileSync(new URL("../src/CommercialSources.tsx", import.meta.url), "utf8");
const settings = readFileSync(new URL("../src/pages/Settings.tsx", import.meta.url), "utf8");
const project = readFileSync(new URL("../src/pages/Project.tsx", import.meta.url), "utf8");

test("commercial views keep vendor amounts as text and reports separate from funding", () => {
  assert.match(component, /<th>金额<\/th>/);
  assert.match(component, /r.amount \|\| "未披露"/);
  assert.match(component, /kind === "funding"/);
  assert.match(component, /kind === "news"/);
  assert.match(component, /历史结果/);
  assert.doesNotMatch(component, /amount_value|parseFloat|dangerouslySetInnerHTML/);
});
test("financing information has no cosmetic investment preference switch", () => {
  assert.doesNotMatch(component, /commercialFocus|investment_status|投资关系|关注中|已投/);
  assert.match(component, /指定跟踪/);
  assert.match(component, /rel="noopener noreferrer"/);
});
test("presets preserve local keys and fees; model use is an explicit, described consent", () => {
  assert.match(settings, /professional|专业数据来源/);
  assert.match(settings, /按服务商计费/);
  assert.match(settings, /缓存一天/);
  // Off by default; the visible sentence follows the switch, so the promise always matches behaviour.
  assert.match(settings, /目前不发送给任何模型/);
  assert.match(settings, /已允许交给 AI 深度分析/);
  assert.match(settings, /checked=\{form\.commercial\.model_use\}/);
  assert.match(settings, /连接测试不批量查询商业来源/);
  assert.match(settings, /type="password"/);
});
test("source reads follow stored project updates without triggering paid collection", () => {
  assert.match(component, /useAsync\(\(\) => api.commercial\(projectId\)/);
  assert.match(project, /CommercialSources projectId=\{id\} revision=\{commercialRevision\} peers/);
  assert.match(readFileSync(new URL("../src/workState.ts", import.meta.url), "utf8"), /commercial: "更新专业来源"/);
});

test("company discrepancies never appear as unsupported peer funding discrepancies", () => {
  assert.match(component, /const different = funding.some/);
  assert.match(component, /a.company === b.company && a.round === b.round/);
  assert.doesNotMatch(component, /data.different_conventions &&/);
  assert.match(component, /<details className="commercial-source-details">/);
  assert.match(component, /口径差异/);
  assert.doesNotMatch(component, /data.notice|未自动合并|不代表融资款到账/);
});

test("compact source disclosure preserves attribution, failures and limits", () => {
  assert.match(component, /部分来源未更新/);
  assert.match(component, /"error", "held", "limited"/);
  assert.match(component, /数据来源于万得 Wind 金融数据服务/);
  assert.match(component, /daily_queries.remaining === 0/);
  assert.match(component, /刷新融资动态/);
  assert.match(component, /<ErrorNote error=\{actionError\}/);
});

import assert from "node:assert/strict";
import { createRequire, Module } from "node:module";
import { fileURLToPath } from "node:url";
import { readFileSync } from "node:fs";
import test from "node:test";

const require = createRequire(new URL("../package.json", import.meta.url));
const { buildSync } = require("esbuild");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const { StaticRouter } = require("react-router-dom");
function load(path) {
  const entry = fileURLToPath(new URL(path, import.meta.url));
  const mod = new Module(entry);
  mod.paths = require.resolve.paths("react");
  mod._compile(buildSync({ entryPoints: [entry], bundle: true, write: false, platform: "node",
    format: "cjs", packages: "external", loader: { ".css": "empty" } }).outputFiles[0].text, entry);
  return mod.exports;
}
const { CompanyBasics, CompanyPortrait, CompanyRegistry } = load("../src/overview.tsx");
const { IndustryView, ActivityPanel, CompetitionView, comparisonValue } = load("../src/landscape.tsx");
const { ToastProvider } = load("../src/motion.tsx");
const { TONE_LABEL } = load("../src/ui.tsx");
const render = (component, props) => renderToStaticMarkup(React.createElement(StaticRouter, null,
  React.createElement(ToastProvider, null, React.createElement(component, props))));
const detail = { project: { id: "synthetic", name: "合成项目", legal_name: "合成项目有限公司", activated: false,
  industry: "合成赛道", website: "example.invalid", description: "合成业务摘要" },
  documents: [], jobs: [], model_ready: false };
const ind = { industry: "合成行业", overview: "合成行业摘要", overview_refs: [], facts: [], funding: [],
  metrics: [], implications: [], heat: "升温", heat_reason: "这是合成 AI 解读", heat_refs: [] };

test("new cited business leads old identity copy, without overwriting internal research", () => {
  const facts = [
    { kind: "business", label: "历史业务", text: "过去制造天线", refs: [{ published_at: "2008-01-01" }] },
    { kind: "business", label: "业务", text: "车载连接器与线束", refs: [{ published_at: "2026-01-01" }] },
  ];
  const props = { detail: { ...detail, public_profile: { facts, updated_at: "2026-10-10" } }, onRefreshDossier() {}, refreshing: false };
  const html = render(CompanyBasics, props);
  assert.match(render(CompanyPortrait, props), /basic-intro[^>]*>车载连接器与线束/);
  assert.ok(html.indexOf("车载连接器与线束") < html.indexOf("过去制造天线"));
  assert.match(html, /来源日期.*2026-01-01/s);
  const internal = render(CompanyPortrait, { ...props, detail: { ...props.detail, profile: { one_liner: "内部研究主线", sections: [] } } });
  assert.match(internal, /basic-intro[^>]*>内部研究主线/);
  const manual = render(CompanyPortrait, { ...props, detail: { ...props.detail,
    project: { ...detail.project, entity_source: "manual", description: "人工修订介绍" } } });
  assert.match(manual, /basic-intro[^>]*>人工修订介绍/);
  assert.equal(TONE_LABEL.neutral, "中性");
});

test("fresh registry fields do not displace the actual business introduction", () => {
  const props = { detail: { ...detail, public_profile: { updated_at: "2026-10-10", facts: [
    { kind: "business", label: "曾用名", text: "合成旧企业名称", refs: [{ published_at: "2026-01-01" }] },
    { kind: "business", label: "注册资本", text: "登记资本100万元", refs: [{ published_at: "2026-01-01" }] },
    { kind: "business", label: "经营范围", text: "登记范围包括各种业务", refs: [{ published_at: "2026-01-01" }] },
    { kind: "business", label: "主营业务", text: "提供激光雷达产品", refs: [{ published_at: "2025-01-01" }] },
  ] } }, onRefreshDossier() {}, refreshing: false };
  const html = render(CompanyBasics, props);
  assert.match(render(CompanyPortrait, props), /basic-intro[^>]*>提供激光雷达产品/);
  const main = html.split('class="basic-main"')[1].split("<aside")[0];
  assert.doesNotMatch(main, /合成旧企业名称|登记资本100万元/);
  const registry = render(CompanyRegistry, props);
  assert.match(registry, /合成旧企业名称/);
  assert.match(registry, /登记资本100万元/);
});

test("company business and customers lead; team/capital is secondary; registry moved out", () => {
  const html = render(CompanyBasics, { detail: { ...detail, profile: { one_liner: "合成业务摘要", documents: 1,
    sections: [{ title: "产品", points: [{ text: "合成产品用途", refs: [] }] },
      { title: "客户", points: [{ text: "合成客户群", refs: [] }] }] } }, onRefreshDossier() {}, refreshing: false });
  assert.match(html, /basic-editorial/); assert.match(html, /basic-main.*合成产品用途.*合成客户群/s);
  assert.match(html, /<aside[^>]+aria-label="团队与资本"/);
  assert.doesNotMatch(html, /basic-archive|<details/);
  assert.match(html, /暂无团队资料/);
  assert.doesNotMatch(html, /basic-rows/);
});

test("company public assertions remain identifiable with sources and no invented team", () => {
  const html = render(CompanyBasics, { detail: { ...detail, public_profile: { updated_at: "2026-10-09", facts: [
    { kind: "product", label: "产品", text: "合成公开产品", date: "", refs: [
      { url: "https://example.invalid", title: "合成来源", source: "来源", quote: "合成公开产品" }] }] } }, onRefreshDossier() {}, refreshing: false });
  assert.match(html, /is-public/); assert.match(html, /合成公开产品/);
  assert.match(html, /查看来源/); assert.match(html, /公开报道（未经公司确认）/);
  assert.match(html, /暂无团队资料/);
});

test("comparison dimensions are exact product-scoped, actual and cited, not headline-derived", () => {
  const row = { subject_product_id: "own", peer_product_id: "peer", basis_facts: [
    { subject_id: "own", predicate: "buyer_type", value: "药企", basis: "actual", refs: [{}] },
    { subject_id: "own", predicate: "buyer_type", value: "药企", basis: "actual", refs: [{}] },
    { subject_id: "peer", predicate: "buyer_type", value: "医院", basis: "actual", refs: [{}] },
    { subject_id: "own", predicate: "buyer_type", value: "未来客户", basis: "forecast", refs: [{}] },
    { subject_id: "own", predicate: "buyer_type", value: "无来源客户", basis: "actual", refs: [] },
    { subject_id: "other", predicate: "buyer_type", value: "集团客户", basis: "actual", refs: [{}] },
  ] };
  assert.equal(comparisonValue(row, "own", "buyer_type"), "药企");
  assert.equal(comparisonValue(row, "peer", "buyer_type"), "医院");
  assert.equal(comparisonValue(row, "own", "use_case"), "—");
  assert.equal(comparisonValue(row, null, "buyer_type"), "—");
});

test("explicit equity labels enter capital rather than business even in a legacy dossier", () => {
  const html = render(CompanyBasics, { detail: { ...detail, public_profile: { facts: [
    { kind: "business", label: "股权结构", text: "合成股权摘录", refs: [] },
    { kind: "business", label: "经营范围", text: "合成登记范围", refs: [] },
    { kind: "product", label: "产品", text: "合成工具", refs: [] }], updated_at: "2026-10-09" } },
    onRefreshDossier() {}, refreshing: false });
  const main = html.split('class="basic-main"')[1].split("<aside")[0];
  assert.doesNotMatch(main, /合成股权摘录/);
  assert.ok(main.indexOf("合成工具") < main.indexOf("合成登记范围"));
  assert.match(html.split("<aside")[1], /合成股权摘录/);
});

test("bilateral comparison precedes financing and legacy source leads stay folded", () => {
  const html = render(CompetitionView, { detail: { ...detail,
    relationships: { items: [{ scope_key: "s", origin: "auto_research", support: "supported", status: "current",
      peer_name: "合成同行", subject_product_name: "合成我方产品", peer_product_name: "合成对方产品", relation_type: "direct" }], exclusions: [] },
    landscape: { generated_at: "2026-10-09", evidence_count: 1, competition: { dimensions: ["主要客户"],
      companies: [{ is_self: true, cells: { 主要客户: { text: "一则关于客户的报纸标题", refs: [] } } }] } } },
    financing: React.createElement("section", null, "合成融资模块"), expandAll: true });
  assert.ok(html.indexOf("竞对对照") < html.indexOf("合成融资模块"));
  assert.match(html, /购买方类型/); assert.match(html, /本公司/); assert.match(html, /对照公司/);
  // Legacy excerpts sit in the folded reference area, after the comparison.
  assert.match(html, /历史整理摘录<\/button><div class="tf-ref-body"><div class="legacy-excerpts">/);
  const comparison = html.split("product-comparison")[1].split("</table>")[0];
  assert.doesNotMatch(comparison, /报纸标题/);
});

test("industry hierarchy separates market, capital, policy and AI interpretation", () => {
  const html = render(IndustryView, { detail: { ...detail, landscape: { generated_at: "2026-10-09", evidence_count: 1,
    industry: { ...ind, facts: [{ kind: "政策", text: "合成地区条件", refs: [] },
      { kind: "资本", text: "合成资本摘录", refs: [] }], metrics: [
      { name: "合成规模", period: "2030", value: 100, unit: "亿元", basis: "forecast", refs: [] }] } } } });
  assert.match(html, /id="industry-synthetic-market"/); assert.match(html, /id="industry-synthetic-capital"/);
  assert.match(html, /id="industry-synthetic-policy"/); assert.match(html, /预测 \/ 估计/);
  assert.match(html, /<details[^>]+industry-interpretation/); assert.match(html, /AI 整理/);
  const policy = html.split('class="card card-pad industry-policy"')[1].split('class="card card-pad industry-interpretation"')[0];
  assert.match(policy, /合成地区条件/); assert.doesNotMatch(policy, /合成资本摘录/);
  assert.doesNotMatch(html, /行业热度|行业景气升温/);
});

test("missing industry abstract shows located source excerpts rather than an empty hero", () => {
  const html = render(IndustryView, { detail: { ...detail, landscape: { generated_at: "2026-10-09", evidence_count: 1,
    industry: { ...ind, overview: "", facts: [{ kind: "技术", text: "合成技术摘录", refs: [] }] } } } });
  const abstract = html.split('class="industry-abstract"')[1].split('class="activity-panel"')[0];
  assert.match(abstract, /资料摘录/); assert.match(abstract, /合成技术摘录/);
  assert.doesNotMatch(abstract, /暂无行业摘要/);
});

test("activity bars preserve actual count ratios, zero values, window and meaning", () => {
  const html = render(ActivityPanel, { ind: { ...ind, heat_index: { window_days: 30, baseline_days: 90,
    signals: [{ key: "news", name: "资讯", recent: 8, baseline: 4 }, { key: "policy", name: "政策", recent: 0, baseline: 2 }] } } });
  assert.match(html, /资讯活跃度/); assert.match(html, /近 30 天 8，此前同期均值 4/);
  assert.match(html, /width="240"/); assert.match(html, /width="120"/); assert.match(html, /width="0"/);
  assert.match(html, /不代表行业景气度/); assert.match(html, /此前 90 天/);
});

test("missing and invalid activity never synthesizes trend charts", () => {
  for (const input of [ind, { ...ind, heat_index: { signals: [{ recent: NaN, baseline: -1 }] } }]) {
    const html = render(ActivityPanel, { ind: input });
    assert.match(html, /暂无可对照的报道计数/);
    assert.doesNotMatch(html, /<svg|NaN|升温/);
  }
});

test("responsive styles reuse theme tokens and contain only intentional table overflow", () => {
  const css = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
  assert.match(css, /\.basic-editorial \{[^}]*minmax\(0, 2fr\)/);
  assert.match(css, /\.landscape \.table-scroll \{[^}]*overflow-x: auto/);
  assert.match(css, /\.activity-recent \{ fill: var\(--brass\)/);
  assert.match(css, /\.competition-reports \.move \{ grid-template-columns: 86px 110px minmax\(0, 1fr\)/);
  assert.match(css, /\.competition-reports \.move > div \{ grid-column: 2/);
  assert.match(css, /@media \(max-width: 760px\)[\s\S]*\.basic-editorial \{ grid-template-columns: 1fr/);
});

test("portrait leads with what the company does and keeps every profile section in the open dossier", () => {
  const profile = { one_liner: "合成公司做什么", summary: "合成概述", industry: "合成行业全称", stage: "合成阶段", documents: 2,
    highlights: [{ text: "合成亮点", refs: [] }], risks: [], questions: [],
    milestones: [{ date: "2025-01", status: "done", text: "合成已完成事项", refs: [] }, { date: "2027", status: "planned", text: "合成计划事项", refs: [] }],
    sections: [{ title: "业务与产品", points: [{ text: "合成产品", refs: [] }] }, { title: "经营进展", points: [{ text: "合成经营进展", refs: [] }] }] };
  const props = { detail: { ...detail, profile }, onRefreshDossier() {}, refreshing: false };
  const portrait = render(CompanyPortrait, props);
  assert.match(portrait, /合成公司做什么/); assert.match(portrait, /合成概述/);
  assert.match(portrait, /据 2 份内部资料由 AI 整理/);
  assert.doesNotMatch(portrait, /profile-side|profile-mark/); // identity stays in the project header
  const dossier = render(CompanyBasics, props);
  assert.match(dossier, /<h3>公司档案<\/h3>/);
  assert.match(dossier, /经营进展.*合成经营进展/s); // sections outside the fixed rules are not dropped
  assert.match(dossier, /合成亮点/);
  assert.match(dossier, /milestone-line/); assert.match(dossier, /class="planned"/);
  assert.doesNotMatch(dossier, /<details class="ov-fold"/);
});

test("five sections (8945) group the original pages; old ?tab= links still land on their page", () => {
  const project = readFileSync(new URL("../src/pages/Project.tsx", import.meta.url), "utf8");
  for (const [section, pages] of [["当前判断", ['"brief"']], ["项目总览", ['"overview"']], ["内部信息", ['"numbers"', '"documents"']],
    ["外部信息", ['"external"', '"trials"', '"papers"', '"patents"', '"competition"', '"industry"']], ["监控范围", ['"plan"']]]) {
    const at = project.indexOf(`label: "${section}"`);
    assert.ok(at > 0, section);
    const line = project.slice(at, project.indexOf("] },", at));
    for (const page of pages) assert.ok(line.includes(`id: ${page}`), `${section} holds ${page}`);
  }
  assert.match(project, /insight_generation === true \? "brief" : "overview"/);  // full app opens judgment; Core opens overview
  assert.ok(project.indexOf('label: "当前判断"') < project.indexOf('label: "项目总览"'));  // and it is the first section
  assert.match(project, /className="subtabs"/);
  const { CompanyOverview } = load("../src/overview.tsx");
  const html = render(CompanyOverview, { detail: { ...detail, profile: { one_liner: "合成公司做什么", sections: [], highlights: [], milestones: [] },
    brief: { headline: "合成判断标题", tone: "watch", summary: "合成判断", updated_at: "2026-10-01" } }, onReload() {} });
  assert.doesNotMatch(html, /合成公司做什么/); // introduction belongs to the shared company header
  assert.match(html, /公司档案/); assert.match(html, /业务与产品/);
  assert.doesNotMatch(html, /合成判断标题|本期变化|跟踪问题/);
});

test("overview keeps the full company summary in an expandable section", () => {
  const { CompanyOverview } = load("../src/overview.tsx");
  const overview = (summary) => render(CompanyOverview, { detail: { ...detail,
    profile: { one_liner: "合成公司介绍", summary, sections: [], highlights: [], milestones: [] } }, onReload() {} });
  const short = overview("合成摘要".repeat(25));
  assert.doesNotMatch(short, /class="clamp-3"/);
  const long = overview("合成摘要".repeat(40));
  assert.match(long, /<details class="cd-abstract"><summary>完整公司介绍<\/summary>/);
  assert.ok(long.includes("合成摘要".repeat(40)));
});

test("工商档案 folds below the dossier and distinguishes registration from headquarters", () => {
  const facts = [
    { kind: "location", label: "总部", text: "总部位于合成市", refs: [] },
    { kind: "location", label: "注册地址", text: "合成市合成区合成路1号", refs: [{ url: "https://example.invalid/r", source: "合成登记站", title: "", quote: "合成路1号" }] },
  ];
  const html = render(CompanyRegistry, { detail: { ...detail, project: { ...detail.project, location: "" }, public_profile: { facts, updated_at: "2026-10-10" } } });
  assert.match(html, /工商档案/); assert.doesNotMatch(html, /<details/);
  assert.match(html, /注册地址.*合成市合成区合成路1号.*合成登记站/s);
  assert.match(html, /所在地.*总部位于合成市/s);
  assert.match(html, /合成项目有限公司/);
  assert.match(render(CompanyRegistry, { detail }), /注册地址.*尚未取得/s);
  const { CompanyOverview } = load("../src/overview.tsx");
  const overview = render(CompanyOverview, { detail: { ...detail, public_profile: { facts, updated_at: "2026-10-10" } }, onReload() {} });
  assert.match(overview, /aria-label="工商档案"/);
  assert.match(overview, /注册地址.*合成市合成区合成路1号.*合成登记站/s);
  assert.match(overview, /业务与产品/);
});

test("外部信息 is navigated by its count strip: one cell per page, research pages read structured fields", () => {
  const project = readFileSync(new URL("../src/pages/Project.tsx", import.meta.url), "utf8");
  assert.match(project, /\{ page: "industry", label: "行业与政策", cats: \["industry", "policy"\], always: true \}/);
  // 内部信息 and 外部信息 use the same count strip, so their sub-navigation looks and works alike
  assert.match(project, /section\.id !== "external" && section\.id !== "internal" && <nav className="subtabs"/);
  assert.match(project, /<PageStrip label="内部信息分区"/);
  const research = readFileSync(new URL("../src/researchPages.tsx", import.meta.url), "utf8");
  assert.match(research, /x\.nct_id/); assert.match(research, /e\.openalex_id/); assert.match(research, /x\.cited_by/);
  assert.match(research, /登记不代表已完成或获批/);
  assert.match(research, /没有接入国家知识产权局或专利数据库/);  // patents say plainly how thin the source is
  assert.match(research, /既不能代表本公司的专利布局/);  // and that the hits are mostly other companies'
  assert.match(research, /其他专利与注册线索/); // no name match is not proof of another patent owner
  const news = readFileSync(new URL("../src/landscape.tsx", import.meta.url), "utf8");
  assert.match(news, /<NewsStats items=\{own\}/);  // 公司进展 leads with how much actually moved, not a financing table
  assert.match(news, /key: "events", title: "事件进展"/);  // stages move into the folded references
});

test("项目总览 lists the research questions with their latest status, newest judgment first", () => {
  const overview = readFileSync(new URL("../src/overview.tsx", import.meta.url), "utf8");
  assert.match(overview, /<ResearchQuestions projectId=/);
  assert.match(overview, /尚未判断/);
  assert.match(overview, /\?tab=plan`\}>在监控范围维护/);
});

test("内部信息 has five pages read from internal materials, each with page-numbered sources", () => {
  const project = readFileSync(new URL("../src/pages/Project.tsx", import.meta.url), "utf8");
  for (const page of ['"ops"', '"business"', '"numbers"', '"fin"', '"risks"', '"documents"']) assert.match(project, new RegExp(`id: ${page}`));
  const fa = readFileSync(new URL("../src/financeAnalysis.tsx", import.meta.url), "utf8");
  for (const check of ["收入现金含量", "利润现金含量", "盈利趋势", "报表覆盖"]) assert.match(fa, new RegExp(check));
  assert.match(fa, /资料未披露同期经营活动现金流净额/);  // a missing statement is said, not guessed
  const internal = readFileSync(new URL("../src/internalPages.tsx", import.meta.url), "utf8");
  assert.match(internal, /sections\[m\.id\] !== "operating"/);  // operating metrics come from the finance classification
  assert.match(internal, /已过计划时间，资料中未见完成/);  // planned milestones past their date are flagged, not hidden
  assert.match(internal, /<SignalsPanel data=/); assert.match(internal, /<IssueList issues=/);
  assert.match(internal, /去上传资料 →/);  // projects without materials say what to upload
  const overview = readFileSync(new URL("../src/overview.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(overview, /key: "risk"|key: "check"|key: "ask"/);  // moved to 风险与核对, not shown twice
});

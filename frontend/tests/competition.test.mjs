import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire, Module } from "node:module";
import { fileURLToPath } from "node:url";
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
const { CompetitionView, NewsView } = load("../src/landscape.tsx");
const { ToastProvider } = load("../src/motion.tsx");
const render = (detail) => renderToStaticMarkup(React.createElement(StaticRouter, null,
  React.createElement(ToastProvider, null, React.createElement(CompetitionView, { detail, expandAll: true }))));
const detail = {
  project: { id: "synthetic", name: "合成目标", activated: false }, jobs: [], model_ready: false,
  plan: { competitors: [{ name: "合成候选甲", reason: "同属行业，因此高威胁" }] },
};

test("placeholder candidates are hidden across legacy, research and relationship sources", () => {
  const html = render({ ...detail, plan: { competitors: [{ name: "未知" }, { name: "未知科技" }, { name: "AI" }] },
    competition_research: { candidates: [{ name: "ＵＮＫＮＯＷＮ", id: "x" }] },
    relationships: { items: [{ peer_name: "待补充", origin: "user_override", scope_key: "p" }], exclusions: [] } });
  assert.doesNotMatch(html, /<h4>未知<|ＵＮＫＮＯＷＮ|<h4>待补充</);
  assert.match(html, /未知科技/); assert.match(html, /<h4>AI</);
});

test("quantity display preserves source records without inventing a rank or a pair", () => {
  const { QuantityRecords } = load("../src/race.tsx");
  const row = { company: "合成甲", metric: "出货量", raw_value: "出货量10万台", date: "2026-09-01",
    title: "合成甲2026Q1出货量10万台", url: "https://news.example/a", source: "媒体", scope: {} };
  const html = renderToStaticMarkup(React.createElement(QuantityRecords, { data: { records: [row], groups: [], ranked: false } }));
  assert.match(html, /来源记录/); assert.match(html, /https:\/\/news.example\/a/);
  assert.doesNotMatch(html, /同口径数字|领先|排名|落后/);
  assert.equal(renderToStaticMarkup(React.createElement(QuantityRecords, {})), "");
});

test("paired quantity table states explicit period and product without sorting values", () => {
  const { QuantityRecords } = load("../src/race.tsx");
  const scope = { period: ["2026Q1"], product: ["激光雷达"], segment: ["ADAS"], region: ["CN"] };
  const rows = ["甲", "乙"].map((company, i) => ({ company, metric: "出货量", raw_value: `出货量${10 - i}万台`,
    date: "2026-09-01", title: `${company}出货量`, url: "https://news.example/a", source: "媒体", scope }));
  const html = renderToStaticMarkup(React.createElement(QuantityRecords, {
    data: { records: rows, groups: [{ scope, metric: "出货量", unit: "台", records: rows }], ranked: false } }));
  assert.match(html, /2026Q1 · 激光雷达 · ADAS · 中国/);
  assert.match(html, /同口径数字/); assert.doesNotMatch(html, /领先|落后/);
});

test("legacy schema errors never expose validation internals or source inputs", () => {
  const { Progress } = load("../src/pages/Project.tsx");
  const html = renderToStaticMarkup(React.createElement(StaticRouter, null, React.createElement(Progress, {
    detail: { ...detail, documents: [], pending_documents: 0, jobs: [{ kind: "details", status: "failed",
      created_at: "2026-10-09", error: "2 validation errors for Proposal events.1.state input_value='private-value' errors.pydantic.dev" }] }, onReload: () => {},
  })));
  assert.match(html, /部分字段未能正确识别/);
  assert.doesNotMatch(html, /private-value|validation errors|pydantic/);
});

test("partial detail extraction shows retained results rather than full success", () => {
  const { Progress } = load("../src/pages/Project.tsx");
  const html = renderToStaticMarkup(React.createElement(Progress, { detail: { ...detail, documents: [], jobs: [],
    detail_summary: { status: "partial", fact_count: 3, rejected: 1, unquantified: 1 } }, onReload: () => {} }));
  assert.match(html, /本次整理保留 3 条结果/);
  assert.match(html, /1 条线索未采纳/);
  assert.match(html, /1 条金额未明确/);
  assert.doesNotMatch(html, /自动监控中|全部完成/);
});

test("only current bilateral support is presented as a supported relationship", () => {
  const base = { origin: "auto_research", relation_type: "direct", subject_product_name: "筛选工具", peer_product_name: "软件平台" };
  const html = render({ ...detail, relationships: { exclusions: [], items: [
    { ...base, scope_key: "current", peer_name: "当前支持公司", support: "supported", status: "current" },
    { ...base, scope_key: "old", peer_name: "旧版公司", support: "supported", status: "stale" },
    { ...base, scope_key: "changed", peer_name: "身份变更公司", support: "supported", status: "current", scope_identity_changed: true },
  ] } });
  assert.match(html, /竞对对照/);
  assert.doesNotMatch(html, /竞争关系待核验|双方核验未完成/);
  const supported = html.split("竞对对照")[1].split("</table>")[0];
  assert.match(supported, /当前支持公司/);
  assert.doesNotMatch(supported, /旧版公司|身份变更公司/);
  assert.match(html, /<h4>旧版公司<\/h4>/);
  assert.match(html, /<h4>身份变更公司<\/h4>/);
});

test("bilateral source dimensions have on-demand stored excerpts", () => {
  const html = render({ ...detail, relationships: { exclusions: [], items: [{ origin: "auto_research", scope_key: "s",
    peer_name: "合成乙", subject_product_id: "p", subject_product_name: "合成甲产品", peer_product_name: "合成乙产品",
    relation_type: "direct", support: "supported", status: "current", created_at: "2026-10-08",
    basis_facts: [{ fact_id: "f", subject_id: "p", predicate: "buyer_type", value: "药企", basis: "actual",
      refs: [{ evidence_version_id: "v", part_index: 0, start: 0, end: 20 }] }] }] } });
  assert.match(html, /双方依据 · 2026-10-08/); assert.match(html, /购买方类型：药企/);
  assert.match(html, /查看依据/); assert.match(html, /原文 1/);
});

test("legacy storylines do not claim completed business progress or silence", () => {
  const { Storylines } = load("../src/monitor.tsx");
  const html = renderToStaticMarkup(React.createElement(Storylines, { threads: [{ id: "synthetic", kind: "订单",
    title: "合成线索", status: "done", reached: 1, stages: ["签约", "交付"], last_at: "2026-10-08",
    steps: [{ event_id: "e", stage: "交付", date: "2026-10-08", title: "合成报道" }], next_step: "" }] }));
  assert.match(html, /待核对/); assert.match(html, /报道线索/);
  assert.doesNotMatch(html, /已完成|class="track-stop past|没有新进展|停滞/);
});

test("stale brief does not present old opportunities or threats as current", () => {
  const html = renderToStaticMarkup(React.createElement(StaticRouter, null,
    React.createElement(ToastProvider, null, React.createElement(NewsView, { detail: {
      ...detail, counts: {}, brief: { dependency: { status: "stale" }, updated_at: "2026-10-08",
        outlook: { threats: [{ text: "旧竞对威胁", refs: [] }] } },
    } }))));
  assert.match(html, /资料或关系已更正/);
  assert.doesNotMatch(html, /旧竞对威胁/);
});

test("planned peers are immediately visible with simple confirmation and removal", () => {
  const html = render(detail);
  assert.match(html, /<li class="rival-row open"/);
  assert.match(html, /确认 合成候选甲/);
  assert.match(html, /移除 合成候选甲/);
  assert.doesNotMatch(html, /待核验|尚未确认竞争关系/);
  assert.match(html, /合成候选甲/);
  assert.doesNotMatch(html, /rival-kpis|高威胁|同属行业|逐项对比|跟踪的竞品/);
});

test("legacy result cannot bypass safety projection in the client", () => {
  const html = render({ ...detail, landscape: {
    generated_at: "2026-10-01", evidence_count: 1, moves: [],
    competition: { summary: "危险的竞争结论", dimensions: ["技术路线"], claims: [],
      companies: [{ name: "合成候选乙", is_self: false, threat: "high", threat_reason: "客户完全重叠",
        cells: { "技术路线": { text: "投资银行评级", refs: [] } } }] },
  } });
  assert.match(html, /<h4>合成候选乙<\/h4>/);
  assert.match(html, /合成候选乙/);
  assert.doesNotMatch(html, /危险的竞争结论|客户完全重叠|投资银行评级|rival-kpis|高威胁/);
});

test("reports keep original titles without claiming competitor action or impact", () => {
  const html = render({ ...detail, landscape: {
    generated_at: "2026-10-01", evidence_count: 1,
    moves: [{ id: "e", title: "合成公司融资报道", date: "2026-10-01", topic: "融资", type: "其他",
      company: "", mentioned_companies: ["合成候选甲"], attribution_status: "pending", impact: "错误影响结论" }],
  } });
  assert.match(html, /相关报道/);
  assert.match(html, /合成公司融资报道/);
  assert.doesNotMatch(html, /错误影响结论|竞品动作|最活跃/);
});

test("user-selected relationships are labeled and excluded companies do not reappear as candidates", () => {
  const html = render({ ...detail, relationships: {
    items: [{ scope_key: "scope-B", peer_name: "合成候选乙", subject_product_name: "药筛工具", peer_product_name: "替代平台",
      origin: "user_override", support: "user_specified", relation_type: "substitute" }],
    exclusions: [{ scope_key: "scope-A", peer_name: "合成候选甲", peer_names: ["合成候选甲"], company_wide: true,
      subject_product_name: "公司整体", peer_product_name: "公司整体" }],
  } });
  assert.match(html, /已确认/);
  assert.match(html, /已移除/);
  const board = html.split('aria-label="竞争对手"')[1].split("</section>")[0];
  assert.doesNotMatch(board, /合成候选甲/);
  assert.doesNotMatch(html, /待核验候选/);
});

test("source-backed profiles and reports are readable before confirmation", () => {
  const ref = { id: "r", title: "合成候选甲产品介绍", quote: "合成候选甲提供合成筛选平台", url: "https://example.test/peer" };
  const html = render({ ...detail, project: { ...detail.project, config_version: 1 }, landscape: {
    generated_at: "2026-10-09", evidence_count: 1,
    competition: { companies: [], dimensions: [], profiles: [{ name: "合成候选甲",
      cells: { 产品: { text: "合成筛选平台", refs: [ref] } } }] },
    moves: [{ id: "m", title: "合成候选甲发布合成新品", url: "https://example.test/news", date: "2026-10-09", type: "产品" }],
  } });
  const board = html.split('aria-label="竞争对手"')[1].split("</section>")[0];
  assert.match(board, /合成筛选平台/); assert.match(board, /合成候选甲发布合成新品/);
  assert.match(board, /example.test\/peer/); assert.match(board, /确认 合成候选甲/);
  assert.doesNotMatch(board, /待核验|尚未完成|高威胁/);
});

test("bilingual display names still show literal Chinese company mentions", () => {
  const html = render({ ...detail, plan: { competitors: [{ name: "合成科技 ExampleTech" }, { name: "合成君（Xexample）" }] },
    landscape: { generated_at: "2026-10-09", evidence_count: 2, moves: [
      { id: "one", title: "合成科技发布新品", date: "2026-10-09", type: "产品" },
      { id: "two", title: "合成君发布新技术", date: "2026-10-09", type: "产品" },
    ] } });
  const board = html.split('aria-label="竞争对手"')[1].split("</section>")[0];
  assert.match(board, /合成科技发布新品/); assert.match(board, /合成君发布新技术/);
  assert.doesNotMatch(board, /暂无资料摘要/);
});

test("project navigation uses meaningful status dots, never unread counts", () => {
  const { default: ProjectNavStatus } = load("../src/ProjectNavStatus.tsx");
  const renderStatus = (project) => renderToStaticMarkup(React.createElement(ProjectNavStatus, { project }));
  const html = renderStatus({ activated: true, status: "active", unread: 123, major_recent: 3, nav_alerts: [] });
  assert.match(html, /status-ok/);
  assert.match(html, /aria-label="监控已启用"/);
  assert.doesNotMatch(html, /99\+|未读|123/);
  assert.match(renderStatus({ activated: true, status: "active", job_status: "running", nav_alerts: [] }), /status-ok.*正在更新/);
  assert.match(renderStatus({ activated: true, status: "active", job_status: "queued", nav_alerts: [] }), /status-ok.*等待更新/);
  assert.match(renderStatus({ activated: true, status: "active", job_status: "running", worker_state: "stopped", nav_alerts: [] }), /status-idle.*自动更新未运行/);
  assert.match(renderStatus({ activated: true, status: "active", worker_state: "stopped", nav_alerts: ["有新的重大事件"] }), /status-alert.*有新的重大事件/);
  assert.match(renderStatus({ activated: true, nav_alerts: ["有新的重大事件", "外部更新未完成"] }), /status-alert.*有新的重大事件；外部更新未完成/);
  assert.match(renderStatus({ activated: true, status: "paused" }), /status-idle.*监控已暂停/);
  assert.match(renderStatus({ activated: false }), /status-idle.*尚未启用监控/);
  assert.match(renderStatus({ activated: true, status: "active" }), /status-idle.*监控状态暂不可用/);
});

test("queued and offline work never renders as executing, but running parsing stays separate", () => {
  const { Progress } = load("../src/pages/Project.tsx");
  const base = { ...detail, project: { ...detail.project, activated: true, status: "active" }, documents: [],
    pending_documents: 0, model_ready: true, execution: { analysis: "running", parsing: "stopped" },
    jobs: [{ kind: "external", status: "queued", stage: "旧执行阶段" }] };
  const html = d => renderToStaticMarkup(React.createElement(Progress, { detail: d, onReload() {} }));
  assert.match(html(base), /等待更新.*排队中/);
  assert.doesNotMatch(html(base), /正在更新|旧执行阶段/);
  const offline = { ...base, execution: { analysis: "stopped", parsing: "stopped" } };
  assert.match(html(offline), /自动更新未运行.*等待后台启动/);
  assert.doesNotMatch(html(offline), /working-dot|正在更新/);
  assert.match(html({ ...base, jobs: [{ kind: "external", status: "running", stage: "本次阶段" }] }), /正在更新.*本次阶段/);
  assert.match(html({ ...offline, project: { ...base.project, status: "paused" } }), /监控已暂停/);
  assert.match(html({ ...offline, documents: [{ status: "processing" }], execution: { analysis: "stopped", parsing: "running" } }), /正在更新.*正在解析/);
  assert.match(html({ ...base, jobs: [{ kind: "external", status: "running" }], execution: { analysis: "stopping", parsing: "stopped" } }), /正在结束当前更新/);
});

test("industry summary gaps do not claim first search is pending", () => {
  const { IndustryView } = load("../src/landscape.tsx");
  const html = renderToStaticMarkup(React.createElement(StaticRouter, null, React.createElement(ToastProvider, null,
    React.createElement(IndustryView, { detail: { ...detail, execution: { analysis: "stopped", parsing: "stopped" },
      project: { ...detail.project, last_external_at: "2026-01-01" } } }))));
  assert.match(html, /行业摘要尚未整理/);
  assert.match(html, /自动更新未运行，已有报道保留在下方/);
  assert.doesNotMatch(html, /等待首次外部检索/);
});

test("company navigation owns a fresh workspace instead of reusing old company data and dialogs", () => {
  const source = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(source, /const routeKey = section === "projects" \? location\.pathname : section/);
  assert.match(source, /<AnimatePresence key=\{section === "projects" \? routeKey : "sections"\}/);
  assert.match(source, /<Routes location=\{location\} key=\{routeKey\}>/);
  const { default: ProjectPage } = load("../src/pages/Project.tsx");
  const html = renderToStaticMarkup(React.createElement(StaticRouter, null,
    React.createElement(ProjectPage, { onChanged() {} })));
  assert.match(html, /加载项目/);
  assert.doesNotMatch(html, /立即更新|暂停监控/);
});

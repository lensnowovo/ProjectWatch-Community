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
const entry = fileURLToPath(new URL("../src/judgment.tsx", import.meta.url));
const mod = new Module(entry);
mod.paths = require.resolve.paths("react");
mod._compile(buildSync({ entryPoints: [entry], bundle: true, write: false, platform: "node", format: "cjs",
  packages: "external", loader: { ".css": "empty" } }).outputFiles[0].text, entry);
const { PositionJudgment, IndustryJudgment, TechnologyJudgment, EvidenceGaps, verdictTone } = mod.exports;
const source = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const render = (el) => renderToStaticMarkup(React.createElement(StaticRouter, { location: "/projects/x" }, el));

const ref = (code, label) => ({ code, kind: "public", label, text: `合成来源 ${code}`, url: `https://example.com/${code}` });
const view = {
  record: { generated_at: "2026-10-14T00:00:00Z", counts: { N: 3, P: 1 }, total: 4, model: "synthetic",
    analysis: {
      headline: "合成总判断",
      position: { verdict: "behind", confidence: "medium", summary: "合成竞争摘要", peers: ["合成对手甲"],
        for: [{ text: "对手已进入后期", refs: [ref("N1", "新闻")] }], against: [{ text: "平台独家", refs: [] }], change_if: ["中期读数优于同类"] },
      technology: { route: "合成技术路线", maturity: "advanced", consensus: "forming", confidence: "low", summary: "合成技术摘要",
        academic: [{ text: "论文信号", refs: [ref("P1", "论文")] }], industry: [], risks: [{ text: "路线风险", refs: [] }] },
      industry: { verdict: "growing", confidence: "high", summary: "合成行业摘要", drivers: [{ text: "政策驱动", refs: [] }], headwinds: [] },
      gaps: ["缺少合成收入明细"], cited: [ref("N1", "新闻"), ref("P1", "论文")],
    } },
  history: [], job: null, model_ready: true, sources: { N: "新闻", P: "论文" },
  labels: {
    position: { lagging: "明显落后", behind: "略落后", on_par: "相当", ahead: "领先", leading: "明显领先" },
    industry: { cooling: "降温", consolidating: "整合", stable: "平稳", growing: "增长", expanding: "扩张" },
    maturity: { dated: "趋于落后", mature: "成熟", mainstream: "主流", advanced: "先进", frontier: "前沿" },
    consensus: { contrarian: "非共识", forming: "正在形成", consensus: "已成共识" },
    operations: {}, chain: {},
  },
};

test("深度分析 is no longer a separate page: the old address lands on the matching tab", () => {
  const project = source("../src/pages/Project.tsx");
  assert.doesNotMatch(project, /tab-deep|\/deep/);
  const app = source("../src/App.tsx");
  assert.match(app, /path="\/projects\/:id\/deep" element=\{<DeepRedirect \/>\}/);
  assert.match(app, /position: "competition", technology: "industry", industry: "industry"/);
  assert.doesNotMatch(app, /pages\/Deep/);
  assert.doesNotMatch(source("../src/styles.css"), /\.deep\b|\.tab-deep/);
});

test("each judgment sits in the tab about its subject", () => {
  assert.match(source("../src/landscape.tsx"), /<PositionJudgment view=/);
  const project = source("../src/pages/Project.tsx");
  assert.match(project, /part="industry"/); assert.match(project, /part="technology"/);
  const overview = source("../src/overview.tsx");
  assert.match(overview, /<EvidenceGaps view=/);
  // equal question tiles choose the question; its chart and conclusion card open in one panel below
  const qa = readFileSync(new URL("../src/questionAnalysis.tsx", import.meta.url), "utf8");
  assert.match(overview, /<QuestionAnalysis detail=\{detail\}/);
  assert.match(qa, /role="tab" aria-selected=\{active === x\.key\}/);
  assert.match(qa, /role="tabpanel" className=\{`qa-panel qa-panel-/);
  for (const ask of ["公司自己的经营与资金撑得住吗？", "和同路线对手比，领先还是落后？", "押注的技术路线是前沿还是趋于落后？", "赛道在扩张还是降温？"])
    assert.match(qa, new RegExp(ask));
  const css = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
  assert.match(css, /\.rj-tiles \{[^}]*align-items: stretch/);
  assert.match(css, /\.rj-tile-summary \{[^}]*-webkit-line-clamp: 3/);
});

test("competitive position shows scale, both sides with sources, peers and what would change it", () => {
  const html = render(React.createElement(PositionJudgment, { view }));
  assert.match(html, /竞争位置判断/);
  assert.match(html, /class="on down"/); // 略落后 marked on the scale as adverse
  for (const text of ["对手已进入后期", "平台独家", "合成对手甲", "中期读数优于同类", "把握中"]) assert.match(html, new RegExp(text));
  assert.match(html, /href="https:\/\/example.com\/N1"/);
});

test("industry and technology judgments render their own parts and nothing without a record", () => {
  const industry = render(React.createElement(IndustryJudgment, { view }));
  assert.match(industry, /驱动/); assert.match(industry, /政策驱动/); assert.match(industry, /没有带出处的相反依据/);
  const tech = render(React.createElement(TechnologyJudgment, { view }));
  for (const text of ["合成技术路线", "成熟度：先进", "共识：正在形成", "论文信号", "路线风险"]) assert.match(tech, new RegExp(text));
  assert.match(tech, /<svg[^>]*aria-label="技术成熟度：先进；行业共识：正在形成"/);
  for (const C of [PositionJudgment, IndustryJudgment, TechnologyJudgment, EvidenceGaps])
    assert.equal(render(React.createElement(C, { view: { ...view, record: null } })), "");
});

test("evidence keeps the AI label, the gaps and every cited source", () => {
  const html = render(React.createElement(EvidenceGaps, { view }));
  assert.match(html, /不是事实结论/);
  assert.match(html, /缺少合成收入明细/);
  assert.match(html, /共引用 2 条/);
});

test("verdict tone: upper half favourable, bottom adverse, middle neutral", () => {
  assert.equal(verdictTone("position", "leading"), "up");
  assert.equal(verdictTone("position", "on_par"), "");
  assert.equal(verdictTone("position", "behind"), "down");
  assert.equal(verdictTone("operations", "pressured"), "down");
  assert.equal(verdictTone("chain", "favorable"), "up");
  assert.equal(verdictTone("industry", "unclear"), "");
});

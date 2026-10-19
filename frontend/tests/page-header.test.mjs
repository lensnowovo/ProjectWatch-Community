import assert from "node:assert/strict";
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
  const component = new Module(entry);
  component.paths = require.resolve.paths("react");
  component._compile(buildSync({ entryPoints: [entry], bundle: true, write: false,
    platform: "node", format: "cjs", packages: "external", loader: { ".css": "empty" },
  }).outputFiles[0].text, entry);
  return component.exports.default;
}
const Header = load("../src/PageHeader.tsx");
const ProjectHeader = load("../src/ProjectHeader.tsx");
const render = (props) => renderToStaticMarkup(React.createElement(Header, {
  title: "项目组合", label: "关注的公司", description: "合成页面说明", ...props,
}));

test("page header keeps hierarchy and native action without decorative content", () => {
  const html = render({ meta: "3 个项目已加入", action: React.createElement("a", { href: "/new" }, "添加项目") });
  assert.match(html, /<h1><span class="header-blur-text" aria-label="项目组合">/);
  assert.doesNotMatch(html, /opacity:0(?:;|")/);
  assert.match(html, /page-header-label/);
  assert.match(html, /page-header-meta[^>]*>3 个项目已加入/);
  assert.match(html, /href="\/new"/);
  assert.doesNotMatch(html, /<img|<canvas|role="button"/);
});

test("optional footer and action leave no empty UI", () => {
  const html = render();
  assert.doesNotMatch(html, /page-header-meta|page-header-action/);
  assert.match(html, /<p>合成页面说明<\/p>/);
});

test("feed with existing projects preserves updating status in the new header", () => {
  const Feed = load("../src/pages/Feed.tsx");
  const html = renderToStaticMarkup(React.createElement(StaticRouter, { location: "/feed" },
    React.createElement(Feed, { projects: [{ id: "synthetic", name: "合成公司", job_status: "running" }] })));
  assert.match(html, /<h1><span class="header-blur-text" aria-label="今日动态">/);
  assert.match(html, /1 个项目正在更新/);
  assert.match(html, /只看未读/);
});

test("working headers stay compact: no artwork or canvas competes with the information", () => {
  for (const variant of ["feed", "portfolio", "digest"]) {
    const html = render({ variant, action: React.createElement("a", { href: "/new" }, "添加项目") });
    assert.match(html, new RegExp(`data-variant="${variant}"`));
    assert.doesNotMatch(html, /header-background|aurora-container|color-bends-container|grainient-container|<canvas/);
    assert.doesNotMatch(html, /page-header-gear|<img|tabindex|role="button"/);
  }
});

const cover = (project, extra = {}, headerProps = {}) => renderToStaticMarkup(React.createElement(StaticRouter, { location: "/projects/synthetic" },
  React.createElement(ProjectHeader, { detail: { project, documents: [], jobs: [], counts: {}, ...extra },
    goto: () => {}, actions: null, ...headerProps })));

test("project header keeps company identity, portfolio navigation, actions and update status", () => {
  const html = cover({ id: "synthetic", name: "合成公司", legal_name: "合成公司测试有限公司",
    industry: "合成测试行业", stage: "A 轮（2024年5月完成）", location: "测试城市", founded: "2020-01-01",
    credit_code: "synthetic-code", website: "example.com", entity_source: "ai_search" }, {}, {
    actions: React.createElement("button", null, "上传资料"),
    status: React.createElement("p", { role: "status" }, "正在整理新资料"),
  });
  assert.match(html, /<h1(?:\s[^>]*)?>合成公司<\/h1>/);
  assert.match(html, /合成公司测试有限公司/);
  assert.match(html, /<a\b[^>]*href="\/projects"[^>]*>[\s\S]*?项目组合<\/a>/);
  assert.match(html, /href="https:\/\/example\.com\/?"/);
  for (const fact of ["合成测试行业", "测试城市"]) assert.match(html, new RegExp(fact));
  assert.match(html, /A 轮/);
  assert.doesNotMatch(html, /2024年5月完成|2020-01-01|synthetic-code/);
  assert.match(html, /<button>上传资料<\/button>/);
  assert.match(html, /role="status">正在整理新资料<\/p>/);
});

test("name-only projects have one identity without invented metadata", () => {
  const html = cover({ id: "synthetic", name: "仅名称项目", legal_name: "仅名称项目" });
  assert.equal((html.match(/仅名称项目/g) ?? []).length, 1);
  assert.doesNotMatch(html, /融资阶段|所在地|成立日期|已由你确认|公司官网|无法判断/);
});

test("header leaves detailed judgments, operating numbers and milestones to the content tabs", () => {
  const html = cover({ id: "synthetic", name: "合成公司", description: "合成项目描述" }, {
    brief: { headline: "合成的一句话判断" },
    profile: { one_liner: "合成业务概括", milestones: [
      { text: "合成中期读数", date: "2027-03", status: "planned", refs: [] },
    ] },
    finance: { key_numbers: { cash: { value: 8830, unit: "万元", period: "2025", refs: [] },
      runway: { months: 20.4, status: "ok" }, revenue: null, revenue_growth: null,
      net_profit: { value: -5308, unit: "万元", period: "2025", refs: [] } } },
  });
  assert.doesNotMatch(html, /合成的一句话判断|合成业务概括|合成项目描述|合成中期读数/);
  assert.doesNotMatch(html, /账上现金|现金跑道|净亏损|四圈判断|下一节点/);
});

test("unconfirmed placeholder metadata is omitted while known facts remain", () => {
  const html = cover({ id: "synthetic", name: "合成公司", industry: "汽车连接器",
    stage: "未确认最新融资轮次", location: "暂无所在地" });
  assert.match(html, /汽车连接器/);
  assert.doesNotMatch(html, /未确认最新融资轮次|暂无所在地/);
  const repeated = cover({ id: "synthetic", name: "合成公司", industry: "合成赛道", location: "合成赛道" });
  assert.equal((repeated.match(/合成赛道/g) ?? []).length, 1);
});

test("company website links support real HTTP destinations and ignore invalid values", () => {
  for (const [website, destination] of [
    ["example.com/about", "https://example.com/about"],
    ["https://example.com/about", "https://example.com/about"],
    ["http://example.com/", "http://example.com/"],
  ]) {
    const html = cover({ id: "synthetic", name: "合成公司", website });
    assert.ok(html.includes(`href="${destination}"`), website);
    assert.match(html, /公司官网/);
  }
  for (const website of ["", "not a website", "javascript:alert(1)", "data:text/html,test"]) {
    const html = cover({ id: "synthetic", name: "合成公司", website });
    assert.doesNotMatch(html, /公司官网/);
  }
});

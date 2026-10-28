import assert from "node:assert/strict";
import { createRequire, Module } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";

// Compile the actual component in memory using Vite's existing esbuild dependency.
// No browser, credentials, model calls or persisted project records are involved.
const require = createRequire(new URL("../package.json", import.meta.url));
const { buildSync } = require("esbuild");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const { StaticRouter } = require("react-router-dom");
const entry = fileURLToPath(new URL("../src/pages/Home.tsx", import.meta.url));
const compiled = buildSync({
  entryPoints: [entry], bundle: true, write: false, platform: "node", format: "cjs",
  packages: "external", loader: { ".css": "empty" },
}).outputFiles[0].text;
const component = new Module(entry);
component.paths = require.resolve.paths("react");
component._compile(compiled, entry);
const Home = component.exports.default;
const render = (props) => renderToStaticMarkup(React.createElement(StaticRouter, { location: "/" },
  React.createElement(Home, { modelReady: false, error: null, onRetry() {}, ...props })));

test("empty cover keeps the original artwork and BP entry", () => {
  const html = render({ projects: [] });
  assert.match(html, /hero-waves/);
  assert.match(html, /输入公司名/);
  assert.match(html, /添加第一个项目/);
  assert.match(html, /href="\/new"/);
  assert.match(html, /先配置模型/);
});

test("existing projects retain the cover with feed and portfolio destinations", () => {
  const html = render({ projects: [{ id: "synthetic", name: "合成公司", activated: false }], modelReady: true });
  assert.match(html, /hero-waves/);
  assert.match(html, /持续看见变化/);
  assert.doesNotMatch(html, /个项目已加入|最近变化|需要你处理/);  // the cover says nothing about any project
  assert.match(html, /href="\/feed"/);
  assert.match(html, /href="\/projects"/);
  assert.match(html, /href="\/new"/);
  assert.doesNotMatch(html, /添加第一个项目|正在监控|先配置模型/);
});

test("loading does not briefly report an empty portfolio", () => {
  const html = render({ projects: null });
  assert.match(html, /hero-waves/);
  assert.match(html, /正在读取项目/);
  assert.doesNotMatch(html, /添加第一个项目|0 个项目/);
});

test("failed initial reading offers retry without claiming there are no projects", () => {
  const html = render({ projects: null, error: "暂时无法读取项目，请重试。" });
  assert.match(html, /hero-waves/);
  assert.match(html, /重新读取项目/);
  assert.match(html, /暂时无法读取项目/);
  assert.doesNotMatch(html, /添加第一个项目|正在读取项目/);
});

test("configured empty portfolio does not ask to configure a model again", () => {
  const html = render({ projects: [], modelReady: true });
  assert.match(html, /添加第一个项目/);
  assert.doesNotMatch(html, /先配置模型/);
});

test("working routes stay free of line artwork and retain a real home link", () => {
  const appEntry = fileURLToPath(new URL("../src/App.tsx", import.meta.url));
  const appModule = new Module(appEntry);
  appModule.paths = require.resolve.paths("react");
  appModule._compile(buildSync({
    entryPoints: [appEntry], bundle: true, write: false, platform: "node", format: "cjs",
    packages: "external", loader: { ".css": "empty", ".svg": "dataurl" },
  }).outputFiles[0].text, appEntry);
  for (const location of ["/feed", "/projects", "/digest", "/projects/synthetic", "/new", "/settings"]) {
    const html = renderToStaticMarkup(React.createElement(StaticRouter, { location },
      React.createElement(appModule.exports.default)));
    assert.doesNotMatch(html, /hero-waves|sig-field/, location);
    assert.match(html, /<a(?=[^>]*href="\/")(?=[^>]*aria-label="项目监控首页")[^>]*>/, location);
    const sidebar = html.match(/<aside class="sidebar">([\s\S]*?)<\/aside>/)?.[1];
    assert.ok(sidebar, location);
    assert.match(sidebar, /<a(?=[^>]*href="\/new")(?=[^>]*class="[^"]*sidebar-add)[^>]*>/, location);
    assert.equal((sidebar.match(/href="\/new"/g) ?? []).length, 1, location);
    assert.ok(sidebar.indexOf("sidebar-add") < sidebar.indexOf("search-trigger"), location);
    const navigation = sidebar.match(/<nav class="nav">([\s\S]*?)<\/nav>/)?.[1];
    assert.doesNotMatch(navigation, /添加项目|href="\/new"/, location);
    if (["/feed", "/projects", "/digest"].includes(location)) {
      assert.match(html, /<header class="page-header" data-variant="(?:feed|portfolio|digest)">/, location);
      assert.match(html, /class="page page-workspace"/, location);
      assert.equal((html.match(/<h1\b/g) ?? []).length, 1, location);
      assert.doesNotMatch(html, /<header class="masthead/, location);
    } else {
      assert.doesNotMatch(html, /<header class="page-header"/, location);
      if (location === "/settings") {
        assert.match(html, /<header class="settings-heading"><h1>设置<\/h1>/);
        assert.match(html, /class="page page-workspace"/);
        assert.equal((html.match(/<h1\b/g) ?? []).length, 1);
        assert.match(sidebar, /<polygon/); // Sidebar gear remains.
        assert.doesNotMatch(html, /header-background|page-header-gear|<canvas/);
      }
    }
  }
  const home = renderToStaticMarkup(React.createElement(StaticRouter, { location: "/" },
    React.createElement(appModule.exports.default)));
  assert.match(home, /hero-waves/);
});

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// Copy contracts: compact interface text must not erase disclosure or actions.
// Actual rendering/routing is covered by home and page-header tests and UI checks.
const source = (path) => readFileSync(new URL(`../src/${path}`, import.meta.url), "utf8");

test("upload prompt stays compact and preserves entry points", () => {
  const text = source("upload.tsx");
  assert.match(text, /上传后自动分析，并更新项目信息。/);
  assert.match(text, /拖入文件，或点击选择/);
  assert.match(text, /PDF \/ Word \/ PPT \/ Excel \/ TXT · Ctrl\+V 粘贴/);
  assert.match(text, /aria-label="网页或 PDF 链接"/);
  assert.match(text, /仅支持公开链接 · 原件本机保存/);
  assert.match(text, /读取链接/);
  assert.match(text, /onDrop=/);
  assert.match(text, /addEventListener\("paste"/);
  assert.match(text, /createPortal/);
  assert.doesNotMatch(text, /加入后自动识别并纳入分析|例如公司公告、新闻稿、招股书|依赖脚本的页面会提示/);
});

test("upload layout has generous desktop space and short-viewport limits", () => {
  const css = source("styles.css");
  assert.match(css, /\.upload-modal \{ min-height: min\(520px, calc\(100dvh - 32px\)\); max-height: calc\(100dvh - 32px\)/);
  assert.match(css, /\.upload-drop \{ min-height: 220px; flex-shrink: 0/);
  assert.match(css, /\.upload-modal \.modal-foot \{ margin-top: 16px; padding-top: 0/);
  assert.doesNotMatch(css, /\.upload-modal \.modal-foot \{ margin-top: auto/);
  assert.match(css, /@media \(max-height: 640px\)/);
  assert.match(css, /\.upload-drop \{ min-height: 160px/);
});

test("project consent still discloses model transfer and public search", () => {
  const text = source("pages/NewProject.tsx");
  assert.match(text, /份资料将发送至模型/);
  assert.match(text, /后续资料将发送至配置的模型分析/);
  assert.match(text, /公司、产品、核心人员与竞品名称用于公开新闻、论文、临床与专利检索/);
  assert.match(text, /AI 整理的内容都附原文出处/);
  assert.match(text, /disabled=\{!consent/);
  assert.doesNotMatch(text, /每份 ≤20MB/);
});

test("settings retains service costs, key destination and test usage", () => {
  const text = source("pages/Settings.tsx");
  assert.match(text, /密钥本机保存，仅发送给对应服务商/);
  assert.match(text, /费用与套餐额度以服务商为准/);
  assert.match(text, /服务商分别计费/);
  assert.match(text, /连接测试各调用一次/);
  assert.match(text, /未设用量上限/);
  assert.match(text, /preset\.hint/); // Endpoint/plan-specific boundaries remain visible.
});

test("shorter evidence notes preserve uncertainty and fact boundaries", () => {
  assert.match(source("overview.tsx"), /公开报道（未经公司确认）/);
  assert.match(source("overview.tsx"), /未识别页面不参与分析/);
  assert.doesNotMatch(source("landscape.tsx"), /竞争关系待核验|待核验候选|尚未完成双方核验/);
  assert.match(source("landscape.tsx"), /已确认/);
  assert.match(source("landscape.tsx"), /EvidenceLinks refs=\{cell.refs\}/);
  assert.match(source("landscape.tsx"), /预测 \/ 估计/);
  assert.match(source("metrics.tsx"), /比率与现金跑道按公式计算/);
  assert.match(source("signals.tsx"), /AI 对照仅作追问线索/);
  assert.match(source("pages/Feed.tsx"), /暂无收录动态/); // Describes collected records, not absence of real events.
});

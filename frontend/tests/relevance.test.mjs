import assert from "node:assert/strict";
import { createRequire, Module } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";

const require = createRequire(new URL("../package.json", import.meta.url));
const { buildSync } = require("esbuild");
const entry = fileURLToPath(new URL("../src/relevance.ts", import.meta.url));
const mod = new Module(entry);
mod._compile(buildSync({ entryPoints: [entry], bundle: true, write: false, platform: "node", format: "cjs" }).outputFiles[0].text, entry);
const { focus, sameStory, rank, dedupe } = mod.exports;

test("reposts of one story collapse; unrelated stories do not", () => {
  assert.ok(!sameStory("合成药物临床试验获批启动", "合成药物临床试验因安全问题暂停"));
  assert.ok(!sameStory("合成药物临床试验获批启动", "合成药物临床试验获批启动后暂停"));
  assert.ok(sameStory("688089，将被“ST”", "688089,将被ST"));
  assert.ok(!sameStory("巨子生物：二零二六中期报告", "嘉必优：2026年半年度报告摘要"));
});

test("major and company-relevant items lead; 无直接关联 and routine fold away", () => {
  const items = [
    { title: "行业政策A", importance: "notable", impact: "与已知计划无直接关联。", date: "2026-10-12" },
    { title: "竞品获批上市", importance: "major", impact: "直接竞争加剧", date: "2026-09-01" },
    { title: "股价日报", importance: "routine", impact: "", date: "2026-10-13" },
    { title: "新规利好原料备案", importance: "notable", impact: "利好公司注册进度", date: "2026-10-10" },
    { title: "新规利好原料备案", importance: "notable", impact: "利好公司注册进度", date: "2026-10-10" },
  ];
  assert.deepEqual(rank(items).map((i) => i.title)[0], "竞品获批上市");
  const { top, rest, duplicates } = focus(items, 3);
  assert.deepEqual(top.map((i) => i.title), ["竞品获批上市", "新规利好原料备案"]);
  assert.deepEqual(rest.map((i) => i.title).sort(), ["股价日报", "行业政策A"].sort());
  assert.equal(duplicates, 1);
});

test("equal headlines on different dates are separate updates", () => {
  const items = ["2026-09-01", "2026-10-01"].map((date) => ({ title: "合成公司月度进展", date }));
  assert.equal(dedupe(items).length, 2);
});

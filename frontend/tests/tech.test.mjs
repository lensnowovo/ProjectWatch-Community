import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const tech = readFileSync(new URL("../src/tech.tsx", import.meta.url), "utf8");
const monitor = readFileSync(new URL("../src/monitor.tsx", import.meta.url), "utf8");
const landscape = readFileSync(new URL("../src/landscape.tsx", import.meta.url), "utf8");

test("technical routes are system-picked first, editable, and charted with a legend and hover values", () => {
  assert.match(tech, /技术标签由系统从已收录材料中提取/);
  assert.match(tech, /调整路线/);
  assert.match(tech, /恢复系统建议/);
  assert.match(tech, /tech-legend/);
  assert.match(tech, /<title>/);
  assert.match(tech, /不是市场份额/);
  assert.match(tech, /不代表技术领先或产业共识/);
  assert.match(tech, /不同维度的标签可并存/);
  assert.doesNotMatch(tech, /往哪边倒|反映关注度/);
  assert.doesNotMatch(tech, /核实|核验/);
});

test("watch points come with system suggestions and no verify-it-yourself column", () => {
  assert.match(monitor, /系统建议关注/);
  assert.match(landscape, /export function watchSuggestions/);
  assert.match(landscape, /title="值得追问"/);
  assert.doesNotMatch(landscape, /title="需要核实"/);
});

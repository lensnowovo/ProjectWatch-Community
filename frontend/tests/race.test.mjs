import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const race = readFileSync(new URL("../src/race.tsx", import.meta.url), "utf8");
const project = readFileSync(new URL("../src/pages/Project.tsx", import.meta.url), "utf8");

test("race board shows evidence grades and keeps missing data visible", () => {
  assert.match(race, /公示/);
  assert.match(race, /数据商/);
  assert.match(race, /公司宣传/);
  assert.match(race, /阶段未知/);
  assert.match(race, /暂无记录/);
  assert.match(race, /未采集/);
  assert.doesNotMatch(race, /待核实|请核实|自行核实/);
  assert.match(race, /计划和预计不算/);
});

test("race board reads the stored projection only and sits on the competitor tab", () => {
  assert.match(race, /useRace\(projectId, revision\)/);
  const shared = readFileSync(new URL('../src/raceData.ts', import.meta.url), 'utf8');
  assert.match(shared, /api\.race/);
  assert.doesNotMatch(race, /api\.refresh|dangerouslySetInnerHTML/);
  assert.match(project, /<RaceBoard projectId=\{id\}/);
});

test("vendor amounts stay as source text", () => {
  assert.doesNotMatch(race, /parseFloat|Number\(row\.funding/);
  assert.match(race, /row\.funding\.amount/);
});

test("race grid contains table overflow and quantity table has its own compact layout", () => {
  const css = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
  assert.match(css, /\.race-board \{[^}]*grid-template-columns: minmax\(0, 1fr\)/);
  assert.match(css, /\.race-board \.table-scroll \{[^}]*min-width: 0[^}]*overflow-x: auto/);
  assert.match(race, /className="quantity-table"/);
});

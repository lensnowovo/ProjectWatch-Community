import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const flags = readFileSync(new URL("../src/flags.tsx", import.meta.url), "utf8");
const overview = readFileSync(new URL("../src/overview.tsx", import.meta.url), "utf8");
const signals = readFileSync(new URL("../src/signals.tsx", import.meta.url), "utf8");

test("bad signals list the company and competitors with sources, never asking the reader to verify", () => {
  assert.match(flags, /本公司/);
  assert.match(flags, /竞对/);
  assert.match(flags, /<Grade grade=\{f\.grade\}/);
  assert.match(flags, /f\.source/);
  assert.doesNotMatch(flags, /核实|核验/);
  assert.match(flags, /api\.redFlags\(projectId\)/);
});

test("silence signals are shown once: in 坏信号 (公司进展), not again under 需要核对", () => {
  assert.match(readFileSync(new URL("../src/internalPages.tsx", import.meta.url), "utf8"), /<SignalsPanel[^>]*hideSilence/);  // 风险与核对
  assert.match(readFileSync(new URL("../src/landscape.tsx", import.meta.url), "utf8"), /<RedFlags projectId=/);
  assert.match(signals, /hideSilence/);
});

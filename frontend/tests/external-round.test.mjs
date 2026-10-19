import assert from "node:assert/strict";
import { createRequire, Module } from "node:module";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import test from "node:test";

const require = createRequire(new URL("../package.json", import.meta.url));
const { buildSync } = require("esbuild");
const entry = fileURLToPath(new URL("../src/workState.ts", import.meta.url));
const mod = new Module(entry);
mod._compile(buildSync({ entryPoints: [entry], bundle: true, write: false, platform: "node",
  format: "cjs", packages: "external" }).outputFiles[0].text, entry);
const { monitoringRevision, jobWorkState } = mod.exports;

test("first result and first brief refresh reads before whole-round completion", () => {
  const detail = { project: { last_external_at: null }, monitoring_progress: {}, brief: null };
  const empty = monitoringRevision(detail);
  detail.monitoring_progress.first_result_at = "synthetic-first";
  const first = monitoringRevision(detail);
  assert.notEqual(first, empty);
  detail.brief = { updated_at: "synthetic-brief" };
  assert.notEqual(monitoringRevision(detail), first);
  assert.equal(detail.project.last_external_at, null);
  for (const file of ["landscape.tsx", "overview.tsx"]) {
    const source = readFileSync(new URL(`../src/${file}`, import.meta.url), "utf8");
    assert.match(source, /monitoringRevision\(detail\)/);
  }
});

test("queued followup is held by pause and is not presented as executing", () => {
  const detail = { project: { status: "paused" }, execution: { analysis: "running" } };
  assert.equal(jobWorkState(detail, { kind: "external_followup", status: "queued" }), "paused");
  detail.project.status = "active";
  detail.execution.analysis = "stopped";
  assert.equal(jobWorkState(detail, { kind: "external_followup", status: "queued" }), "offline");
});

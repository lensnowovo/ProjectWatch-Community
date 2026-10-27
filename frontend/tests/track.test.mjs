import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const track = readFileSync(new URL("../src/track.tsx", import.meta.url), "utf8");
const project = readFileSync(new URL("../src/pages/Project.tsx", import.meta.url), "utf8");

test("track board reads stored projection, shows evidence and an accessible chart", () => {
  assert.match(track, /api\.track\(projectId\)/);
  assert.match(track, /<Grade grade=\{e\.grade\}/);
  assert.match(track, /role="img" aria-label=/);
  assert.match(track, /<title>/);
  assert.match(track, /还没有这类记录/);
  assert.doesNotMatch(track, /核实|核验|dangerouslySetInnerHTML/);
  assert.match(project, /part==='industry' && [^\n]*<TrackBoard projectId=\{projectId\} revision=\{revision\} keys=\{\['demand','capital','price'\]\}/);
  assert.match(project, /part==='policy' && <TrackBoard[^\n]*keys=\{\['policy'\]\}/);
  assert.match(project, /part==='technology' && [^\n]*<TechRoutes/); // Each chart mounts only in its own macro tab.
});

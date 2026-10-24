import assert from "node:assert/strict";
import fs from "node:fs";
import { createRequire, Module } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";

const require = createRequire(new URL("../package.json", import.meta.url));
const { buildSync } = require("esbuild");
const entry = fileURLToPath(new URL("../src/planTreeLayout.ts", import.meta.url));
const mod = new Module(entry);
mod._compile(buildSync({ entryPoints: [entry], bundle: true, write: false, platform: "node", format: "cjs" }).outputFiles[0].text, entry);
const { layoutTree, hasEvidence, hopOf, arc, wedge, ringArc } = mod.exports;

// Synthetic framework mirroring the server's four rings and nine dimensions (monitoring_plan.RINGS / FRAMEWORK).
const rings = [{ key: "company", label: "公司本体" }, { key: "competition", label: "竞争" }, { key: "chain", label: "产业链" }, { key: "environment", label: "环境" }];
const framework = [["asset", "company"], ["operations", "company"], ["peers", "competition"], ["alternatives", "competition"],
  ["customer", "chain"], ["partners", "chain"], ["market", "environment"], ["science", "environment"], ["rules", "environment"]]
  .map(([key, ring]) => ({ key, label: key, ring }));
let seq = 0;
const item = (fields) => ({ id: `w${++seq}`, text: `问题 ${seq}`, section: "peers", status: "active", hits: 0,
  created_at: `2026-10-${String(seq).padStart(2, "0")}T00:00:00`, ...fields });
const dist = (p) => Math.hypot(p.x, p.y);
const angle = (p) => (Math.atan2(p.y, p.x) * 180) / Math.PI;

test("evidence requires accepted stored hits; a note alone cannot illuminate the graph", () => {
  assert.equal(hasEvidence({ hits: 0 }, { kind: "hit", fresh: false }), false);
  assert.equal(hasEvidence({ hits: 0 }, { kind: "note", fresh: false }), false);
  assert.equal(hasEvidence({ hits: 2 }, { kind: "none", fresh: false }), true);
  assert.equal(hasEvidence({ hits: 0 }, { kind: "none", fresh: false }), false);
  assert.equal(hasEvidence({ hits: 0 }, undefined), false);
});

test("hop defaults like the table (0 without parent, 1 with) and is clamped to the reachable maximum", () => {
  assert.equal(hopOf({}, 2), 0);
  assert.equal(hopOf({ parent_id: "p" }, 2), 1);
  assert.equal(hopOf({ hop: "2" }, 2), 2);
  assert.equal(hopOf({ hop: "5" }, 2), 2);
  assert.equal(hopOf({ hop: "-1" }, 2), 0);
  assert.equal(hopOf({ hop: "x" }, 2), 0);
});

test("empty plan: four arms, dimensions evenly spaced within an arm and a wider gap between arms", () => {
  const t = layoutTree({ rings, framework, items: [], latest: {}, maxHop: 2 });
  assert.equal(t.rings.length, 4);
  assert.equal(t.dims.length, 9);
  assert.equal(t.items.length, 0);
  assert.equal(t.chains.length, 0);
  const degs = t.dims.map((d) => d.deg);
  for (let i = 1; i < degs.length; i++) {
    const sameArm = t.dims[i].ring === t.dims[i - 1].ring;
    const gap = degs[i] - degs[i - 1];
    if (sameArm) assert.ok(Math.abs(gap - t.step) < 1e-9);
    else assert.ok(gap > t.step * 1.5, "arms are visibly apart");
  }
  // all slots plus the arm gaps fill the circle exactly once
  const spans = t.rings.map((r) => r.span[1] - r.span[0]);
  assert.ok(spans.reduce((a, b) => a + b, 0) < 360 && Math.abs(spans.reduce((a, b) => a + b, 0) - 9 * t.step) < 1e-9);
  // level names sit in a gap between arms, not on any dimension's slot
  const norm = (d) => ((d % 360) + 360) % 360;
  for (const d of t.dims) assert.ok(Math.abs(((norm(t.levelLabelDeg - d.deg) + 180) % 360) - 180) > t.step / 2);
  for (const d of t.dims) { assert.equal(d.reach_r, null); assert.equal(d.planned_r, null); }
  // the three-dimension ring (环境) is centred at the top
  assert.ok(Math.abs(t.rings.find((r) => r.key === "environment").deg - -90) < 1e-9);
  for (const r of t.rings) assert.equal(r.reached, null);
  for (const d of t.dims) { assert.ok(Math.abs(dist(d.at) - t.radii.dim) < 0.05); assert.equal(d.count, 0); }
  assert.equal(t.radii.levels.length, 3);
  assert.ok(t.edges.every((e) => !e.lit));
  // everything, labels included, fits inside the viewBox; ring names sit outside the dimension names
  assert.ok(t.radii.ringLabel + 20 < t.height / 2 && t.radii.dimLabel + 160 < t.width / 2);
  assert.ok(t.radii.ringLabel > t.radii.dimLabel);
  assert.equal(t.rings.find((r) => r.key === "environment").anchor, "middle");
});

test("a chain reaches the deepest hop that has evidence; deeper points without evidence stay unlit", () => {
  seq = 0;
  const root = item({ section: "peers", hop: "1" });
  const mid = item({ section: "peers", hop: "2", parent_id: root.id, hits: 3 });
  const leaf = item({ section: "peers", hop: "2", parent_id: mid.id });
  const other = item({ section: "asset", hop: "0" });
  const latest = { [root.id]: { kind: "hit", fresh: false }, [mid.id]: { kind: "none", fresh: true } };
  const t = layoutTree({ rings, framework, items: [root, mid, leaf, other], latest, maxHop: 2 });

  const chain = t.chains.find((c) => c.root === root.id);
  assert.deepEqual(chain.ids, [root.id, mid.id, leaf.id]);
  assert.equal(chain.reached, 2);
  assert.equal(chain.reached_id, mid.id);
  assert.equal(chain.deepest, 2);
  assert.equal(chain.fresh, true);
  assert.equal(t.chains.find((c) => c.root === other.id).reached, null);

  const edge = (from, to) => t.edges.find((e) => e.from === from && e.to === to);
  assert.equal(edge("center", "competition").lit, true);
  assert.equal(edge("competition", "peers").lit, true);
  assert.equal(edge("peers", root.id).lit, true);
  assert.equal(edge(root.id, mid.id).lit, true);
  assert.equal(edge(mid.id, leaf.id).lit, false);  // nothing found that far out yet
  assert.equal(edge("center", "company").lit, false);

  assert.equal(t.dims.find((d) => d.key === "peers").reached, 2);
  assert.equal(t.dims.find((d) => d.key === "peers").fresh, true);
  assert.equal(t.rings.find((r) => r.key === "competition").reached, 2);
  assert.equal(t.rings.find((r) => r.key === "company").reached, null);

  // reach petal ends just past the deepest level with information, planned arc just past the deepest planned level
  const peers = t.dims.find((d) => d.key === "peers");
  assert.equal(peers.planned, 2);
  assert.ok(peers.reach_r > t.radii.levels[2] && peers.reach_r < t.radii.territory);
  assert.equal(peers.planned_r, peers.reach_r);
  const asset = t.dims.find((d) => d.key === "asset");
  assert.equal(asset.reach_r, null);
  assert.ok(asset.planned_r > t.radii.levels[0] && asset.planned_r < t.radii.levels[1]);

  // radius follows hop; the leaf (hop 2 under a hop-2 parent) is clamped to the outermost level
  const at = new Map(t.items.map((n) => [n.id, n]));
  assert.ok(Math.abs(dist(at.get(root.id).at) - t.radii.levels[1]) < 0.05);
  assert.ok(Math.abs(dist(at.get(mid.id).at) - t.radii.levels[2]) < 0.05);
  assert.equal(at.get(leaf.id).level, 2);
  assert.ok(Math.abs(dist(at.get(other.id).at) - t.radii.levels[0]) < 0.05);
  // evidence and fresh are per point; never-hit points are marked quiet
  assert.equal(at.get(root.id).evidence, false);
  assert.equal(at.get(leaf.id).evidence, false);
  assert.equal(at.get(mid.id).fresh, true);
});

test("first-version plan (all hop 0, nothing searched yet): every point sits on the inner level, nothing lit", () => {
  seq = 0;
  const items = framework.map((f) => item({ section: f.key, hop: "0" }));
  const t = layoutTree({ rings, framework, items, latest: {}, maxHop: 2 });
  assert.equal(t.items.length, 9);
  assert.equal(t.chains.length, 9);
  for (const n of t.items) {
    assert.ok(Math.abs(dist(n.at) - t.radii.levels[0]) < 0.05);
    const dim = t.dims.find((d) => d.key === n.section);
    assert.ok(Math.abs(n.deg - dim.deg) < 1e-9);  // a single point sits on its dimension's spoke
  }
  assert.ok(t.chains.every((c) => c.reached === null));
  assert.ok(t.edges.every((e) => !e.lit));
});

test("many points (≥ max_items) stay inside their dimension's sector and keep chains side by side", () => {
  seq = 0;
  const items = [];
  for (const f of framework) {
    const a = item({ section: f.key, hop: "1" });
    items.push(a, item({ section: f.key, hop: "2", parent_id: a.id }), item({ section: f.key, hop: "0" }));
  }
  const t = layoutTree({ rings, framework, items, latest: {}, maxHop: 2 });
  assert.ok(t.items.length >= 16);
  const step = t.step;
  for (const d of t.dims) {
    const mine = t.items.filter((n) => n.section === d.key);
    assert.equal(mine.length, 3);
    for (const n of mine) assert.ok(Math.abs(n.deg - d.deg) <= step * 0.42 + 1e-9, `${n.id} leaves its sector`);
    // depth-first order: hop 0 root, then the hop 1 root followed directly by its child
    const order = [...mine].sort((a, b) => a.deg - b.deg).map((n) => n.hop);
    assert.deepEqual(order, [0, 1, 2]);
  }
  // no two points overlap
  for (let i = 0; i < t.items.length; i++) for (let j = i + 1; j < t.items.length; j++) {
    assert.ok(Math.hypot(t.items[i].at.x - t.items[j].at.x, t.items[i].at.y - t.items[j].at.y) > 10);
  }
});

test("bad data never breaks the layout: unknown sections, missing parents and parent cycles", () => {
  seq = 0;
  const a = item({ section: "peers", hop: "1" });
  const b = item({ section: "peers", hop: "2", parent_id: a.id });
  a.parent_id = b.id;  // cycle
  const orphan = item({ section: "market", hop: "1", parent_id: "missing" });
  const stray = item({ section: "legacy-thing" });
  const t = layoutTree({ rings, framework, items: [a, b, orphan, stray], latest: {}, maxHop: 2 });
  assert.equal(t.items.length, 3);
  assert.deepEqual(t.unplaced.map((i) => i.id), [stray.id]);
  assert.equal(t.items.find((n) => n.id === orphan.id).parent, null);
  // the cycle is broken: exactly one of the two becomes a root and both are drawn once
  const roots = t.items.filter((n) => n.section === "peers" && !n.parent);
  assert.ok(roots.length >= 1);
  assert.equal(new Set(t.chains.flatMap((c) => c.ids)).size, 3);
});

test("arcs are cubic Béziers with control points at each end's angle on the middle radius", () => {
  assert.equal(arc(0, 100, 90, 200), "M 100 0 C 150 0, 0 150, 0 200");
  // petals are closed annular sectors; arcs are open
  assert.equal(wedge(0, 90, 100, 200), "M 100 0 L 200 0 A 200 200 0 0 1 0 200 L 0 100 A 100 100 0 0 0 100 0 Z");
  assert.equal(ringArc(0, 90, 100), "M 100 0 A 100 100 0 0 1 0 100");
  assert.ok(Math.abs(angle({ x: 0, y: -1 }) - -90) < 1e-9);
});

test("minimal reach is always above the table; previous tree component remains isolated", () => {
  const plan = fs.readFileSync(new URL("../src/plan.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(plan, /scopeView|<PlanChainTree /);
  assert.match(plan, /<MonitorReach /);
  assert.ok(plan.indexOf("<MonitorReach ") < plan.indexOf("<RelationScope "));
  assert.match(plan, /setOpenId\(item\.id\)/);
  const tree = fs.readFileSync(new URL("../src/planTree.tsx", import.meta.url), "utf8");
  assert.match(tree, /role="button" tabIndex=\{0\}/);
  assert.match(tree, /aria-live="polite"/);
  assert.match(tree, /focusing/);  // a focused chain stays, the rest steps back
  assert.match(tree, /不表示利好或结论已证实/);
  const block = fs.readFileSync(new URL("../src/planTree.css", import.meta.url), "utf8");
  assert.doesNotMatch(block, /#[0-9a-f]{3,6}\b/i);  // theme variables only, so light and dark both work
  assert.doesNotMatch(block, /border-left:/);  // no single-side decorative bars
  assert.doesNotMatch(block, /drop-shadow|blur\(|filter:/);  // no glow
  assert.match(block, /prefers-reduced-motion:\s*reduce\)\s*\{[^}]*animation:\s*none/);
});

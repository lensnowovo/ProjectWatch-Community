/** 链式监控树: geometry and reach for the plan's radial tree, kept free of React so it can be tested on synthetic data.
 * Center = the company; level 1 = the four rings; level 2 = the nine dimensions spaced evenly around the circle;
 * focus points grow outwards by relation hop (0 公司自身 / 1 直接相关 / 2 二阶相关) and link to the point they extend. */

export interface TreeRing { key: string; label: string }
export interface TreeSection { key: string; label: string; ring?: string }
export interface TreeItem { id: string; text: string; label?: string; section: string; status: string; hits: number;
  hop?: string; parent_id?: string; created_at?: string; tier?: string; last_run_at?: string }
export interface TreeLatest { kind: "note" | "hit" | "none"; fresh: boolean; date?: string; count?: number }

export interface Point { x: number; y: number }
export interface RingNode { key: string; label: string; deg: number; span: [number, number]; at: Point; label_at: Point; anchor: "start" | "middle" | "end"; reached: number | null; fresh: boolean }
export interface DimNode { key: string; label: string; ring: string; deg: number; span: [number, number]; at: Point; label_at: Point; anchor: "start" | "middle" | "end";
  reached: number | null; fresh: boolean; count: number;
  /** Deepest hop the plan sets here, and the radii of the reach petal (information found) and the planned arc. */
  planned: number | null; reach_r: number | null; planned_r: number | null }
export interface ItemNode { id: string; item: TreeItem; section: string; hop: number; level: number; deg: number; r: number; at: Point;
  parent: string | null; root: string; evidence: boolean; fresh: boolean; status: string; on_path: boolean }
export interface Edge { key: string; d: string; from: string; to: string; lit: boolean; fresh: boolean; kind: "ring" | "dim" | "item" }
export interface Chain { root: string; section: string; ids: string[]; deepest: number; reached: number | null; reached_id: string | null; fresh: boolean }
export interface TreeLayout { width: number; height: number; step: number; levelLabelDeg: number;
  radii: { ring: number; dim: number; levels: number[]; dimLabel: number; ringLabel: number; territory: number };
  maxHop: number; rings: RingNode[]; dims: DimNode[]; items: ItemNode[]; edges: Edge[]; chains: Chain[]; unplaced: TreeItem[] }

/** Has this point produced anything on-topic: a current understanding, a relevant hit, or a stored hit count. */
export function hasEvidence(item: Pick<TreeItem, "hits">, _latest?: TreeLatest): boolean {
  return (item.hits ?? 0) > 0;
}

/** Relation hop as stored, defaulting like the table does: no parent → the company itself, a parent → directly related. */
export function hopOf(item: Pick<TreeItem, "hop" | "parent_id">, maxHop: number): number {
  const raw = item.hop === undefined || item.hop === "" ? (item.parent_id ? 1 : 0) : Number(item.hop);
  return Number.isFinite(raw) ? Math.max(0, Math.min(maxHop, Math.round(raw))) : 0;
}

export const polar = (deg: number, r: number): Point => {
  const a = (deg * Math.PI) / 180;
  return { x: round(Math.cos(a) * r), y: round(Math.sin(a) * r) };
};
const round = (n: number) => Math.round(n * 100) / 100;

/** A radial arc: cubic Bézier whose control points sit at each end's angle on the middle radius. */
export function arc(a0: number, r0: number, a1: number, r1: number): string {
  const p0 = polar(a0, r0), p1 = polar(a1, r1), m = (r0 + r1) / 2;
  const c0 = polar(a0, m), c1 = polar(a1, m);
  return `M ${p0.x} ${p0.y} C ${c0.x} ${c0.y}, ${c1.x} ${c1.y}, ${p1.x} ${p1.y}`;
}

/** Closed annular sector between two angles and two radii. */
export function wedge(a0: number, a1: number, r0: number, r1: number): string {
  const large = Math.abs(a1 - a0) > 180 ? 1 : 0;
  const p0 = polar(a0, r0), p1 = polar(a0, r1), p2 = polar(a1, r1), p3 = polar(a1, r0);
  return `M ${p0.x} ${p0.y} L ${p1.x} ${p1.y} A ${r1} ${r1} 0 ${large} 1 ${p2.x} ${p2.y} L ${p3.x} ${p3.y}`
    + (r0 > 0 ? ` A ${r0} ${r0} 0 ${large} 0 ${p0.x} ${p0.y} Z` : " Z");
}

/** Open circular arc from a0 to a1 (clockwise on screen). */
export function ringArc(a0: number, a1: number, r: number): string {
  const p0 = polar(a0, r), p1 = polar(a1, r);
  return `M ${p0.x} ${p0.y} A ${r} ${r} 0 ${Math.abs(a1 - a0) > 180 ? 1 : 0} 1 ${p1.x} ${p1.y}`;
}

const RING_R = 64, DIM_R = 136, LEVEL0_R = 196, LEVEL_STEP = 48;
/** Space between two rings' arms, in dimension slots, so the four arms read as four. */
const GROUP_GAP = 0.6;
/** How far a petal or planned arc reaches past the level it marks, so the points sit inside it. */
const PETAL_PAD = 12;

export function layoutTree({ rings, framework, items, latest, maxHop = 2 }: {
  rings: TreeRing[]; framework: TreeSection[]; items: TreeItem[]; latest: Record<string, TreeLatest | undefined>; maxHop?: number;
}): TreeLayout {
  const hopMax = Math.max(0, Math.min(4, Math.round(maxHop)));
  const levels = Array.from({ length: hopMax + 1 }, (_, h) => LEVEL0_R + h * LEVEL_STEP);
  const outer = levels[levels.length - 1];
  const territory = outer + 18, dimLabel = outer + 36, ringLabel = outer + 84;
  // Room for horizontal labels: dimension names reach sideways, ring names sit above and below.
  const width = (dimLabel + 170) * 2, height = (ringLabel + 30) * 2;

  // Dimensions grouped by ring, in the server's order; dimensions naming an unknown ring keep their own group.
  const ringOrder = [...rings];
  for (const f of framework) if (f.ring && !ringOrder.some((r) => r.key === f.ring)) ringOrder.push({ key: f.ring, label: f.ring });
  const grouped = ringOrder.map((r) => ({ ring: r, dims: framework.filter((f) => f.ring === r.key) })).filter((g) => g.dims.length);
  const loose = framework.filter((f) => !f.ring || !ringOrder.some((r) => r.key === f.ring));
  if (loose.length) grouped.push({ ring: { key: "", label: "" }, dims: loose });
  const total = grouped.reduce((n, g) => n + g.dims.length, 0);
  const gap = grouped.length > 1 ? GROUP_GAP : 0;
  const step = total ? 360 / (total + gap * grouped.length) : 360;
  // Each ring is an arm: its dimensions evenly spaced, a gap before the next ring.
  let cursor = 0;
  const arms = grouped.map((g) => {
    cursor += (gap * step) / 2;
    const span: [number, number] = [cursor, cursor + g.dims.length * step];
    const degs = g.dims.map((_, j) => cursor + (j + 0.5) * step);
    cursor = span[1] + (gap * step) / 2;
    return { degs, span };
  });
  // Center the widest ring at the top so labels around the circle stack without crossing.
  let widest = 0;
  grouped.forEach((g, i) => { if (g.dims.length > grouped[widest].dims.length) widest = i; });
  const shift = arms.length ? -90 - (arms[widest].span[0] + arms[widest].span[1]) / 2 : 0;
  for (const a of arms) { a.degs = a.degs.map((d) => d + shift); a.span = [a.span[0] + shift, a.span[1] + shift]; }
  // Level names sit in the gap between two arms nearest the upper right, where no point is drawn.
  const gaps = arms.map((a) => a.span[1] + (gap * step) / 2);
  const off = (d: number) => Math.abs(((d - -45) % 360 + 540) % 360 - 180);
  const levelLabelDeg = gaps.length ? gaps.reduce((b, d) => (off(d) < off(b) ? d : b)) : -45;

  // Items: placed only under a known dimension; parents resolved within the placed set, cycles broken.
  const sectionKeys = new Set(framework.map((f) => f.key));
  const placed = items.filter((i) => sectionKeys.has(i.section));
  const unplaced = items.filter((i) => !sectionKeys.has(i.section));
  const byId = new Map(placed.map((i) => [i.id, i]));
  const parentOf = (i: TreeItem): string | null => {
    let p = i.parent_id && byId.has(i.parent_id) && i.parent_id !== i.id ? i.parent_id : null;
    // walk up to make sure following parents never returns to i
    const seen = new Set([i.id]);
    for (let cur = p; cur; ) {
      if (seen.has(cur)) { p = null; break; }
      seen.add(cur);
      const next = byId.get(cur)?.parent_id;
      cur = next && byId.has(next) ? next : null;
    }
    return p;
  };
  const parent = new Map(placed.map((i) => [i.id, parentOf(i)]));
  const children = new Map<string, TreeItem[]>();
  for (const i of placed) { const p = parent.get(i.id); if (p) children.set(p, [...(children.get(p) ?? []), i]); }
  const hop = new Map(placed.map((i) => [i.id, hopOf(i, hopMax)]));
  // Ring level never falls inside its parent's: a child sits at least one level further out (bounded by the max hop).
  const level = new Map<string, number>();
  const levelOf = (id: string): number => {
    if (level.has(id)) return level.get(id)!;
    // Object depth is supplied by treeModel. A research child does not add a relation hop.
    const v = hop.get(id)!;
    level.set(id, v);
    return v;
  };
  placed.forEach((i) => levelOf(i.id));
  const rootOf = (id: string): string => { let cur = id; while (parent.get(cur)) cur = parent.get(cur)!; return cur; };
  const evidence = new Map(placed.map((i) => [i.id, hasEvidence(i, latest[i.id])]));
  const fresh = new Map(placed.map((i) => [i.id, !!latest[i.id]?.fresh]));
  const order = (a: TreeItem, b: TreeItem) => hop.get(a.id)! - hop.get(b.id)! || (a.created_at ?? "").localeCompare(b.created_at ?? "") || a.id.localeCompare(b.id);

  // Subtree flags: does anything at or beyond this node have evidence / fresh evidence.
  const subtree = (id: string, flag: Map<string, boolean>, memo: Map<string, boolean>): boolean => {
    if (memo.has(id)) return memo.get(id)!;
    const v = flag.get(id)! || (children.get(id) ?? []).some((c) => subtree(c.id, flag, memo));
    memo.set(id, v);
    return v;
  };
  const litMemo = new Map<string, boolean>(), freshMemo = new Map<string, boolean>();

  const ringNodes: RingNode[] = [], dimNodes: DimNode[] = [], itemNodes: ItemNode[] = [], edges: Edge[] = [];
  grouped.forEach((g, gi) => {
    const { degs, span } = arms[gi];
    const ringDeg = degs.reduce((a, b) => a + b, 0) / degs.length;
    const ringNode: RingNode = { key: g.ring.key, label: g.ring.label, deg: ringDeg, span, at: polar(ringDeg, RING_R),
      label_at: polar(ringDeg, ringLabel), anchor: anchorFor(ringDeg), reached: null, fresh: false };
    let ringLit = false, ringFresh = false;
    g.dims.forEach((f, di) => {
      const deg = degs[di];
      // Depth-first so each chain's points sit side by side; roots (no parent in this plan) start chains.
      const roots = placed.filter((i) => i.section === f.key && !parent.get(i.id)).sort(order);
      const seq: TreeItem[] = [];
      const visit = (i: TreeItem) => { seq.push(i); (children.get(i.id) ?? []).filter((c) => c.section === f.key).sort(order).forEach(visit); };
      roots.forEach(visit);
      // children filed under a different dimension than their parent still sit in their own dimension
      for (const i of placed) if (i.section === f.key && !seq.includes(i)) seq.push(i);
      const sector = step * 0.84;
      const gap = seq.length > 1 ? Math.min(sector / (seq.length - 1), 14) : 0;
      let dimReached: number | null = null, dimFresh = false, dimPlanned: number | null = null;
      seq.forEach((i, n) => {
        const lv = level.get(i.id)!;
        const ideg = deg + (n - (seq.length - 1) / 2) * gap;
        const r = levels[lv];
        const p = parent.get(i.id) ?? null;
        const lit = subtree(i.id, evidence, litMemo);
        const fr = subtree(i.id, fresh, freshMemo);
        itemNodes.push({ id: i.id, item: i, section: f.key, hop: hop.get(i.id)!, level: lv, deg: ideg, r, at: polar(ideg, r), parent: p,
          root: rootOf(i.id), evidence: evidence.get(i.id)!, fresh: fresh.get(i.id)!, status: i.status, on_path: lit });
        if (evidence.get(i.id)) dimReached = Math.max(dimReached ?? -1, lv);
        dimPlanned = Math.max(dimPlanned ?? -1, lv);
        dimFresh ||= fresh.get(i.id)!;
        if (!p) edges.push({ key: `${f.key}>${i.id}`, d: arc(deg, DIM_R, ideg, r), from: f.key, to: i.id, lit, fresh: fr, kind: "item" });
      });
      dimNodes.push({ key: f.key, label: f.label, ring: g.ring.key, deg, span: [deg - step / 2, deg + step / 2], at: polar(deg, DIM_R),
        label_at: polar(deg, dimLabel), anchor: anchorFor(deg), reached: dimReached, fresh: dimFresh, count: seq.length, planned: dimPlanned,
        reach_r: dimReached === null ? null : levels[dimReached] + PETAL_PAD, planned_r: dimPlanned === null ? null : levels[dimPlanned] + PETAL_PAD });
      edges.push({ key: `${g.ring.key}>${f.key}`, d: arc(ringDeg, RING_R, deg, DIM_R), from: g.ring.key, to: f.key,
        lit: dimReached !== null, fresh: dimFresh, kind: "dim" });
      if (dimReached !== null) ringNode.reached = Math.max(ringNode.reached ?? -1, dimReached);
      ringLit ||= dimReached !== null; ringFresh ||= dimFresh;
    });
    ringNode.fresh = ringFresh;
    ringNodes.push(ringNode);
    edges.push({ key: `center>${g.ring.key}`, d: arc(ringDeg, 0, ringDeg, RING_R), from: "center", to: g.ring.key, lit: ringLit, fresh: ringFresh, kind: "ring" });
  });
  // Parent → child arcs, drawn after all items have positions.
  const at = new Map(itemNodes.map((n) => [n.id, n]));
  for (const n of itemNodes) {
    if (!n.parent) continue;
    const p = at.get(n.parent)!;
    edges.push({ key: `${n.parent}>${n.id}`, d: arc(p.deg, p.r, n.deg, n.r), from: n.parent, to: n.id, lit: n.on_path, fresh: subtree(n.id, fresh, freshMemo), kind: "item" });
  }

  // One chain per root: how far out it reaches, and the deepest point that has produced evidence.
  const chains: Chain[] = [];
  for (const n of itemNodes) {
    if (n.parent) continue;
    const ids: string[] = [];
    const walk = (id: string) => { ids.push(id); (children.get(id) ?? []).sort(order).forEach((c) => walk(c.id)); };
    walk(n.id);
    let reached: number | null = null, reachedId: string | null = null;
    for (const id of ids) if (evidence.get(id) && (reached === null || hop.get(id)! > reached)) { reached = hop.get(id)!; reachedId = id; }
    chains.push({ root: n.id, section: n.section, ids, deepest: Math.max(...ids.map((id) => hop.get(id)!)), reached, reached_id: reachedId,
      fresh: ids.some((id) => fresh.get(id)) });
  }

  return { width, height, step, levelLabelDeg, radii: { ring: RING_R, dim: DIM_R, levels, dimLabel, ringLabel, territory }, maxHop: hopMax,
    rings: ringNodes, dims: dimNodes, items: itemNodes, edges, chains, unplaced };
}

function anchorFor(deg: number): "start" | "middle" | "end" {
  const c = Math.cos((deg * Math.PI) / 180);
  return c > 0.26 ? "start" : c < -0.26 ? "end" : "middle";
}

/** A short arc centered on an angle, oriented so text set along it reads upright. */
export function tangentArc(deg: number, r: number, span: number): string {
  const bottom = Math.sin((deg * Math.PI) / 180) > 0.2;
  const p0 = polar(bottom ? deg + span / 2 : deg - span / 2, r), p1 = polar(bottom ? deg - span / 2 : deg + span / 2, r);
  return `M ${p0.x} ${p0.y} A ${r} ${r} 0 0 ${bottom ? 0 : 1} ${p1.x} ${p1.y}`;
}

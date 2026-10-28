import type { MonitoringPlanView, PlanItem } from "./api";

export const REACH_DIRECTIONS = [
  { id: "company", label: "公司本体" }, { id: "competition", label: "竞争" },
  { id: "chain", label: "产业链" }, { id: "environment", label: "环境" },
] as const;
type Direction = typeof REACH_DIRECTIONS[number]["id"];
const SECTION: Record<string, Direction> = {
  asset: "company", operations: "company", peers: "competition", alternatives: "competition",
  customer: "chain", partners: "chain", market: "environment", science: "environment", rules: "environment",
  self: "company", commercial: "company", capital: "company", competition: "competition",
  demand: "competition", supply: "chain", technology: "environment",
};
const TYPE: Record<string, Direction> = {
  product: "company", indication: "company", technology: "environment", competitor: "competition",
  alternative: "competition", supplier: "chain", material: "chain", customer: "chain",
  industry: "environment", policy: "environment",
};
type Reach = { direction: Direction; depth: number };

/** Read-only projection. Root framework sets direction; actual relation parents set depth.
 * Research, legacy hop labels, event volume and task state never add relationship tiers.
 * A closed ancestor may preserve provenance for an active descendant, but a closed leaf adds no reach.
 */
export function monitorReachModel(plan: Pick<MonitoringPlanView, "items" | "framework">) {
  const rows = REACH_DIRECTIONS.map(r => ({ ...r, depth: 0 }));
  const byId = new Map<string, PlanItem>(), duplicates = new Set<string>();
  for (const item of plan.items) {
    if (byId.has(item.id)) duplicates.add(item.id);
    byId.set(item.id, item);
  }
  const rings = new Map((plan.framework ?? []).map(f => [f.key, f.ring]));
  const directionOf = (item: PlanItem): Direction | undefined => {
    const ring = rings.get(item.section);
    if (REACH_DIRECTIONS.some(r => r.id === ring)) return ring as Direction;
    return SECTION[item.section] ?? TYPE[item.relation_type ?? ""];
  };
  const memo = new Map<string, Reach | null>();
  function resolve(id: string): Reach | null {
    const path: string[] = [], seen = new Set<string>();
    let current = id, result: Reach | null = null;
    while (true) {
      if (memo.has(current)) { result = memo.get(current)!; break; }
      const item = byId.get(current);
      if (!item || duplicates.has(current) || seen.has(current) || item.scope_kind !== "relation") break;
      seen.add(current); path.push(current);
      if (!item.parent_id) {
        const direction = directionOf(item);
        if (direction) { result = { direction, depth: 1 }; memo.set(current, result); path.pop(); }
        break;
      }
      current = item.parent_id;
    }
    for (let i = path.length - 1; i >= 0; i--) {
      result = result ? { direction: result.direction, depth: result.depth + 1 } : null;
      memo.set(path[i], result);
    }
    return memo.get(id) ?? null;
  }
  let excluded = 0;
  for (const item of plan.items) {
    if (item.scope_kind !== "relation" || item.status === "retired") continue;
    const reach = resolve(item.id);
    if (!reach) { excluded++; continue; }
    const row = rows.find(r => r.id === reach.direction)!;
    row.depth = Math.max(row.depth, reach.depth);
  }
  return { rows, depth: Math.max(...rows.map(r => r.depth)), excluded };
}

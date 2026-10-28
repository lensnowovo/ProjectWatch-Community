import type { MonitoringPlanView, PlanItem } from './api';
import { branchState } from './relationScope';

export interface ScopeNode extends PlanItem {
  role: 'relation' | 'question' | 'legacy'; name: string; stateLabel: string;
  evidence: boolean; fresh: boolean; contextOnly: boolean;
}

/** A view projection only. Never changes stored parentage, roles or lifecycle. */
export function treeModel(items: PlanItem[], explorations: MonitoringPlanView['explorations'] = {},
  signals: MonitoringPlanView['tree_signals'] = {}, showClosed = false) {
  const byId = new Map(items.map(i => [i.id, i]));
  const wanted = new Set(items.filter(i => showClosed || i.status !== 'retired').map(i => i.id));
  const warnings = new Set<string>();
  for (const id of [...wanted]) {
    const seen = new Set([id]);
    let cur = byId.get(id);
    while (cur?.parent_id) {
      if (seen.has(cur.parent_id)) { warnings.add('存在循环来源，相关连线暂不展示'); break; }
      seen.add(cur.parent_id);
      cur = byId.get(cur.parent_id);
      if (!cur) { warnings.add('部分来源节点缺失，不能还原完整关联'); break; }
      wanted.add(cur.id);
    }
  }
  const parentOf = (i: PlanItem) => {
    const seen = new Set([i.id]); let cur = i;
    while (cur.parent_id) {
      if (seen.has(cur.parent_id)) return '';
      seen.add(cur.parent_id);
      const p = byId.get(cur.parent_id); if (!p) return i.parent_id && byId.has(i.parent_id) ? i.parent_id : '';
      cur = p;
    }
    return i.parent_id || '';
  };
  const depth = (i: PlanItem, seen = new Set<string>()): number => {
    if (seen.has(i.id)) return 0;
    seen.add(i.id);
    const p = byId.get(parentOf(i));
    if (i.scope_kind === 'question') return p ? depth(p, seen) : 0;
    if (i.scope_kind !== 'relation') return 0;
    return p?.scope_kind === 'relation' ? depth(p, seen) + 1 : 1;
  };
  const nodes: ScopeNode[] = items.filter(i => wanted.has(i.id)).map(i => {
    const role = i.scope_kind || 'legacy';
    const signal = signals[i.id];
    return {...i, role, parent_id: parentOf(i), hop: String(depth(i)),
      name: role === 'relation' ? i.relation_target || i.label || i.text : i.text || i.label,
      stateLabel: role === 'question' || explorations[i.id] ? branchState(i, explorations[i.id]) :
        ({active:'持续关注',trial:'试运行',reduced:'已降频',retired:'已关闭'}[i.status] || '状态未记录'),
      evidence: (signal?.count ?? 0) > 0, fresh: (signal?.count ?? 0) > 0 && !!signal?.fresh,
      contextOnly: !showClosed && i.status === 'retired',
      // Layout must not read cumulative unfiltered hit counts.
      hits: signal?.count ?? 0,
    };
  });
  return {items:nodes, warnings:[...warnings]};
}

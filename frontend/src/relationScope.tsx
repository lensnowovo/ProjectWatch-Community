import { Fragment, useEffect, useState, type ReactNode } from "react";
import type { MonitoringPlanView, PlanItem } from "./api";
import "./relationScope.css";

const TYPES: Record<string, string> = { technology: "技术路线", product: "产品", indication: "适应症",
  competitor: "竞争对手", supplier: "供应商", customer: "客户", industry: "行业", policy: "政策" };
const BASIS = { summary: "标题 / 摘要关联", full_body: "正文关联", partial_body: "部分正文关联", search_summary: "搜索摘要关联" };
export function branchState(item: PlanItem, state?: { state: string; label: string; reason?: string }) {
  if (item.status === "retired") return "已关闭";
  if (state?.state === "stopped") return "已结束";
  if (state?.state === "waiting") {
    if (state.reason === "waiting_recovery") return "待确认恢复";
    if (state.reason === "source_failure") return "来源待恢复";
    if (state.reason === "request_budget") return "本轮额度已到";
    return state.label || "等待后续运行";
  }
  if (state?.state === "active") return "可继续探索";
  if (state?.state === "held") return "待处理";
  return "待运行";
}
export function publicLink(url: string) {
  try { return /^https?:$/.test(new URL(url).protocol) ? url : undefined; } catch { return undefined; }
}

/** Persistent subjects, with temporary research shown beside their originating relation. */
export function RelationScope({ items, updates = {}, explorations = {}, reveal, renderDetail, renderState }: {
  reveal?: {id: string; n: number} | null;
  items: PlanItem[]; updates: MonitoringPlanView["relation_updates"]; explorations: MonitoringPlanView["explorations"];
  renderDetail: (item: PlanItem) => ReactNode; renderState: (item: PlanItem) => ReactNode;
}) {
  const [open, setOpen] = useState<string | null>(null);
  const [branch, setBranch] = useState<string | null>(null);
  const [all, setAll] = useState(false);
  const relations = items.filter((i) => i.scope_kind === "relation" && i.status !== "retired");
  const children = (id: string): PlanItem[] => items.filter((i) => i.parent_id === id && i.scope_kind === "question");
  const descendants = (id: string, visited = new Set<string>()): PlanItem[] => {
    if (visited.has(id)) return [];
    visited.add(id);
    return children(id).flatMap((i) => [i, ...descendants(i.id, visited)]);
  };
  useEffect(()=>{
    if (!reveal) return;
    const root = relations.find(i=>i.id===reveal.id || descendants(i.id).some(b=>b.id===reveal.id));
    if (!root) return;
    setAll(true);setOpen(root.id);setBranch(root.id===reveal.id?null:reveal.id);
    const frame=requestAnimationFrame(()=>document.getElementById(`relation-row-${root.id}`)?.scrollIntoView({block:'nearest'}));
    return ()=>cancelAnimationFrame(frame);
  },[reveal]);
  return <section className="relation-scope" aria-label="关联监控范围">
    <header><h3>关联监控</h3><span className="muted small">{relations.length} 个长期关注对象</span></header>
    <div className="table-wrap"><table>
      <colgroup><col style={{ width: "23%" }} /><col style={{ width: "27%" }} /><col style={{ width: "29%" }} /><col style={{ width: "21%" }} /></colgroup>
      <thead><tr><th>关注对象</th><th>纳入原因</th><th>最近变化</th><th>研究分支</th></tr></thead>
      <tbody>{(all ? relations : relations.slice(0, 12)).map((item) => {
        const expanded = open === item.id;
        const news = updates[item.id]?.items ?? [];
        const branches = descendants(item.id);
        return <Fragment key={item.id}>
          <tr id={`relation-row-${item.id}`}>
            <td><span className="rs-type">{TYPES[item.relation_type ?? ""] ?? "关联对象"}</span>
              <button className="rs-target" aria-expanded={expanded} onClick={() => { setOpen(expanded ? null : item.id); setBranch(null); }}>{item.relation_target || item.label}<span aria-hidden="true">{expanded ? "−" : "+"}</span></button>
              {!!item.relation_aliases?.length && <span className="rs-alias" title={item.relation_aliases.join(" / ")}>{item.relation_aliases.join(" / ")}</span>}
              <div className="rs-state">{renderState(item)}</div></td>
            <td><p className="rs-reason">{item.reason || "尚未记录纳入原因"}</p>
              {item.sources && <button className="link small" onClick={() => { setOpen(item.id); setBranch(null); }}>查看纳入依据</button>}</td>
            <td>{news[0] ? <>
              <a className="rs-news" href={publicLink(news[0].url)} target="_blank" rel="noreferrer">{news[0].title}</a>
              <div className="rs-meta">{news[0].date?.slice(0, 10) || "日期未标注"} · {BASIS[news[0].basis]}</div>
              <button className="link small" onClick={() => { setOpen(item.id); setBranch(null); }}>{updates[item.id]?.count} 条匹配动态</button>
            </> : <span className="muted small">暂无匹配动态</span>}</td>
            <td>{branches.length ? <div className="rs-branches">{branches.slice(0, expanded ? branches.length : 2).map((b) =>
              <button key={b.id} onClick={() => { setOpen(item.id); setBranch(branch === b.id ? null : b.id); }}>
                <span>{b.text}</span><small title={explorations[b.id]?.label}>{branchState(b, explorations[b.id])}</small>
              </button>)}{!expanded && branches.length > 2 && <button className="link small" onClick={() => setOpen(item.id)}>另 {branches.length - 2} 个分支</button>}</div> :
              <span className="muted small">暂无临时研究</span>}</td>
          </tr>
          {expanded && <tr className="rs-expanded"><td colSpan={4}>
            {branch ? renderDetail(branches.find((b) => b.id === branch) ?? item) : <>
              <div className="rs-evidence">{news.length > 0 && <span className="muted small">最近 {news.length} 条 · 共 {updates[item.id]?.count} 条匹配动态</span>}
              {news.map((n) => <article key={n.event_id}>
                <a href={publicLink(n.url)} target="_blank" rel="noreferrer">{n.title}</a>
                <small>{BASIS[n.basis]} · {n.source} · 命中「{n.matched_term}」</small><p>{n.excerpt}</p>
              </article>)}</div>{renderDetail(item)}
            </>}
          </td></tr>}
        </Fragment>;
      })}</tbody>
    </table></div>
    {relations.length > 12 && <button className="link small" onClick={() => setAll(!all)}>{all ? "收起" : `显示其余 ${relations.length - 12} 个对象`}</button>}
  </section>;
}

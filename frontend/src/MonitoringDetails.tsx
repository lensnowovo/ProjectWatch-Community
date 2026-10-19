import { useState } from "react";
import { api, type LocatedEvidence, type ProjectDetail } from "./api";
import { ErrorNote } from "./ui";

const PHASE: Record<string, string> = { original: "原定", adjusted: "调整", actual: "完成", delayed: "延期", cancelled: "取消" };
const MONEY: Record<string, string> = { funding: "融资", cash_balance: "现金余额", operating_cash_flow: "经营现金流", net_profit: "净利润", planned_spend: "资金用途" };
const STAGE: Record<string, string> = { plan: "计划", agreement: "协议", received: "到账", balance: "余额", flow: "现金流", profit: "利润", spend: "投入" };
const STATUS: Record<string, string> = { open: "关注中", resolved: "已处理", dismissed: "不再关注", observation_insufficient: "依据待补", resolved_by_source_change: "资料已更新" };
const EVENT_STAGE: Record<string, string> = { intent: "意向", agreement: "协议", received: "到账", signed: "签约", trial: "试用", delivered: "交付", accepted: "验收", recognized: "确认收入", paid: "回款", cancelled: "取消", delayed: "延期", withdrawn: "撤回", paused: "暂停", resumed: "恢复" };

export function EventStates({ detail }: { detail: ProjectDetail }) {
  const events = detail.event_states?.items ?? [];
  if (!events.length) return null;
  return <section className="card card-pad"><h3>事件进展</h3>
    <div className="table-scroll"><table className="table compact-table"><thead><tr><th>事件</th><th>最近有依据的阶段</th><th>发生日期</th></tr></thead>
      <tbody>{events.map(event => <tr key={`${event.subject_id}:${event.channel}:${event.name}:${event.period}`}>
        <th scope="row">{event.name}</th><td>{event.support === "conflict" ? "来源冲突" : event.state ? EVENT_STAGE[event.state] || "阶段待定" : "依据不足"}</td>
        <td>{event.effective_at || "—"}<details><summary className="muted small">阶段记录</summary>
          {event.history.map(h => <div key={h.fact_id}><p>{h.occurred_at || "日期未披露"} · {EVENT_STAGE[h.state] || "阶段待定"} · {h.support === "pending" ? "依据不足" : "来源陈述"}</p>
            {!!h.refs?.length && <StoredEvidence key={`${detail.project.id}:${h.fact_id}`} projectId={detail.project.id} refs={h.refs} />}</div>)}
          {event.history_truncated && <p className="muted small">仅显示部分阶段记录。</p>}</details></td>
      </tr>)}</tbody></table></div>
    {detail.event_states?.truncated && <p className="muted small">仅显示部分事件。</p>}
  </section>;
}

export function StoredEvidence({ projectId, refs }: { projectId: string; refs: LocatedEvidence[] }) {
  const [quote, setQuote] = useState<{ quote: string; locator: string; read_scope: string } | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);
  const unique = refs.filter((ref, i) => refs.findIndex(r => JSON.stringify(r) === JSON.stringify(ref)) === i);
  const read = async (ref: LocatedEvidence) => {
    setLoading(true); setError(null); setQuote(null);
    try { setQuote(await api.locatedExcerpt(projectId, ref)); } catch (e) { setError(e); }
    finally { setLoading(false); }
  };
  return <details><summary className="muted small">查看依据</summary>
    <div className="button-row">{unique.slice(0, 10).map((ref, n) => <button key={n} className="link" disabled={loading}
      onClick={() => read(ref)}>原文 {n + 1}</button>)}</div>
    {loading && <p className="muted small">读取中…</p>}
    {quote && <><p>{quote.quote}</p><small className="muted">{quote.locator}{quote.read_scope === "summary" ? " · 摘要" : ""}</small></>}
    <ErrorNote error={error} />
  </details>;
}

function Concern({ detail, row, onReload }: { detail: ProjectDetail; row: NonNullable<ProjectDetail["concerns"]>["items"][number]; onReload: () => void }) {
  const [editing, setEditing] = useState(false), [reason, setReason] = useState("");
  const [status, setStatus] = useState<"open" | "resolved" | "dismissed">("resolved");
  const [pending, setPending] = useState<{ key: string; status: typeof status; reason: string; version: number } | null>(null);
  const [saving, setSaving] = useState(false), [error, setError] = useState<unknown>(null);
  const save = async () => {
    if (!reason.trim() || !detail.project.config_version) return;
    const body = pending ?? { key: crypto.randomUUID(), status, reason: reason.trim(), version: detail.project.config_version };
    setPending(body); setSaving(true); setError(null);
    try {
      await api.concernFeedback(detail.project.id, { concern_id: row.id, status: body.status, reason: body.reason,
        expected_version: body.version, idempotency_key: body.key });
      setPending(null); setReason(""); setEditing(false); onReload();
    } catch (e) { setError(e); }
    finally { setSaving(false); }
  };
  return <li>
    <div className="card-head"><strong>{row.title}</strong><span className="muted small">{STATUS[row.status] ?? "依据不足"}</span></div>
    <StoredEvidence key={detail.project.id} projectId={detail.project.id} refs={row.refs} />
    {!!row.feedback_history.length && <details><summary className="muted small">处理记录</summary>
      {row.feedback_history.map((h, n) => <p key={n}>{h.at.slice(0, 10)} · {STATUS[h.status]}{h.reason ? ` · ${h.reason}` : ""}</p>)}</details>}
    <button className="link small" onClick={() => setEditing(!editing)}>{editing ? "收起" : "记录处理"}</button>
    {editing && <form onSubmit={e => { e.preventDefault(); void save(); }}>
      <label>状态 <select value={status} disabled={saving || !!pending} onChange={e => setStatus(e.target.value as typeof status)}>
        <option value="resolved">已处理</option><option value="dismissed">不再关注</option><option value="open">继续关注</option></select></label>
      <label>备注 <input aria-label="处理备注" maxLength={500} value={reason} disabled={saving || !!pending} onChange={e => setReason(e.target.value)} required /></label>
      <button className="btn" disabled={saving || !reason.trim()}>{saving ? "保存中…" : pending ? "重试保存" : "保存"}</button>
      {pending && !saving && <button type="button" className="link" onClick={() => { setPending(null); onReload(); }}>读取最新版本</button>}
      <ErrorNote error={error} />
    </form>}
  </li>;
}

export default function MonitoringDetails({ detail, onReload }: { detail: ProjectDetail; onReload: () => void }) {
  const funds = detail.observations?.items.filter(r => r.kind === "funding_fact") ?? [];
  const nodes = detail.milestones?.items ?? [];
  const concerns = detail.concerns?.items ?? [];
  return <>
    {!!nodes.length && <section className="card card-pad"><h3>项目节点</h3>
      <div className="table-scroll"><table className="table compact-table"><thead><tr><th>节点</th><th>原定</th><th>调整</th><th>实际</th><th>状态</th></tr></thead>
        <tbody>{nodes.map(row => <tr key={`${row.subject_id}:${row.name}`}><th scope="row">{row.name}</th>
          <td>{row.original_dates.join(" / ") || "—"}</td><td>{row.adjusted_dates.join(" / ") || "—"}</td><td>{row.actual_dates.join(" / ") || "—"}</td>
          <td>{row.support === "source_assertion" ? PHASE[row.phase ?? ""] : row.support === "conflict" ? "来源冲突" : "依据不足"}
            <StoredEvidence key={detail.project.id} projectId={detail.project.id} refs={row.history.flatMap(h => h.refs)} /></td></tr>)}</tbody></table></div>
      {detail.milestones?.truncated && <p className="muted small">仅显示部分节点。</p>}
    </section>}
    {!!funds.length && <details className="card card-pad"><summary>资金记录 · {funds.length} 项</summary>
      <div className="table-scroll"><table className="table compact-table"><thead><tr><th>项目</th><th>原始金额</th><th>期间 / 口径</th><th>资料阶段</th></tr></thead>
        <tbody>{funds.map(row => <tr key={row.fact_id}><th scope="row">{MONEY[row.measure ?? ""]}{row.milestone_name && <small> · {row.milestone_name}</small>}</th>
          <td>{row.raw_value ?? "—"} {row.raw_unit} {row.currency}</td><td>{row.period || "—"} · {row.consolidation_scope === "unknown" ? "口径未明" : row.consolidation_scope}</td>
          <td>{STAGE[row.stage ?? ""]} · {row.basis === "actual" ? "实际" : row.basis === "forecast" ? "预测" : row.basis === "budget" ? "预算" : "口径未明"}
            {row.support !== "source_assertion" && <small> · 依据不足</small>}<StoredEvidence key={detail.project.id} projectId={detail.project.id} refs={row.refs} /></td></tr>)}</tbody></table></div>
      {detail.observations?.truncated && <p className="muted small">仅显示部分记录。</p>}
    </details>}
    {!!concerns.length && <section className="card card-pad"><h3>持续关注</h3><ul className="basic-list">
      {concerns.map(row => <Concern key={row.id} detail={detail} row={row} onReload={onReload} />)}</ul>
      {detail.concerns?.truncated && <p className="muted small">仅显示部分关注项。</p>}
    </section>}
  </>;
}

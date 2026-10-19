import { useState } from "react";
import { api, type ModuleHit, type PlanModule } from "./api";
import { useToast } from "./motion";
import { relTime } from "./ui";
import { parseMetricValue } from "./planValues";

/** 展示模块库: the fixed set of ways a focus point shows its latest state. New industries add data, not modules. */
export function PlanModuleView({ watchId, module, onChanged }: { watchId: string; module?: PlanModule; onChanged: () => void }) {
  if (!module) return null;
  switch (module.view) {
    case "timeline": return <Timeline points={module.points} />;
    case "peer_table": return <PeerTable rows={module.rows} />;
    case "metric_series": return <MetricSeries watchId={watchId} m={module} onChanged={onChanged} />;
    case "commitments": return <Commitments rows={module.rows} />;
    default: return <Evidence items={module.items} total={module.total} />;
  }
}

function Link({ hit }: { hit: ModuleHit }) {
  return hit.url ? <a href={hit.url} target="_blank" rel="noreferrer noopener">{hit.title}</a> : <span>{hit.title}</span>;
}

function Evidence({ items, total }: { items: ModuleHit[]; total: number }) {
  const [expanded, setExpanded] = useState(false);
  if (!items.length) return <p className="pm-empty">还没有命中的信息。</p>;
  return <div className="pm pm-evidence">
    <ol>{(expanded ? items : items.slice(0, 3)).map((h) => <li key={h.id ?? h.title}><time>{h.date || "—"}</time><Link hit={h} />
      {h.importance === "major" && <span className="imp imp-major">重大</span>}</li>)}</ol>
    {items.length > 3 && <button className="link small pm-more" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
      {expanded ? "收起" : `展开本次列出的 ${items.length} 条`}</button>}
    {total > items.length && <p className="pm-more">本次列出 {items.length} 条，共 {total} 条</p>}
  </div>;
}

function Timeline({ points }: { points: ModuleHit[] }) {
  if (!points.length) return <p className="pm-empty">还没有节点。命中的进展和计划节点会按时间排在这里。</p>;
  return <ol className="pm pm-timeline">{points.map((p, i) => <li key={`${p.date}-${i}`} className={p.planned ? "planned" : p.importance === "major" ? "major" : ""}>
    <i aria-hidden="true" /><time>{p.date || "日期未知"}</time>
    <span>{p.planned ? <>{p.title}<em>计划</em></> : <Link hit={p} />}</span>
  </li>)}</ol>;
}

function PeerTable({ rows }: { rows: { name: string; latest: { title: string; category: string; at: string } | null; recent_90d: number }[] }) {
  if (!rows.length) return <p className="pm-empty">还没有对照对象。</p>;
  return <table className="pm pm-peers">
    <thead><tr><th>对象</th><th>最近一次动作</th><th className="num">近 90 天</th></tr></thead>
    <tbody>{rows.map((r) => <tr key={r.name}>
      <th scope="row">{r.name}</th>
      <td>{r.latest ? <><span>{r.latest.title}</span> <span className="muted small">{relTime(r.latest.at)}</span></> : <span className="muted">暂无收录</span>}</td>
      <td className="num">{r.recent_90d || <span className="muted">0</span>}</td>
    </tr>)}</tbody>
  </table>;
}

function Commitments({ rows }: { rows: { claim: string; source: string; due: string; progress: string; evidence: ModuleHit | null }[] }) {
  if (!rows.length) return <p className="pm-empty">还没有登记承诺。</p>;
  return <table className="pm pm-commit">
    <thead><tr><th>资料中的承诺</th><th>出处</th><th>公开进展</th></tr></thead>
    <tbody>{rows.map((r) => <tr key={r.claim}>
      <td>{r.claim}{r.due && <span className="muted small"> · 期限 {r.due}</span>}</td>
      <td className="muted small">{r.source || "—"}</td>
      <td>{r.evidence ? <><time className="muted small">{r.evidence.date}</time> <Link hit={r.evidence} /></>
        : r.progress ? <span>{r.progress}</span> : <span className="muted">暂无公开证据</span>}</td>
    </tr>)}</tbody>
  </table>;
}

/** One series, thin line with ≥8px markers, dashed target line, hover titles; the table below is the readable form. */
function MetricSeries({ watchId, m, onChanged }: { watchId: string; m: Extract<PlanModule, { view: "metric_series" }>; onChanged: () => void }) {
  const [adding, setAdding] = useState(false);
  const pts = m.points;
  const values = pts.map((p) => p.value);
  const target = typeof m.target === "number" ? m.target : null;
  const max = Math.max(1, ...values, target ?? 0) * 1.1;
  const W = 520, H = 120, padL = 8, padR = 8, padT = 10, padB = 22;
  const x = (i: number) => padL + (pts.length <= 1 ? (W - padL - padR) / 2 : i * (W - padL - padR) / (pts.length - 1));
  const y = (v: number) => padT + (H - padT - padB) * (1 - v / max);
  const fmt = (v: number) => v.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
  return <div className="pm pm-metric">
    <div className="pm-metric-head"><strong>{m.metric || "指标"}</strong>{m.unit && <span className="muted small">单位：{m.unit}</span>}
      {target !== null && <span className="muted small">{m.target_label || "目标"}：{fmt(target)}</span>}</div>
    {pts.length ? <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${m.metric}：${pts.map((p) => `${p.period} ${fmt(p.value)}`).join("，")}`}>
      <line x1={padL} x2={W - padR} y1={H - padB} y2={H - padB} className="pm-axis" />
      {target !== null && <><line x1={padL} x2={W - padR} y1={y(target)} y2={y(target)} className="pm-target" />
        <text x={W - padR} y={y(target) - 4} className="pm-target-label" textAnchor="end">{m.target_label || "目标"}</text></>}
      {pts.length > 1 && <polyline className="pm-line" points={pts.map((p, i) => `${x(i)},${y(p.value)}`).join(" ")} />}
      {pts.map((p, i) => <g key={p.id}>
        <circle cx={x(i)} cy={y(p.value)} r={4} className="pm-dot"><title>{`${p.period}：${fmt(p.value)} ${p.unit || m.unit}`}</title></circle>
        <text x={x(i)} y={H - 6} className="pm-tick" textAnchor="middle">{p.period}</text>
      </g>)}
    </svg> : <p className="pm-empty">还没有数据点。{m.source_hint ? `建议来源：${m.source_hint}。` : ""}</p>}
    {pts.length > 0 && <details className="pm-table"><summary>数据与出处（{pts.length}）</summary>
      <table><tbody>{pts.map((p) => <tr key={p.id}><td>{p.period}</td><td className="num">{fmt(p.value)} {p.unit || m.unit}</td>
        <td>{p.url ? <a href={p.url} target="_blank" rel="noreferrer noopener">来源 ↗</a> : <span className="muted">无链接</span>}{p.note && <span className="muted small"> · {p.note}</span>}</td>
        <td><RemovePoint id={p.id} onChanged={onChanged} /></td></tr>)}</tbody></table></details>}
    {adding ? <AddPoint watchId={watchId} unit={m.unit} onDone={() => { setAdding(false); onChanged(); }} />
      : <button className="link small" onClick={() => setAdding(true)}>+ 添加数据点</button>}
  </div>;
}

function RemovePoint({ id, onChanged }: { id: string; onChanged: () => void }) {
  return <button className="link small muted" onClick={async () => { await api.removeMetricPoint(id); onChanged(); }}>删除</button>;
}

function AddPoint({ watchId, unit, onDone }: { watchId: string; unit: string; onDone: () => void }) {
  const [period, setPeriod] = useState("");
  const [value, setValue] = useState("");
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const toast = useToast();
  const save = async () => {
    if (saving) return;
    const number = parseMetricValue(value);
    if (!period.trim()) { setError("请填写期间"); return; }
    if (number === null) { setError("请填写有效数值，零值可输入 0"); return; }
    setError("");
    setSaving(true);
    try { await api.addMetricPoint(watchId, { period: period.trim(), value: number, unit, url: url.trim() }); toast("已添加数据点"); onDone(); }
    catch (e) { toast(e instanceof Error ? e.message : "未保存", "error"); }
    finally { setSaving(false); }
  };
  return <form className="pm-add" onSubmit={(e) => { e.preventDefault(); void save(); }}>
    <input value={period} onChange={(e) => setPeriod(e.target.value)} placeholder="期间，如 2026-09" aria-label="期间" maxLength={20} />
    <input value={value} onChange={(e) => setValue(e.target.value)} placeholder={`数值${unit ? `（${unit}）` : ""}`} aria-label="数值" inputMode="decimal" />
    <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="来源链接（建议填写）" aria-label="来源链接" />
    <button className="btn btn-sm" disabled={saving}>{saving ? "保存中…" : "保存"}</button>
    {error && <span role="alert" style={{ gridColumn: "1 / -1" }}>{error}</span>}
  </form>;
}

import { useState } from "react";
import { api, type Assumption, type EarlyWarning, type WarningSource } from "./api";
import { ErrorNote, relTime, useAsync } from "./ui";
import "./roundView.css";

const STATUS: Record<string, string> = { strengthened: "得到支持", weakened: "受到削弱", unchanged: "本轮无相关信息", unclear: "无法判断" };

function Source({ source, why }: { source?: WarningSource; why?: string }) {
  if (!source) return null;
  const href = /^https?:\/\//i.test(source.url) ? source.url : undefined;
  return <li>{href ? <a href={href} target="_blank" rel="noreferrer noopener">{source.title}</a> : source.title}
    <span className="muted small"> · {source.source}{source.published_at ? ` · ${relTime(source.published_at)}` : ""}</span>
    {why && <span className="small"> — {why}</span>}</li>;
}

/** Investment assumptions and this round's check against them. Signals, not confirmed facts. */
export function AssumptionsPanel({ projectId }: { projectId: string }) {
  const list = useAsync(() => api.assumptions(projectId), [projectId]);
  const warning = useAsync<EarlyWarning | null>(() => api.warning(projectId), [projectId]);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [adding, setAdding] = useState("");
  const [error, setError] = useState<unknown>(null);
  const items = list.data?.items ?? [];
  const checked = new Map((warning.data?.assumptions ?? []).map((a) => [a.id, a]));
  const act = async (action: () => Promise<unknown>) => {
    setError(null);
    try { await action(); list.reload(); } catch (e) { setError(e); }
  };
  const save = (a: Assumption) => act(async () => { await api.patchAssumption(projectId, a.id, { text: draft }); setEditing(null); });
  if (list.loading && !list.data) return null;
  return <section className="card card-pad assumptions-panel" aria-label="投资假设">
    <div className="card-head"><h3>投资假设</h3>
      <span className="muted small">{warning.data?.status === "checked" ? `最近核对 ${relTime(warning.data.created_at)} · 依据为公开信息，未经确认` : "每次更新后对照新信息核对"}</span></div>
    {items.length === 0 ? <p className="muted">还没有投资假设。首次分析时会根据公司资料起草，也可以直接添加。</p> :
      <table>
        <thead><tr><th>假设</th><th>本轮</th><th>依据</th><th /></tr></thead>
        <tbody>{items.map((a) => {
          const row = checked.get(a.id);
          return <tr key={a.id}>
            <td>{editing === a.id
              ? <input type="text" value={draft} onChange={(e) => setDraft(e.target.value)} aria-label="修改假设" />
              : <>{a.text}{a.author === "model" && !a.user_override && <span className="muted small"> · AI 起草</span>}</>}</td>
            <td className={`status status-${row?.status ?? "none"}`}>{row ? STATUS[row.status] : "—"}</td>
            <td>{row && row.signals.length > 0 ? <ul>{row.signals.map((s) => <Source key={s.item} source={s.source} why={s.why} />)}</ul> : <span className="muted">—</span>}</td>
            <td className="row-actions">{editing === a.id
              ? <><button className="btn btn-sm" onClick={() => save(a)} disabled={draft.trim().length < 4}>保存</button>
                <button className="btn btn-sm btn-ghost" onClick={() => setEditing(null)}>取消</button></>
              : <><button className="link small" onClick={() => { setEditing(a.id); setDraft(a.text); }}>修改</button>
                <button className="link small" onClick={() => act(() => api.patchAssumption(projectId, a.id, { status: "retired" }))}>不再跟踪</button></>}</td>
          </tr>;
        })}</tbody>
      </table>}
    <form className="assumptions-add" onSubmit={(e) => { e.preventDefault(); act(async () => { await api.addAssumption(projectId, adding); setAdding(""); }); }}>
      <input type="text" value={adding} onChange={(e) => setAdding(e.target.value)} placeholder="添加一条假设，例如：核心管线 2027 年前进入三期" aria-label="添加假设" />
      <button className="btn btn-sm" type="submit" disabled={adding.trim().length < 4}>添加</button>
    </form>
    {(warning.data?.unforeseen?.length ?? 0) > 0 && <div>
      <h4>假设之外的变化</h4>
      <ul>{warning.data!.unforeseen!.map((u, i) => <li key={i}>{u.text}
        {u.sources && u.sources.length > 0 && <ul>{u.sources.map((s) => <Source key={s.id} source={s} />)}</ul>}</li>)}</ul>
    </div>}
    {(warning.data?.explore?.length ?? 0) > 0 && <p className="muted small">下一次更新将补充检索：{warning.data!.explore!.map((e) => e.query).join("、")}</p>}
    <ErrorNote error={error} />
  </section>;
}

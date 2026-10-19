import { api, type CommitmentsView, type MonitoringProgress } from "./api";
import { showStoredPanel } from "./MonitoringProgress";
import { Reveal } from "./motion";
import { useAsync } from "./ui";

export function Commitments({ projectId, revision, progress }: { projectId: string; revision: string; progress?: MonitoringProgress }) {
  const { data, error } = useAsync<CommitmentsView>(() => api.commitments(projectId), [projectId, revision]);
  if (!data && !error) return null;
  if (!error && !data?.truncated && !showStoredPanel(progress, data?.items.length ?? 0)) return null;
  // No dated, sourced promise: the card stays hidden instead of a placeholder. Failures still show.
  if (!error && !data?.truncated && !data?.items.length) return null;
  return <Reveal as="section" className="card card-pad commitments-panel">
    <div className="card-head"><h3>说过的话兑现了吗</h3><span className="muted small">公开表态 · 规则对照</span></div>
    {error ? <p className="muted small">公开承诺读取失败，请稍后重试。</p> : data?.truncated ?
      <p className="muted small">记录超出当前读取范围，依据不足。</p> : !data?.items.length ? null : <>
      <p className="muted small">{data.basis}</p>
      <div className="table-scroll"><table className="commitments-table"><thead><tr>
        <th scope="col">公司与原话</th><th scope="col">目标时间</th><th scope="col">后续记录</th>
      </tr></thead><tbody>{data.items.map(p => <tr key={p.fact_id}>
        <td><strong>{p.subject_name}</strong><p>“{p.statement}”</p>
          <span className="muted small">{p.first_said_at}{p.statements > 1 ? ` · 共 ${p.statements} 次表态，最近 ${p.last_said_at}` : ""}</span>
          <div className="small">{p.sources.map((s, i) => <a key={i} href={s.url} target="_blank" rel="noreferrer noopener">{s.source || "原话出处"} ↗ </a>)}</div></td>
        <td>{p.due_start !== p.due_end ? `${p.due_start} 至 ` : ""}{p.due_end}
          {p.due_changed && <p className="muted small">后续表态调整了期限</p>}</td>
        <td><span className={`commitment-status status-${p.status}`}>{p.status_label}</span>
          <div className="small">{p.outcome_sources.map((s, i) => <a key={i} href={s.url} target="_blank" rel="noreferrer noopener">{s.source || "后续出处"} ↗ </a>)}</div></td>
      </tr>)}</tbody></table></div>
    </>}
  </Reveal>;
}

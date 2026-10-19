import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "./api";
import { ErrorNote, Spinner, relTime, useAsync } from "./ui";
import { useToast } from "./motion";

const LABELS: Record<string, string> = { xiniu: "烯牛数据", pharmcube: "医药魔方", wind: "万得 Wind" };
const STATES: Record<string, string> = { ok: "已更新", partial: "部分结果", error: "暂不可用",
  held: "结果未确认", started: "更新中", not_collected: "尚未查询", limited: "今日查询已达上限" };

export default function CommercialSources({ projectId, revision, peers = false, onChanged }: {
  projectId: string; revision: string; peers?: boolean; onChanged: () => void;
}) {
  const { data, error, reload } = useAsync(() => api.commercial(projectId), [projectId, revision]);
  const [actionError, setActionError] = useState<unknown>(null);
  const [updating, setUpdating] = useState(false);
  const toast = useToast();
  if (!data) return error ? <ErrorNote error={error} /> : null;
  const records = data.records.filter(r => peers ? r.relationship !== "company" : r.relationship === "company");
  const sources = data.sources.filter(s => peers ? s.relationship !== "company" : s.relationship === "company");
  const enabled = sources.some(s => s.enabled);
  const funding = records.filter(r => r.kind === "funding");
  const news = records.filter(r => r.kind === "news");
  if (peers && !records.length && !sources.length) return null;
  // Not connected and nothing stored: one quiet line, not an empty card above the news.
  if (!peers && !enabled && !records.length && !sources.some(s => s.last_checked)) {
    return <p className="commercial-empty muted small">融资动态 · 尚未连接专业数据来源 <Link to="/settings" className="link small">设置来源</Link></p>;
  }
  const different = funding.some(a => funding.some(b => a.company === b.company && a.round === b.round
    && a.provider !== b.provider && ((a.date && b.date && a.date !== b.date)
      || (a.amount && b.amount && a.amount !== b.amount))));
  const refresh = async () => {
    setUpdating(true); setActionError(null);
    try {
      await api.refresh(projectId, "commercial");
      toast("正在更新融资动态");
      onChanged(); reload();
    } catch (e) { setActionError(e); }
    finally { setUpdating(false); }
  };

  return <section className="card card-pad commercial-panel">
    <div className="card-head">
      <h2>{peers ? "同行融资" : "融资动态"}</h2>
      {enabled ? <button className="link small commercial-refresh" disabled={updating} onClick={refresh}
        aria-label={peers ? "刷新同行融资" : "刷新融资动态"}>{updating ? <Spinner /> : "↻ 刷新"}</button>
        : !peers && <Link to="/settings" className="link small">设置来源</Link>}
    </div>
    {funding.length > 0 && <div className="table-scroll"><table className="commercial-table">
      <thead><tr>{peers && <th>公司</th>}<th>日期</th><th>轮次</th><th>金额</th><th>投资方</th><th>来源</th></tr></thead>
      <tbody>{funding.map((r, i) => <tr key={`${r.provider}-${r.company}-${i}`}>
        {peers && <td>{r.company}<div className="muted small">{r.relationship === "tracked_peer" ? "指定跟踪" : "有竞争依据"}</div></td>}
        <td>{r.date || "未披露"}</td><td>{r.round || "未披露"}</td><td>{r.amount || "未披露"}</td>
        <td>{r.investors || "未披露"}</td><td>{LABELS[r.provider]}{r.stale && <div className="muted small">历史结果</div>}</td>
      </tr>)}</tbody>
    </table></div>}
    {!funding.length && <p className="muted small">{peers && !sources.length ? "暂无跟踪公司。"
      : records.length ? "暂无融资记录，可查看相关报道。" : enabled ? "暂无融资记录。" : "尚未连接数据来源。"}</p>}
    {news.length > 0 && <details className="commercial-news"><summary>相关报道 · {news.length}</summary>
      <ul>{news.map((r, i) => <li key={`${r.provider}-${i}`}>
        <div>{r.url ? <a href={r.url} target="_blank" rel="noopener noreferrer">{r.title} ↗</a> : r.title}</div>
        <span className="muted small">{r.date || "日期未披露"} · {LABELS[r.provider]}{r.stale ? " · 历史结果" : ""}</span>
        {r.excerpt && <details><summary className="small muted">查看摘录</summary><p className="small">{r.excerpt}</p></details>}
      </li>)}</ul>
    </details>}
    {sources.some(s => s.enabled || s.last_checked) && <details className="commercial-source-details">
      <summary>来源{different && <span className="commercial-convention">口径差异</span>}
        {sources.some(s => s.enabled && ["error", "held", "limited"].includes(s.status))
          && <span className="commercial-convention">部分来源未更新</span>}</summary>
      <div className="commercial-coverage">
      {sources.filter(s => s.enabled || s.last_checked).map(s => <span key={`${s.provider}-${s.company}`}
        className="muted small" title={`${s.company}${s.truncated ? " · 只查询第一页/有限条目" : ""}${s.rejected ? ` · ${s.rejected} 条主体未匹配` : ""}`}>
        {s.label} · {s.enabled ? (STATES[s.status] || "未查询") : "已停用"}
        {s.last_checked && ` · ${relTime(s.last_checked)}`}
      </span>)}
      </div>
      {different && <p className="muted small">日期、金额按来源列示。</p>}
      {sources.some(s => s.provider === "wind" && s.last_checked) && <p className="muted small">数据来源于万得 Wind 金融数据服务。</p>}
    </details>}
    {data.daily_queries.remaining === 0 && <p className="muted small">今日查询已达上限，明天恢复。</p>}
    <ErrorNote error={actionError} />
  </section>;
}

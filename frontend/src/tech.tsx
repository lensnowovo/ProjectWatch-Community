import { Skeleton } from "./frame";
import { useEffect, useState } from "react";
import { api, type TechView } from "./api";
import { Reveal, useToast } from "./motion";
import { ErrorNote, relTime, useAsync } from "./ui";
import { publicLink } from "./relationScope";
import './techReading.css';

/** Full-width, bounded summaries; original titles remain available to keyboard and touch. */
export function TechEntrants({ entrants }: { entrants: TechView['entrants'] }) {
  const [all, setAll] = useState(false);
  return <section className="tech-side" aria-label="新面孔">
    <h4>新面孔 <span className="muted small">近 90 天首次出现的机构或公司</span></h4>
    {entrants.length ? <>
      <table className="table compact-table tech-new-table"><colgroup><col className="tech-name-col"/><col className="tech-date-col"/><col/></colgroup>
        <thead><tr><th>机构或公司</th><th>首次出现</th><th>主题与来源</th></tr></thead>
        <tbody>{(all ? entrants : entrants.slice(0,5)).map(e => {
          const url = publicLink(e.url);
          return <tr key={e.name}>
            <th scope="row"><span className="tech-entrant-name" title={e.name}>{e.name}</span><span className="tech-entrant-note muted small">{e.what}</span></th>
            <td className="num tech-entrant-date"><time>{e.first || '日期未知'}</time></td>
            <td><details className="tech-topic"><summary><span>{e.title || '主题未标注'}</span><small>展开</small></summary>
              <p>{e.title || '主题未标注'}</p>{url && <a href={url} target="_blank" rel="noreferrer noopener">查看原始来源 ↗</a>}
            </details></td>
          </tr>;
        })}</tbody>
      </table>
      {entrants.length > 5 && <button className="link small tech-entrants-more" aria-expanded={all} onClick={()=>setAll(v=>!v)}>{all ? '收起更多机构' : `查看其余 ${entrants.length-5} 家`}</button>}
    </> : <p className="muted small">近 90 天没有新出现的机构或公司。</p>}
  </section>;
}

/** Grouped quarterly bars, one series per route; colour follows the route's slot, the company's route first. */
function RouteChart({ data }: { data: TechView }) {
  const rows = data.rows;
  const max = Math.max(1, ...rows.flatMap((r) => r.values));
  const W = 560, H = 150, groupGap = 18, barGap = 2;
  const gw = (W - groupGap * (data.quarters.length - 1)) / data.quarters.length;
  const bw = Math.min(16, (gw - barGap * (rows.length - 1)) / Math.max(1, rows.length));
  const inner = bw * rows.length + barGap * (rows.length - 1);
  return (
    <figure className="tech-chart">
      <ul className="tech-legend">
        {rows.map((r, i) => <li key={r.term}><span className={`tech-swatch tech-s${i + 1}`} aria-hidden />{r.term}{r.own && <span className="muted">（公司所用）</span>}
          <span className="muted small"> · 共 {r.total}</span></li>)}
      </ul>
      <div className="tech-scroll"><svg viewBox={`-6 0 ${W + 12} ${H + 20}`} role="img"
        aria-label={`已收录材料中各技术标签按季度的提及次数：${rows.map((r) => `${r.term} ${data.quarters.map((q, i) => `${q} ${r.values[i]}`).join("，")}`).join("；")}`}>
        <line x1="0" x2={W} y1={H} y2={H} className="track-axis" />
        {data.quarters.map((q, qi) => {
          const x0 = qi * (gw + groupGap) + (gw - inner) / 2;
          return (
            <g key={q}>
              {rows.map((r, ri) => {
                const v = r.values[qi] ?? 0;
                const bh = v ? Math.max(3, (v / max) * (H - 18)) : 0;
                const x = x0 + ri * (bw + barGap);
                return <g key={r.term}>
                  <rect x={x - 1} y={0} width={bw + 2} height={H} className="track-hit"><title>{`${q} · ${r.term}：${v} 次`}</title></rect>
                  {bh > 0 && <path className={`tech-bar tech-f${ri + 1}`} d={`M${x},${H} v${-(bh - 3)} q0,-3 3,-3 h${bw - 6} q3,0 3,3 v${bh - 3} z`} />}
                </g>;
              })}
              <text x={qi * (gw + groupGap) + gw / 2} y={H + 15} className="track-tick">{q}</text>
            </g>
          );
        })}
      </svg></div>
      <figcaption className="muted small"><details className="chart-evidence"><summary>统计口径</summary>已收录论文、专利与报道的季度提及次数，不是市场份额，也不代表技术领先或产业共识。不同维度的标签可并存。</details></figcaption>
    </figure>
  );
}

function RouteEditor({ data, projectId, onSaved, onClose }: { data: TechView; projectId: string; onSaved: (v: TechView) => void; onClose: () => void }) {
  const [terms, setTerms] = useState(data.routes.map((r) => r.term));
  const [own, setOwn] = useState(data.routes.find((r) => r.own)?.term ?? "");
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<unknown>(null);
  const toast = useToast();
  const add = () => { const t = draft.trim(); if (t && terms.length < 4 && !terms.includes(t)) setTerms([...terms, t]); setDraft(""); };
  const save = async (reset = false) => {
    setError(null);
    try {
      onSaved(await api.saveTech(projectId, reset ? null : terms, reset ? null : own || null));
      toast(reset ? "已恢复系统建议" : "已保存技术路线");
      onClose();
    } catch (e) { setError(e); }
  };
  return (
    <div className="tech-editor">
      <fieldset>
        <legend className="small muted">最多 4 条；选中公司所用的路线</legend>
        <ul className="tech-terms">{terms.map((t) => (
          <li key={t}>
            <label><input type="radio" name="own-route" checked={own === t} onChange={() => setOwn(t)} />{t}</label>
            <button type="button" className="link small" aria-label={`移除 ${t}`} onClick={() => { setTerms(terms.filter((x) => x !== t)); if (own === t) setOwn(""); }}>移除</button>
          </li>))}
        </ul>
      </fieldset>
      <form className="peer-add" onSubmit={(e) => { e.preventDefault(); add(); }}>
        <input aria-label="添加技术路线" placeholder="如 1550nm、FMCW、固态" maxLength={20} value={draft} onChange={(e) => setDraft(e.target.value)} disabled={terms.length >= 4} />
        <button className="btn btn-sm" disabled={!draft.trim() || terms.length >= 4}>添加</button>
      </form>
      {data.suggested.some((s) => !terms.includes(s.term)) && <p className="small muted">系统建议：
        {data.suggested.filter((s) => !terms.includes(s.term)).map((s) => <button key={s.term} type="button" className="link small tech-sugg"
          disabled={terms.length >= 4} onClick={() => setTerms([...terms, s.term])}>＋{s.term}</button>)}</p>}
      <div className="button-row">
        <button className="btn btn-sm btn-primary" onClick={() => save()} disabled={!terms.length}>保存</button>
        <button className="btn btn-sm" onClick={onClose}>取消</button>
        {data.source === "user" && <button className="link small" onClick={() => save(true)}>恢复系统建议</button>}
      </div>
      <ErrorNote error={error} />
    </div>
  );
}

/** Collected technical mentions and newly observed names, not route superiority. */
export function TechRoutes({ projectId, revision, onSettled }: { projectId: string; revision: string; onSettled?: () => void }) {
  const { data, error, setData, reload } = useAsync<TechView>(() => api.tech(projectId), [projectId, revision]);
  const settled = !!data || !!error;
  useEffect(() => { if (settled) onSettled?.(); }, [settled]); // eslint-disable-line react-hooks/exhaustive-deps
  const [editing, setEditing] = useState(false);
  useEffect(() => setEditing(false), [projectId]);
  if (!data) return error ? <section><ErrorNote error={error}/><button className="link small" onClick={reload}>重新读取技术信息</button></section> : <section className="card card-pad tech-panel tech-skel" role="status" aria-label="读取技术信息"><Skeleton lines={4} /></section>;
  return (
    <Reveal as="section" className="card card-pad tech-panel">
      <div className="card-head tech-head">
        <h3>技术信息动态</h3>
        <span className="muted small">{data.source === "auto" ? "技术标签由系统从已收录材料中提取" : "标签已按你的设置"} · {relTime(data.generated_at)}</span>
        <button className="link small tech-edit" onClick={() => setEditing((v) => !v)}>{editing ? "收起" : "调整路线"}</button>
      </div>
      {editing && <RouteEditor data={data} projectId={projectId} onSaved={(v) => setData(() => v)} onClose={() => setEditing(false)} />}
      <div className="tech-body">
        <div className="tech-main">
          {data.rows.length ? <RouteChart data={data} /> : <p className="muted small">已收录材料中还没有技术标签。可点“调整路线”添加。</p>}
          {data.insight && <p className="tech-insight">{data.insight}</p>}
        </div>
        <TechEntrants key={projectId} entrants={data.entrants}/>
      </div>
    </Reveal>
  );
}

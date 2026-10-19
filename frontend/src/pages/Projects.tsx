import { Skeleton } from "../frame";
import { motion } from "motion/react";
import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type Project } from "../api";
import { SignalChips } from "../attention";
import SpotlightCard from "../bits/SpotlightCard";
import { EASE, Page, useToast } from "../motion";
import { Empty, ErrorNote, TONE_LABEL, formatAmount, relTime, useAsync } from "../ui";
import { periodLabel } from "../metrics";
import { Seg } from "../widgets";
import PageHeader from "../PageHeader";

type SortKey = "recent" | "major" | "unread" | "revenue" | "growth" | "runway" | "name" | "signals";
type View = "cards" | "table";

function readView(): View {
  try {
    return localStorage.getItem("pw-projects-view") === "cards" ? "cards" : "table";
  } catch {
    return "table";
  }
}

/** 移出关注 from the portfolio: inline confirmation; the project's data stays on this machine. */
function RemoveControl({ project, onRemoved }: { project: Project; onRemoved: () => void }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const stop = (e: React.SyntheticEvent) => e.stopPropagation();
  const remove = async () => {
    setBusy(true);
    try { await api.removeProject(project.id); toast(`已将 ${project.name} 移出关注`, "info"); onRemoved(); }
    finally { setBusy(false); setConfirming(false); }
  };
  return (
    <span className="remove-wrap" onClick={stop} onKeyDown={stop}>
      <button type="button" className="btn-row-remove" aria-expanded={confirming} title="移出关注" onClick={() => setConfirming((v) => !v)}>移出</button>
      {confirming && <div className="remove-confirm" role="dialog" aria-label={`确认将 ${project.name} 移出关注`}>
        <p>将 <strong>{project.name}</strong> 移出关注？之后不再自动更新、不再调用模型，也不出现在侧栏和列表中；资料与历史记录保留，可在本页底部恢复。</p>
        <div><button className="btn btn-sm btn-danger" disabled={busy} onClick={remove}>确认移出</button>
          <button className="btn btn-sm btn-ghost" onClick={() => setConfirming(false)}>取消</button></div>
      </div>}
    </span>
  );
}

/** Projects taken off the watchlist: listed at the bottom with a restore action. */
function RemovedProjects({ revision, onRestored }: { revision: number; onRestored: () => void }) {
  const { data, reload } = useAsync(() => api.removedProjects(), [revision]);
  const toast = useToast();
  if (!data?.length) return null;
  const restore = async (id: string, name: string) => {
    await api.restoreProject(id); toast(`已恢复关注 ${name}`); reload(); onRestored();
  };
  return (
    <details className="removed-projects">
      <summary>已移出的项目 <span className="muted small">{data.length} 个 · 资料仍保留在本机</span></summary>
      <ul>{data.map((p) => <li key={p.id}>
        <div><Link to={`/projects/${p.id}`} className="cell-title">{p.name}</Link>
          <span className="muted small">{[p.industry, p.stage].filter(Boolean).join(" · ")}{p.updated_at ? ` · 移出于 ${relTime(p.updated_at)}` : ""}</span></div>
        <button className="btn btn-sm" onClick={() => restore(p.id, p.name)}>恢复关注</button>
      </li>)}</ul>
    </details>
  );
}

export default function ProjectsPage({ onChanged }: { onChanged?: () => void }) {
  const navigate = useNavigate();
  const { data, error, loading, reload } = useAsync(() => api.projects(), []);
  const [removedRevision, setRemovedRevision] = useState(0);
  const changed = () => { reload(); setRemovedRevision((n) => n + 1); onChanged?.(); };
  const attention = useAsync(() => api.attention(7), []);
  const signals = useMemo(() => Object.fromEntries((attention.data?.projects ?? []).map((p) => [p.id, p])), [attention.data]);
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<SortKey>("recent");
  const [view, setViewState] = useState<View>(readView);
  const setView = (v: View) => {
    setViewState(v);
    try { localStorage.setItem("pw-projects-view", v); } catch { /* per-viewer preference only */ }
  };

  const rows = useMemo(() => {
    const list = (data ?? []).filter((p) =>
      [p.name, p.legal_name, p.industry, ...(p.aliases ?? [])].join(" ").toLowerCase()
        .includes(query.trim().toLowerCase()));
    const by: Record<SortKey, (a: Project, b: Project) => number> = {
      recent: (a, b) => (b.last_event_at ?? "").localeCompare(a.last_event_at ?? ""),
      major: (a, b) => (b.major_recent ?? 0) - (a.major_recent ?? 0),
      unread: (a, b) => (b.unread ?? 0) - (a.unread ?? 0),
      revenue: (a, b) => (b.finance?.revenue?.value ?? -Infinity) - (a.finance?.revenue?.value ?? -Infinity),
      growth: (a, b) => (b.finance?.revenue_growth ?? -Infinity) - (a.finance?.revenue_growth ?? -Infinity),
      // Shortest runway first: that is where attention is needed.
      runway: (a, b) => (a.finance?.runway?.months ?? Infinity) - (b.finance?.runway?.months ?? Infinity),
      name: (a, b) => a.name.localeCompare(b.name, "zh-CN"),
      signals: (a, b) => (signals[b.id]?.score ?? 0) - (signals[a.id]?.score ?? 0),
    };
    return [...list].sort(by[sort]);
  }, [data, query, sort, signals]);

  return (
    <Page className="page-workspace">
      <PageHeader variant="portfolio" title="项目组合" label="关注的公司"
        description="经营情况与最新变化。"
        meta={data ? `${data.length} 个项目已加入` : error ? "暂时无法读取项目" : "正在读取项目…"}
        action={<Link to="/new" className="btn">＋ 添加项目</Link>} />
      <div className="filters">
        <input className="search" placeholder="搜索公司、行业" value={query} onChange={(e) => setQuery(e.target.value)} />
        {view === "cards" && <Seg id="sort" value={sort} onChange={setSort} options={[
          { value: "recent", label: "最近动态" }, { value: "signals", label: "待处理" }, { value: "major", label: "重大事件" }, { value: "name", label: "名称" }]} />}
        <span className="spacer" />
        <Seg id="view" value={view} onChange={setView} options={[{ value: "cards", label: "卡片" }, { value: "table", label: "表格" }]} />
      </div>
      <ErrorNote error={error} />
      {loading && !data && <Skeleton lines={6} />}
      {data && data.length === 0 && (
        <Empty title="还没有项目" action={<Link to="/new" className="btn btn-primary">添加第一个项目</Link>} />
      )}
      {view === "cards" && rows.length > 0 && (
        <motion.div className="project-grid" initial="hidden" animate="show"
          variants={{ show: { transition: { staggerChildren: 0.05 } } }}>
          {rows.map((p) => (
            <motion.div key={p.id} className="pc-wrap" variants={{ hidden: { opacity: 0, y: 14 }, show: { opacity: 1, y: 0, transition: { duration: 0.45, ease: EASE } } }}
              whileHover={{ y: -3 }} transition={{ type: "spring", stiffness: 400, damping: 30 }}>
              <SpotlightCard className="project-card" role="link" tabIndex={0}
                onClick={() => navigate(`/projects/${p.id}`)}
                onKeyDown={(e) => e.key === "Enter" && navigate(`/projects/${p.id}`)}>
                <div className="pc-top">
                  <div className="pc-id">
                    <div className="pc-name truncate">{p.name}</div>
                    <div className="muted small truncate pc-sub">{p.legal_name !== p.name ? p.legal_name : p.industry}</div>
                  </div>
                  <span className="pc-actions">
                    {(p.major_recent ?? 0) > 0 && <span className="pc-major">{p.major_recent} 重大</span>}
                    <RemoveControl project={p} onRemoved={changed} />
                  </span>
                </div>
                <div className="pc-meta truncate">{[p.industry, p.stage, p.location].filter(Boolean).join(" · ") || "—"}</div>
                {/* The chips row keeps its place even with nothing to show, so cards share one rhythm. */}
                <div className="pc-signals">{signals[p.id] && <SignalChips counts={signals[p.id].counts} />}</div>
                <div className="pc-headline">
                  {p.headline ? (
                    <>{p.tone && <span className={`tone tone-${p.tone}`}>{TONE_LABEL[p.tone as keyof typeof TONE_LABEL]}</span>}{p.headline}</>
                  ) : <span className="muted">{p.job_status === "running" || p.job_status === "queued" ? "正在整理…" : "暂无判断"}</span>}
                </div>
                <div className="pc-foot">
                  <span className="truncate">{p.last_event || "暂无外部动态"}</span>
                  <span className="muted pc-when">{relTime(p.last_event_at)}</span>
                </div>
              </SpotlightCard>
            </motion.div>
          ))}
        </motion.div>
      )}
      {view === "table" && rows.length > 0 && (
        <div className="table-wrap portfolio-wrap">
          <table className="table table-click portfolio">
            <colgroup><col className="pcol-company" /><col className="pcol-judge" /><col className="pcol-todo" /><col className="pcol-major" />
              <col className="pcol-unread" /><col className="pcol-rev" /><col className="pcol-runway" /><col className="pcol-recent" /><col className="pcol-act" /></colgroup>
            <thead>
              <tr>
                <SortTh k="name" sort={sort} setSort={setSort}>公司</SortTh>
                <th>最新判断</th>
                <SortTh k="signals" sort={sort} setSort={setSort}>待处理</SortTh>
                <SortTh k="major" sort={sort} setSort={setSort} num>重大<small className="muted"> 30天</small></SortTh>
                <SortTh k="unread" sort={sort} setSort={setSort} num>未读</SortTh>
                <SortTh k="revenue" sort={sort} setSort={setSort} num>收入 · 同比</SortTh>
                <SortTh k="runway" sort={sort} setSort={setSort} num>现金跑道</SortTh>
                <SortTh k="recent" sort={sort} setSort={setSort}>最近动态</SortTh>
                <th aria-label="操作" />
              </tr>
            </thead>
            <tbody>
              {rows.map((p) => {
                const f = p.finance;
                const rev = formatAmount(f?.revenue?.value, f?.revenue?.unit ?? "");
                const runway = f?.runway;
                return (
                  <tr key={p.id} onClick={() => navigate(`/projects/${p.id}`)}>
                    <td><div className="cell-title">{p.name}</div>
                      <div className="muted small" title={[p.industry, p.stage].filter(Boolean).join(" · ")}>{[p.industry, p.stage].filter(Boolean).join(" · ") || "—"}</div></td>
                    <td className="cell-wide">
                      {p.headline ? <>{p.tone && <span className={`tone tone-${p.tone}`}>{TONE_LABEL[p.tone as keyof typeof TONE_LABEL]}</span>}<span className="truncate-2">{p.headline}</span></>
                        : <span className="muted">{p.job_status === "running" || p.job_status === "queued" ? "正在整理…" : "暂无"}</span>}
                    </td>
                    <td title={signals[p.id]?.entries.slice(0, 3).map((e) => e.title).join("\n")}><SignalChips counts={signals[p.id]?.counts} /></td>
                    <td className="num">{p.major_recent ? <strong className="text-major">{p.major_recent}</strong> : <span className="muted">0</span>}</td>
                    <td className="num">{p.unread ? <span className="unread-pill">{p.unread}</span> : <span className="muted">0</span>}</td>
                    <td className="num">{f?.revenue ? <>{rev.value}<small> {rev.unit}</small><div className="muted small">{periodLabel(f.revenue.period)}
                      {f.revenue_growth != null && <> · <span className={f.revenue_growth >= 0 ? "up" : "down"}>{f.revenue_growth >= 0 ? "+" : ""}{f.revenue_growth}%</span></>}</div></> : <span className="muted">—</span>}</td>
                    <td className="num">{runway?.months != null ? <span className={runway.status === "short" ? "text-warn" : ""} title={runway.text}>{Math.floor(runway.months)} 个月</span>
                      : runway?.status === "positive" ? <span className="up">经营现金流非负</span> : <span className="muted">—</span>}</td>
                    <td><div className="truncate-2">{p.last_event || <span className="muted">—</span>}</div><div className="muted small">{relTime(p.last_event_at)}</div></td>
                    <td className="cell-actions"><RemoveControl project={p} onRemoved={changed} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <RemovedProjects revision={removedRevision} onRestored={changed} />
    </Page>
  );
}

function SortTh({ k, sort, setSort, num, children }: {
  k: SortKey; sort: SortKey; setSort: (k: SortKey) => void; num?: boolean; children: React.ReactNode;
}) {
  return (
    <th className={`${num ? "num" : ""} sortable ${sort === k ? "sorted" : ""}`} aria-sort={sort === k ? "descending" : "none"}>
      <button onClick={() => setSort(k)}>{children}{sort === k && <span aria-hidden> ↓</span>}</button>
    </th>
  );
}

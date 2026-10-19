import { Skeleton } from "../frame";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Job, type TaskBoard, type TaskDocument, type TaskJob } from "../api";
import { RecoveryDialog } from "../RecoveryDialog";
import { Page } from "../motion";
import PageHeader from "../PageHeader";
import { ErrorNote, errorMessage, relTime, useAsync } from "../ui";
import { Seg } from "../widgets";

/** 任务看板: only what is being processed stays open; finished work folds into the history. */
const STEP_STATE: Record<string, string> = { done: "完成", running: "进行中", waiting: "等待", failed: "失败", blocked: "受阻", partial: "部分完成" };
type Filter = "all" | "failed" | "done";

function duration(seconds: number | null) {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds} 秒`;
  const m = Math.floor(seconds / 60);
  return m < 60 ? `${m} 分 ${seconds % 60} 秒` : `${Math.floor(m / 60)} 小时 ${m % 60} 分`;
}

const docDone = (d: TaskDocument) => d.steps.every((s) => s.state === "done");
const docFailed = (d: TaskDocument) => d.steps.some((s) => s.state === "failed" || s.state === "blocked");

/** Failures not followed by a later success of the same task for the same project. */
export function unresolvedFailures(jobs: TaskJob[]): TaskJob[] {
  const latest = new Map<string, TaskJob>();
  for (const j of jobs) {
    if (j.status !== "done" && j.status !== "failed") continue;
    const key = `${j.project_id}:${j.kind}`;
    const seen = latest.get(key);
    if (!seen || (j.finished_at ?? "") > (seen.finished_at ?? "")) latest.set(key, j);
  }
  const active = new Set(jobs.filter((j) => j.status === "running" || j.status === "queued").map((j) => `${j.project_id}:${j.kind}`));
  return [...latest.values()].filter((j) => j.status === "failed" && !j.dismissed && !active.has(`${j.project_id}:${j.kind}`));
}

export default function TasksPage({ projects }: { projects: { id: string; name: string }[] }) {
  const [project, setProject] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const { data, error, loading, reload } = useAsync<TaskBoard>(() => api.tasks(project || undefined), [project]);
  const busy = !!data && data.summary.running + data.summary.queued > 0;
  useEffect(() => {
    const timer = window.setInterval(() => { if (!document.hidden) reload(); }, busy ? 4000 : 30000);
    return () => window.clearInterval(timer);
  }, [busy, reload]);

  const jobs = data?.jobs ?? [];
  const docs = data?.documents ?? [];
  const active = useMemo(() => jobs.filter((j) => j.status === "running" || j.status === "queued"), [jobs]);
  const activeDocs = useMemo(() => docs.filter((d) => !docDone(d) && !docFailed(d)), [docs]);
  const failures = useMemo(() => unresolvedFailures(jobs), [jobs]);
  const stuckDocs = useMemo(() => docs.filter(docFailed), [docs]);
  const finished = useMemo(() => jobs.filter((j) => (j.status === "done" || j.status === "failed")
    && (filter === "all" || (filter === "done" ? j.status === "done" && j.runtime?.state !== "partial" : j.status === filter))), [jobs, filter]);
  const doneDocs = docs.filter(docDone);
  const working = active.length + activeDocs.length;

  return (
    <Page className="page-workspace">
      <PageHeader variant="feed" title="任务" label="运行看板" description="只列出正在处理和需要你处理的事；完成的收在下方记录里。"
        meta={data ? `${data.runtime?.label ?? (data.worker === "running" ? "后台运行中" : "后台未运行")} · ${busy ? "每 4 秒" : "每 30 秒"}自动刷新` : ""} />
      {data?.runtime?.restart_required && <div className="note note-error" role="status">后台长时间没有进展。已保留结果；托管模式会尝试恢复，普通启动需重启应用。结果不明的请求不会自动重发。</div>}
      {data?.runtime?.state === "degraded" && <div className="note" role="status">后台调度暂时受阻，正在重试。</div>}
      {data && data.worker !== "running" && <div className="note note-error">后台任务未运行：排队中的任务不会开始。重启应用后会继续。</div>}
      {data && !data.model_ready && <div className="note">尚未连接 AI 模型：资料只会识别文字，不会进入 AI 吸收。<Link to="/settings">去设置 →</Link></div>}
      <ErrorNote error={error} />
      {loading && !data && <Skeleton lines={6} />}
      {data && <>
        {projects.length > 1 && <div className="filters">
          <select value={project} onChange={(e) => setProject(e.target.value)} aria-label="项目">
            <option value="">全部项目</option>
            {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </div>}

        <section className="card card-pad task-now">
          <div className="card-head"><h3>正在处理</h3>
            <span className="muted small">{working ? `${data.summary.running} 项运行中${data.summary.queued ? ` · ${data.summary.queued} 项排队` : ""}` : "后台一次执行一个任务"}</span>
          </div>
          {working === 0 ? <p className="task-idle">现在没有在处理的任务。{data.summary.done_24h ? `近 24 小时完成了 ${data.summary.done_24h} 项。` : ""}</p> : <>
            {active.length > 0 && <ul className="task-list">{active.map((j) => <li key={j.id} className={j.status}>
              <span className="task-state">{j.status === "running" ? <span className={j.runtime?.state === "stalled" ? "tag tag-warn" : "task-run"}>{j.runtime?.state !== "stalled" && <i />}{j.runtime?.label ?? "运行中"}</span> : <span className="muted">{j.runtime?.state && j.runtime.state !== "queued" ? j.runtime.label : `排队第 ${j.position} 位`}</span>}</span>
              <span className="task-what"><Link to={`/projects/${j.project_id}`} className="cell-title">{j.project_name}</Link> · {j.label}
                {j.status === "running" && j.stage && <span className="muted"> — {j.stage}</span>}</span>
              <span className="muted small num" title={j.runtime?.heartbeat_at ? `后台心跳：${j.runtime.heartbeat_at}` : undefined}>{j.runtime?.progress_at ? `进展 ${relTime(j.runtime.progress_at)}` : `加入于 ${relTime(j.created_at)}`}</span>
            </li>)}</ul>}
            {activeDocs.length > 0 && <ul className="doc-pipeline">{activeDocs.map((d) => <DocumentRow key={d.id} d={d} />)}</ul>}
          </>}
        </section>

        {(failures.length > 0 || stuckDocs.length > 0) && <section className="card card-pad task-attention">
          <div className="card-head"><h3>需要处理</h3><span className="muted small">失败后还没有成功重跑的任务；处理或忽略后从这里移走</span></div>
          {failures.length > 0 && <ul className="fix-list">{failures.map((j) => <FailureRow key={j.id} j={j} onChanged={reload} />)}</ul>}
          {stuckDocs.length > 0 && <ul className="doc-pipeline">{stuckDocs.map((d) => <DocumentRow key={d.id} d={d} />)}</ul>}
        </section>}

        <details className="task-history">
          <summary>处理记录 <span className="muted small">近 24 小时完成 {data.summary.done_24h} 项{data.summary.failed_24h ? ` · 失败 ${data.summary.failed_24h} 项` : ""}{doneDocs.length ? ` · 已吸收资料 ${doneDocs.length} 份` : ""}</span></summary>
          <div className="task-history-body">
            <div className="task-history-tools">
              <Seg id="task-filter" value={filter} onChange={setFilter} options={[{ value: "all", label: "全部" }, { value: "failed", label: "失败" }, { value: "done", label: "成功" }]} />
            </div>
            {finished.length ? <div className="table-wrap"><table className="table task-table">
              <thead><tr><th>完成时间</th><th>项目</th><th>任务</th><th>结果</th><th className="num">用时</th><th className="num">模型调用</th></tr></thead>
              <tbody>{finished.map((j) => <FinishedRow key={j.id} j={j} />)}</tbody>
            </table></div> : <p className="muted small">{filter === "failed" ? "没有失败的任务。" : "还没有完成的任务。"}</p>}
            {doneDocs.length > 0 && <>
              <h4>已完成的资料</h4>
              <ul className="doc-done">{doneDocs.map((d) => <li key={d.id}><span className="truncate" title={d.name}>{d.name}</span>
                <span className="muted small">{d.project_name} · 上传于 {relTime(d.created_at)}</span></li>)}</ul>
            </>}
          </div>
        </details>
      </>}
    </Page>
  );
}

function FinishedRow({ j }: { j: TaskJob }) {
  return <tr className={j.status === "failed" ? "task-failed" : ""}>
    <td className="muted small num">{relTime(j.finished_at)}</td>
    <td><Link to={`/projects/${j.project_id}`} className="cell-title">{j.project_name}</Link></td>
    <td>{j.label}{j.status === "failed" && j.error && <div className="task-error">{errorMessage(j.error)}</div>}</td>
    <td>{j.runtime?.state === "partial" ? <span className="tag tag-warn" title={j.runtime.label}>部分完成</span>
      : j.status === "done" ? <span className="tag tag-ok">成功</span> : <span className="tag tag-warn">{j.runtime?.label || "失败"}</span>}</td>
    <td className="num muted">{duration(j.seconds)}</td>
    <td className="num muted">{j.calls ? `${j.calls} 次` : "—"}{!!j.unknown && <div>另有 {j.unknown} 次服务请求待确认</div>}</td>
  </tr>;
}

function DocumentRow({ d }: { d: TaskDocument }) {
  const current = d.steps.find((s) => s.state !== "done") ?? d.steps[d.steps.length - 1];
  return <li className="doc-row">
    <div className="doc-name">
      <strong className="truncate" title={d.name}>{d.name}</strong>
      <span className="muted small"><Link to={`/projects/${d.project_id}?tab=documents`}>{d.project_name}</Link> · 上传于 {relTime(d.created_at)}</span>
    </div>
    <ol className="doc-steps" aria-label={`${d.name} 处理进度`}>
      {d.steps.map((s) => <li key={s.key} className={`step-${s.state}`} title={s.note || STEP_STATE[s.state]}>
        <i aria-hidden="true" /><span>{s.label}</span><small>{STEP_STATE[s.state]}</small></li>)}
    </ol>
    <p className={`doc-note step-${current.state}`}>{current.state === "done" ? "已全部完成" : `${current.label}：${current.note || STEP_STATE[current.state]}`}</p>
  </li>;
}

/** One failed task: what happened, what to do, and the button that does it. */
function FailureRow({ j, onChanged }: { j: TaskJob; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [recovering, setRecovering] = useState(false);
  const [done, setDone] = useState("");
  const advice = j.advice ?? { why: errorMessage(j.error), todo: "点“重新运行”再试一次，或忽略。", action: "rerun" as const };
  const act = async (fn: () => Promise<unknown>, message: string) => {
    setBusy(true); setError(null);
    try { await fn(); setDone(message); onChanged(); } catch (e) { setError(e); } finally { setBusy(false); }
  };
  return <li className="fix-row">
    <div className="fix-head">
      <span className="tag tag-warn">失败</span>
      <strong><Link to={`/projects/${j.project_id}`}>{j.project_name}</Link> · {j.label}</strong>
      <span className="muted small">{relTime(j.finished_at)}</span>
    </div>
    <dl className="fix-body">
      <div><dt>发生了什么</dt><dd>{advice.why}</dd></div>
      <div><dt>怎么处理</dt><dd>{advice.todo}</dd></div>
    </dl>
    {done ? <p className="fix-done small">{done}</p> : <div className="fix-actions">
      {advice.action === "recover" && <button className="btn btn-sm btn-primary" disabled={busy} onClick={() => setRecovering(true)}>查看并重发</button>}
      {advice.action === "settings" && <Link className="btn btn-sm" to="/settings">去设置测试连接</Link>}
      {(advice.action === "rerun" || advice.action === "settings") && <button className={`btn btn-sm${advice.action === "rerun" ? " btn-primary" : ""}`} disabled={busy}
        onClick={() => act(() => api.refresh(j.project_id, j.kind), "已重新加入队列，可在上方“正在处理”里看到进度。")}>重新运行</button>}
      <button className="btn btn-sm btn-ghost" disabled={busy} onClick={() => act(() => api.dismissTask(j.id), "已忽略，仍可在处理记录里查看。")}>忽略</button>
      {j.error && <details className="fix-raw"><summary>原始信息</summary><code>{errorMessage(j.error)}</code></details>}
    </div>}
    <ErrorNote error={error} />
    {recovering && <RecoveryDialog projectId={j.project_id} job={j as unknown as Job} onClose={() => setRecovering(false)}
      onQueued={() => { setDone("已确认重发，任务重新排队。"); onChanged(); }} />}
  </li>;
}

import { Skeleton } from "../frame";
import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { JobActivity, WorkIndicator, useVisiblePoll } from "../Operational";
import { isCoverageNote, jobReason, unresolvedJobs } from "../workState";
import { api, type Job, type TaskBoard, type TaskDocument, type TaskJob } from "../api";
import { RecoveryDialog } from "../RecoveryDialog";
import { Page } from "../motion";
import PageHeader from "../PageHeader";
import { ErrorNote, errorMessage, relTime, useAsync } from "../ui";
import { Seg } from "../widgets";

/** 任务看板: only what is being processed stays open; finished work folds into the history. */
const STEP_STATE: Record<string, string> = { done: "完成", running: "进行中", waiting: "等待", failed: "失败", blocked: "受阻", partial: "部分完成" };
type Filter = "all" | "failed" | "done" | "partial";

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
  return unresolvedJobs(jobs);
}

export default function TasksPage({ projects }: { projects: { id: string; name: string }[] }) {
  const [params] = useSearchParams();
  const [project, setProject] = useState(params.get("project") || "");
  const [filter, setFilter] = useState<Filter>("all");
  const { data, error, loading, reload } = useAsync<TaskBoard>(() => api.tasks(project || undefined), [project]);
  const busy = !!data && data.summary.running + data.summary.queued > 0;
  useVisiblePoll(reload, busy ? 4000 : 30000);

  const jobs = data?.jobs ?? [];
  const docs = data?.documents ?? [];
  const active = useMemo(() => jobs.filter((j) => j.status === "running" || j.status === "queued"), [jobs]);
  const activeDocs = useMemo(() => docs.filter((d) => !docDone(d) && !docFailed(d)), [docs]);
  const unresolved = useMemo(() => unresolvedFailures(jobs), [jobs]);
  const failures = unresolved.filter(j => !isCoverageNote(j));
  const coverage = unresolved.filter(isCoverageNote);
  const dismissed = useMemo(() => unresolvedJobs(jobs, true).filter(j => j.dismissed), [jobs]);
  const stuckDocs = useMemo(() => docs.filter(docFailed), [docs]);
  const finished = useMemo(() => jobs.filter((j) => (j.status === "done" || j.status === "failed")
    && (filter === "all" || (filter === "partial" ? j.runtime?.state === "partial" : j.status === filter
      && j.runtime?.state !== "partial" && (filter !== 'done' || j.runtime?.state !== 'needs_confirmation')))), [jobs, filter]);
  const doneDocs = docs.filter(docDone);
  const working = active.length + activeDocs.length;

  return (
    <Page className="page-workspace">
      <PageHeader variant="feed" title="任务" label="运行看板" description="查看研究进展和已有结果；只有运行异常需要进一步确认。"
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

        <section className="card card-pad task-now" data-stale={!!error}>
          <div className="card-head"><h3>正在处理</h3>
            <span className="muted small">{working ? `${data.summary.running} 项运行中${data.summary.queued ? ` · ${data.summary.queued} 项排队` : ""}` : "后台一次执行一个任务"}</span>
          </div>
          {working === 0 ? <p className="task-idle">现在没有在处理的任务。{data.summary.done_24h ? `近 24 小时完成了 ${data.summary.done_24h} 项。` : ""}</p> : <>
            {active.length > 0 && <ul className="task-list">{active.map((j) => <li key={j.id} className={j.status}>
              <span className="task-state"><WorkIndicator state={error ? "unavailable" : j.runtime?.state ?? j.status}
                label={error ? "状态暂时无法读取" : j.runtime?.label ?? (j.status === "queued" ? `排队第 ${j.position} 位` : "正在执行")} /></span>
              <span className="task-what"><Link to={`/projects/${j.project_id}`} className="cell-title">{j.project_name}</Link> · {j.label}
                {j.status === "running" && j.stage && <span className="muted"> — {j.stage}</span>}</span>
              <span className="muted small num" title={j.runtime?.heartbeat_at ? `后台心跳：${j.runtime.heartbeat_at}` : undefined}>{j.runtime?.progress_at ? `进展 ${relTime(j.runtime.progress_at)}` : `加入于 ${relTime(j.created_at)}`}</span>
              <JobActivity job={j} stale={!!error} />
            </li>)}</ul>}
            {activeDocs.length > 0 && <ul className="doc-pipeline">{activeDocs.map((d) => <DocumentRow key={d.id} d={d} />)}</ul>}
          </>}
        </section>

        {(failures.length > 0 || stuckDocs.length > 0) && <section className="card card-pad task-attention">
          <div className="card-head"><h3>运行异常 · 待确认</h3><span className="muted small">以下任务未完成；不会自动重跑，收起提醒不改变结果</span></div>
          {failures.length > 0 && <ul className="fix-list">{failures.map((j) => <FailureRow key={j.id} j={j} onChanged={reload} />)}</ul>}
          {stuckDocs.length > 0 && <ul className="doc-pipeline">{stuckDocs.map((d) => <DocumentRow key={d.id} d={d} />)}</ul>}
        </section>}

        {coverage.length > 0 && <section className="card card-pad task-coverage">
          <div className="card-head"><h3>已有结果 · 可选查看</h3><span className="muted small">{coverage.length} 项</span></div>
          <ul className="fix-list">{coverage.map(j => <CoverageRow key={j.id} j={j} />)}</ul>
        </section>}

        {dismissed.length > 0 && <details className="task-history">
          <summary>已收起的提醒 · {dismissed.length} 项仍未完成</summary>
          <div className="task-history-body"><p className="muted small">仅收起提醒，不代表更新成功。已有结果保留，可在此查看原因和恢复选项。</p>
            <ul className="fix-list">{dismissed.map(j => <FailureRow key={j.id} j={j} onChanged={reload} />)}</ul>
          </div>
        </details>}

        <details className="task-history">
          <summary>处理记录 <span className="muted small">近 24 小时完成 {data.summary.done_24h} 项{data.summary.partial_24h ? ` · 部分完成 ${data.summary.partial_24h} 项` : ""}{data.summary.failed_24h ? ` · 失败 ${data.summary.failed_24h} 项` : ""}{doneDocs.length ? ` · 已吸收资料 ${doneDocs.length} 份` : ""}</span></summary>
          <div className="task-history-body">
            <div className="task-history-tools">
              <Seg id="task-filter" value={filter} onChange={setFilter} options={[{ value: "all", label: "全部" }, { value: "failed", label: "失败" }, { value: "partial", label: "部分完成" }, { value: "done", label: "成功" }]} />
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

export function CoverageRow({ j }: { j: TaskJob }) {
  return <li className="coverage-row">
    <div><Link to={`/projects/${j.project_id}`} className="cell-title">{j.project_name}</Link><span className="muted"> · {j.label}</span>
      <details><summary>部分完成 · 查看详情</summary><p>{jobReason(j)}</p></details>
    </div>
    <span className="muted small">{relTime(j.finished_at)}</span>
    <Link className="btn btn-sm" to={`/projects/${j.project_id}?tab=external`}>查看已有结果</Link>
  </li>;
}

export function FinishedRow({ j }: { j: TaskJob }) {
  return <tr className={j.status === "failed" ? "task-failed" : ""}>
    <td className="muted small num">{relTime(j.finished_at)}</td>
    <td><Link to={`/projects/${j.project_id}`} className="cell-title">{j.project_name}</Link></td>
    <td>{j.label}{j.status === "failed" && j.error && <div className="task-error">{errorMessage(j.error)}</div>}</td>
    <td>{j.runtime?.state === "partial" ? <span className="tag tag-warn" title={j.runtime.label}>部分完成</span>
      : j.runtime?.state === 'needs_confirmation' ? <span className="tag tag-warn" title={j.runtime.label}>仍有请求待确认</span>
      : j.status === "done" ? <span className="tag tag-ok">成功</span> : <span className="tag tag-warn">{j.runtime?.label || "失败"}</span>}</td>
    <td className="num muted">{duration(j.seconds)}</td>
    <td className="num muted">{j.calls ? `${j.calls} 次` : "—"}{!!j.unknown && <div>另有 {j.unknown} 次服务请求待确认</div>}
      {!!j.runtime?.activity?.model_requests && <details className="task-usage"><summary>查看 Token 用量</summary><JobActivity job={j} /></details>}
    </td>
  </tr>;
}

export function DocumentRow({ d }: { d: TaskDocument }) {
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
export function FailureRow({ j, onChanged }: { j: TaskJob; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [recovering, setRecovering] = useState(false);
  const [done, setDone] = useState("");
  const partial = j.runtime?.state === "partial";
  const fallback = { why: jobReason(j), todo: partial
    ? "已有结果可继续查看。先查看来源状态，再决定是否补全；不会自动重跑。"
    : "请查看任务记录和服务状态，再决定是否重试。", action: "none" as const };
  const advice = { ...fallback, ...j.advice, why: j.advice?.why?.trim() || fallback.why,
    todo: j.advice?.todo?.trim() || fallback.todo };
  const act = async (fn: () => Promise<unknown>, message: string) => {
    setBusy(true); setError(null);
    try { await fn(); setDone(message); onChanged(); } catch (e) { setError(e); } finally { setBusy(false); }
  };
  return <li className="fix-row">
    <div className="fix-head">
      <span className="tag tag-warn">{partial ? "部分完成" : j.runtime?.state === "needs_confirmation" ? "结果待确认" : "未完成"}</span>
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
      {partial && <Link className="btn btn-sm" to={`/projects/${j.project_id}?tab=external`}>查看来源与结果</Link>}
      {!j.dismissed && <button className="btn btn-sm btn-ghost" disabled={busy} onClick={() => act(() => api.dismissTask(j.id), "提醒已收起，任务结果未改变。")}>收起提醒</button>}
      {j.error && <details className="fix-raw"><summary>原始信息</summary><code>{errorMessage(j.error)}</code></details>}
    </div>}
    <ErrorNote error={error} />
    {recovering && <RecoveryDialog projectId={j.project_id} job={j as unknown as Job} onClose={() => setRecovering(false)}
      onQueued={() => { setDone("已确认重发，任务重新排队。"); onChanged(); }} />}
  </li>;
}

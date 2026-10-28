import { useEffect, useRef, useState } from "react";
import type { Job, ProjectDetail } from "./api";
import { isMoving, needsAttention, projectWork } from "./workState";
import { relTime } from "./ui";

export function WorkIndicator({ state, label }: { state: string; label: string }) {
  return <span className={`op-indicator op-${state}${isMoving(state) ? " op-moving" : ""}${needsAttention(state) ? " op-attention" : ""}`}>
    <i aria-hidden="true" /><span>{label}</span>
  </span>;
}

export function ProjectActivity({ detail }: { detail: ProjectDetail }) {
  const work = projectWork(detail);
  return <div className={`op-ribbon${needsAttention(work.state) ? " op-ribbon-alert" : ""}`}>
    <div role="status" aria-live="polite" aria-atomic="true"><WorkIndicator state={work.state} label={work.label} /></div>
    {work.stage && <details className="op-stage"><summary>当前任务</summary>{work.stage}</details>}
    {work.alongside && <span className="op-alongside"><WorkIndicator state={work.alongside.state} label={`另有 ${work.alongside.label}`} /></span>}
    {work.additional > 0 && <span className="muted small">另有 {work.additional} 项{work.parsingAlongside ? " · 资料正在识别" : ""}</span>}
    <a className="op-detail" href={`/tasks?project=${detail.project.id}`}>查看运行状态 →</a>
    {work.progressAt && <span className="op-time muted small">最近进展 {relTime(work.progressAt)}</span>}
    <TaskActivity detail={detail} />
  </div>;
}

const VALIDATION_LABELS: Record<string, string> = {
  unsupported_numbers: "数字或日期需要与引用核对", missing_qualifications: "需要保留原文限定条件",
  invalid_field: "记录字段不完整", missing_citation: "缺少引用", quote_not_found: "引用未匹配原文",
  source_not_found: "来源需要核对", invalid_date: "日期需要核对", record_validation: "记录未通过证据校验",
  quote_not_verbatim: "引用未逐字匹配原文",
};

export function TaskActivity({ detail }: { detail: ProjectDetail }) {
  const job = detail.jobs.find(j => j.status === 'running') ?? detail.jobs.find(j => (j.status === 'done' || j.status === 'failed') && j.runtime?.activity?.model_requests);
  return job ? <JobActivity job={job} stale={detail.status_stale} /> : null;
}

export function JobActivity({ job, stale = false }: { job: Job; stale?: boolean }) {
  const value = job?.runtime?.activity;
  if (!value || (!value.model_requests && !value.search_completed && !value.pending_kind && value.records_saved == null && !value.validation_attempts)) return null;
  const terminal = job?.status === 'done' || job?.status === 'failed';
  const count = (n: number | null) => n == null ? '未知' : n.toLocaleString();
  return <div className="task-activity">
    <details className="task-run-details"><summary>查看运行详情</summary>
    <span className="task-results">
      {value.records_saved != null && <>已保存 <strong>{value.records_saved}</strong> 条研究记录 · </>}
      已完成 <strong>{value.search_completed}</strong> 次搜索
      {stale && <span>（上次记录，状态未刷新）</span>}
    </span>
    {!terminal && value.pending_seconds != null && <span>
      {value.pending_kind === 'model' ? `第 ${value.model_requests} 次模型请求` : '来源请求'} · 本次已用时 {value.pending_seconds} 秒
      {stale ? '（状态未刷新）' : ''}
    </span>}
      <p>已完成模型调用 {value.model_completed} 次</p>
      {!!value.validation_attempts && <>
        <p>内容校验退回 {value.validation_attempts}{value.validation_attempts >= 100 ? '+' : ''} 次</p>
        <p>最近原因：{value.validation_reasons.map(code => VALIDATION_LABELS[code] ?? '记录需要核对').join('；')}。</p>
      </>}
    {terminal && value.model_requests > 0 && <span>本任务已记录 Token：输入 {count(value.input_tokens)} · 输出 {count(value.output_tokens)}
      {value.usage_missing_calls > 0 ? ` · ${value.usage_missing_calls} 次用量不全，总量未知` : ' · 服务商已返回完整输入/输出用量'}
    </span>}
    {terminal && (value.cache_read_tokens != null || value.cache_creation_tokens != null) && <span>
      另报缓存：读取 {count(value.cache_read_tokens)} · 创建 {count(value.cache_creation_tokens)}（不重复累加）
    </span>}
    </details>
  </div>;
}

/** Read-only polling. One in-flight poll, no hidden-tab polling or catch-up burst. */
export function useVisiblePoll(load: () => Promise<unknown>, interval: number, enabled = true) {
  const latest = useRef(load);
  latest.current = load;
  useEffect(() => {
    if (!enabled) return;
    let stopped = false, pending = false;
    let timer: ReturnType<typeof setTimeout>;
    const schedule = () => { if (!stopped) timer = setTimeout(tick, interval); };
    const tick = async () => {
      clearTimeout(timer);
      if (stopped || pending) return;
      if (document.hidden) { schedule(); return; }
      pending = true;
      try { await latest.current(); } catch { /* caller renders the read error */ }
      finally { pending = false; schedule(); }
    };
    const visible = () => { if (!document.hidden) void tick(); };
    schedule();
    document.addEventListener("visibilitychange", visible);
    return () => { stopped = true; clearTimeout(timer); document.removeEventListener("visibilitychange", visible); };
  }, [interval, enabled]);
}

/** Keep readers on the current snapshot until they choose to reveal new results. */
export function useStagedRead<T>(key: string, read: () => Promise<T>, changed: (next: T) => boolean, enabled: boolean) {
  const [pending, setPending] = useState<{ key: string; value: T } | null>(null);
  const [failed, setFailed] = useState(false);
  const current = useRef(key);
  current.current = key;
  useVisiblePoll(async () => {
    const requested = key;
    try {
      const value = await read();
      if (current.current !== requested) return;
      setFailed(false);
      setPending(changed(value) ? { key, value } : null);
    } catch { if (current.current === requested) setFailed(true); }
  }, 30000, enabled);
  useEffect(() => { setPending(null); setFailed(false); }, [key]);
  return { pending: pending?.key === key ? pending.value : null, failed, clear: () => setPending(null) };
}

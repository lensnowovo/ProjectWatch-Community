import { useEffect, useRef, useState } from "react";
import type { ProjectDetail } from "./api";
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
    {work.stage && <span className="op-stage">{work.stage}</span>}
    {work.alongside && <span className="op-alongside"><WorkIndicator state={work.alongside.state} label={`另有 ${work.alongside.label}`} /></span>}
    {work.additional > 0 && <span className="muted small">另有 {work.additional} 项{work.parsingAlongside ? " · 资料正在识别" : ""}</span>}
    <a className="op-detail" href={`/tasks?project=${detail.project.id}`}>查看运行状态 →</a>
    {work.progressAt && <span className="op-time muted small">最近进展 {relTime(work.progressAt)}</span>}
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

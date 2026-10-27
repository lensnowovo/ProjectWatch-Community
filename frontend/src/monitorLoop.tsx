import { useEffect, useRef, useState } from "react";
import { motion, useReducedMotion } from "motion/react";
import { api, type MonitoringPlanView, type ProjectDetail, type Signals } from "./api";
import { Block } from "./frame";
import { errorMessage, relTime, useAsync } from "./ui";
import { STOP_LABEL, projectWork } from "./workState";
import { WorkIndicator } from "./Operational";
import { RoundOutcome } from "./RoundOutcome";

/**
 * 监控循环: the four stages this project actually runs — collect, read, judge, revise the plan — with the real
 * cumulative results. The halo breathes on the stage the server reports while it executes; the light crosses a
 * link only when that stage really moves on, and glides slowly back to 采集 only on a real revise → collect. Where work
 * stopped the node is marked with its reason. A rotating ring indicates in-flight work, not stage completion.
 */

export type Stage = "collect" | "read" | "judge" | "revise";
const ORDER: Stage[] = ["collect", "read", "judge", "revise"];
const STAGE: Record<Stage, { no: string; label: string; what: string }> = {
  collect: { no: "01", label: "采集", what: "按监控方案检索公开信息和官网变化" },
  read: { no: "02", label: "解析", what: "提取正文与内部资料中的事件和出处" },
  judge: { no: "03", label: "判断", what: "逐个对照关注点，确认是否有新进展" },
  revise: { no: "04", label: "修订方案", what: "把新出现的对象和问题加入监控方案" },
};

/** Seconds for the light to cross one link, and to glide the whole return path back to 采集. */
const LINK = 1.6, RETURN = 5.5;

export interface LoopCounts { collect: string; read: string; judge: string; revise: string }

export interface LoopSnapshot { project: string; job?: string; phase: Stage | null; state: string; round: number }
/** No inferred intermediate stages, cross-task transitions or unconfirmed new rounds. */
export function loopTransition(previous: LoopSnapshot, next: LoopSnapshot, previousJobDone = false) {
  if (previous.project !== next.project || previous.state !== "running" || next.state !== "running"
      || !previous.job || !next.job || !previous.phase || !next.phase) return null;
  const from = ORDER.indexOf(previous.phase), to = ORDER.indexOf(next.phase);
  if (previous.job === next.job && previous.round === next.round && to === from + 1) return { from, to };
  if (from === 3 && to === 0 && next.round === previous.round + 1 && previousJobDone) return { from, to };
  return null;
}

export function loopOutcome(detail: ProjectDetail, plan?: MonitoringPlanView | null) {
  const progress = detail.monitoring_progress;
  const at = progress?.completed_at;
  const valid = !!at && Number.isFinite(Date.parse(at)) && Date.parse(at) <= Date.now();
  // A timestamp alone (including a first-pass timestamp) does not certify completion.
  const completedAt = !detail.status_stale && progress?.state === "observed" && valid ? at : null;
  const revision = plan?.state?.last;
  const parts: string[] = [];
  if (revision && Number.isInteger(revision.added) && revision.added > 0) parts.push(`新增 ${revision.added} 个关注点`);
  if (revision && Number.isInteger(revision.noted) && revision.noted > 0) parts.push(`更新 ${revision.noted} 条认识`);
  return { completedAt, revision: parts.length ? `最近一次方案修订：${parts.join("，")}` : null };
}

export function MonitorLoop({ detail, plan, inline = false }: { detail: ProjectDetail; plan?: MonitoringPlanView | null; inline?: boolean }) {
  const initialReduced = useReducedMotion();
  const [motionPreference, setMotionPreference] = useState<boolean | null>(null);
  useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const sync = () => setMotionPreference(media.matches);
    sync(); media.addEventListener("change", sync);
    return () => media.removeEventListener("change", sync);
  }, []);
  const reduced = motionPreference ?? initialReduced;
  const signals = useAsync<Signals | null>(() => api.signals(detail.project.id).catch(() => null), [detail.project.id, detail.project.last_external_at]);
  const work = projectWork(detail);
  const activeStage = work.moving ? work.phase : null;
  const stopStage = work.stop?.phase ?? null;
  const paused = detail.project.status === "paused";

  const channels = Object.values(plan?.coverage ?? {}).flat();
  const live = (plan?.items ?? []).filter((i) => i.status !== "retired");
  const collected = (["company", "competitor", "industry", "policy", "paper", "trial", "patent"] as const)
    .reduce((n, c) => n + (detail.counts[c] ?? 0), 0);
  const facts = (detail.observations?.items.length ?? 0) + (detail.event_states?.items.length ?? 0);
  const counts: LoopCounts = {
    collect: channels.length ? `已启用 ${channels.filter((c) => c.state === "on" || c.state === "error").length} 个来源` : "来源待确认",
    read: collected ? `已收录 ${collected.toLocaleString()} 条` : "尚无收录",
    judge: plan?.week ? `近 7 日 ${plan.week.fresh} 个关注点有收录` : live.length ? `跟踪 ${live.length} 个关注点` : "尚无关注点",
    revise: plan?.state ? `已修订 ${plan.state.revisions} 轮` : "首版方案待生成",
  };
  const compactCounts: LoopCounts = { ...counts,
    judge: plan?.week ? `近 7 日 ${plan.week.fresh} 项有收录` : live.length ? `${live.length} 个关注点` : "尚无关注点",
  };
  const notes: Record<Stage, string> = {
    collect: !channels.length ? "请在监控方案中确认来源" : detail.documents.length ? `另有 ${detail.documents.length} 份内部资料` : "官网与公开渠道",
    read: facts ? `${facts} 条带出处的事实与事件` : "尚未提取事实",
    judge: plan?.health ? `近 30 天 ${plan.health.hit_30d}/${plan.health.items} 个关注点有命中` : "等待首轮判断",
    revise: plan?.health?.system_added ? `累计自动新增 ${plan.health.system_added} 个关注点` : "尚无自动新增的关注点",
  };

  const cadence = signals.data?.cadence;
  const outcome = loopOutcome(detail, plan);
  const round = detail.monitoring_progress?.round ?? 0;
  const snapshot: LoopSnapshot = { project: detail.project.id, job: work.job?.id, phase: activeStage, state: work.state, round };
  // Light moves only between two stages the server actually reported, never on a timer.
  const previous = useRef(snapshot);
  const [transit, setTransit] = useState<{ from: number; to: number; key: number } | null>(null);
  useEffect(() => {
    const done = detail.jobs?.some(j => j.id === previous.current.job && j.status === "done" && !!j.finished_at);
    const next = loopTransition(previous.current, snapshot, done);
    previous.current = snapshot;
    setTransit(next && !reduced ? { ...next, key: Date.now() } : null);
    if (next && !reduced) {
      const timer = setTimeout(() => setTransit(null), (next.to === 0 ? RETURN : LINK) * 1000);
      return () => clearTimeout(timer);
    }
  }, [detail.project.id, work.job?.id, activeStage, work.state, round, reduced]);
  const back = transit?.from === 3 && transit?.to === 0;
  // A real progress report earns one ripple; the value present on first render does not.
  const firstProgress = useRef(work.progressAt);
  const completionKey = outcome.completedAt ? `${detail.project.id}|${outcome.completedAt}` : null;
  const previousCompletion = useRef({ project: detail.project.id, key: completionKey });
  const [freshCompletion, setFreshCompletion] = useState<string | null>(null);
  useEffect(() => {
    const fresh = previousCompletion.current.project === detail.project.id && completionKey
      && completionKey !== previousCompletion.current.key;
    if (completionKey || previousCompletion.current.project !== detail.project.id)
      previousCompletion.current = { project: detail.project.id, key: completionKey };
    setFreshCompletion(fresh ? completionKey : null);
    if (fresh) {
      const timer = setTimeout(() => setFreshCompletion(null), 8000);
      return () => clearTimeout(timer);
    }
  }, [detail.project.id, completionKey]);
  // The closed track: from 04 along the top edge, round the frame and back into 01. Measured, since it follows the layout.
  const loopRef = useRef<HTMLSpanElement>(null);
  const [loopPath, setLoopPath] = useState<string | null>(null);
  useEffect(() => {
    const el = loopRef.current, track = el?.parentElement;
    if (!el || !track || typeof ResizeObserver === "undefined") return;
    const measure = () => {
      const nodes = track.querySelectorAll(".ml-node");
      if (getComputedStyle(el).display === "none" || nodes.length < 4) { setLoopPath(null); return; }
      const box = el.getBoundingClientRect(), ox = box.left + el.clientLeft;
      const cx = (n: Element) => { const r = n.getBoundingClientRect(); return r.left + r.width / 2 - ox; };
      // Coordinates are in the padding box; the 1px border's centre line sits half a pixel outside it.
      const W = el.clientWidth, H = el.clientHeight, L = -.5, T = -.5, R = W + .5, B = H + .5;
      const r = Math.max(0, parseFloat(getComputedStyle(el).borderTopRightRadius) - .5);
      const f = (v: number) => v.toFixed(1);
      setLoopPath(`M${f(cx(nodes[3]))} ${T} H${f(R - r)} A${r} ${r} 0 0 1 ${f(R)} ${f(T + r)} V${f(B - r)} A${r} ${r} 0 0 1 ${f(R - r)} ${f(B)}`
        + ` H${f(L + r)} A${r} ${r} 0 0 1 ${f(L)} ${f(B - r)} V${f(T + r)} A${r} ${r} 0 0 1 ${f(L + r)} ${f(T)} H${f(cx(nodes[0]))}`);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(track);
    return () => observer.disconnect();
  }, []);
  const ripple = work.state === "running" && work.progressAt && work.progressAt !== firstProgress.current ? work.progressAt : null;

  // The cadence is the minimum gap between external checks, not a promise that a round finishes on schedule.
  const loopMarker = [round ? `第 ${round} 轮` : "新对象和问题进入下一轮", cadence ? `检查间隔约 ${cadence.hours} 小时` : ""].filter(Boolean).join(" · ");
  const statusLine = detail.status_stale ? "状态暂时无法读取 · 显示上次结果"
    : paused && work.moving ? "自动监控已暂停，在途任务尚未结束"
    : work.stop ? null
    : outcome.completedAt ? `${freshCompletion === completionKey ? "本轮处理已收口" : "最近一轮处理已收口"} · ${Date.now() - Date.parse(outcome.completedAt) < 60000 ? "刚刚" : relTime(outcome.completedAt)}`
    : work.moving ? null : work.state === "idle" ? "首轮监控尚未完成" : work.label;
  const alongside = !!work.stop && work.stop.job !== work.job;

  const body = <div className={`ml${inline ? " ml-inline" : ""}${activeStage ? " ml-live" : ""}`}>
    {work.moving && !activeStage && !inline && <p className="ml-activity" role="status"><i className="ml-busy" aria-hidden="true" />{work.label}</p>}
    <div className="ml-track" role="list" aria-label="监控处理阶段">
      {ORDER.map((key, i) => {
        const active = activeStage === key;
        const stopped = !!work.stop && stopStage === key;
        return <div key={key} className={`ml-stage${active ? ` on ${work.state === "running" ? "run" : "wait"}` : ""}${stopped ? alongside ? " has-other-stop" : ` stop stop-${work.stop!.state}` : ""}`}
          role="listitem" aria-current={active ? "step" : undefined}>
          <div className="ml-node">
            <span className="ml-no">{STAGE[key].no}</span>
            {active && <i className="ml-spinner" aria-hidden="true" />}
            {active && !reduced && (work.state === "running"
              ? <motion.i key={transit?.key ?? "rest"} className="ml-halo" aria-hidden="true" initial={{ opacity: 0, scale: 1 }}
                  animate={{ opacity: [.45, .9, .45], scale: [1, 1.08, 1] }} transition={{ duration: 3, repeat: Infinity, ease: "easeInOut" }} />
              : <i className="ml-halo ml-halo-wait" aria-hidden="true" />)}
            {active && ripple && !reduced && <motion.i key={ripple} className="ml-ripple" aria-hidden="true" initial={{ opacity: .7, scale: 1 }}
              animate={{ opacity: 0, scale: 1.7 }} transition={{ duration: 1.6, ease: "easeOut" }} />}
          </div>
          <div className="ml-copy">
            {/* The stage name stays centred; state tags hang beside it instead of pushing it aside. */}
            <h4><span className="ml-label">{STAGE[key].label}
              {(active || stopped) && <span className="ml-tags">
                {active && <em>{work.state === "waiting_response" ? "请求处理中" : work.state === "stopping" ? "正在结束" : "进行中"}</em>}
                {stopped && !alongside && <em className="ml-stop-tag">{STOP_LABEL[work.stop!.state]}</em>}</span>}</span></h4>
            <b className="ml-count-full">{counts[key]}</b><b className="ml-count-compact">{compactCounts[key]}</b>
            {stopped && alongside && <span className="ml-other-stop" title={`${work.stop!.label}：${errorMessage(work.stop!.error)}`}>另有任务未完成</span>}
          </div>
          {i < ORDER.length - 1 && <span className="ml-link" aria-hidden="true">
            {transit && !back && transit.from === i && work.state === "running" && !reduced && <motion.i key={`${transit.key}-${i}`} initial={{ left: "0%", opacity: 0 }}
              animate={{ left: ["0%", "calc(100% - 9px)"], opacity: [0, 1, 1, 0] }}
              transition={{ left: { duration: LINK, ease: "easeInOut" }, opacity: { duration: LINK, times: [0, .15, .85, 1] } }} />}
          </span>}
        </div>;
      })}
      <span className="ml-return" aria-hidden="true" ref={loopRef}>
        <span className="ml-return-label">{loopMarker}</span>
        {loopPath && back && work.state === "running" && !reduced && <i key={transit!.key} className="ml-orbit"
          style={{ offsetPath: `path("${loopPath}")`, animationDuration: `${RETURN}s` }} />}
      </span>
    </div>
    <details className="ml-explanations"><summary>步骤说明与明细</summary>{ORDER.map(key => <p key={key}><b>{STAGE[key].label}</b> · {STAGE[key].what}<br /><span className="muted">{notes[key]}</span></p>)}</details>
    {statusLine && <p className={`ml-status${outcome.completedAt && !detail.status_stale ? " ml-completed" : ""}${freshCompletion && freshCompletion === completionKey ? " ml-completed-fresh" : ""}`}>
      <span role="status" aria-live="polite">{statusLine}</span>
      {outcome.completedAt && !work.stop && <a href={`/projects/${encodeURIComponent(detail.project.id)}?tab=external`}>查看结果 →</a>}
    </p>}
    {outcome.revision && !detail.status_stale && !plan?.last_round && <p className="ml-revision">{outcome.revision}</p>}
    <RoundOutcome round={plan?.last_round} stale={detail.status_stale} />
    {work.stop && <p className="ml-stopline" role="note">
      <WorkIndicator state={work.stop.state} label={`${work.stop.job === work.job ? "" : "另有 "}${work.stop.label}`} />
      <span>{work.stop.job.dismissed && "提醒已收起，任务仍未完成 · "}{stopStage && work.stop.state !== "partial" ? `停在「${STAGE[stopStage].label}」 · ` : ""}{errorMessage(work.stop.error)}</span>
      <a href={`/tasks?project=${encodeURIComponent(detail.project.id)}`}>查看任务记录 →</a>
    </p>}
  </div>;

  if (inline) return body;
  return <Block card title="监控循环" note="当前步骤与累计结果">{body}</Block>;
}

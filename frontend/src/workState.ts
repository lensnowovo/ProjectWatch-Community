import type { Job, Project, ProjectDetail } from "./api";

const PAUSED_KINDS = new Set(["external", "external_followup", "public_profile", "landscape", "business", "competition", "details", "commercial"]);

export function monitoringRevision(detail: ProjectDetail) {
  return `${detail.project.last_external_at ?? ""}|${detail.monitoring_progress?.first_result_at ?? ""}|${detail.brief?.updated_at ?? ""}`;
}

export function insightRevision(detail: ProjectDetail) {
  return `${monitoringRevision(detail)}|${(detail.jobs ?? []).filter(j => j.kind === "insight")
    .map(j => `${j.id}:${j.status}:${j.finished_at ?? ""}`).join("|")}`;
}

export function workRevision(detail: ProjectDetail) {
  return (detail.jobs ?? []).map(j => `${j.id}:${j.status}:${j.runtime?.phase ?? ""}:${j.finished_at ?? ""}`).join("|");
}

export function jobWorkState(detail: ProjectDetail, job: Job) {
  if (detail.status_stale) return "unavailable";
  if (job.runtime?.state) return job.runtime.state;
  if (job.status !== "queued" && job.status !== "running") return job.status;
  if (job.status === "queued" && detail.project.status === "paused" && PAUSED_KINDS.has(job.kind)) return "paused";
  if (detail.execution?.analysis === "stopped") return "offline";
  if (detail.execution?.analysis === "stopping") return job.status === "running" ? "stopping" : "waiting_restart";
  return job.status;
}

export const WORK_LABEL: Record<string, string> = {
  queued: "排队中", running: "正在执行", stopping: "正在结束", waiting_restart: "等待恢复",
  offline: "等待后台启动", paused: "已暂停",
  waiting_response: "等待响应", recovering: "等待恢复", blocked: "等待后台恢复",
  stalled: "长时间无进展", needs_confirmation: "请求结果待确认", partial: "部分完成",
  failed: "未完成", done: "已完成", idle: "监控待命", agent: "等待 Agent 更新",
  unavailable: "状态暂时无法读取",
};

export const JOB_LABEL: Record<string, string> = {
  internal: "分析内部资料", external: "收集公开信息", external_followup: "补全外部信息",
  brief: "生成简报", public_profile: "补充公开档案", landscape: "整理竞争与行业",
  business: "整理产品与用途", competition: "更新竞对资料", details: "整理商业关系与政策条件",
  commercial: "更新专业来源", insight: "深度分析",
};
export const isMoving = (state: string) => state === "running" || state === "waiting_response" || state === "stopping";
export const needsAttention = (state: string) => ["failed", "needs_confirmation", "stalled", "partial", "blocked", "offline", "recovering", "unavailable"].includes(state);

export function projectListWork(p: Pick<Project, "job_runtime" | "job_status" | "worker_state" | "worker_health" | "status" | "execution_mode">) {
  if (p.job_runtime) return p.job_runtime;
  if (p.execution_mode === "agent") return {state:"agent",label:"Agent 按需更新"};
  if (p.worker_health === "stalled" || p.worker_health === "degraded") return {state:"blocked",label:"后台受阻，等待恢复"};
  if (p.status === "paused") return {state:"paused",label:"监控已暂停"};
  if (p.worker_state === "stopped") return {state:"offline",label:"自动更新未运行"};
  if (p.job_status === "queued") return {state:"queued",label:"排队中"};
  if (p.job_status === "running" && p.worker_state === "running") return {state:"running",label:"正在更新"};
  return {state:"idle",label:"监控已启用"};
}

export function unresolvedJobs<T extends Job & { project_id?: string }>(jobs: T[], includeDismissed = false): T[] {
  const latest = new Map<string, T>();
  for (const job of jobs) {
    const key = `${job.project_id ?? ""}:${job.kind}`;
    const prior = latest.get(key);
    if (!prior || (job.finished_at || job.created_at) > (prior.finished_at || prior.created_at)) latest.set(key, job);
  }
  const candidates = new Map([...latest.values(), ...jobs.filter(j => j.runtime?.state === 'needs_confirmation')].map(j => [j.id, j]));
  return [...candidates.values()].filter(j => (includeDismissed || !j.dismissed)
    && (j.status === "failed" || j.runtime?.state === "partial" || j.runtime?.state === 'needs_confirmation'));
}

/** A quiet reminder is not a successful outcome. Never infer a retry from partial coverage. */
export function jobReason(job: Job) {
  const state = job.runtime?.state ?? job.status;
  if (state === "partial") return job.runtime?.label?.trim() || "部分内容已完成，其余暂未取得结果。";
  if (state === "needs_confirmation") return job.runtime?.label?.includes('模型服务出错')
    ? '模型服务返回错误，未取得可用回答，计费仍待核；不会自动重发。'
    : "上次请求结果不明，需确认后才能继续；不会自动重发。";
  return job.error?.trim() || "任务未完成，已有结果保留。请查看任务记录后决定如何处理。";
}

/** States in which work has stopped at a known place and needs the user's eye. */
export const STOP_LABEL: Record<string, string> = {
  failed: "停在这里", needs_confirmation: "结果待确认", stalled: "无进展", partial: "部分完成",
};

const jobLabel = (detail: ProjectDetail, job: Job) => {
  const state = jobWorkState(detail, job);
  return `${JOB_LABEL[job.kind] ?? job.kind} · ${job.runtime?.label || WORK_LABEL[state] || state}`;
};

export function projectWork(detail: ProjectDetail) {
  const jobs = detail.jobs ?? [];
  const active = jobs.filter(j => j.status === "running" || j.status === "queued");
  const failures = unresolvedJobs(jobs, true);
  // Executing work is the focus; a held request or unresolved failure stays visible beside it, never hidden.
  const blocked = active.find(j => needsAttention(jobWorkState(detail, j)));
  const executing = active.find(j => isMoving(jobWorkState(detail, j)));
  const held = blocked ?? failures.find(j => !j.dismissed) ?? failures[0];
  const job = executing ?? blocked ?? active[0] ?? held;
  const parsing = detail.documents?.find(d => d.status === "processing");
  const queuedDoc = detail.documents?.some(d => d.status === "queued");
  let state = job ? jobWorkState(detail, job) : "idle";
  let label = job ? jobLabel(detail, job) : "监控待命";
  let phase = job?.runtime?.phase ?? null;
  let stage = isMoving(state) ? job?.stage || "" : "";
  let docFocus = false;
  if (!job || (!isMoving(state) && !needsAttention(state))) {
    if (parsing || queuedDoc) {
      docFocus = !!parsing;
      state = detail.execution?.parsing === "stopped" ? "offline" : parsing ? "running" : "queued";
      phase = "read"; stage = parsing?.name ?? "";
      label = state === "running" ? "正在识别资料" : state === "offline" ? "资料解析等待后台启动" : "资料等待识别";
    } else if (!job && detail.project.execution_mode === "agent") { state = "agent"; label = "Agent 按需更新"; }
    else if (!job && detail.project.status === "paused") { state = "paused"; label = "自动监控已暂停"; }
    else if (!job && detail.execution?.analysis === "stopped") { state = "offline"; label = "自动更新未运行"; }
    else if (!job && !detail.project.activated) { label = "尚未启用监控"; }
  }
  if (detail.execution?.health?.restart_required) { state = "blocked"; label = "后台受阻，等待恢复"; }
  if (state === "idle" && detail.monitoring_progress?.state === "partial") {
    state = "partial";
    label = detail.monitoring_progress.pending_items ? "本轮仍有信息待整理" : "本轮已整理，部分来源未覆盖";
  } else if (state === "idle" && detail.monitoring_progress?.state === "failed") {
    state = "failed"; label = "本轮监控未完成，已有结果保留";
  }
  if (detail.status_stale) { state = "unavailable"; label = "状态暂时无法读取 · 显示上次结果"; stage = ""; }
  // Where and why work stopped. Phase is only what the server reported; an unknown place stays unknown.
  const heldState = held ? jobWorkState(detail, held) : "";
  const stop = held && heldState in STOP_LABEL
    ? { job: held, state: heldState, phase: held.runtime?.phase ?? null, error: jobReason(held), label: jobLabel(detail, held) } : null;
  if (job?.dismissed && held === job && !isMoving(state) && !detail.status_stale) label += " · 提醒已收起，任务仍未完成";
  const shown = new Set([job, stop?.job].filter(Boolean));
  return { state, label, phase, stage, job, failures, moving: isMoving(state), stop,
    // The held item is shown in its own lane only when something else holds the focus.
    alongside: stop && stop.job !== job ? stop : null,
    parsingAlongside: !!job && !!parsing && detail.execution?.parsing !== "stopped",
    additional: active.filter(j => !shown.has(j)).length + (parsing && !docFocus ? 1 : 0),
    progressAt: job?.runtime?.progress_at };
}

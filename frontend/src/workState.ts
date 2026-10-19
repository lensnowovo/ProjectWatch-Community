import type { Job, ProjectDetail } from "./api";

const PAUSED_KINDS = new Set(["external", "external_followup", "public_profile", "landscape", "business", "competition", "details", "commercial"]);

export function monitoringRevision(detail: ProjectDetail) {
  return `${detail.project.last_external_at ?? ""}|${detail.monitoring_progress?.first_result_at ?? ""}|${detail.brief?.updated_at ?? ""}`;
}

export function jobWorkState(detail: ProjectDetail, job: Job) {
  if (job.status !== "queued" && job.status !== "running") return job.status;
  if (job.status === "queued" && detail.project.status === "paused" && PAUSED_KINDS.has(job.kind)) return "paused";
  if (detail.execution?.analysis === "stopped") return "offline";
  if (detail.execution?.analysis === "stopping") return job.status === "running" ? "stopping" : "waiting_restart";
  return job.status;
}

export const WORK_LABEL: Record<string, string> = {
  queued: "排队中", running: "正在执行", stopping: "正在结束", waiting_restart: "等待恢复",
  offline: "等待后台启动", paused: "已暂停",
};

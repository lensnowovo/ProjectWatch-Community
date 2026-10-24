import type { Project } from "./api";
import { isMoving, needsAttention, projectListWork } from "./workState";

export default function ProjectNavStatus({ project, stale = false }: { project: Pick<Project, "activated" | "status" | "nav_alerts" | "job_status" | "job_runtime" | "worker_state" | "worker_health" | "execution_mode">; stale?: boolean }) {
  if (stale) return <span className="project-status-dot status-idle" role="img" aria-label="状态暂时无法读取" title="状态暂时无法读取" />;
  if (project.execution_mode === "agent") return <span className="project-status-dot status-idle" role="img" aria-label="Agent 按需更新" title="Agent 按需更新" />;
  const alerts = project.nav_alerts ?? [];
  const known = Array.isArray(project.nav_alerts);
  const paused = project.status === "paused";
  const offline = project.worker_state === "stopped";
  const blocked = project.worker_health === "stalled" || project.worker_health === "degraded" || (!!project.job_runtime && needsAttention(project.job_runtime.state));
  const tone = alerts.length || blocked ? "alert" : known && project.activated && !paused && !offline ? "ok" : "idle";
  const label = alerts.length ? alerts.join("；") : paused ? "监控已暂停"
    : !project.activated ? "尚未启用监控" : !known ? "监控状态暂不可用" : offline ? "自动更新未运行"
    : blocked ? "后台受阻，等待恢复" : project.worker_state === "stopping" ? "正在结束当前更新" : project.job_status === "running" ? "正在更新"
    : project.job_status === "queued" ? "等待更新" : "监控已启用";
  const work = projectListWork(project);
  const moving = tone === "ok" && isMoving(work.state);
  const visibleLabel = alerts.length || !known || !project.activated ? label : work.label;
  return <span className={`project-status-dot status-${tone}${moving ? " op-nav-running" : ""}${tone === "ok" && work.state === "queued" ? " op-nav-queued" : ""}`} role="img" aria-label={visibleLabel} title={visibleLabel} />;
}

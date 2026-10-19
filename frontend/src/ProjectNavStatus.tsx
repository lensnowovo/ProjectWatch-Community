import type { Project } from "./api";

export default function ProjectNavStatus({ project }: { project: Pick<Project, "activated" | "status" | "nav_alerts" | "job_status" | "worker_state" | "worker_health"> }) {
  const alerts = project.nav_alerts ?? [];
  const known = Array.isArray(project.nav_alerts);
  const paused = project.status === "paused";
  const offline = project.worker_state === "stopped";
  const blocked = project.worker_health === "stalled" || project.worker_health === "degraded";
  const tone = alerts.length || blocked ? "alert" : known && project.activated && !paused && !offline ? "ok" : "idle";
  const label = alerts.length ? alerts.join("；") : paused ? "监控已暂停"
    : !project.activated ? "尚未启用监控" : !known ? "监控状态暂不可用" : offline ? "自动更新未运行"
    : blocked ? "后台受阻，等待恢复" : project.worker_state === "stopping" ? "正在结束当前更新" : project.job_status === "running" ? "正在更新"
    : project.job_status === "queued" ? "等待更新" : "监控已启用";
  return <span className={`project-status-dot status-${tone}`} role="img" aria-label={label} title={label} />;
}

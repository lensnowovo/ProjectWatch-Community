import { Link } from "react-router-dom";
import type { DigestPending, MonitoringProgress } from "./api";

export function showStoredPanel(progress: MonitoringProgress | undefined, count: number) {
  return count > 0 || progress?.state === "observed";
}

export function monitoringLabel(progress?: MonitoringProgress, paused = false) {
  return paused ? "监控已暂停" : progress?.label ?? "监控状态尚未更新";
}

export function MonitoringEmpty({ progress, error }: { progress?: MonitoringProgress; error?: unknown }) {
  return <p className="overview-empty">{error ? "变化信息暂时未能加载。" : progress?.empty_text ?? "监控状态尚未更新。"}</p>;
}

export function DigestProgress({ incomplete }: { incomplete?: DigestPending[] }) {
  if (!incomplete?.length) return null;
  return <section aria-label="监控进度" className="card card-pad">
    <h3>监控进度</h3>
    <ul className="bullets">{incomplete.map((p) => <li key={p.id}>
      <Link to={`/projects/${p.id}`}>{p.name}</Link> <span className="muted small">· {p.label}</span>
    </li>)}</ul>
  </section>;
}

import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, type AgentAuto, type AgentRun } from "./api";
import { useVisiblePoll } from "./Operational";
import { ErrorNote, relTime } from "./ui";
import "./codexAuto.css";

const OUTCOME: Record<AgentRun["status"], string> = {
  queued: "排队中", running: "正在研究", done: "完成", failed: "未完成", unknown: "中途停止", cancelled: "已取消",
};

/** Auto research by the local Codex for one Agent project: state, and polling while a run is active. */
export function useCodexAuto(projectId: string, onFinished?: () => void) {
  const [auto, setAuto] = useState<AgentAuto | null>(null);
  const active = useRef(false);
  // The caller's reload may be a new function each render; keep the latest without refetching.
  const finished = useRef(onFinished);
  finished.current = onFinished;
  const load = useCallback(async () => {
    const next = await api.agentAuto(projectId);
    if (active.current && !next.active) finished.current?.();
    active.current = !!next.active;
    setAuto(next);
  }, [projectId]);
  useEffect(() => { load().catch(() => undefined); }, [load]);
  // The header button and the status line are separate; an action in one updates both at once.
  useEffect(() => {
    const listen = (event: Event) => {
      const { id, next } = (event as CustomEvent<{ id: string; next: AgentAuto }>).detail;
      if (id === projectId) { active.current = !!next.active; setAuto(next); }
    };
    window.addEventListener("codex-auto", listen);
    return () => window.removeEventListener("codex-auto", listen);
  }, [projectId]);
  useVisiblePoll(load, auto?.active ? 5000 : 60000, !!auto?.enabled || !!auto?.active);
  const publish = (next: AgentAuto) => window.dispatchEvent(new CustomEvent("codex-auto", { detail: { id: projectId, next } }));
  return { auto, setAuto: publish, reload: load };
}

/** Header action for an Agent project: start Codex now, or go connect it. */
export function CodexUpdateButton({ projectId, onReload }: { projectId: string; onReload: () => void }) {
  const { auto, setAuto } = useCodexAuto(projectId, onReload);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  if (!auto?.enabled) return <Link className="btn" to={`/agents?project=${projectId}`}>用 Agent 更新</Link>;
  const run = async () => {
    setBusy(true); setError(null);
    try { setAuto(await api.runAgent(projectId)); } catch (e) { setError(e); } finally { setBusy(false); }
  };
  return <>
    <button className="btn" onClick={run} disabled={busy || !!auto.active}>
      {auto.active ? "Codex 正在研究…" : "现在更新"}</button>
    {error != null && <ErrorNote error={error} />}
  </>;
}

/** One line under the header: what Codex did last and when it runs next. */
export function CodexAutoStatus({ projectId, onReload }: { projectId: string; onReload: () => void }) {
  const { auto, setAuto } = useCodexAuto(projectId, onReload);
  const [error, setError] = useState<unknown>(null);
  if (!auto || (!auto.enabled && !auto.active && !auto.last)) return null;
  const last = auto.last;
  const toggle = async () => {
    try { setAuto(await api.setAgentAuto(projectId, { enabled: !auto.enabled, cadence_days: auto.cadence_days })); }
    catch (e) { setError(e); }
  };
  const stop = async () => {
    if (!auto.active) return;
    try { setAuto(await api.cancelAgentRun(projectId, auto.active.id)); } catch (e) { setError(e); }
  };
  return <div className="codex-auto muted small">
    {auto.active ? <span className="codex-live">
      <span className="scope-running" aria-hidden="true" />
      Codex {auto.active.status === "queued" ? "排队中" : `正在研究 · 开始于 ${relTime(auto.active.started_at)}`}
      <button className="btn btn-ghost btn-sm" onClick={stop}>停止</button>
    </span> : last && <span>
      Codex 上次{OUTCOME[last.status]} · {relTime(last.finished_at ?? last.created_at)}
      {last.status !== "done" && last.error && <> · {last.error}</>}
    </span>}
    {auto.enabled && !auto.active && auto.next_at && <span> · 下次 {new Date(auto.next_at).toLocaleDateString("zh-CN")}</span>}
    {!auto.enabled && <span> · 自动研究已关闭</span>}
    {auto.connection_id && <button className="btn btn-ghost btn-sm" onClick={toggle}>
      {auto.enabled ? "关闭自动研究" : "开启自动研究"}</button>}
    {last?.summary && !auto.active && <details><summary>Codex 的总结</summary><p>{last.summary}</p></details>}
    <ErrorNote error={error} />
  </div>;
}

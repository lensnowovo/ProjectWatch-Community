import { useEffect, useState } from "react";
import { api } from "./api";

export default function DesktopExit() {
  const [instance, setInstance] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [accepted, setAccepted] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    api.desktopIdentity().then(value => {
      if (active && value.exit_available) {
        setInstance(value.instance);
        setAccepted(Boolean(value.exit_requested));
      }
    }).catch(() => undefined); // Web/server mode has no desktop owner to close.
    return () => { active = false; };
  }, []);
  if (!instance) return null;
  return <div aria-live="polite">
    <button className="btn btn-sm" disabled={pending || accepted}
      title="等待当前任务结束后关闭后台和托盘" onClick={async () => {
        setPending(true); setError("");
        try { await api.exitDesktop(instance); setAccepted(true); }
        catch { setError("未确认退出请求，请检查实例状态。"); }
        finally { setPending(false); }
      }}>{accepted ? "正在退出…" : pending ? "正在请求…" : "退出 ProjectWatch"}</button>
    {accepted && <p className="muted small">正在等待当前任务结束，可以关闭此页面。</p>}
    {error && <p className="text-warn small" role="alert">{error}</p>}
  </div>;
}

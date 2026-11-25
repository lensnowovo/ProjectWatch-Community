import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { api, type Project } from "../api";
import { EASE, Page } from "../motion";
import { useVisiblePoll } from "../Operational";
import { ErrorNote, relTime } from "../ui";
import "./agents.css";

type Client = "codex" | "workbuddy";
type Instance = Awaited<ReturnType<typeof api.instanceAgentConnections>>[number];
type Connection = Awaited<ReturnType<typeof api.agentConnection>>;
type LinkState = "idle" | "waiting" | "stale" | "live";

const LABEL: Record<Client, string> = { codex: "Codex", workbuddy: "WorkBuddy" };
const HINT: Record<Client, string> = { codex: "本机 Codex CLI 或桌面版", workbuddy: "WorkBuddy 的 MCP 连接器" };

/** Connect the client first and confirm it really reached ProjectWatch; then create projects. */
export default function AgentsPage() {
  const [params] = useSearchParams();
  const [client, setClient] = useState<Client>("codex");
  const [instances, setInstances] = useState<Instance[] | null>(null);
  const [generated, setGenerated] = useState<Instance | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [copied, setCopied] = useState("");
  const [creating, setCreating] = useState(false);
  const [result, setResult] = useState<{ project: Project; connection: Connection } | null>(null);
  const nameField = useRef<HTMLInputElement>(null);
  const reduce = useReducedMotion();

  // A failed list load is shown until a later poll succeeds; errors from actions stay until the next action.
  const [loadError, setLoadError] = useState<unknown>(null);
  const reload = async () => {
    try { setInstances(await api.instanceAgentConnections()); setLoadError(null); }
    catch (e) { setLoadError(e); throw e; }
  };
  // After a panel replaces the control that was used, focus moves into the new panel instead of the page.
  const [moveFocus, setMoveFocus] = useState(false);
  useEffect(() => { reload().catch(() => undefined); api.projects().then(setProjects).catch(setError); }, []);

  // A configuration just generated waits for its own first contact; otherwise the newest connection
  // of this client that has reached ProjectWatch counts as connected.
  const mine = (instances ?? []).filter(i => i.client === client);
  const latestSeen = mine.filter(i => i.last_seen_at).sort((a, b) => (b.last_seen_at ?? "").localeCompare(a.last_seen_at ?? ""))[0];
  const fresh = generated && generated.client === client
    ? (instances ?? []).find(i => i.connection_id === generated.connection_id) ?? generated : null;
  // A saved connection that never reported (set up before this page could tell) is reused, not duplicated.
  const waiting = fresh ? (fresh.last_seen_at ? null : fresh) : latestSeen ? null : mine[mine.length - 1] ?? null;
  const seen = waiting ? undefined : (fresh?.last_seen_at ? fresh : latestSeen);
  // Every tool call from a connected client reports in; an old record only says it connected once.
  const live = !!seen?.last_seen_at && Date.now() - Date.parse(seen.last_seen_at) < 30 * 60 * 1000;
  // Fast while waiting for a first contact (or the first load); a quiet client is only checked now and then.
  useVisiblePoll(reload, !instances || waiting ? 3000 : seen && !live ? 10000 : 30000);
  const state: LinkState = waiting ? "waiting" : seen ? (live ? "live" : "stale") : "idle";

  const generate = async () => {
    setBusy(true); setError(null); setCopied(""); setCreating(false); setResult(null);
    try {
      const made = await api.instanceAgentConnection(client);
      await api.saveSettings({ preferred_mode: "agent" });
      setGenerated({ ...made, client, last_seen_at: null }); setMoveFocus(true);
      await reload();
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  const copy = async (text: string, label: string) => {
    try { await navigator.clipboard.writeText(text); setCopied(label); }
    catch { setError(new Error("无法复制，请手动选择文本。")); }
  };
  const startProject = () => { setCreating(true); setResult(null); setTimeout(() => nameField.current?.focus(), 80); };
  const step = state === "idle" ? 0 : result ? 3 : state === "live" ? 2 : 1;
  const panel = reduce
    ? { initial: false as const, animate: { opacity: 1 }, exit: { opacity: 1 }, transition: { duration: 0 } }
    : { initial: { opacity: 0, y: 8 }, animate: { opacity: 1, y: 0 }, exit: { opacity: 0, y: -6 },
        transition: { duration: 0.26, ease: EASE } };

  return <Page className="page-workspace setup-page agents-page">
    <header className="settings-heading"><span className="muted small">已有工具，接着用</span>
      <h1>连接 Agent</h1><p>在 Codex 或 WorkBuddy 对话，在这里查看项目与结果。</p></header>

    {/* Nothing until the saved connections are known, so a connected client never flashes as unconnected. */}
    {instances && <section className="card card-pad setup-service agent-connect" aria-labelledby="agent-connect">
      <h2 id="agent-connect" className="sr-only">连接工具</h2>
      <Steps step={step} />
      <div className="agent-clients" role="radiogroup" aria-label="客户端">
        {(Object.keys(LABEL) as Client[]).map(c => <label key={c} className={client === c ? "selected" : ""}>
          <input type="radio" name="agent-client" checked={client === c} disabled={busy}
            onChange={() => { setClient(c); setCopied(""); setCreating(false); setResult(null); setMoveFocus(false); }} />
          <span><strong>{LABEL[c]}</strong><small>{HINT[c]}</small></span>
        </label>)}
      </div>
      <ConnectionHero client={client} state={state} />

      <AnimatePresence mode="wait" initial={false}>
        <motion.div key={client + state} className="agent-panel" {...panel}>
          {seen ? <Connected client={client} seen={seen} live={live} onNew={startProject} hasProjects={projects.length > 0}
              onReconnect={generate} busy={busy} copy={copy} copied={copied} focus={moveFocus} />
            : waiting ? <div className="agent-steps">
              <ol>
                <li><span><b>放入配置</b><small>写入 {waiting.destination}，保存后重新载入 MCP</small></span>
                  <CopyButton autoFocus={moveFocus} done={copied === "配置"} onClick={() => copy(waiting.configuration, "配置")}>复制配置</CopyButton></li>
                <li><span><b>发送连接检查</b><small>在 {LABEL[client]} 里发送，收到后这里自动变为已连接</small></span>
                  <CopyButton done={copied === "检查"} onClick={() => copy(waiting.prompt, "检查")}>复制检查指令</CopyButton></li>
              </ol>
              <details><summary>查看配置</summary><pre className="agent-config">{waiting.configuration}</pre></details>
              <p className="muted small">ProjectWatch 需保持打开。配置含授权信息，请勿分享配置或数据目录。</p>
              {generated && latestSeen && <button className="btn btn-ghost btn-sm" onClick={() => { setGenerated(null); setMoveFocus(true); }}>取消，继续用原连接</button>}
            </div>
            : <button className="btn btn-primary" disabled={busy} onClick={generate}>{busy ? "正在准备…" : `生成 ${LABEL[client]} 连接配置`}</button>}
        </motion.div>
      </AnimatePresence>
    </section>}

    <AnimatePresence initial={false}>
      {seen && (creating || result) && <motion.section key="project" className="card card-pad setup-service" aria-labelledby="agent-project"
        {...panel}>
        <h2 id="agent-project">新建项目</h2>
        {result ? <ProjectReady client={client} result={result} copied={copied} copy={copy} onAnother={startProject} />
          : <NewAgentProject client={client} connectionId={seen.connection_id} nameField={nameField}
              onDone={async (value) => { setResult(value); setCreating(false); setMoveFocus(true); setProjects(await api.projects()); }}
              onError={setError} />}
      </motion.section>}
    </AnimatePresence>

    {seen && projects.length > 0 && <ExistingProjects key={client + seen.connection_id} client={client} connectionId={seen.connection_id} projects={projects}
      initial={params.get("project") ?? ""} copy={copy} copied={copied} onError={setError} />}

    <p className="sr-only" role="status" aria-live="polite">{seen && live ? `${LABEL[client]} 已连接` : waiting ? `等待 ${LABEL[client]} 连接` : ""}</p>
    <ErrorNote error={error ?? loadError} />
    <p className="muted small">需要独立后台监控？<Link to="/setup">配置模型与搜索 API</Link>。已有项目的模式和状态不会因切换入口而改变。</p>
  </Page>;
}

const STEPS = ["选择工具", "放入配置并检查", "新建项目"];
function Steps({ step }: { step: number }) {
  return <ol className="agent-progress" aria-label="连接进度">
    {STEPS.map((label, i) => <li key={label} className={i < step ? "done" : i === step ? "current" : ""}
      aria-current={i === step ? "step" : undefined}>
      <span className="dot" aria-hidden="true">{i < step
        ? <svg viewBox="0 0 16 16"><path d="M3.5 8.5 l3 3 l6 -7" /></svg> : i + 1}</span>
      <span>{label}</span>{i < step && <span className="sr-only">（已完成）</span>}
    </li>)}
  </ol>;
}

const CAPTION: Record<LinkState, (c: string) => string> = {
  idle: () => "尚未连接",
  waiting: c => `等待 ${c} 发来连接检查`,
  stale: c => `${c} 最近没有连接`,
  live: c => `${c} 已连接`,
};

/** ProjectWatch and the client, joined by a line whose style shows the connection state. Decorative:
 *  the panel text and the live region carry the same state for assistive technology. */
function ConnectionHero({ client, state }: { client: Client; state: LinkState }) {
  return <div className={`agent-hero is-${state}`} aria-hidden="true">
    <svg viewBox="0 0 480 96" preserveAspectRatio="xMidYMid meet">
      <path className="track" d="M150 48 H330" />
      <path key={state} className="beam" d="M150 48 H330" pathLength={1} />
      <circle className="packet" r="4.5" cx="150" cy="48" />
      <g className="node"><rect x="6" y="22" width="144" height="52" rx="12" /><text x="78" y="53">ProjectWatch</text></g>
      <g className="node node-client"><rect x="330" y="22" width="144" height="52" rx="12" /><text x="402" y="53">{LABEL[client]}</text></g>
      <g key={`badge-${state}`} className="badge"><circle cx="240" cy="48" r="15" /><path d="M233 48.5 l5 5 l9.5 -10" /></g>
    </svg>
    <span className="agent-hero-caption">{CAPTION[state](LABEL[client])}</span>
  </div>;
}

function CopyButton({ done, onClick, children, primary, autoFocus }: {
  done: boolean; onClick: () => void; children: React.ReactNode; primary?: boolean; autoFocus?: boolean;
}) {
  return <button autoFocus={autoFocus} className={`btn ${primary ? "btn-primary" : "btn-sm"} agent-copy${done ? " is-done" : ""}`} onClick={onClick}>
    {done && <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8.5 l3 3 l6 -7" /></svg>}
    {done ? "已复制" : children}</button>;
}

const sinceSeen = (iso: string | null) => iso && Date.now() - Date.parse(iso) < 60_000 ? "刚刚" : relTime(iso);

function Connected({ client, seen, live, onNew, hasProjects, onReconnect, busy, copy, copied, focus }: {
  client: Client; seen: Instance; live: boolean; onNew: () => void; hasProjects: boolean; onReconnect: () => void; busy: boolean;
  copy: (text: string, label: string) => void; copied: string; focus: boolean;
}) {
  if (!live) return <div className="agent-stale">
    <div><strong>上次连接 {relTime(seen.last_seen_at)}</strong>
      <span className="muted small">在 {LABEL[client]} 里发送连接检查，确认后这里会显示已连接。</span></div>
    <div className="actions">
      <CopyButton primary autoFocus={focus} done={copied === "复查"} onClick={() => copy(seen.prompt, "复查")}>复制检查指令</CopyButton>
      <button className="btn" onClick={onNew}>{hasProjects ? "新建项目" : "新建第一个项目"}</button>
      <button className="btn btn-ghost btn-sm" disabled={busy} onClick={onReconnect}>重新生成配置</button>
    </div>
  </div>;
  return <div className="agent-connected">
    <span className="muted small">最近确认 {sinceSeen(seen.last_seen_at)} · 本机连接 {seen.connection_id.slice(0, 8)}</span>
    <div className="actions">
      <button className="btn btn-primary" autoFocus={focus} onClick={onNew}>{hasProjects ? "新建项目" : "新建第一个项目"}</button>
      <button className="btn btn-ghost btn-sm" disabled={busy} onClick={onReconnect}>重新生成配置</button>
    </div>
  </div>;
}

function NewAgentProject({ client, connectionId, nameField, onDone, onError }: {
  client: Client; connectionId: string; nameField: React.RefObject<HTMLInputElement>;
  onDone: (value: { project: Project; connection: Connection }) => void; onError: (e: unknown) => void;
}) {
  const [name, setName] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [internal, setInternal] = useState(false);
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  // Kept when authorization fails, so a retry authorizes it instead of creating a second project.
  const [made, setMade] = useState<Project | null>(null);
  const create = async () => {
    setBusy(true); onError(null);
    try {
      let project = made;
      if (!project) {
        const drafts: string[] = [];
        for (const file of files) drafts.push((await api.draft(file)).draft_id);
        project = await api.createAgentProject(name.trim(), drafts);
        setMade(project);
      }
      const connection = await api.agentConnection(project.id, client, internal && files.length > 0, connectionId);
      onDone({ project, connection });
    } catch (e) { onError(e); } finally { setBusy(false); }
  };
  return <>
    {made && <p className="muted small">{made.name} 已建立，授权未完成；再点一次只补授权。</p>}
    <label>公司名称<input ref={nameField} value={name} disabled={busy || !!made} onChange={e => setName(e.target.value)}
      placeholder="输入名字，交给 Agent 核对与研究" /></label>
    <label>内部材料 · 可选<input type="file" multiple disabled={busy} accept=".pdf,.docx,.pptx,.xlsx,.txt"
      onChange={e => { setFiles(Array.from(e.target.files ?? [])); setInternal(false); }} /></label>
    {files.length > 0 && <label className="setup-consent"><input type="checkbox" checked={internal} disabled={busy}
      onChange={e => setInternal(e.target.checked)} />允许 {LABEL[client]} 读取这些内部材料（可能发送至它的模型服务）</label>}
    <label className="setup-consent"><input type="checkbox" checked={consent} disabled={busy}
      onChange={e => setConsent(e.target.checked)} />允许 {LABEL[client]} 读取本项目、写回结果并调整监控范围，30 天，可撤销</label>
    <button className="btn btn-primary" onClick={create} disabled={busy || !name.trim() || !consent}>
      {busy ? "正在建立…" : made ? "补授权" : "建立项目并授权"}</button>
  </>;
}

function ProjectReady({ client, result, copied, copy, onAnother }: {
  client: Client; result: { project: Project; connection: Connection }; copied: string;
  copy: (text: string, label: string) => void; onAnother: () => void;
}) {
  return <div className="agent-ready">
    <p><strong>{result.project.name}</strong> 已建好并授权给 {LABEL[client]}。把研究指令发给它即可开始。</p>
    <div className="actions">
      <CopyButton primary autoFocus done={copied === "研究"} onClick={() => copy(result.connection.prompt, "研究")}>复制研究指令</CopyButton>
      <Link className="btn" to={`/projects/${result.project.id}`}>查看项目</Link>
      <button className="btn btn-ghost" onClick={onAnother}>再建一个</button>
    </div>
    <details><summary>研究指令</summary><pre className="agent-config">{result.connection.prompt}</pre></details>
  </div>;
}

function ExistingProjects({ client, connectionId, projects, initial, copy, copied, onError }: {
  client: Client; connectionId: string; projects: Project[]; initial: string;
  copy: (text: string, label: string) => void; copied: string; onError: (e: unknown) => void;
}) {
  const [projectId, setProjectId] = useState(projects.some(p => p.id === initial) ? initial : "");
  const [internal, setInternal] = useState(false);
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [connection, setConnection] = useState<Connection | null>(null);
  const [grants, setGrants] = useState<Awaited<ReturnType<typeof api.agentGrants>>>([]);
  useEffect(() => {
    setConnection(null); setConsent(false); setInternal(false); setGrants([]);
    let live = true;
    if (projectId) api.agentGrants(projectId).then(v => { if (live) setGrants(v); }).catch(e => { if (live) onError(e); });
    return () => { live = false; };
  }, [projectId, onError]);
  const authorize = async () => {
    setBusy(true); onError(null);
    try { setConnection(await api.agentConnection(projectId, client, internal, connectionId)); setGrants(await api.agentGrants(projectId)); }
    catch (e) { onError(e); } finally { setBusy(false); }
  };
  return <details className="provider-editor" open={!!initial || undefined}>
    <summary>授权已有项目 <span className="muted small">仅开放你选中的项目</span></summary>
    <section className="card card-pad setup-service">
      <label>项目<select disabled={busy} value={projectId} onChange={e => setProjectId(e.target.value)}>
        <option value="">选择项目</option>{projects.map(p => <option value={p.id} key={p.id}>{p.name}</option>)}</select></label>
      {projectId && <>
        <label className="setup-consent"><input type="checkbox" checked={internal} disabled={busy} onChange={e => { setInternal(e.target.checked); setConnection(null); }} />
          允许读取本项目内部材料（可能发送至 {LABEL[client]} 的模型服务）</label>
        <label className="setup-consent"><input type="checkbox" checked={consent} disabled={busy} onChange={e => setConsent(e.target.checked)} />
          允许 {LABEL[client]} 读取本项目、写回结果并调整监控范围，30 天，可撤销</label>
        <button className="btn btn-primary" disabled={busy || !consent || !!connection} onClick={authorize}>{busy ? "正在授权…" : "授权给已连接的工具"}</button>
      </>}
      {connection && <div className="agent-ready"><p>已授权。把研究指令发给 {LABEL[client]}。</p>
        <CopyButton done={copied === "已有"} onClick={() => copy(connection.prompt, "已有")}>复制研究指令</CopyButton></div>}
      {grants.some(g => !g.revoked_at) && <div className="agent-grants">{grants.filter(g => !g.revoked_at).map(g => <div className="actions" key={g.id}>
        <span>{g.label} · {g.allow_internal ? "含内部材料" : "公开信息"} · {new Date(g.expires_at * 1000).toLocaleDateString()} 到期</span>
        <button className="btn btn-sm" disabled={busy} aria-label={`撤销 ${g.label} 的授权`} onClick={async () => {
          setBusy(true);
          try { await api.revokeAgent(projectId, g.id); setGrants(await api.agentGrants(projectId)); if (connection?.grant_id === g.id) setConnection(null); }
          catch (e) { onError(e); } finally { setBusy(false); }
        }}>撤销</button></div>)}</div>}
    </section>
  </details>;
}

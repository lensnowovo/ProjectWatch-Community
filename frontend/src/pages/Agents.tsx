import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type Project } from "../api";
import { Page } from "../motion";
import { useVisiblePoll } from "../Operational";
import { ErrorNote, relTime } from "../ui";
import "./agents.css";

type Client = "codex" | "workbuddy";
type Instance = Awaited<ReturnType<typeof api.instanceAgentConnections>>[number];
type Connection = Awaited<ReturnType<typeof api.agentConnection>>;

const LABEL: Record<Client, string> = { codex: "Codex", workbuddy: "WorkBuddy" };

/** Connect the client first and confirm it really reached ProjectWatch; then create projects. */
export default function AgentsPage() {
  const [params] = useSearchParams();
  const [client, setClient] = useState<Client>("codex");
  const [instances, setInstances] = useState<Instance[]>([]);
  const [generated, setGenerated] = useState<Instance | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [copied, setCopied] = useState("");
  const [creating, setCreating] = useState(false);
  const [result, setResult] = useState<{ project: Project; connection: Connection } | null>(null);
  const nameField = useRef<HTMLInputElement>(null);

  const reload = async () => setInstances(await api.instanceAgentConnections());
  useEffect(() => { reload().catch(setError); api.projects().then(setProjects).catch(setError); }, []);

  // A configuration just generated waits for its own first contact; otherwise the newest connection
  // of this client that has reached ProjectWatch counts as connected.
  const fresh = generated && generated.client === client
    ? instances.find(i => i.connection_id === generated.connection_id) ?? generated : null;
  const waiting = fresh && !fresh.last_seen_at ? fresh : null;
  const seen = waiting ? undefined : (fresh?.last_seen_at ? fresh : instances.filter(i => i.client === client && i.last_seen_at)
    .sort((a, b) => (b.last_seen_at ?? "").localeCompare(a.last_seen_at ?? ""))[0]);
  useVisiblePoll(reload, 3000, !!waiting);

  const generate = async () => {
    setBusy(true); setError(null); setCopied("");
    try {
      const made = await api.instanceAgentConnection(client);
      await api.saveSettings({ preferred_mode: "agent" });
      setGenerated({ ...made, client, last_seen_at: null });
      await reload();
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  const copy = async (text: string, label: string) => {
    try { await navigator.clipboard.writeText(text); setCopied(label); }
    catch { setError(new Error("无法复制，请手动选择文本。")); }
  };
  const startProject = () => { setCreating(true); setResult(null); setTimeout(() => nameField.current?.focus(), 50); };

  return <Page className="page-workspace setup-page agents-page">
    <header className="settings-heading"><span className="muted small">已有工具，接着用</span>
      <h1>连接 Agent</h1><p>在 Codex 或 WorkBuddy 对话，在这里查看项目与结果。</p></header>

    <section className="card card-pad setup-service" aria-labelledby="agent-connect">
      <h2 id="agent-connect"><span className="setup-number">01</span> 连接工具</h2>
      <div className="agent-clients" role="radiogroup" aria-label="客户端">
        {(Object.keys(LABEL) as Client[]).map(c => <label key={c} className={client === c ? "selected" : ""}>
          <input type="radio" name="agent-client" checked={client === c} disabled={busy}
            onChange={() => { setClient(c); setCopied(""); setCreating(false); setResult(null); }} />{LABEL[c]}
        </label>)}
      </div>

      {seen ? <Connected client={client} seen={seen} onNew={startProject} hasProjects={projects.length > 0}
          onReconnect={generate} busy={busy} />
        : waiting ? <div className="agent-steps">
          <ol>
            <li><span>把配置放进 {waiting.destination}，保存后重新载入 MCP</span>
              <button className="btn btn-sm" onClick={() => copy(waiting.configuration, "配置")}>{copied === "配置" ? "已复制" : "复制配置"}</button></li>
            <li><span>在 {LABEL[client]} 里发送连接检查</span>
              <button className="btn btn-sm" onClick={() => copy(waiting.prompt, "检查")}>{copied === "检查" ? "已复制" : "复制检查指令"}</button></li>
          </ol>
          <details><summary>查看配置</summary><pre className="agent-config">{waiting.configuration}</pre></details>
          <p className="agent-waiting" role="status"><span className="scope-running" aria-hidden="true" />
            等待 {LABEL[client]} 连接… 收到后这里会自动更新。</p>
        </div>
        : <button className="btn btn-primary" disabled={busy} onClick={generate}>{busy ? "正在准备…" : `生成 ${LABEL[client]} 连接配置`}</button>}
    </section>

    {seen && (creating || result) && <section className="card card-pad setup-service" aria-labelledby="agent-project">
      <h2 id="agent-project"><span className="setup-number">02</span> 新建项目</h2>
      {result ? <ProjectReady client={client} result={result} copied={copied} copy={copy} onAnother={startProject} />
        : <NewAgentProject client={client} connectionId={seen.connection_id} nameField={nameField}
            onDone={async (value) => { setResult(value); setCreating(false); setProjects(await api.projects()); }}
            onError={setError} />}
    </section>}

    {seen && projects.length > 0 && <ExistingProjects client={client} connectionId={seen.connection_id} projects={projects}
      initial={params.get("project") ?? ""} copy={copy} copied={copied} onError={setError} />}

    <ErrorNote error={error} />
    <p className="muted small">需要独立后台监控？<Link to="/setup">配置模型与搜索 API</Link>。已有项目的模式和状态不会因切换入口而改变。</p>
  </Page>;
}

function Connected({ client, seen, onNew, hasProjects, onReconnect, busy }: {
  client: Client; seen: Instance; onNew: () => void; hasProjects: boolean; onReconnect: () => void; busy: boolean;
}) {
  return <div className="agent-connected" role="status">
    <svg className="agent-check" viewBox="0 0 52 52" aria-hidden="true">
      <circle cx="26" cy="26" r="24" /><path d="M15 27 l7 7 l15 -16" />
    </svg>
    <div>
      <strong>{LABEL[client]} 已连接</strong>
      <span className="muted small">最近确认 {relTime(seen.last_seen_at)} · 本机连接 {seen.connection_id.slice(0, 8)}</span>
    </div>
    <div className="actions">
      <button className="btn btn-primary" onClick={onNew}>{hasProjects ? "新建项目" : "新建第一个项目"}</button>
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
  const create = async () => {
    setBusy(true); onError(null);
    try {
      const drafts: string[] = [];
      for (const file of files) drafts.push((await api.draft(file)).draft_id);
      const project = await api.createAgentProject(name.trim(), drafts);
      const connection = await api.agentConnection(project.id, client, internal && files.length > 0, connectionId);
      onDone({ project, connection });
    } catch (e) { onError(e); } finally { setBusy(false); }
  };
  return <>
    <label>公司名称<input ref={nameField} value={name} disabled={busy} onChange={e => setName(e.target.value)}
      placeholder="输入名字，交给 Agent 核对与研究" /></label>
    <label>内部材料 · 可选<input type="file" multiple disabled={busy} accept=".pdf,.docx,.pptx,.xlsx,.txt"
      onChange={e => setFiles(Array.from(e.target.files ?? []))} /></label>
    {files.length > 0 && <label className="setup-consent"><input type="checkbox" checked={internal} disabled={busy}
      onChange={e => setInternal(e.target.checked)} />允许 {LABEL[client]} 读取这些内部材料（可能发送至它的模型服务）</label>}
    <label className="setup-consent"><input type="checkbox" checked={consent} disabled={busy}
      onChange={e => setConsent(e.target.checked)} />允许 {LABEL[client]} 读取本项目、写回结果并调整监控范围，30 天，可撤销</label>
    <button className="btn btn-primary" onClick={create} disabled={busy || !name.trim() || !consent}>
      {busy ? "正在建立…" : "建立项目并授权"}</button>
  </>;
}

function ProjectReady({ client, result, copied, copy, onAnother }: {
  client: Client; result: { project: Project; connection: Connection }; copied: string;
  copy: (text: string, label: string) => void; onAnother: () => void;
}) {
  return <div className="agent-ready">
    <p><strong>{result.project.name}</strong> 已建好并授权给 {LABEL[client]}。把研究指令发给它即可开始。</p>
    <div className="actions">
      <button className="btn btn-primary" onClick={() => copy(result.connection.prompt, "研究")}>{copied === "研究" ? "已复制研究指令" : "复制研究指令"}</button>
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
        <button className="btn" onClick={() => copy(connection.prompt, "已有")}>{copied === "已有" ? "已复制研究指令" : "复制研究指令"}</button></div>}
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

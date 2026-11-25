import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { AnimatePresence, MotionConfig, motion, useReducedMotion } from "motion/react";
import { api, type CodexStatus, type Project } from "../api";
import { Page } from "../motion";
import { useVisiblePoll } from "../Operational";
import { ErrorNote, relTime } from "../ui";
import "./agents.css";

type Client = "codex" | "workbuddy";
type Instance = Awaited<ReturnType<typeof api.instanceAgentConnections>>[number];
type Connection = Awaited<ReturnType<typeof api.agentConnection>>;
type LinkState = "idle" | "waiting" | "stale" | "live";
type Made = { project: Project; connection: Connection; auto?: boolean };

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
  const [result, setResult] = useState<Made | null>(null);
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
  // Whether ProjectWatch can start this machine's Codex by itself (installed and logged in).
  const [codex, setCodex] = useState<CodexStatus | null>(null);
  const codexSeen = client === "codex" && !!seen;
  useEffect(() => { if (codexSeen) api.codexStatus().then(setCodex).catch(() => setCodex(null)); }, [codexSeen]);
  const canAuto = codexSeen && !!codex?.available && codex.logged_in;

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
  // Apple-like motion: soft springs that settle without bounce on panels, a little bounce on the check.
  const spring = { type: "spring" as const, stiffness: 340, damping: 32, mass: 0.9 };
  const panel = reduce
    ? { initial: false as const, animate: { opacity: 1 }, exit: { opacity: 1 }, transition: { duration: 0 } }
    : { initial: { opacity: 0, y: 14, filter: "blur(6px)" }, animate: { opacity: 1, y: 0, filter: "blur(0px)", transitionEnd: { filter: "none" } },
        exit: { opacity: 0, y: -8, filter: "blur(4px)" }, transition: spring };
  const pick = (c: Client) => { setClient(c); setCopied(""); setCreating(false); setResult(null); setMoveFocus(false); };

  return <Page className="page-workspace setup-page agents-page">
    <header className="settings-heading"><span className="muted small">已有工具，接着用</span>
      <h1>连接 Agent</h1><p>在 Codex 或 WorkBuddy 对话，在这里查看项目与结果。</p></header>

    {/* Nothing until the saved connections are known, so a connected client never flashes as unconnected. */}
    {instances && <MotionConfig reducedMotion="user">
      <section className="card agent-stage" aria-labelledby="agent-connect">
        <h2 id="agent-connect" className="sr-only">连接工具</h2>
        <div className="agent-seg" role="radiogroup" aria-label="客户端">
          {(Object.keys(LABEL) as Client[]).map(c => <label key={c} className={client === c ? "on" : ""}>
            <input type="radio" name="agent-client" checked={client === c} disabled={busy} onChange={() => pick(c)} />
            {client === c && <motion.span layoutId="agent-seg-thumb" className="thumb" transition={spring} />}
            <span className="seg-label">{LABEL[c]}</span>
          </label>)}
        </div>

        <Pairing client={client} state={state} reduce={!!reduce} />
        <div className="agent-headline">
          <AnimatePresence mode="wait" initial={false}>
            <motion.div key={client + state} {...panel}>
              <strong>{TITLE[state](LABEL[client])}</strong>
              <span>{state === "idle" ? HINT[client] : state === "waiting" ? "完成下面两步，这里会自动确认"
                : state === "live" ? `最近确认 ${sinceSeen(seen?.last_seen_at ?? null)}` : `上次连接 ${relTime(seen?.last_seen_at)}`}</span>
            </motion.div>
          </AnimatePresence>
        </div>

        <AnimatePresence mode="wait" initial={false}>
          <motion.div key={client + state} className="agent-panel" {...panel} onAnimationComplete={() => setMoveFocus(false)}>
            {seen ? <Connected seen={seen} live={live} onNew={startProject} hasProjects={projects.length > 0}
                onReconnect={generate} busy={busy} copy={copy} copied={copied} focus={moveFocus} />
              : waiting ? <>
                <ol className="agent-group">
                  <li><span className="n" aria-hidden="true">1</span>
                    <span className="t"><b>放入配置</b><small>写入 {waiting.destination}，保存后重新载入 MCP</small></span>
                    <CopyButton autoFocus={moveFocus} done={copied === "配置"} onClick={() => copy(waiting.configuration, "配置")}>复制配置</CopyButton></li>
                  <li><span className="n" aria-hidden="true">2</span>
                    <span className="t"><b>发送连接检查</b><small>在 {LABEL[client]} 里发送</small></span>
                    <CopyButton done={copied === "检查"} onClick={() => copy(waiting.prompt, "检查")}>复制检查指令</CopyButton></li>
                </ol>
                <details className="agent-more"><summary>查看配置</summary><pre className="agent-config">{waiting.configuration}</pre></details>
                <p className="agent-note">ProjectWatch 需保持打开。配置含授权信息，请勿分享配置或数据目录。</p>
                {generated && latestSeen && <button className="btn btn-ghost btn-sm" onClick={() => { setGenerated(null); setMoveFocus(true); }}>取消，继续用原连接</button>}
              </>
              : <button className="btn btn-primary agent-cta" disabled={busy} onClick={generate}>{busy ? "正在准备…" : `生成 ${LABEL[client]} 连接配置`}</button>}
          </motion.div>
        </AnimatePresence>
        <PageDots step={step} />
      </section>
    </MotionConfig>}

    <AnimatePresence initial={false}>
      {seen && (creating || result) && <motion.section key="project" className="card card-pad setup-service agent-project" aria-labelledby="agent-project"
        {...panel}>
        <h2 id="agent-project">新建项目</h2>
        {result ? <ProjectReady client={client} result={result} copied={copied} copy={copy} onAnother={startProject} />
          : <NewAgentProject client={client} connectionId={seen.connection_id} nameField={nameField} canAuto={canAuto}
              codex={codexSeen ? codex : null}
              onDone={async (value) => { setResult(value); setCreating(false); setMoveFocus(true); setProjects(await api.projects()); }}
              onError={setError} />}
      </motion.section>}
    </AnimatePresence>

    {seen && projects.length > 0 && <ExistingProjects key={client + seen.connection_id} client={client} connectionId={seen.connection_id} projects={projects} canAuto={canAuto}
      initial={params.get("project") ?? ""} copy={copy} copied={copied} onError={setError} />}

    <p className="sr-only" role="status" aria-live="polite">{seen && live ? `${LABEL[client]} 已连接` : waiting ? `等待 ${LABEL[client]} 连接` : ""}</p>
    <ErrorNote error={error ?? loadError} />
    <p className="muted small">需要独立后台监控？<Link to="/setup">配置模型与搜索 API</Link>。已有项目的模式和状态不会因切换入口而改变。</p>
  </Page>;
}

const STEPS = ["选择工具", "放入配置并检查", "新建项目"];
/** iOS-style page dots: the current step stretches into a pill. */
function PageDots({ step }: { step: number }) {
  const current = Math.min(step, 2);
  return <div className="agent-dots">
    <span className="sr-only">第 {current + 1} 步，共 3 步：{STEPS[current]}</span>
    {STEPS.map((label, i) => <motion.span key={label} aria-hidden="true" className={i <= current ? "on" : ""}
      animate={{ width: i === current ? 22 : 7 }} transition={{ type: "spring", stiffness: 420, damping: 34 }} />)}
  </div>;
}

const TITLE: Record<LinkState, (c: string) => string> = {
  idle: c => `连接 ${c}`,
  waiting: c => `正在等待 ${c}`,
  stale: c => `${c} 有一段时间没连接`,
  live: c => `${c} 已连接`,
};

/** The client's icon in the middle: rings ripple out while waiting, a check springs on when connected.
 *  Decorative; the headline and the live region carry the state. */
function Pairing({ client, state, reduce }: { client: Client; state: LinkState; reduce: boolean }) {
  return <div className={`agent-pairing is-${state}`} aria-hidden="true">
    {state === "waiting" && !reduce && [0, 1, 2].map(i => <span key={i} className="ring" style={{ animationDelay: `${i * 0.9}s` }} />)}
    {state === "live" && !reduce && <motion.span key="glow" className="glow" initial={{ scale: 0.9, opacity: 0.55 }}
      animate={{ scale: 2.2, opacity: 0 }} transition={{ duration: 1.2, ease: [0.16, 1, 0.3, 1] }} />}
    <div className={`icon icon-${client}`}>
      <AnimatePresence initial={false}>
        <motion.svg key={client} viewBox="0 0 48 48" initial={{ opacity: 0, scale: 0.6, filter: "blur(6px)" }}
          animate={{ opacity: 1, scale: 1, filter: "blur(0px)", transitionEnd: { filter: "none" } }} exit={{ opacity: 0, scale: 0.6, filter: "blur(6px)" }}
          transition={{ type: "spring", stiffness: 380, damping: 30 }}>
          {client === "codex"
            ? <><path d="M13 17 l8 7 l-8 7" /><path d="M25 32 h10" /></>
            : <><path d="M11 15 h26 a3 3 0 0 1 3 3 v12 a3 3 0 0 1 -3 3 h-15 l-7 6 v-6 h-4 a3 3 0 0 1 -3 -3 v-12 a3 3 0 0 1 3 -3 z" />
              <circle cx="18" cy="24" r="1.6" className="fill" /><circle cx="24" cy="24" r="1.6" className="fill" /><circle cx="30" cy="24" r="1.6" className="fill" /></>}
        </motion.svg>
      </AnimatePresence>
    </div>
    <AnimatePresence>
      {state === "live" && <motion.span key="badge" className="badge" initial={{ scale: 0, opacity: 0 }}
        animate={{ scale: 1, opacity: 1 }} exit={{ scale: 0, opacity: 0 }}
        transition={{ type: "spring", stiffness: 520, damping: 18, delay: reduce ? 0 : 0.15 }}>
        <svg viewBox="0 0 24 24"><motion.path d="M6.5 12.5 l3.8 3.8 l7.5 -8.3" initial={{ pathLength: reduce ? 1 : 0 }}
          animate={{ pathLength: 1 }} transition={{ duration: 0.35, delay: reduce ? 0 : 0.3, ease: "easeOut" }} /></svg>
      </motion.span>}
    </AnimatePresence>
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

function Connected({ seen, live, onNew, hasProjects, onReconnect, busy, copy, copied, focus }: {
  seen: Instance; live: boolean; onNew: () => void; hasProjects: boolean; onReconnect: () => void; busy: boolean;
  copy: (text: string, label: string) => void; copied: string; focus: boolean;
}) {
  const create = hasProjects ? "新建项目" : "新建第一个项目";
  if (!live) return <div className="agent-actions">
    <CopyButton primary autoFocus={focus} done={copied === "复查"} onClick={() => copy(seen.prompt, "复查")}>复制检查指令</CopyButton>
    <button className="btn" onClick={onNew}>{create}</button>
    <button className="btn btn-ghost btn-sm" disabled={busy} onClick={onReconnect}>重新生成配置</button>
  </div>;
  return <div className="agent-actions">
    <button className="btn btn-primary agent-cta" autoFocus={focus} onClick={onNew}>{create}</button>
    <button className="btn btn-ghost btn-sm" disabled={busy} onClick={onReconnect}>重新生成配置</button>
  </div>;
}

function NewAgentProject({ client, connectionId, nameField, canAuto, codex, onDone, onError }: {
  client: Client; connectionId: string; nameField: React.RefObject<HTMLInputElement>; canAuto: boolean;
  codex: CodexStatus | null; onDone: (value: Made) => void; onError: (e: unknown) => void;
}) {
  const [auto, setAuto] = useState(true);
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
      let started = false;
      if (canAuto && auto) {
        try { await api.setAgentAuto(project.id, { enabled: true, connection_id: connectionId, run_now: true }); started = true; }
        catch (e) { onError(e); }
      }
      onDone({ project, connection, auto: started });
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
    {canAuto && <label className="setup-consent"><input type="checkbox" checked={auto} disabled={busy}
      onChange={e => setAuto(e.target.checked)} />建好后由 ProjectWatch 启动 Codex 研究，之后每 7 天更新一次（使用你的 Codex 额度，授权随之续期，可在项目页关闭）</label>}
    {client === "codex" && codex && !canAuto && <p className="muted small">{codex.available
      ? "本机 Codex 未登录（在终端运行 codex login），建好后需要把研究指令发给 Codex。"
      : "未检测到本机 Codex CLI，建好后需要把研究指令发给 Codex。"}</p>}
    <button className="btn btn-primary" onClick={create} disabled={busy || !name.trim() || !consent}>
      {busy ? "正在建立…" : made ? "补授权" : "建立项目并授权"}</button>
  </>;
}

function ProjectReady({ client, result, copied, copy, onAnother }: {
  client: Client; result: Made; copied: string;
  copy: (text: string, label: string) => void; onAnother: () => void;
}) {
  if (result.auto) return <div className="agent-ready">
    <p><strong>{result.project.name}</strong> 已建好，Codex 已开始研究。通常几分钟，结果会写入项目页。</p>
    <div className="actions">
      <Link className="btn btn-primary" autoFocus to={`/projects/${result.project.id}`}>查看项目</Link>
      <button className="btn btn-ghost" onClick={onAnother}>再建一个</button>
    </div>
    <details><summary>手动研究指令</summary><pre className="agent-config">{result.connection.prompt}</pre>
      <CopyButton done={copied === "研究"} onClick={() => copy(result.connection.prompt, "研究")}>复制研究指令</CopyButton></details>
  </div>;
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

function ExistingProjects({ client, connectionId, projects, initial, copy, copied, onError, canAuto }: {
  client: Client; connectionId: string; projects: Project[]; initial: string; canAuto: boolean;
  copy: (text: string, label: string) => void; copied: string; onError: (e: unknown) => void;
}) {
  const [projectId, setProjectId] = useState(projects.some(p => p.id === initial) ? initial : "");
  const [internal, setInternal] = useState(false);
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [connection, setConnection] = useState<Connection | null>(null);
  const [autoOn, setAutoOn] = useState(false);
  const agentProject = canAuto && projects.find(p => p.id === projectId)?.execution_mode === "agent";
  const [grants, setGrants] = useState<Awaited<ReturnType<typeof api.agentGrants>>>([]);
  useEffect(() => {
    setConnection(null); setConsent(false); setInternal(false); setGrants([]); setAutoOn(false);
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
      {connection && <div className="agent-ready">{autoOn
        ? <p>已授权，Codex 已开始研究，之后每 7 天更新一次。</p>
        : <><p>已授权。{agentProject ? "可让 ProjectWatch 启动 Codex，也可以自己发研究指令。" : `把研究指令发给 ${LABEL[client]}。`}</p>
          <div className="actions">
            {agentProject && <button className="btn btn-primary" disabled={busy} onClick={async () => {
              setBusy(true); onError(null);
              try { await api.setAgentAuto(projectId, { enabled: true, connection_id: connectionId, run_now: true }); setAutoOn(true); }
              catch (e) { onError(e); } finally { setBusy(false); }
            }}>让 Codex 自动研究</button>}
            <CopyButton done={copied === "已有"} onClick={() => copy(connection.prompt, "已有")}>复制研究指令</CopyButton>
          </div></>}</div>}
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

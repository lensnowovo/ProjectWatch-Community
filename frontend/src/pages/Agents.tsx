import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type Project } from "../api";
import { Page } from "../motion";
import { ErrorNote } from "../ui";

export default function AgentsPage() {
  const [params, setParams] = useSearchParams();
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState(params.get("project") ?? "");
  const [client, setClient] = useState<"codex" | "workbuddy">("codex");
  const [name, setName] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [internal, setInternal] = useState(false);
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [copied, setCopied] = useState("");
  const [connection, setConnection] = useState<Awaited<ReturnType<typeof api.agentConnection>> | null>(null);
  const [grants, setGrants] = useState<Awaited<ReturnType<typeof api.agentGrants>>>([]);
  useEffect(() => { api.projects().then(setProjects).catch(setError); }, []);
  useEffect(() => {
    setConnection(null); setConsent(false); setInternal(false); setGrants([]); setCopied("");
    let live = true;
    if (projectId) api.agentGrants(projectId).then(v => { if (live) setGrants(v); }).catch(e => { if (live) setError(e); });
    return () => { live = false; };
  }, [projectId]);
  const project = projects.find(p => p.id === projectId);
  const create = async () => {
    setBusy(true); setError(null);
    try {
      const drafts: string[] = [];
      for (const file of files) drafts.push((await api.draft(file)).draft_id);
      const p = await api.createAgentProject(name.trim(), drafts);
      await api.saveSettings({ preferred_mode: "agent" });
      setProjects(await api.projects()); setProjectId(p.id); setParams({ project: p.id }); setFiles([]); setName("");
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  const connect = async () => {
    setBusy(true); setError(null); setCopied("");
    try {
      setConnection(await api.agentConnection(projectId, client, internal));
      setGrants(await api.agentGrants(projectId));
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  const copy = async (text: string, label: string) => {
    try { await navigator.clipboard.writeText(text); setCopied(label); }
    catch { setError(new Error("无法复制，请手动选择文本。")); }
  };
  return <Page className="page-workspace setup-page">
    <header className="settings-heading"><span className="muted small">已有工具，接着用</span>
      <h1>连接 Agent</h1><p>在 Codex 或 WorkBuddy 对话，在这里保存项目、查看结果。</p></header>
    <div className="setup-grid">
      <section className="card card-pad setup-service"><h2><span className="setup-number">01</span> 选择项目</h2>
        <label>已有项目<select disabled={busy} value={projectId} onChange={e => { setProjectId(e.target.value); setParams(e.target.value ? { project: e.target.value } : {}); }}>
          <option value="">新建项目</option>{projects.map(p => <option value={p.id} key={p.id}>{p.name}</option>)}</select></label>
        {!projectId ? <><label>公司名称<input value={name} disabled={busy} onChange={e => setName(e.target.value)} placeholder="输入名字，交给 Agent 核对与研究" /></label>
          <label>内部材料 · 可选<input type="file" multiple disabled={busy} accept=".pdf,.docx,.pptx,.xlsx,.txt" onChange={e => setFiles(Array.from(e.target.files ?? []))} /></label>
          <p className="muted small">先本地建档、解析，不调用模型或搜索。材料读取需下一步单独授权。</p>
          <button className="btn btn-primary" onClick={create} disabled={busy || !name.trim()}>{busy ? "正在建档…" : "建立项目"}</button></>
          : <><p><strong>{project?.name}</strong></p><p className="muted small">{project?.execution_mode === "agent" ? "Agent 按需更新 · 内置 API 调度关闭" : "保留本项目原有 API 模式和暂停状态；接入不会停止已有任务。"}</p>
            <Link to={`/projects/${projectId}`}>查看项目 →</Link></>}
      </section>
      <section className="card card-pad setup-service"><h2><span className="setup-number">02</span> 授权连接</h2>
        <label>客户端<select value={client} disabled={busy} onChange={e => { setClient(e.target.value as "codex" | "workbuddy"); setConnection(null); setConsent(false); }}><option value="codex">Codex</option><option value="workbuddy">WorkBuddy</option></select></label>
        <label className="setup-consent"><input type="checkbox" checked={internal} disabled={busy} onChange={e => { setInternal(e.target.checked); setConnection(null); setConsent(false); }} />允许读取本项目内部材料（可能发送至 Agent 的模型服务）</label>
        <label className="setup-consent"><input type="checkbox" checked={consent} disabled={busy || !projectId} onChange={e => setConsent(e.target.checked)} />允许此 Agent 读取本项目、写回结果及调整监控范围，授权 30 天，可撤销。</label>
        <button className="btn btn-primary" disabled={busy || !projectId || !consent || !!connection} onClick={connect}>{busy ? "正在准备…" : "生成连接配置"}</button>
        <p className="muted small">不会自动修改客户端配置，不触发付费研究。Agent 需自带登录与可用的联网工具。</p>
      </section>
    </div>
    <ErrorNote error={error} />
    {connection && <section className="card card-pad"><h2>把连接放进 {client === "codex" ? "Codex" : "WorkBuddy"}</h2>
      <p className="muted small">{connection.destination} · 合并以下条目，保留已有配置；同名条目请替换，不要重复添加。保存后重新载入客户端的 MCP。</p>
      <p className="muted small">配置已生成，不等于已连接。请先让 Agent 调用 project_overview，确认返回正确公司，再开始研究。ProjectWatch 需保持打开；升级或移动程序后请重新生成配置。</p>
      <pre className="agent-config">{connection.configuration}</pre>
      <div className="actions"><button className="btn" onClick={() => copy(connection.configuration, "配置")}>{copied === "配置" ? "已复制配置" : "复制连接配置"}</button>
        <button className="btn btn-primary" onClick={() => copy(connection.prompt, "指令")}>{copied === "指令" ? "已复制研究指令" : "复制研究指令"}</button></div>
      <details><summary>研究指令</summary><pre className="agent-config">{connection.prompt}</pre></details>
      <p className="muted small">使用 Agent 自己的搜索与模型额度。连接文件含授权，请勿分享配置或数据目录。关闭 Agent 不会继续执行无人值守研究。</p>
    </section>}
    {grants.some(g => !g.revoked_at) && <section className="card card-pad"><h2>项目授权</h2>{grants.filter(g => !g.revoked_at).map(g => <div className="actions" key={g.id}>
      <span>{g.label} · {g.allow_internal ? "含内部材料" : "公开信息"} · {new Date(g.expires_at * 1000).toLocaleDateString()} 到期</span>
      <button className="btn btn-sm" disabled={busy} onClick={async () => { setBusy(true); try { await api.revokeAgent(projectId, g.id); setGrants(await api.agentGrants(projectId)); if (connection?.grant_id === g.id) setConnection(null); } catch(e) { setError(e); } finally { setBusy(false); } }}>撤销</button></div>)}</section>}
    <p className="muted small">需要独立后台监控？<Link to="/setup">配置模型与搜索 API</Link>。已有项目的模式和状态不会因切换入口而改变。</p>
  </Page>;
}

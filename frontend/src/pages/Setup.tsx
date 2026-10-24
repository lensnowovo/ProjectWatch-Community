import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type SettingsView } from "../api";
import { Page } from "../motion";
import { ErrorNote } from "../ui";

type Results = Record<string, { ok: boolean | null; message: string }>;
const SEARCH = [{ id: "deepseek", label: "DeepSeek Search", url: "https://platform.deepseek.com/api_keys" },
  { id: "bocha", label: "博查", url: "https://open.bochaai.com/" },
  { id: "tavily", label: "Tavily", url: "https://app.tavily.com/" },
  { id: "brave", label: "Brave Search", url: "https://api.search.brave.com/" }];

export function setupPassed(results: Results | null, provider: string): boolean {
  return results?.model?.ok === true && results?.[`search_${provider}`]?.ok === true;
}

export default function SetupPage({ onSaved }: { onSaved: (ready: boolean) => void }) {
  const navigate = useNavigate();
  const [apiMode, setApiMode] = useState(false);
  const [saved, setSaved] = useState<SettingsView | null>(null);
  const [model, setModel] = useState({ protocol: "openai", base_url: "", model: "", api_key: "" });
  const [provider, setProvider] = useState("deepseek");
  const [custom, setCustom] = useState(false);
  const [searchKey, setSearchKey] = useState("");
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [results, setResults] = useState<Results | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [attempted, setAttempted] = useState(false);
  useEffect(() => {
    let live = true;
    // Read stored, masked configuration only. Never test on mount or in an effect.
    api.settings().then(s => {
      if (!live) return;
      setSaved(s);
      setModel({ protocol: s.model.protocol, base_url: s.model.base_url, model: s.model.model, api_key: "" });
      setCustom(!!s.model.base_url && !s.presets.some(p => p.base_url === s.model.base_url && p.model === s.model.model));
      setProvider(s.search.providers?.[0] ?? (s.search.provider !== "none" ? s.search.provider : "deepseek"));
    }).catch(e => { if (live) setError(e); });
    return () => { live = false; };
  }, []);
  const changed = () => { setResults(null); setAttempted(false); setConsent(false); setError(null); };
  const knownKey = !!saved?.model.api_key && model.base_url === saved.model.base_url && model.protocol === saved.model.protocol;
  const knownSearch = !!saved?.search.keys?.[provider] || (saved?.search.provider === provider && !!saved.search.api_key);
  const complete = !!(model.base_url.trim() && model.model.trim() && (model.api_key.trim() || knownKey) && (searchKey.trim() || knownSearch));
  const passed = setupPassed(results, provider);
  const saveAndTest = async () => {
    if (busy || !complete || !consent || attempted) return;
    setBusy(true); setError(null); setResults(null); setAttempted(true);
    try {
      // Patch just these two sections. Do not reset proxy, commercial keys, notifications or projects.
      const next = await api.saveSettings({ model, preferred_mode: "api",
        search: { providers: [provider], keys: searchKey.trim() ? { [provider]: searchKey.trim() } : {} } });
      setSaved(next); onSaved(next.model.ready);
      setModel(m => ({ ...m, api_key: "" })); setSearchKey("");
      setResults(await api.testSetup());
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  return <Page className="page-workspace setup-page">
    <header className="settings-heading"><span className="muted small">首次使用</span>
      <h1>选择使用方式</h1><p>连接已有 Agent，或让 ProjectWatch 独立运行。</p></header>
    <div className="setup-grid">
      <section className="card card-pad"><h2>已有 Codex / WorkBuddy</h2><p>通过对话研究项目，结果保存到这里。无需另外填写模型或搜索 Key。</p>
        <button className="btn btn-primary" disabled={busy} onClick={async () => { setBusy(true); try { await api.saveSettings({ preferred_mode: "agent" }); navigate("/agents"); } catch(e) { setError(e); } finally { setBusy(false); } }}>连接已有 Agent →</button></section>
      <section className="card card-pad"><h2>独立 API 模式</h2><p>配置一个模型和一个搜索服务，由本机后台按计划监控。</p>
        <button className="btn" disabled={busy} onClick={() => setApiMode(true)}>配置模型与搜索</button></section>
    </div>
    {!apiMode && error != null && <ErrorNote error={error} />}
    {apiMode && <>
    {!saved ? (error ? <ErrorNote error={error} /> : <p role="status">正在读取配置…</p>) : <>
      <fieldset className="setup-grid" disabled={busy}>
        <section className="card card-pad setup-service">
          <h2><span className="setup-number">01</span> AI 模型</h2>
          <label>选择服务<select value={saved.presets.find(p => p.base_url === model.base_url && p.model === model.model)?.id ?? (custom ? "custom" : "")}
            onChange={e => { const p = saved.presets.find(p => p.id === e.target.value); changed(); setCustom(e.target.value === "custom");
              setModel(p ? { protocol: p.protocol, base_url: p.base_url, model: p.model, api_key: "" }
                : { protocol: "openai", base_url: "", model: "", api_key: "" }); }}>
            <option value="" disabled>请选择模型服务</option>
            {saved.presets.filter(p => p.id !== "glm-coding").map(p => <option value={p.id} key={p.id}>{p.label}</option>)}
            <option value="custom">自定义兼容服务</option>
          </select></label>
          <label>API Key<input type="password" autoComplete="off" value={model.api_key}
            placeholder={knownKey ? "已保存，留空保留" : "粘贴模型 API Key"}
            onChange={e => { changed(); setModel({ ...model, api_key: e.target.value }); }} /></label>
          <details open={custom}><summary>服务地址与模型</summary>
            <label>协议<select value={model.protocol} onChange={e => { changed(); setModel({ ...model, protocol: e.target.value, api_key: "" }); }}>
              <option value="openai">OpenAI 兼容</option><option value="anthropic">Anthropic 兼容</option></select></label>
            <label>服务地址<input value={model.base_url} placeholder="https://…" autoComplete="off"
              onChange={e => { changed(); setModel({ ...model, base_url: e.target.value, api_key: "" }); }} /></label>
            <label>模型名称<input value={model.model} onChange={e => { changed(); setModel({ ...model, model: e.target.value }); }} /></label>
          </details>
        </section>
        <section className="card card-pad setup-service">
          <h2><span className="setup-number">02</span> 搜索服务</h2>
          <label>选择服务<select value={provider} onChange={e => { changed(); setProvider(e.target.value); setSearchKey(""); }}>
            {SEARCH.map(s => <option key={s.id} value={s.id}>{s.label}</option>)}
          </select></label>
          <label>API Key<input type="password" autoComplete="off" value={searchKey}
            placeholder={knownSearch ? "已保存，留空保留" : "粘贴搜索 API Key"}
            onChange={e => { changed(); setSearchKey(e.target.value); }} /></label>
          <a className="small" href={SEARCH.find(s => s.id === provider)?.url} target="_blank" rel="noopener noreferrer">前往服务商获取 Key ↗</a>
          <p className="muted small">先选一个即可，之后可在设置中添加更多来源。</p>
        </section>
      </fieldset>
      <div className="setup-actions">
        <label className="setup-consent"><input type="checkbox" checked={consent} disabled={busy || attempted}
          onChange={e => setConsent(e.target.checked)} />允许连接测试使用所选服务额度。密钥仅在本机保存。</label>
        <div className="actions"><button className="btn btn-primary" disabled={busy || !complete || !consent || attempted} onClick={saveAndTest}>
          {busy ? "正在保存并测试…" : "保存并测试连接"}</button><Link to="/settings">完整设置</Link><Link to="/">先看看</Link></div>
        {error != null && <ErrorNote error={error} />}
        <div aria-live="polite">{results && Object.entries(results).map(([id, r]) => <p key={id} className={r.ok ? "small" : "small text-warn"}>
          <strong>{id === "model" ? "模型" : "搜索"}</strong> · {r.message}</p>)}</div>
        {passed && <div className="setup-success"><strong>连接就绪</strong><p className="muted small">输入公司名，或上传一份 BP。确认公司后开始首轮整理。</p>
          <Link className="btn btn-primary" to="/new">添加第一个项目 →</Link></div>}
        {attempted && !busy && !passed && <p className="muted small">未自动重试。请先核对服务商记录；可修改配置，或到完整设置处理连接问题。</p>}
      </div>
    </>}
    </>}
  </Page>;
}

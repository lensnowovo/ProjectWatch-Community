import { Skeleton } from "../frame";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { api, type ExecutorView, type NotifyView, type SettingsView, type UpdateView } from "../api";
import { Page, ThemeToggle, useToast } from "../motion";
import { ErrorNote, Spinner, useAsync } from "../ui";
import { Seg } from "../widgets";

const SEARCH = [
  { id: "deepseek", label: "DeepSeek Search", hint: "自动模式优先 · 搜索与正文读取" },
  { id: "bocha", label: "博查搜索", hint: "中文搜索 · 国内直连" },
  { id: "tavily", label: "Tavily", hint: "英文与全球信息" },
  { id: "brave", label: "Brave Search", hint: "需海外网络" },
];
const NOTIFY_LABEL: Record<NotifyView["kind"], string> = { none: "不推送", wecom: "企业微信群机器人", feishu: "飞书群机器人", dingtalk: "钉钉群机器人" };
const WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
const HOURS_LABEL: Record<number, string> = { 6: "每 6 小时", 12: "每 12 小时", 24: "每天", 72: "每 3 天", 168: "每周" };
const TEST_LABEL: Record<string, string> = { model: "AI 模型", news: "免费新闻源（必应）", google_news: "Google News", search: "搜索服务", search_deepseek: "DeepSeek Search", search_bocha: "博查搜索", search_tavily: "Tavily", search_brave: "Brave Search" };

type Tests = Record<string, { ok: boolean | null; message: string }>;

export function UpdateSummary({ value }: { value: UpdateView }) {
  return <div aria-live="polite">
    <p><strong>软件版本 {value.running_version}</strong>
      {value.target_version && <span className="muted small"> · 稳定版 {value.target_version}</span>}</p>
    <p className={value.state === "agent_required" || value.state === "check_failed" ? "text-warn small" : "muted small"}>{value.message}</p>
    {value.current_commit && <p className="muted small">源码提交 {value.current_commit.slice(0, 10)} · 运行版本以启动环境为准</p>}
    {value.state === "available" && <details>
      <summary>停机升级指引</summary>
      <p className="muted small">结束运行中的任务并正常关闭实例。在源码目录使用原运行环境执行以下命令，替换数据目录与全新升级目录。会下载构建依赖、备份并在数据副本验证；成功后按回执使用新环境启动。</p>
      <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{`python -m project_watch.studio.updates apply --repo . --home "<现有数据目录>" --stage "<全新升级目录>" --current ${value.current_commit} --target ${value.target_commit} --confirm`}</pre>
    </details>}
    {value.state === "manual_install" && <p className="muted small">安装包升级：停机备份，在独立环境安装新版，先用数据副本验证。不要将新版直接覆盖到旧目录。</p>}
  </div>;
}

function SoftwareUpdates() {
  const [value, setValue] = useState<UpdateView | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    let active = true;
    api.updates().then(v => { if (active) setValue(v); }).catch(e => { if (active) setError(e); });
    return () => { active = false; };
  }, []);
  return <div className="card card-pad" style={{ marginBottom: 20 }}>
    {value ? <UpdateSummary value={value} /> : <p className="muted small">软件更新</p>}
    {error != null && <ErrorNote error={error} />}
    <div className="actions">
      <button className="btn btn-sm" disabled={busy} onClick={async () => {
        setBusy(true); setError(null); setCopied(false);
        try { setValue(await api.checkUpdates()); } catch (e) { setError(e); } finally { setBusy(false); }
      }}>{busy ? "检查中…" : "检查软件更新"}</button>
      {value && value.state !== "unchecked" && <button className="btn btn-sm" disabled={busy} onClick={async () => {
        setError(null);
        try { await navigator.clipboard.writeText(value.handoff); setCopied(true); }
        catch { setError(new Error("无法复制，请手动选择下方交接说明。")); }
      }}>{copied ? "已复制" : "复制 Agent 交接说明"}</button>}
    </div>
    {value && value.state !== "unchecked" && <details><summary className="muted small">交接说明</summary><pre style={{ whiteSpace: "pre-wrap" }}>{value.handoff}</pre></details>}
  </div>;
}
type SectionId = "model" | "search" | "data" | "notify" | "network";
type Form = {
  profile: string;
  model: { protocol: string; base_url: string; model: string; api_key: string; max_output_tokens: number };
  search: { providers: string[]; keys: Record<string, string>; mode: string; google_news: boolean };
  registry: { provider: string; api_key: string };
  commercial: { providers: string[]; keys: Record<string, string>; daily_limit: number; clear: string[]; model_use: boolean };
  notify: NotifyView;
  proxy: string;
  hours: number;
  overlap: number;
  baseline: number;
};

const SECTIONS: { id: SectionId | "overview" | "executor" | "interface" | "usage"; label: string }[] = [
  { id: "overview", label: "配置概况" },
  { id: "model", label: "AI 模型" },
  { id: "search", label: "新闻与搜索" },
  { id: "data", label: "工商与专业数据" },
  { id: "notify", label: "推送" },
  { id: "network", label: "更新与网络" },
  { id: "executor", label: "研究引擎" },
  { id: "interface", label: "界面" },
  { id: "usage", label: "用量" },
];

// Google News is opt-in: titles only, originals unreadable.
const SEARCH_DEFAULT: Form["search"] = { providers: [], keys: {}, mode: "auto", google_news: false };

// Saved values as an editable form; secrets always start empty (empty keeps the stored key).
function fromSaved(data: SettingsView): Form {
  return {
    profile: data.active_model_profile ?? 'default',
    model: { protocol: data.model.protocol, base_url: data.model.base_url, model: data.model.model, api_key: "",
      max_output_tokens: data.model.max_output_tokens ?? 1000000 },
    search: { ...SEARCH_DEFAULT, providers: data.search.providers ?? (data.search.provider === "none" ? [] : [data.search.provider]),
      mode: data.search.mode ?? "combined", google_news: data.search.google_news ?? SEARCH_DEFAULT.google_news },
    registry: { provider: data.registry.provider, api_key: "" },
    commercial: { providers: data.commercial?.providers ?? [], keys: {}, daily_limit: data.commercial?.daily_limit ?? 12, clear: [],
      model_use: data.commercial?.model_use ?? false },
    notify: data.notify ? { ...data.notify, webhook_url: "" } : { kind: "none", webhook_url: "", weekly: true, weekday: 1, hour: 9, instant_major: false },
    proxy: data.proxy_url,
    hours: data.refresh_hours,
    overlap: data.collection_overlap_days ?? 7,
    baseline: data.collection_baseline_days ?? 30,
  };
}

const sameSet = (a: string[], b: string[]) => a.length === b.length && a.every((x) => b.includes(x));
const typed = (keys: Record<string, string>) => Object.values(keys).some((v) => v.trim());
const endpoint = (url: string) => url.trim().replace(/\/+$/, "");
const hostLabel = (url: string) => { try { return new URL(url).hostname; } catch { return '待配置地址'; } };

function changedSections(form: Form, saved: Form): SectionId[] {
  const m = form.model, sm = saved.model;
  const s = form.search, ss = saved.search;
  const n = form.notify, sn = saved.notify;
  const changed: Record<SectionId, boolean> = {
    model: form.profile !== saved.profile || m.protocol !== sm.protocol || endpoint(m.base_url) !== endpoint(sm.base_url) || m.model !== sm.model
      || m.max_output_tokens !== sm.max_output_tokens || !!m.api_key,
    search: !sameSet(s.providers, ss.providers) || s.mode !== ss.mode || s.google_news !== ss.google_news || typed(s.keys),
    data: form.registry.provider !== saved.registry.provider || !!form.registry.api_key
      || !sameSet(form.commercial.providers, saved.commercial.providers) || form.commercial.daily_limit !== saved.commercial.daily_limit
      || form.commercial.model_use !== saved.commercial.model_use
      || typed(form.commercial.keys) || form.commercial.clear.length > 0,
    notify: n.kind !== sn.kind || n.weekly !== sn.weekly || n.weekday !== sn.weekday || n.hour !== sn.hour
      || n.instant_major !== sn.instant_major || !!n.webhook_url,
    network: form.proxy.trim() !== saved.proxy.trim() || form.hours !== saved.hours
      || form.overlap !== saved.overlap || form.baseline !== saved.baseline,
  };
  return (Object.keys(changed) as SectionId[]).filter((k) => changed[k]);
}

export default function SettingsPage({ onSaved }: { onSaved: (ready: boolean) => void }) {
  const { data, error, setData } = useAsync(() => api.settings(), []);
  const usage = useAsync(() => api.usage(), []);
  const [form, setForm] = useState<Form | null>(null);
  const [notifyResult, setNotifyResult] = useState<{ ok: boolean; message: string } | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<unknown>(null);
  const [testing, setTesting] = useState(false);
  const [tests, setTests] = useState<Tests | null>(null);
  const toast = useToast();

  useEffect(() => { if (data) setForm(fromSaved(data)); }, [data]);
  const saved = useMemo(() => (data ? fromSaved(data) : null), [data]);
  const dirty = form && saved ? changedSections(form, saved) : [];

  // Leaving or reloading the page with unsaved edits asks first.
  useEffect(() => {
    if (!dirty.length) return;
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty.length]);

  const header = <header className="settings-heading"><h1>设置</h1>
    <p>密钥本机保存，仅发送给对应服务商。修改后统一保存；连接测试会调用已配置的服务。</p></header>;
  if (!data || !form || !saved) return <Page className="page-workspace">{header}{error ? <ErrorNote error={error} /> : <Skeleton lines={10} />}</Page>;

  const set = <K extends keyof Form>(key: K, value: Form[K]) => setForm({ ...form, [key]: value });
  const save = async () => {
    if (!Number.isInteger(form.overlap) || form.overlap < 0 || form.overlap > 90
      || !Number.isInteger(form.baseline) || form.baseline < 1 || form.baseline > 365) {
      setSaveError(new Error("增量回看天数应为 0–90 的整数，完整范围回查间隔应为 1–365 的整数。"));
      return false;
    }
    setSaving(true);
    setSaveError(null);
    try {
      const { clear, ...commercial } = form.commercial;
      const next: SettingsView = await api.saveSettings({
        active_model_profile: form.profile,
        model: form.model, search: form.search, registry: form.registry, notify: form.notify,
        commercial: clear.length ? { ...commercial, clear_keys: clear } : commercial,
        proxy_url: form.proxy, refresh_hours: form.hours,
        collection_overlap_days: form.overlap, collection_baseline_days: form.baseline,
      });
      setData(next);
      toast("设置已保存");
      onSaved(next.model.ready);
      return true;
    } catch (e) {
      setSaveError(e);
      return false;
    } finally {
      setSaving(false);
    }
  };
  const test = async () => {
    if (dirty.length && !(await save())) return;
    setSaveError(null);
    setTesting(true);
    setTests(null);
    try {
      setTests(await api.testSettings());
      usage.reload();
    } catch (e) {
      setSaveError(e);
    } finally {
      setTesting(false);
    }
  };
  const discard = () => { setForm(saved); setSaveError(null); };

  const status = overview(data, tests);
  const dirtyLabel = (id: SectionId) => SECTIONS.find((s) => s.id === id)!.label;

  return (
    <Page className="page-workspace">
      {header}
      <div className="settings-layout">
        <nav className="settings-nav" aria-label="设置分区">
          {SECTIONS.map((s) => {
            const row = status.find((r) => r.id === s.id);
            return <a key={s.id} href={`#settings-${s.id}`} onClick={(e) => {
              e.preventDefault();
              const target = document.getElementById(`settings-${s.id}`);
              if (target instanceof HTMLDetailsElement) target.open = true;
              target?.scrollIntoView({ behavior: "smooth", block: "start" });
            }}>
              <span>{s.label}</span>
              {dirty.includes(s.id as SectionId) ? <i className="settings-dot dirty" title="未保存" />
                : row && <i className={`settings-dot ${row.tone}`} title={row.state} />}
            </a>;
          })}
        </nav>

        <div className="settings-main">
          <details className="card card-pad settings-section settings-secondary" id="settings-overview">
            <summary><strong>配置概况</strong><span className="tag">{data.model.ready ? '模型已配置' : '模型待配置'}</span></summary>
            <div className="settings-secondary-body">
            <div className="card-head">
              <h2>配置概况</h2>
              <button className="btn btn-sm" disabled={saving || testing} onClick={test}>
                {testing ? <Spinner label="正在测试…" /> : dirty.length ? "保存并测试连接" : "测试连接"}
              </button>
            </div>
            <table className="table settings-overview">
              <thead><tr><th>服务</th><th>状态</th><th>当前设置</th></tr></thead>
              <tbody>
                {status.map((r) => (
                  <tr key={r.id}>
                    <th scope="row"><a href={`#settings-${r.id}`}>{r.label}</a>{r.required && <span className="muted small"> · 必需</span>}</th>
                    <td><span className={`tag ${r.tone === "ok" ? "tag-ok" : r.tone === "warn" ? "tag-warn" : ""}`}>{dirty.includes(r.id) ? "有未保存修改" : r.state}</span></td>
                    <td className="muted">{r.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="muted small">测试会向 AI 模型发送一条短消息，并对每个已启用的搜索服务各检索一次；不查询工商与专业数据。</p>
            </div>
          </details>

          <Section id="model" title="AI 模型" badge={<span className="tag tag-warn">必填</span>} dirty={dirty.includes("model")}
            state={data.model.ready ? "已配置" : "待配置"} ok={data.model.ready}>
            <div className="provider-current">
              <label className="field"><span>当前运转模型</span>
                <select value={form.profile} onChange={e => {
                  const profile = data.model_profiles?.find(p => p.id === e.target.value);
                  setForm({ ...form, profile: e.target.value, model: profile ? { ...profile, api_key: '', max_output_tokens: profile.max_output_tokens ?? 1000000 }
                    : { protocol: 'openai', base_url: '', model: '', api_key: '', max_output_tokens: 1000000 } });
                }}>
                  {!data.model_profiles?.length && <option value="default">尚未配置模型</option>}
                  {data.model_profiles?.map(p => <option value={p.id} key={p.id}>{p.model || '未命名模型'} · {hostLabel(p.base_url)}{p.ready ? '' : '（待配置）'}</option>)}
                  <option value="new">＋ 添加模型配置</option>
                </select>
              </label>
              <p className="muted small">选择后点击保存生效。不会中断正在执行的请求，也不会自动发起连接测试。</p>
            </div>
            <details className="provider-editor" key={form.profile} open={form.profile === 'new' || !data.model.ready ? true : undefined}>
              <summary>配置模型 <span className="muted small">服务商、密钥与高级参数</span></summary>
              <ModelFields form={form} data={data} onChange={(model) => set("model", model)} />
            </details>
            <TestRows tests={tests} keys={["model"]} />
          </Section>

          <Section id="search" title="新闻与搜索" badge={<span className="tag">可选</span>} dirty={dirty.includes("search")}
            state={data.search.ready ? "已配置搜索服务" : "仅免费来源"} ok={!!data.search.ready}>
            <div className="provider-current">
              <span className="muted small">当前搜索来源</span>
              <div className="provider-choices">{SEARCH.map(source => <label key={source.id} className={form.search.providers.includes(source.id) ? 'selected' : ''}>
                <input type="checkbox" checked={form.search.providers.includes(source.id)} onChange={e => set('search', { ...form.search,
                  providers: e.target.checked ? [...form.search.providers, source.id] : form.search.providers.filter(id => id !== source.id) })} />{source.label}
              </label>)}</div>
              <p className="muted small">{form.search.providers.length ? '保存后使用所选来源；可在下方配置各自的密钥。' : '未选择付费来源，使用免费新闻源。'}</p>
            </div>
            <details className="provider-editor"><summary>配置搜索 <span className="muted small">密钥、自动搭配与联合检索</span></summary>
            <p className="muted small">服务商分别计费。未启用时使用免费来源；相同检索缓存 1 小时，重启清空。</p>
            <p className="small"><a href="https://platform.deepseek.com/api_keys" target="_blank" rel="noopener noreferrer">获取 DeepSeek Key ↗</a>
              {" · "}<a href="https://open.bochaai.com/dashboard" target="_blank" rel="noopener noreferrer">获取博查 Key ↗</a>
              {" · "}<a href="https://app.tavily.com/home" target="_blank" rel="noopener noreferrer">获取 Tavily Key ↗</a></p>
            <div className="radio-list">
              {SEARCH.map((s) => {
                const on = form.search.providers.includes(s.id);
                const stored = data.search.keys?.[s.id] || (data.search.provider === s.id ? data.search.api_key : "");
                return <div key={s.id}>
                  <label className={`radio-row ${on ? "on" : ""}`}>
                    <input type="checkbox" checked={on} onChange={(e) => set("search", { ...form.search,
                      providers: e.target.checked ? [...form.search.providers, s.id] : form.search.providers.filter((p) => p !== s.id) })} />
                    <div><strong>{s.label}</strong><div className="muted small">{s.hint}{stored ? ` · 已保存 Key ${stored}` : ""}</div></div>
                  </label>
                  {on && <label className="field nested-field">
                    <span>{s.label} API Key</span>
                    <input type="password" value={form.search.keys[s.id] ?? ""} autoComplete="off"
                      placeholder={stored ? "本机已有 Key，留空保留" : "粘贴 API Key"}
                      onChange={(e) => set("search", { ...form.search, keys: { ...form.search.keys, [s.id]: e.target.value } })} />
                  </label>}
                </div>;
              })}
            </div>
            {form.search.providers.length > 1 && <label className="field">
              <span>搜索方式</span>
              <select value={form.search.mode} onChange={(e) => set("search", { ...form.search, mode: e.target.value })}>
                <option value="auto">自动搭配（推荐）</option>
                <option value="combined">联合检索</option>
              </select>
              <small className="muted">自动：已启用的 DeepSeek 优先，中文由博查、英文由 Tavily 补位。无结果或明确失败时换源；结果不明则停止。联合：调用全部服务，费用更高。连接测试各调用一次。</small>
            </label>}
            <label className="field checkbox-field">
              <span><input type="checkbox" checked={form.search.google_news} onChange={(e) => set("search", { ...form.search, google_news: e.target.checked })} /> 同时使用 Google News</span>
              <small className="muted">默认关闭。新闻默认用必应（原文直链）和已配置的搜索服务；Google News 只有标题，链接读不到正文。</small>
            </label>
            <TestRows tests={tests} keys={Object.keys(tests ?? {}).filter((k) => k !== "model")} />
            </details>
          </Section>

          <Section id="data" title="工商与专业数据" badge={<span className="tag">可选 · 付费</span>} dirty={dirty.includes("data")}
            state={data.registry.provider !== "none" || (data.commercial?.ready.length ?? 0) > 0 ? "已启用" : "未使用"}
            ok={data.registry.provider !== "none" || (data.commercial?.ready.length ?? 0) > 0}>
            <h3>工商数据</h3>
            <p className="muted small">优先用工商登记信息识别公司。</p>
            <div className="grid-2">
              <label className="field">
                <span>服务商</span>
                <select value={form.registry.provider} onChange={(e) => set("registry", { provider: e.target.value, api_key: "" })}>
                  <option value="none">不使用</option>
                  <option value="tianyancha">天眼查开放平台</option>
                </select>
              </label>
              {form.registry.provider !== "none" && <label className="field">
                <span>Token</span>
                <input type="password" value={form.registry.api_key} autoComplete="off"
                  placeholder={data.registry.api_key && data.registry.provider === form.registry.provider ? `已保存 ${data.registry.api_key}，留空不修改` : "粘贴 Token"}
                  onChange={(e) => set("registry", { ...form.registry, api_key: e.target.value })} />
              </label>}
            </div>

            <h3>专业数据来源 <span className="muted small">按服务商计费</span></h3>
            <p className="muted small">随项目更新，结果缓存一天。仅查询公开公司名；结果保存在本机。
              {form.commercial.model_use ? "已允许交给 AI 深度分析：融资记录会发送给上方配置的模型服务。" : "目前不发送给任何模型。"}</p>
            <div className="radio-list">
              {(data.commercial?.presets ?? []).map((source) => {
                const on = form.commercial.providers.includes(source.id);
                const stored = data.commercial?.keys[source.id];
                const clearing = form.commercial.clear.includes(source.id);
                return <div key={source.id}>
                  <label className={`radio-row ${on ? "on" : ""}`}>
                    <input type="checkbox" checked={on} onChange={(e) => set("commercial", { ...form.commercial,
                      providers: e.target.checked ? [...form.commercial.providers, source.id] : form.commercial.providers.filter((p) => p !== source.id) })} />
                    <div><strong>{source.label}</strong><div className="muted small">{source.description}</div></div>
                  </label>
                  {(on || stored) && <div className="field nested-field">
                    <span>{source.label} API Key</span>
                    {on && <input type="password" autoComplete="off" value={form.commercial.keys[source.id] ?? ""} aria-label={`${source.label} API Key`}
                      placeholder={stored && !clearing ? "本机已有 Key，留空保留" : "粘贴 API Key / appKey"}
                      onChange={(e) => set("commercial", { ...form.commercial, keys: { ...form.commercial.keys, [source.id]: e.target.value } })} />}
                    <small className="muted">{source.endpoint}
                      {stored && <> · {clearing ? "保存后移除已保存的 Key" : `已保存 ${stored}`}{" "}
                        <button type="button" className="link" onClick={() => set("commercial", { ...form.commercial,
                          clear: clearing ? form.commercial.clear.filter((p) => p !== source.id) : [...form.commercial.clear, source.id] })}>
                          {clearing ? "撤销移除" : "移除 Key"}</button></>}
                    </small>
                  </div>}
                </div>;
              })}
            </div>
            <label className="check check-explained">
              <input type="checkbox" checked={form.commercial.model_use}
                onChange={(e) => set("commercial", { ...form.commercial, model_use: e.target.checked })} />
              <span>交给 AI 深度分析<small className="muted">开启后，本公司与同行的融资记录会作为依据发送给模型服务，用于判断竞争位置与资本情况。请确认与数据服务商的协议允许这样使用。</small></span>
            </label>
            <label className="field"><span>每天新查询上限（所有项目合计）</span>
              <select value={form.commercial.daily_limit} onChange={(e) => set("commercial", { ...form.commercial, daily_limit: Number(e.target.value) })}>
                {[3, 6, 12, 24, 60].map((n) => <option key={n} value={n}>{n} 次</option>)}
              </select>
              <small className="muted">缓存读取不计入；失败与结果未确认的请求也计入。不是金额预算，账单以服务商为准。</small>
            </label>
            <p className="muted small">保存后到项目「公司动态」点击更新，可查看各家结果；连接测试不批量查询商业来源。</p>
          </Section>

          <Section id="notify" title="推送" badge={<span className="tag">可选</span>} dirty={dirty.includes("notify")}
            state={data.notify?.ready ? NOTIFY_LABEL[data.notify.kind] : "未推送"} ok={!!data.notify?.ready}>
            <p className="muted small">使用群机器人的 Webhook，推送周报与重大事件。</p>
            <div className="grid-2">
              <label className="field">
                <span>渠道</span>
                <select value={form.notify.kind} onChange={(e) => { set("notify", { ...form.notify, kind: e.target.value as NotifyView["kind"], webhook_url: "" }); setNotifyResult(null); }}>
                  {(Object.keys(NOTIFY_LABEL) as NotifyView["kind"][]).map((k) => <option key={k} value={k}>{NOTIFY_LABEL[k]}</option>)}
                </select>
              </label>
              {form.notify.kind !== "none" && <label className="field">
                <span>Webhook 地址</span>
                <input type="password" value={form.notify.webhook_url} autoComplete="off"
                  placeholder={data.notify?.webhook_url && data.notify.kind === form.notify.kind ? `已保存 ${data.notify.webhook_url}，留空不修改` : "https://…"}
                  onChange={(e) => set("notify", { ...form.notify, webhook_url: e.target.value })} />
              </label>}
            </div>
            {form.notify.kind !== "none" && <>
              <div className="notify-options">
                <label className="check">
                  <input type="checkbox" checked={form.notify.weekly} onChange={(e) => set("notify", { ...form.notify, weekly: e.target.checked })} />
                  每周推送组合动态
                </label>
                {form.notify.weekly && <div className="inline-fields">
                  <select aria-label="推送日" value={form.notify.weekday} onChange={(e) => set("notify", { ...form.notify, weekday: Number(e.target.value) })}>
                    {WEEKDAYS.map((d, i) => <option key={d} value={i + 1}>{d}</option>)}
                  </select>
                  <select aria-label="推送时间" value={form.notify.hour} onChange={(e) => set("notify", { ...form.notify, hour: Number(e.target.value) })}>
                    {Array.from({ length: 24 }, (_, h) => <option key={h} value={h}>{`${h}:00`}</option>)}
                  </select>
                </div>}
                <label className="check">
                  <input type="checkbox" checked={form.notify.instant_major} onChange={(e) => set("notify", { ...form.notify, instant_major: e.target.checked })} />
                  重大事件即时推送
                </label>
              </div>
              <div className="actions">
                {notifyResult && <span className={notifyResult.ok ? "text-ok small" : "text-warn small"}>{notifyResult.message}</span>}
                <button className="btn btn-sm" disabled={saving} onClick={async () => {
                  if (dirty.length && !(await save())) return;
                  try { setNotifyResult(await api.testNotify()); } catch (e) { setSaveError(e); }
                }}>{dirty.length ? "保存并发送测试消息" : "发送测试消息"}</button>
              </div>
              <p className="muted small">需保持运行；漏发周报下次启动补发。钉钉关键词设为 ProjectWatch。</p>
            </>}
          </Section>

          <Section id="network" title="更新与网络" dirty={dirty.includes("network")}
            state={HOURS_LABEL[data.refresh_hours] ?? `${data.refresh_hours} 小时`} ok>
            <SoftwareUpdates />
            <div className="grid-2">
              <label className="field">
                <span>外部信息更新频率</span>
                <select value={form.hours} onChange={(e) => set("hours", Number(e.target.value))}>
                  {Object.entries(HOURS_LABEL).map(([h, label]) => <option key={h} value={h}>{label}</option>)}
                </select>
                <small className="muted">活跃期最快每 6 小时，平静期放缓；较慢设置不加速。实际频率见“资料库 → 监控节奏”。</small>
              </label>
              <label className="field">
                <span>代理地址（可选）</span>
                <input value={form.proxy} placeholder="例如 http://127.0.0.1:7890" onChange={(e) => set("proxy", e.target.value)} />
                <small className="muted">留空使用系统代理；海外服务可能需要代理。</small>
              </label>
            </div>
            <details>
              <summary>增量采集范围</summary>
              <div className="grid-2">
                <label className="field">
                  <span>增量回看天数</span>
                  <input type="number" min={0} max={90} step={1} value={Number.isNaN(form.overlap) ? "" : form.overlap}
                    onChange={(e) => set("overlap", e.target.valueAsNumber)} />
                  <small className="muted">从各查询上次成功的窗口向前回看，补充迟到收录的信息。默认 7 天。</small>
                </label>
                <label className="field">
                  <span>完整范围回查间隔（天）</span>
                  <input type="number" min={1} max={365} step={1} value={Number.isNaN(form.baseline) ? "" : form.baseline}
                    onChange={(e) => set("baseline", e.target.valueAsNumber)} />
                  <small className="muted">定期按原查询范围回查，发现较早记录的变化。默认 30 天，仍受来源能力与调用预算限制。</small>
                </label>
              </div>
            </details>
          </Section>

          <ExecutorCard />

          <section className="card card-pad settings-section" id="settings-interface">
            <div className="card-head"><h2>界面</h2><span className="muted small">仅保存在本浏览器，立即生效</span></div>
            <dl className="settings-prefs">
              <div><dt>外观</dt><dd><ThemeToggle pill="theme-pill-settings" /></dd></div>
              <div><dt>项目组合默认视图</dt><dd><ProjectsView /></dd></div>
              <div><dt>旧版界面</dt><dd><a href="/classic">旧版界面 ↗</a> <span className="muted small">仅用于对照旧数据</span></dd></div>
            </dl>
          </section>

          <section className="card card-pad settings-section" id="settings-usage">
            <div className="card-head"><h2>用量（近 30 天）</h2><span className="muted small">本机记录，不是账单</span></div>
            {usage.error ? <ErrorNote error={usage.error} /> : !usage.data ? <Skeleton lines={3} /> : <>
              <div className="stats">
                <div><div className="stat">{usage.data.total.calls.toLocaleString()}</div><div className="muted small">模型请求次数</div></div>
                <div><div className="stat">{usage.data.total.input_tokens.toLocaleString()}</div><div className="muted small">已记录输入 tokens</div></div>
                <div><div className="stat">{usage.data.total.output_tokens.toLocaleString()}</div><div className="muted small">已记录输出 tokens</div></div>
              </div>
              <UsageBars days={usage.data.days ?? []} />
              <p className="muted small">完整用量 {usage.data.total.known_calls ?? 0} 次 · 用量不全 {usage.data.total.unknown_calls ?? 0} 次
                {(usage.data.total.legacy_calls ?? 0) > 0 && <> · 旧版汇总 {usage.data.total.legacy_calls} 次</>}</p>
              {((usage.data.total.cache_read_tokens ?? 0) + (usage.data.total.cache_creation_tokens ?? 0)) > 0 &&
                <p className="muted small">服务商另报缓存读取 {usage.data.total.cache_read_tokens.toLocaleString()} · 缓存创建 {usage.data.total.cache_creation_tokens.toLocaleString()} tokens</p>}
              <p className="muted small">未设用量上限；费用以服务商账单为准。</p>
            </>}
          </section>
        </div>
      </div>

      {(dirty.length > 0 || saving || saveError != null) && (
        <div className="settings-savebar" role="region" aria-label="保存设置">
          <div className="settings-savebar-inner">
            <span className="small">{dirty.length ? <>未保存：{dirty.map(dirtyLabel).join("、")}</> : saving ? "正在保存…" : ""}</span>
            <ErrorNote error={saveError} />
            <span className="spacer" />
            {dirty.length > 0 && <button className="btn btn-ghost btn-sm" disabled={saving} onClick={discard}>放弃修改</button>}
            <button className="btn btn-sm" disabled={saving || !dirty.length} onClick={save}>保存</button>
            <button className="btn btn-primary btn-sm" disabled={saving || testing} onClick={test}>
              {testing ? <Spinner label="正在测试…" /> : "保存并测试连接"}
            </button>
          </div>
        </div>
      )}
    </Page>
  );
}

function Section({ id, title, badge, state, ok, dirty, children }: {
  id: SectionId; title: string; badge?: ReactNode; state: string; ok: boolean; dirty: boolean; children: ReactNode;
}) {
  if (id !== 'model' && id !== 'search') return <details className="card card-pad settings-section settings-secondary" id={`settings-${id}`}>
    <summary><strong>{title}</strong> <span className={`tag ${dirty ? 'tag-warn' : ok ? 'tag-ok' : ''}`}>{dirty ? '未保存' : state}</span></summary>
    <div className="settings-secondary-body">{children}</div>
  </details>;
  return (
    <section className="card card-pad settings-section" id={`settings-${id}`}>
      <div className="card-head">
        <h2>{title} {badge}</h2>
        <span className={`tag ${dirty ? "tag-warn" : ok ? "tag-ok" : ""}`}>{dirty ? "未保存" : state}</span>
      </div>
      {children}
    </section>
  );
}

function ModelFields({ form, data, onChange }: { form: Form; data: SettingsView; onChange: (model: Form["model"]) => void }) {
  const model = form.model;
  const stored = data.model_profiles?.find(p => p.id === form.profile) ?? (form.profile !== 'new' ? data.model : undefined);
  const sameEndpoint = !!stored && model.protocol === stored.protocol && endpoint(model.base_url) === endpoint(stored.base_url);
  const preset = data.presets.find((p) => p.protocol === model.protocol && endpoint(p.base_url) === endpoint(model.base_url));
  const choose = (id: string) => {
    if (id === 'custom') { onChange({ ...model, base_url: '', model: '', api_key: '' }); return; }
    const p = data.presets.find(item => item.id === id);
    if (p) onChange({ ...model, protocol: p.protocol, base_url: p.base_url, model: p.model,
      api_key: p.protocol === model.protocol && endpoint(p.base_url) === endpoint(model.base_url) ? model.api_key : '' });
  };
  return <>
    <div className="grid-2 provider-brand">
      <label className="field"><span>服务商</span><select value={preset?.id.startsWith('glm') ? 'glm' : preset?.id ?? 'custom'} onChange={e => choose(e.target.value)}>
        {data.presets.filter(p => p.id !== 'glm-coding').map(p => <option key={p.id} value={p.id}>{p.id === 'glm' ? 'GLM · 智谱' : p.label}</option>)}
        <option value="custom">自定义兼容服务</option>
      </select></label>
      {preset?.id.startsWith('glm') && <label className="field"><span>接入方式</span><select value={preset.id} onChange={e => choose(e.target.value)}>
        <option value="glm">标准 API · 后台监控</option><option value="glm-coding">Coding Plan · 需符合套餐适用范围</option>
      </select></label>}
    </div>
    {(preset?.id === 'kimi' || preset?.id === 'gemini') && <p className="note small">
      {preset.id === 'kimi' ? 'Kimi Code' : 'Gemini CLI / Code Assist'} 的订阅与标准 API 分开。当前后台研究使用标准 API；订阅型 Agent 接入尚未适配，不能在这里复用登录凭据或承诺抵扣套餐。</p>}
    {preset?.hint && <p className="muted small">{preset.hint}</p>}
    {!sameEndpoint && stored?.api_key && <p className="note small">更换服务地址后，原 Key 不会发给新服务；请填写新服务的 Key。</p>}
    <div className="grid-2">
      <label className="field">
        <span>接口地址</span>
        <input value={model.base_url} placeholder="https://api.deepseek.com/v1" onChange={(e) => onChange({ ...model, base_url: e.target.value })} />
      </label>
      <label className="field">
        <span>模型名称</span>
        <input value={model.model} placeholder="例如 glm-5.3 或 deepseek-flash" onChange={(e) => onChange({ ...model, model: e.target.value })} />
      </label>
      <label className="field">
        <span>接口类型</span>
        <select value={model.protocol} onChange={(e) => onChange({ ...model, protocol: e.target.value })}>
          <option value="openai">OpenAI 兼容（DeepSeek、GLM、Kimi、千问等）</option>
          <option value="anthropic">Anthropic（Claude）</option>
        </select>
      </label>
      <label className="field">
        <span>API Key</span>
        <input type="password" value={model.api_key} autoComplete="off"
          placeholder={sameEndpoint && stored?.api_key ? `已保存 ${stored.api_key}，留空保留` : "填入当前服务的 API Key"}
          onChange={(e) => onChange({ ...model, api_key: e.target.value })} />
      </label>
    </div>
    <details className="settings-advanced">
      <summary>高级：单次回复上限 <span className="muted small">当前 {model.max_output_tokens === 1000000 ? "1M" : `${model.max_output_tokens / 1000}K`} tokens</span></summary>
      <label className="field">
        <span>单次回复上限</span>
        <select value={model.max_output_tokens} onChange={(e) => onChange({ ...model, max_output_tokens: Number(e.target.value) })}>
          {[8000, 16000, 32000, 64000, 100000, 128000, 256000, 512000, 1000000].map((n) => (
            <option key={n} value={n}>{n === 1000000 ? "1M" : `${n / 1000}K`} tokens{n === 1000000 ? "（默认）" : ""}</option>
          ))}
        </select>
        <small className="muted">含部分模型的思考用量，超出服务商上限时下调。费用与套餐额度以服务商为准。</small>
      </label>
    </details>
  </>;
}

function TestRows({ tests, keys }: { tests: Tests | null; keys: string[] }) {
  const rows = keys.filter((k) => tests?.[k]);
  if (!tests || !rows.length) return null;
  return (
    <div className="tests" aria-live="polite">
      {rows.map((k) => {
        const v = tests[k];
        return <div key={k} className="test-row">
          <span className={`test-state ${v.ok === true ? "ok" : v.ok === false ? "bad" : ""}`}>{v.ok === true ? "✓" : v.ok === false ? "✕" : "–"}</span>
          <strong>{TEST_LABEL[k] ?? k}</strong>
          <span className="muted">{v.message}</span>
        </div>;
      })}
    </div>
  );
}

function ProjectsView() {
  const [view, setView] = useState<"table" | "cards">(() => {
    try { return localStorage.getItem("pw-projects-view") === "cards" ? "cards" : "table"; } catch { return "table"; }
  });
  const change = (v: "table" | "cards") => {
    setView(v);
    try { localStorage.setItem("pw-projects-view", v); } catch { /* per-viewer preference only */ }
  };
  return <Seg id="settings-projects-view" value={view} onChange={change} options={[{ value: "table", label: "表格" }, { value: "cards", label: "卡片" }]} />;
}

/** Recorded model requests per day; days without records are drawn empty, not invented. */
function UsageBars({ days }: { days: { day: string; calls: number }[] }) {
  const byDay = new Map(days.map((d) => [d.day, d.calls]));
  const today = new Date();
  const series = Array.from({ length: 30 }, (_, i) => {
    const d = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 29 + i);
    const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    return { key, label: `${d.getMonth() + 1}/${d.getDate()}`, calls: byDay.get(key) ?? 0 };
  });
  if (!series.some((s) => s.calls)) return <p className="muted small">近 30 天没有记录到模型请求。</p>;
  const max = Math.max(...series.map((s) => s.calls));
  const w = 600, h = 80, slot = w / series.length, bw = slot - 4;
  return (
    <figure className="usage-bars">
      <svg viewBox={`0 0 ${w} ${h + 16}`} role="img"
        aria-label={`模型请求次数，按日：${series.filter((s) => s.calls).map((s) => `${s.label} ${s.calls} 次`).join("，")}；其余日期无记录`}>
        <line x1="0" x2={w} y1={h} y2={h} className="track-axis" />
        {series.map((s, i) => {
          const bh = s.calls ? Math.max(3, (s.calls / max) * (h - 6)) : 0;
          const x = i * slot + 2;
          return <g key={s.key}>
            <rect x={x - 2} y={0} width={slot} height={h} className="track-hit"><title>{s.calls ? `${s.key}：${s.calls} 次模型请求` : `${s.key}：无记录`}</title></rect>
            {bh > 0 && <path className="track-bar" d={`M${x},${h} v${-(bh - 4)} q0,-4 4,-4 h${bw - 8} q4,0 4,4 v${bh - 4} z`} />}
            {((i % 7 === 0 && i < series.length - 4) || i === series.length - 1) && <text x={x + bw / 2} y={h + 13} className="track-tick">{s.label}</text>}
          </g>;
        })}
      </svg>
      <figcaption className="muted small">模型请求次数 · 按日（悬停查看当日次数）</figcaption>
    </figure>
  );
}

type Row = { id: SectionId; label: string; state: string; detail: string; tone: "ok" | "warn" | "off"; required?: boolean };

function overview(data: SettingsView, tests: Tests | null): Row[] {
  const tested = (key: string) => tests?.[key];
  const searchFailed = Object.entries(tests ?? {}).filter(([k, v]) => k.startsWith("search_") && v.ok === false);
  const commercialReady = (data.commercial?.ready ?? []).map((id) => data.commercial?.presets.find((p) => p.id === id)?.label ?? id);
  const providers = (data.search.providers ?? []).map((id) => SEARCH.find((s) => s.id === id)?.label ?? id);
  const modelTest = tested("model");
  return [
    { id: "model", label: "AI 模型", required: true,
      state: modelTest ? (modelTest.ok ? "连接正常" : "连接失败") : data.model.ready ? "已配置" : "待配置",
      tone: modelTest ? (modelTest.ok ? "ok" : "warn") : data.model.ready ? "ok" : "warn",
      detail: data.model.ready ? `${data.model.model} · ${endpoint(data.model.base_url).replace(/^https:\/\//, "")}` : "未配置时只收集公开新闻，不分析资料" },
    { id: "search", label: "新闻与搜索",
      state: searchFailed.length ? "部分失败" : data.search.ready ? "已配置" : "仅免费来源",
      tone: searchFailed.length ? "warn" : data.search.ready ? "ok" : "off",
      detail: [providers.length ? providers.join("、") : "必应新闻", data.search.google_news ? "Google News" : ""].filter(Boolean).join(" · ") },
    { id: "data", label: "工商与专业数据",
      state: data.registry.provider !== "none" || commercialReady.length ? "已启用" : "未使用",
      tone: data.registry.provider !== "none" || commercialReady.length ? "ok" : "off",
      detail: [data.registry.provider === "tianyancha" ? "天眼查" : "", ...commercialReady].filter(Boolean).join("、")
        + (commercialReady.length ? ` · 每天最多 ${data.commercial?.daily_limit} 次新查询` : "") || "—" },
    { id: "notify", label: "推送",
      state: data.notify?.ready ? "已配置" : "未推送", tone: data.notify?.ready ? "ok" : "off",
      detail: data.notify?.ready ? [NOTIFY_LABEL[data.notify.kind],
        data.notify.weekly ? `${WEEKDAYS[data.notify.weekday - 1]} ${data.notify.hour}:00 周报` : "",
        data.notify.instant_major ? "重大事件即时" : ""].filter(Boolean).join(" · ") : "—" },
    { id: "network", label: "更新与网络", state: HOURS_LABEL[data.refresh_hours] ?? `${data.refresh_hours} 小时`, tone: "ok",
      detail: data.proxy_url ? `代理 ${data.proxy_url}` : "使用系统代理" },
  ];
}

const EXECUTOR_SOURCE: Record<ExecutorView["source"], string> = {
  bundled: "安装包自带", custom: "自定义路径", environment: "环境变量", setting: "设置", default: "默认",
  upgrade_preserved: "沿用升级前的执行方式",
};

/** The public-research engine. Saved here, applied on the next start; an unavailable DSH never falls back silently. */
export function ExecutorCard() {
  const { data, error, setData } = useAsync(() => api.executor(), []);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<unknown>(null);
  const running = data ? data.executor !== "classic" : false;
  const wanted = data?.saved_enabled ?? running;
  const pending = !!data && data.saved_enabled !== null && data.saved_enabled !== running;
  const change = async (enabled: boolean) => {
    setSaving(true); setSaveError(null);
    try { setData(await api.saveExecutor(enabled)); } catch (e) { setSaveError(e); } finally { setSaving(false); }
  };
  const state = !data ? null : data.executor === "dsh" ? <span className="tag tag-ok">DSH 已启用</span>
    : data.executor === "unavailable" ? <span className="tag tag-warn">不可用</span> : <span className="tag">经典流程</span>;
  return <section className="card card-pad settings-section" id="settings-executor">
    <div className="card-head"><h2>研究引擎</h2>{state}</div>
    {error ? <ErrorNote error={error} /> : !data ? <Skeleton lines={2} /> : <>
      <p className="muted small">
        {data.executor === "dsh" ? "由 DSH 决定检索、阅读和补全的顺序；模型与搜索请求仍经过本机网关，逐次记录用量与回执。"
          : data.executor === "unavailable" ? `${data.reason_text ?? "DSH 无法启动。"} 自动研究已暂停，已有结果保留；不会改用经典流程。`
          : "按固定顺序采集、阅读和整理公开信息。"}
      </p>
      <dl className="settings-prefs">
        <div><dt>当前使用</dt><dd>{data.executor === "classic" ? "经典流程"
          : data.executor === "unavailable" ? `DSH ${data.dsh_version} 未能启动，自动研究暂停中` : `DSH ${data.dsh_version}`}
          {data.executor !== "classic" && data.node_version && <span className="muted small"> · Node {data.node_version}</span>}
          <span className="muted small"> · 来源：{EXECUTOR_SOURCE[data.source] ?? data.source}</span></dd></div>
        <div><dt>使用 DSH</dt><dd>{data.configurable
          ? <label className="settings-check"><input type="checkbox" checked={wanted} disabled={saving}
              onChange={(e) => change(e.target.checked)} /> <span>{wanted ? "开启（推荐）" : "关闭，使用经典流程"}</span></label>
          : <span className="muted small">当前以开发方式运行，由环境变量决定。</span>}</dd></div>
      </dl>
      {pending && <p className="small" role="status">已保存，重启 ProjectWatch 后生效；当前仍按原方式运行。</p>}
      <ErrorNote error={saveError} />
    </>}
  </section>;
}

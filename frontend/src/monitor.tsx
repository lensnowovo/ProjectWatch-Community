import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import { api, type FeedbackKind, type Importance, type Item, type MonitorState, type QuestionJudgement, type QuestionStatus, type Rule, type SiteChange, type SiteDiscovery, type SitePage, type Thread, type TrackedQuestion, type Watchpoint } from "./api";
import { EASE, Reveal, useToast } from "./motion";
import { ErrorNote, IMPORTANCE_LABEL, relTime, useAsync } from "./ui";

// ------------------------------------------------------------------ storylines

const THREAD_STATUS: Record<Thread["status"], string> = { open: "待核对", stalled: "暂无新线索", done: "待核对" };

/** Legacy model groupings are discovery leads, not verified business progress. */
function ThreadCard({ thread }: { thread: Thread }) {
  const [open, setOpen] = useState(false);
  return (
    <article className={`thread thread-${thread.status}`}>
      <header className="thread-head">
        <span className="thread-kind">{thread.kind}</span>
        <h4 className="thread-title" title={thread.title}>{thread.title}</h4>
        <span className={`thread-status st-${thread.status}`}>{THREAD_STATUS[thread.status]}</span>
      </header>
      <ol className="track" aria-label={`${thread.kind}报道线索`}>
        {thread.stages.map((stage) => {
          const step = thread.steps.filter((s) => s.stage === stage).slice(-1)[0];
          const state = step ? "now" : "later";
          return (
            <li key={stage} className={`track-stop ${state}`} title={step ? `${step.date} ${step.title}` : stage}>
              <span className="track-dot" />
              <span className="track-label">{stage}</span>
              {step && <span className="track-date num">{step.date.slice(5)}</span>}
            </li>
          );
        })}
      </ol>
      <div className="thread-foot">
        {thread.status === "stalled" && <span className="muted small">近期未收到相关报道</span>}
        {thread.status !== "done" && thread.next_step && <span className="small muted">下一步关注：<b>{thread.next_step}</b></span>}
        <span className="spacer" />
        <button className="link small" onClick={() => setOpen((v) => !v)}>{open ? "收起" : `${thread.steps.length} 条报道`}</button>
      </div>
      <AnimatePresence initial={false}>
        {open && (
          <motion.ul className="thread-steps" initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.24, ease: EASE }}>
            {thread.steps.map((s) => (
              <li key={s.event_id}>
                <span className="num muted">{s.date}</span><span className="tag">{s.stage}</span>
                {s.url ? <a href={s.url} target="_blank" rel="noreferrer noopener">{s.title}</a> : <span>{s.title}</span>}
              </li>
            ))}
          </motion.ul>
        )}
      </AnimatePresence>
    </article>
  );
}

export function Storylines({ threads }: { threads: Thread[] }) {
  const [all, setAll] = useState(false);
  const order = { open: 0, stalled: 1, done: 2 } as const;
  const sorted = [...threads].sort((a, b) => order[a.status] - order[b.status] || b.last_at.localeCompare(a.last_at));
  const shown = all ? sorted : sorted.slice(0, 3);
  return (
    <Reveal as="section" className="card card-pad storylines">
      <div className="card-head">
        <h3>事件线索</h3>
      </div>
      {threads.length === 0 ? (
        <p className="muted small">暂无可追踪的事件线索。</p>
      ) : (
        <>
          <div className="thread-grid">{shown.map((t) => <ThreadCard key={t.id} thread={t} />)}</div>
          {sorted.length > 3 && <button className="link small" onClick={() => setAll((v) => !v)}>{all ? "收起" : `查看全部 ${sorted.length} 条线索`}</button>}
        </>
      )}
    </Reveal>
  );
}

// ------------------------------------------------------------------ watch points

export function WatchBar({ projectId, watchpoints, active, onSelect, onChanged, suggestions = [] }: {
  projectId: string; watchpoints: Watchpoint[]; active: string | null;
  onSelect: (id: string | null) => void; onChanged: () => void;
  /** System-picked candidates; one click adds them. */
  suggestions?: string[];
}) {
  const [text, setText] = useState("");
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const toast = useToast();
  const add = async (value = text) => {
    if (value.trim().length < 2) return;
    setBusy(true); setError(null);
    try {
      const w = await api.addWatchpoint(projectId, value.trim());
      toast(`已添加关注点“${w.label}”，正在检索`);
      setText(""); setAdding(false); onChanged();
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  const toggleAlert = async (w: Watchpoint) => {
    await api.patchWatchpoint(w.id, !w.alert);
    toast(w.alert ? `“${w.label}”命中时不再推送` : `“${w.label}”命中时会推送提醒`, "info");
    onChanged();
  };
  const remove = async (w: Watchpoint) => {
    await api.removeWatchpoint(w.id);
    if (active === w.id) onSelect(null);
    onChanged();
  };
  return (
    <section className="watchbar">
      <div className="watchbar-head">
        <h4>我的关注点</h4>
      </div>
      <div className="watch-chips">
        {watchpoints.map((w) => (
          <span key={w.id} className={`watch-chip ${active === w.id ? "on" : ""}`} title={`${w.text}\n判断标准：${w.criteria}\n检索：${w.queries.join("、")}`}>
            <button className="watch-main" onClick={() => onSelect(active === w.id ? null : w.id)}>
              <span className="watch-mark" aria-hidden>◎</span>{w.label}
              <span className="count">{w.hits}</span>
            </button>
            <button className={`watch-bell ${w.alert ? "on" : ""}`} title={w.alert ? "命中时推送（点击关闭）" : "命中时不推送（点击开启）"}
              aria-label="推送开关" onClick={() => toggleAlert(w)}>{w.alert ? "推送" : "静默"}</button>
            <button className="watch-x" title="删除关注点" aria-label="删除" onClick={() => remove(w)}>×</button>
          </span>
        ))}
        {adding ? (
          <form className="watch-add" onSubmit={(e) => { e.preventDefault(); add(); }}>
            <input autoFocus value={text} maxLength={200} disabled={busy} onChange={(e) => setText(e.target.value)}
              placeholder="例如：下一轮融资、客户合作、人员变动" />
            <button className="btn btn-sm btn-primary" disabled={busy || text.trim().length < 2}>{busy ? "生成中…" : "添加"}</button>
            <button type="button" className="btn btn-sm btn-ghost" onClick={() => { setAdding(false); setText(""); }}>取消</button>
          </form>
        ) : watchpoints.length < 8 && (
          <button className="watch-new" onClick={() => setAdding(true)}>＋ 添加关注点</button>
        )}
      </div>
      {suggestions.length > 0 && watchpoints.length < 8 && (
        <div className="watch-suggest small">
          <span className="muted">系统建议关注：</span>
          {suggestions.map((s) => <button key={s} className="watch-sugg" disabled={busy} onClick={() => add(s)}>＋ {s}</button>)}
        </div>
      )}
      <ErrorNote error={error} />
    </section>
  );
}

// ------------------------------------------------------------------ feedback on a single event

const IMPORTANCE_ORDER: Importance[] = ["major", "notable", "routine"];

/** Corrections on one event. Each becomes either a visible rule or a remembered example. */
export function FeedbackMenu({ item, onChange }: { item: Item; onChange?: (item: Item) => void }) {
  const [open, setOpen] = useState(false);
  const [naming, setNaming] = useState(false);
  const [name, setName] = useState("");
  const wrap = useRef<HTMLDivElement>(null);
  const toast = useToast();
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (!wrap.current?.contains(e.target as Node)) { setOpen(false); setNaming(false); } };
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", close); document.removeEventListener("keydown", esc); };
  }, [open]);
  const send = async (kind: FeedbackKind, value = "") => {
    try {
      const r = await api.feedback(item.id, kind, value);
      toast(r.message);
      setOpen(false); setNaming(false); setName("");
      onChange?.(kind === "importance" ? { ...item, importance: value as Importance } : { ...item, status: "dismissed" });
    } catch (e) {
      toast(e instanceof Error ? e.message : "操作失败", "info");
    }
  };
  return (
    <div className="fb" ref={wrap}>
      <motion.button whileTap={{ scale: 0.85 }} className={`icon-btn ${open ? "on" : ""}`} title="不准确？告诉系统"
        aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((v) => !v)}>✕</motion.button>
      <AnimatePresence>
        {open && (
          <motion.div className="fb-menu" role="menu" initial={{ opacity: 0, y: -4, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -4 }} transition={{ duration: 0.16, ease: EASE }}>
            <div className="fb-title">这条不准确？</div>
            <button role="menuitem" onClick={() => send("irrelevant")}>不相关，隐藏<small>以后少推类似内容</small></button>
            {naming ? (
              <form className="fb-name" onSubmit={(e) => { e.preventDefault(); send("wrong_entity", name); }}>
                <input autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="被误认的公司名，如 光启技术" />
                <button className="btn btn-sm btn-primary" disabled={name.trim().length < 2}>排除</button>
              </form>
            ) : (
              <button role="menuitem" onClick={() => setNaming(true)}>说的是同名的另一家公司<small>以后排除这个名称</small></button>
            )}
            <button role="menuitem" onClick={() => send("bad_source")}>来源质量差<small>以后屏蔽 {item.source || "该来源"}</small></button>
            <div className="fb-sep">重要性应为</div>
            <div className="fb-imp">
              {IMPORTANCE_ORDER.map((level) => (
                <button key={level} className={item.importance === level ? "on" : ""} disabled={item.importance === level}
                  onClick={() => send("importance", level)}>{IMPORTANCE_LABEL[level]}</button>
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

// ------------------------------------------------------------------ official website

const PAGE_STATUS: Record<SitePage["status"], [string, string]> = {
  pending: ["等待首次读取", ""], ok: ["正常", "tag-ok"], unreadable: ["无法读取", "tag-warn"],
  robots_disallowed: ["网站不允许自动读取", "tag-warn"], error: ["读取失败", "tag-warn"],
};

function Diff({ change }: { change: SiteChange }) {
  const [more, setMore] = useState(false);
  const lines = [...change.added.map((t) => ["+", t] as const), ...change.removed.map((t) => ["-", t] as const)];
  const shown = more ? lines : lines.slice(0, 6);
  return (
    <div className="diff">
      {shown.map(([sign, text], i) => (
        <div key={i} className={`diff-line ${sign === "+" ? "diff-add" : "diff-del"}`}>
          <span className="diff-sign" aria-label={sign === "+" ? "新增" : "删除"}>{sign === "+" ? "+" : "−"}</span><span>{text}</span>
        </div>
      ))}
      {lines.length > 6 && <button className="link small" onClick={() => setMore((v) => !v)}>{more ? "收起" : `还有 ${lines.length - 6} 行`}</button>}
    </div>
  );
}

const REJECT_REASON: Record<string, string> = { robots: "禁止程序访问", no_name: "首页没有公司名", unreadable: "无法读取", refused: "拒绝访问" };
const host = (url: string) => url.replace(/^https?:\/\//, "").replace(/\/$/, "");

/** Where the watched site came from; without one, what the round did to find it. */
function SiteOrigin({ website, pages, found }: { website?: string | null; pages: SitePage[]; found?: SiteDiscovery }) {
  const status = found?.status;
  if (website) return pages.length ? null : <p className="muted small">官网：{host(website)}。下次更新时读取首页并找出新闻、产品、团队等页面。</p>;
  if (status === "found" && pages.some((p) => p.url === found?.url)) {
    return <p className="site-origin small">官网为系统自动找到：<strong>{host(found!.url!)}</strong>
      <span className="muted">（首页出现“{found!.matched}”）。不是这家公司的话，移除“首页”即可，之后不再自动寻找。</span></p>;
  }
  if (pages.length) return null;
  const when = found?.checked_at ? `${relTime(found.checked_at)}，` : "";
  const why = (url: string) => REJECT_REASON[found?.rejected?.[url] ?? ""] ?? "未确认";
  const blocked = Object.entries(found?.rejected ?? {}).filter(([, r]) => r === "robots").map(([u]) => host(u));
  const text = status === "not_found"
    ? `${when}没有找到能确认的官网${found?.tried?.length ? `（看过 ${found.tried.map((u) => `${host(u)} ${why(u)}`).join("、")}）` : ""}，7 天后再试。`
      + (blocked.length ? `${blocked.join("、")} 可能是官网，但网站规则禁止程序访问，因此无法监测。` : "")
    : status === "no_search" ? "未配置检索服务，无法自动寻找官网。"
    : status === "search_failed" ? `${when}检索服务暂时不可用，下次更新时再找官网。`
    : status === "dismissed" ? "已移除自动找到的官网，不再自动寻找。"
    : "暂无官网地址。首轮更新时会自动搜索并确认公司官网（首页需出现公司名）。";
  return <p className="muted small">{text}{status && status !== "found" ? "知道官网的话直接在下面添加。" : ""}</p>;
}

export function SiteWatchCard({ projectId, website, state, onChanged }: {
  projectId: string; website?: string | null; state: MonitorState; onChanged: () => void;
}) {
  const [url, setUrl] = useState("");
  const [error, setError] = useState<unknown>(null);
  const toast = useToast();
  const add = async () => {
    setError(null);
    try { await api.addPage(projectId, url); setUrl(""); toast("已加入官网监测，下次更新时读取基线"); onChanged(); }
    catch (e) { setError(e); }
  };
  const remove = async (p: SitePage) => { await api.removePage(p.id); onChanged(); };
  return (
    <Reveal as="section" className="card card-pad sitewatch">
      <div className="card-head">
        <h3>官网监测</h3>
        <span className="muted small">每日最多一次 · 仅比较可见文字</span>
      </div>
      <SiteOrigin website={website} pages={state.pages} found={state.site_discovery} />
      {state.pages.length === 0 ? null : (
        <ul className="site-pages">
          {state.pages.map((p) => {
            const [label, tone] = PAGE_STATUS[p.status] ?? [p.status, ""];
            return (
              <li key={p.id}>
                <div className="site-page-main">
                  <strong>{p.label}</strong>
                  <a className="muted small mono" href={p.url} target="_blank" rel="noreferrer noopener">{p.url.replace(/^https?:\/\//, "")}</a>
                </div>
                <span className={`tag ${tone}`} title={p.error}>{label}</span>
                <span className="muted small site-when">{p.last_changed ? `变化于 ${relTime(p.last_changed)}` : p.last_checked ? `检查于 ${relTime(p.last_checked)}` : "—"}</span>
                <button className="icon-btn" title="停止监测这个页面" aria-label="移除" onClick={() => remove(p)}>×</button>
              </li>
            );
          })}
        </ul>
      )}
      <form className="inline-add" onSubmit={(e) => { e.preventDefault(); add(); }}>
        <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="官网或页面链接" aria-label="监测页面链接" />
        <button className="btn btn-sm" disabled={url.trim().length < 4}>添加</button>
      </form>
      <ErrorNote error={error} />
      {state.changes.length > 0 && (
        <>
          <h4>最近的变化</h4>
          <ul className="site-changes">
            {state.changes.slice(0, 8).map((c) => (
              <li key={c.id}>
                <div className="site-change-head">
                  <strong>{c.label}</strong>
                  <span className="muted small">{relTime(c.detected_at)} · 新增 {c.added.length} · 删除 {c.removed.length}</span>
                  {c.importance && c.importance !== "routine" && <span className={`imp imp-${c.importance}`}>{IMPORTANCE_LABEL[c.importance]}</span>}
                </div>
                {c.impact && <p className="item-impact"><span>对项目的意义</span>{c.impact}</p>}
                <Diff change={c} />
              </li>
            ))}
          </ul>
        </>
      )}
      <p className="muted small">遵守网站访问规则；登录或脚本页面可能无法读取。</p>
    </Reveal>
  );
}

// ------------------------------------------------------------------ rules

const RULE_LABEL: Record<Rule["kind"], string> = { exclude_entity: "排除同名主体", block_source: "屏蔽来源", exclude_term: "排除关键词" };

export function RulesCard({ projectId, rules, onChanged }: { projectId: string; rules: Rule[]; onChanged: () => void }) {
  const [kind, setKind] = useState<Rule["kind"]>("exclude_term");
  const [value, setValue] = useState("");
  const [error, setError] = useState<unknown>(null);
  const add = async () => {
    setError(null);
    try { await api.addRule(projectId, kind, value); setValue(""); onChanged(); } catch (e) { setError(e); }
  };
  return (
    <section className="card card-pad rules">
      <div className="card-head"><h3>过滤规则</h3></div>
      {rules.length === 0 ? (
        <p className="muted small">暂无规则。动态上的 ✕ 可排除同名公司或屏蔽来源。</p>
      ) : (
        <ul className="rule-list">
          {rules.map((r) => (
            <li key={r.id}>
              <span className="rule-kind">{RULE_LABEL[r.kind]}</span>
              <span className="rule-value">{r.value}</span>
              <span className="muted small num" title={r.note}>已拦截 {r.hits}</span>
              <button className="icon-btn" title="删除规则" aria-label="删除" onClick={() => api.removeRule(r.id).then(onChanged)}>×</button>
            </li>
          ))}
        </ul>
      )}
      <form className="inline-add" onSubmit={(e) => { e.preventDefault(); add(); }}>
        <select value={kind} onChange={(e) => setKind(e.target.value as Rule["kind"])}>
          {(Object.keys(RULE_LABEL) as Rule["kind"][]).map((k) => <option key={k} value={k}>{RULE_LABEL[k]}</option>)}
        </select>
        <input value={value} onChange={(e) => setValue(e.target.value)} placeholder={kind === "block_source" ? "域名，如 example.com" : "名称或词语"} />
        <button className="btn btn-sm" disabled={value.trim().length < 2}>添加</button>
      </form>
      <ErrorNote error={error} />
    </section>
  );
}

// ------------------------------------------------------------------ tracked questions (M2)

const ACTOR_LABEL = { system: "系统判断", human: "你的判断", agent: "外部助手" } as const;
const STATUS_OPTIONS: { value: QuestionStatus; label: string }[] = [
  { value: "progress", label: "有进展" }, { value: "setback", label: "出现反面信息" },
  { value: "unchanged", label: "无新进展" }, { value: "insufficient", label: "证据不足" },
];

function Judgement({ row, events }: { row: QuestionJudgement; events: TrackedQuestion["events"] }) {
  return (
    <div className="q-judgement">
      <div className="q-judgement-head">
        <span className="q-actor">{ACTOR_LABEL[row.actor]}</span>
        <span className={`q-status q-${row.status}`}>{row.status_label}</span>
        <span className="muted small">{relTime(row.created_at)}</span>
      </div>
      {row.note && <p>{row.note}</p>}
      {row.event_ids.length > 0 && (
        <ul className="q-events small">
          {row.event_ids.filter((e) => events[e]).map((e) => (
            <li key={e}><span className="muted num">{(events[e].event_at ?? "").slice(0, 10)}</span> {events[e].title}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function QuestionRow({ projectId, q, onChanged }: { projectId: string; q: TrackedQuestion; onChanged: () => void }) {
  const [editing, setEditing] = useState(false);
  const [status, setStatus] = useState<QuestionStatus>(q.latest.human?.status ?? "progress");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [history, setHistory] = useState<QuestionJudgement[] | null>(null);
  const [open, setOpen] = useState(false);
  const save = async () => {
    setBusy(true); setError(null);
    try { await api.judgeQuestion(projectId, q.id, { status, note: note.trim() }); setEditing(false); setNote(""); onChanged(); }
    catch (e) { setError(e); } finally { setBusy(false); }
  };
  const toggleHistory = async () => {
    if (history) { setHistory(null); return; }
    try { setHistory(await api.questionHistory(projectId, q.id)); } catch (e) { setError(e); }
  };
  const actors = (["system", "agent", "human"] as const).filter((a) => q.latest[a]);
  // One scannable line per question: the latest system reading (or the only available one).
  const primary = q.latest.system ?? q.latest.human ?? q.latest.agent;
  return (
    <li className={`q-row ${open ? "open" : ""}`}>
      <button type="button" className="q-summary" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <span className="q-text">{q.text}</span>
        <span className={`q-status ${primary ? `q-${primary.status}` : "q-none"}`}>{primary ? primary.status_label : ""}</span>
        <span className="q-note">{primary?.note || ""}</span>
        <span className="q-meta muted small">{primary ? relTime(primary.created_at) : ""}<i aria-hidden="true">›</i></span>
      </button>
      {open && <div className="q-detail">
        <p className="q-origin small muted">{q.origin === "ai" ? "AI 建议" : "你添加"} · 命中 {q.hits}</p>
        {actors.length === 0 && <p className="muted small">还没有判断：等命中新的相关信息后由系统给出，或你先写一条。</p>}
        {actors.map((a) => <Judgement key={a} row={q.latest[a]!} events={q.events} />)}
        {editing ? (
          <form className="q-form" onSubmit={(e) => { e.preventDefault(); save(); }}>
            <select value={status} onChange={(e) => setStatus(e.target.value as QuestionStatus)} disabled={busy} aria-label="判断">
              {STATUS_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
            <input value={note} maxLength={500} disabled={busy} onChange={(e) => setNote(e.target.value)} placeholder="一句话说明（可选）" />
            <button className="btn btn-sm btn-primary" disabled={busy}>{busy ? "保存中…" : "保存"}</button>
            <button type="button" className="btn btn-sm btn-ghost" onClick={() => setEditing(false)}>取消</button>
          </form>
        ) : (
          <div className="q-actions small">
            <button className="link" onClick={() => setEditing(true)}>{q.latest.human ? "更新你的判断" : "写下你的判断"}</button>
            {q.history_count > actors.length && <button className="link" onClick={toggleHistory}>{history ? "收起历史" : `历史 ${q.history_count} 条`}</button>}
          </div>
        )}
        {history && (
          <ol className="q-history small">
            {history.map((h) => <li key={h.id}><span className="muted">{relTime(h.created_at)} · {ACTOR_LABEL[h.actor]}</span> {h.status_label}{h.note ? `：${h.note}` : ""}</li>)}
          </ol>
        )}
      </div>}
      <ErrorNote error={error} />
    </li>
  );
}

export function QuestionsCard({ projectId, revision }: { projectId: string; revision: string }) {
  const { data, reload } = useAsync(() => api.questions(projectId), [projectId, revision]);
  if (!data || data.questions.length === 0) return null;
  return (
    <Reveal as="section" className="card card-pad questions-card">
      <div className="card-head">
        <h3>跟踪问题</h3>
        <span className="muted small">点开查看依据与你的判断 · 系统判断只依据已引用的事件；不会覆盖你的判断</span>
      </div>
      <ul className="q-list">{data.questions.map((q) => <QuestionRow key={q.id} projectId={projectId} q={q} onChanged={reload} />)}</ul>
    </Reveal>
  );
}

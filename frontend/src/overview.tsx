import { QuestionsCard } from "./monitor";
import { PositionLine } from "./race";
import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { Block, Columns, Figure, Reference, Skeleton } from "./frame";
import { api, type Item, type KeyNumbers, type ProjectDetail, type PublicFact, type Ref } from "./api";
import { periodLabel } from "./metrics";
import { Reveal } from "./motion";
import { EvidenceGaps } from "./judgment";
import { QuestionAnalysis } from "./questionAnalysis";
import { EventContextDrawer } from "./eventContext";
import MonitoringDetails from "./MonitoringDetails";
import { monitoringRevision } from "./workState";
import { CATEGORY_LABEL, Drawer, ErrorNote, IMPORTANCE_LABEL, RefChips, locatorLabel, TONE_LABEL, formatAmount, relTime, useAsync, useOpenSource } from "./ui";

export type Tab = "overview" | "brief" | "plan" | "ops" | "business" | "fin" | "risks" | "numbers" | "external" | "trials" | "papers" | "patents" | "competition" | "industry" | "documents";

/** Categories that are about the company itself; competitors, industry and policy have their own tabs. */
export const OWN_CATEGORIES = new Set(["company", "trial", "paper", "patent"]);

const CHANGE_LABEL: Record<string, string> = {
  actual_vs_plan: "实际对比计划", restated: "数字被改写", plan_revised: "预测调整", new_period: "新增数据",
  新进展: "新进展", 不一致: "前后不一致", 淡化: "不再提及", 新风险: "新风险",
};

function PublicRefs({ fact }: { fact: PublicFact }) {
  return <span className="public-refs">{fact.refs.map((r, i) => <a key={r.url} href={r.url} target="_blank" rel="noreferrer noopener" title={`${r.title}\n原文摘录：${r.quote}\n${r.published_at || "来源日期未知"}`}>{r.source || `来源 ${i + 1}`} ↗</a>)}</span>;
}

// ------------------------------------------------------------------ key numbers

function Tile({ label, period, value, unit, sub, tone, refs }: {
  label: string; period?: string; value: string; unit: string; sub?: ReactNode; tone?: string; refs?: Ref[];
}) {
  return (
    <div className={`kn ${tone ? `kn-${tone}` : ""}`}>
      <div className="kn-label">{label}{period && <span>{periodLabel(period)}</span>}</div>
      <div className="kn-value"><span className="num-display">{value}</span><span className="kn-unit">{unit}</span></div>
      <div className="kn-sub">{sub}{refs && refs.length > 0 && <RefChips refs={refs} />}</div>
    </div>
  );
}

export function KeyStrip({ numbers, empty }: { numbers?: KeyNumbers; empty?: ReactNode }) {
  if (!numbers || (!numbers.revenue && !numbers.net_profit && !numbers.cash)) {
    return <div className="key-strip key-strip-empty">{empty ?? "暂无财务数字。"}</div>;
  }
  const { revenue, revenue_growth: growth, net_profit: profit, cash, runway } = numbers;
  const rev = formatAmount(revenue?.value, revenue?.unit ?? "");
  const pro = formatAmount(profit?.value, profit?.unit ?? "");
  const csh = formatAmount(cash?.value, cash?.unit ?? "");
  return (
    <div className="key-strip">
      <Tile label="营业收入" period={revenue?.period} value={rev.value} unit={rev.unit} refs={revenue?.refs}
        sub={growth !== null ? <span className={growth >= 0 ? "up" : "down"}>同比 {growth >= 0 ? "+" : ""}{growth}%</span> : <span className="muted">暂无可比上年同期</span>} />
      <Tile label="净利润" period={profit?.period} value={pro.value} unit={pro.unit} refs={profit?.refs}
        tone={profit && profit.value < 0 ? "loss" : undefined} />
      <Tile label="账上现金" period={cash?.period} value={csh.value} unit={csh.unit} refs={cash?.refs} />
      <Tile label="现金跑道" value={runway?.months != null ? String(Math.floor(runway.months)) : runway?.status === "positive" ? "经营现金流非负" : "—"}
        unit={runway?.months != null ? "个月" : ""} tone={runway?.status === "short" ? "warn" : undefined}
        refs={runway?.refs} sub={<span className="muted" title={runway?.text}>{runway?.text || "缺少现金数据"}</span>} />
    </div>
  );
}

/** The four numbers every reader asks first, in the conclusion band of 当前判断 and 经营情况. */
export function KeyFigures({ numbers: k }: { numbers?: KeyNumbers }) {
  if (!k || !(k.revenue || k.net_profit || k.cash)) return null;
  const money = (v?: { value: number; unit: string } | null) => formatAmount(v?.value, v?.unit ?? "");
  return <>
    {k.revenue && <Figure label="营业收入" value={money(k.revenue).value} unit={money(k.revenue).unit}
      note={k.revenue_growth != null ? `${periodLabel(k.revenue.period ?? "")} · 同比 ${k.revenue_growth >= 0 ? "+" : ""}${k.revenue_growth}%` : periodLabel(k.revenue.period ?? "")} />}
    {k.net_profit && <Figure label="净利润" value={money(k.net_profit).value} unit={money(k.net_profit).unit}
      tone={k.net_profit.value < 0 ? "loss" : undefined} note={periodLabel(k.net_profit.period ?? "")} />}
    {k.cash && <Figure label="账上现金" value={money(k.cash).value} unit={money(k.cash).unit} note={periodLabel(k.cash.period ?? "")} />}
    {k.runway?.months != null ? <Figure label="现金跑道" value={String(Math.floor(k.runway.months))} unit="个月"
      tone={k.runway.status === "short" ? "warn" : undefined} note="按近期消耗估算" />
      : k.runway?.status === "positive" ? <Figure label="现金跑道" value="—" note="经营现金流非负" /> : null}
  </>;
}

// ------------------------------------------------------------------ changes

interface Change {
  key: string; origin: "资料" | "公开"; tag: string; text: string; meaning?: string; date?: string;
  importance?: Item["importance"]; refs?: Ref[]; url?: string; source?: string;
}

function collectChanges(detail: ProjectDetail, events: Item[]): Change[] {
  const doc = detail.doc_changes?.[0];
  const internal: Change[] = doc ? [...doc.texts, ...doc.metrics].map((c, i) => ({
    key: `doc-${i}`, origin: "资料", tag: CHANGE_LABEL[c.kind] ?? c.kind, text: c.text, refs: c.refs, date: doc.created_at,
  })) : [];
  const external: Change[] = events
    .filter((e) => OWN_CATEGORIES.has(e.category) && e.importance !== "routine")
    .map((e) => ({
      key: e.id, origin: "公开", tag: `${CATEGORY_LABEL[e.category]}·${IMPORTANCE_LABEL[e.importance]}`, text: e.summary || e.title,
      meaning: e.impact, date: e.published_at ?? e.collected_at, importance: e.importance, url: e.url,
      source: (e.source_count ?? 1) > 1 ? `${e.source} 等 ${e.source_count} 家` : e.source,
    }));
  const majors = external.filter((c) => c.importance === "major");
  const rest = external.filter((c) => c.importance !== "major");
  return [...majors, ...internal.slice(0, 4), ...rest, ...internal.slice(4)];
}

// ------------------------------------------------------------------ basic company information

type Line = { key: string; text: string; label?: string; refs?: Ref[]; fact?: PublicFact };

function sectionPoints(detail: ProjectDetail, pattern: RegExp): Line[] {
  return (detail.profile?.sections ?? []).filter((s) => pattern.test(s.title))
    .flatMap((s) => s.points.map((p, i) => ({ key: `${s.title}-${i}`, text: p.text, refs: p.refs })));
}

function publicLines(facts: PublicFact[]): Line[] {
  return facts.map((f, i) => ({ key: `pub-${f.kind}-${i}`, text: f.text, label: f.label, fact: f }));
}

function sourceDay(fact: PublicFact): string {
  const today = new Date().toISOString().slice(0, 10);
  const days = (fact.refs ?? []).map(r => (r.published_at ?? "").slice(0, 10))
    .filter(d => /^\d{4}-\d{2}-\d{2}$/.test(d) && !Number.isNaN(Date.parse(d))
      && new Date(d).toISOString().slice(0, 10) === d && d <= today).sort();
  return days[days.length - 1] ?? "";
}

const anchorId = (title: string) => `dossier-${title}`;

/** One topic as a standard card: title and count in the card head, the shared bullet list, sources at line end. */
function BasicRow({ title, lines, empty, limit = 3 }: { title: string; lines: Line[]; empty: string; limit?: number }) {
  const [all, setAll] = useState(false);
  const shown = all ? lines : lines.slice(0, limit);
  return (
    <section className="basic-row" id={anchorId(title)}>
      <div className="row-title"><h3>{title}</h3>{lines.length > 0 && <span>{lines.length} 条</span>}</div>
      <div className="basic-body">
        {lines.length === 0 ? <p className="muted small">{empty}</p> : (
          <ul className="basic-list">
            {shown.map((l) => (
              <li key={l.key} className={l.fact ? "is-public" : ""}>
                <span className="basic-text">{l.label && <strong>{l.label}</strong>}{l.text}</span>
                {l.fact && sourceDay(l.fact) && <span className="muted small"> 公开报道 · 来源日期 {sourceDay(l.fact)}</span>}
                {!l.fact && l.refs && l.refs.length > 0 && <> <RefChips refs={l.refs} compact /></>}
              </li>
            ))}
          </ul>
        )}
        {lines.length > limit && <button className="link small basic-more" onClick={() => setAll((v) => !v)}>{all ? "收起" : `展开另外 ${lines.length - limit} 条`}</button>}
      </div>
    </section>
  );
}

/** All sources behind the basic information, grouped by topic, in one drawer instead of on every line. */
function BasicSources({ rows }: { rows: { title: string; lines: Line[] }[] }) {
  const [open, setOpen] = useState(false);
  const openSource = useOpenSource();
  const cited = rows.map((r) => ({ ...r, lines: r.lines.filter((l) => l.fact?.refs.length || l.refs?.length) })).filter((r) => r.lines.length);
  if (!cited.length) return null;
  return (
    <>
      <button className="link basic-sources-btn" onClick={() => setOpen(true)}>查看来源</button>
      <Drawer open={open} onClose={() => setOpen(false)} eyebrow="来源" title="公司基本信息的出处"
        subtitle="公开报道未经公司确认；资料出处可打开原文。">
        {cited.map((r) => (
          <section key={r.title} className="src-group">
            <h4>{r.title}</h4>
            <ol className="src-list">
              {r.lines.map((l) => (
                <li key={l.key}>
                  <p>{l.label && <strong>{l.label} </strong>}{l.text}</p>
                  <div className="src-links">
                    {l.fact ? <>
                      <span className="basic-src">公开</span>
                      {l.fact.refs.map((x, i) => (
                        <a key={i} href={x.url} target="_blank" rel="noreferrer noopener" title={x.quote ? `原文摘录：${x.quote}` : x.title}>
                          {x.source || x.title || "报道"}{x.published_at ? ` · ${x.published_at.slice(0, 10)}` : ""} ↗</a>
                      ))}
                    </> : l.refs?.map((x, i) => (
                      x.url ? <a key={i} href={x.url} target="_blank" rel="noreferrer noopener">{x.source_name} ↗</a>
                      : <button key={i} className="link" onClick={() => { setOpen(false); openSource(x); }}>
                        {x.source_name} · {locatorLabel(x.locator)}</button>
                    ))}
                  </div>
                </li>
              ))}
            </ol>
          </section>
        ))}
      </Drawer>
    </>
  );
}

const REGISTRY_LABELS = /曾用名|注册资本|实缴资本|企业名称|公司名称|统一社会信用|工商登记|登记状态/;
const SECTION_RULES: { title: string; pattern: RegExp }[] = [
  { title: "业务与产品", pattern: /业务|产品|技术/ }, { title: "客户与市场", pattern: /客户|市场|订单/ },
  { title: "核心团队", pattern: /团队|人员|管理|创始/ }, { title: "融资与股东", pattern: /融资|股权|股东/ },
];

/** Public dossier facts split into business lines, capital and registry entries (labelled fields only). */
function dossierFacts(detail: ProjectDetail) {
  const facts = [...(detail.public_profile?.facts ?? [])].sort((a, b) => sourceDay(b).localeCompare(sourceDay(a)));
  const pick = (...kinds: PublicFact["kind"][]) => facts.filter((f) => kinds.includes(f.kind));
  // Public dossier schemas sometimes file an explicit equity statement under business.
  // Move only labelled fields; do not infer customers or investor roles from prose.
  const capital = facts.filter(f => f.kind === "financing" || /股权|股东/.test(f.label));
  const registry = pick("business").filter(f => REGISTRY_LABELS.test(f.label));
  const business = pick("business", "product").filter(f => !capital.includes(f) && !registry.includes(f))
    .sort((a, b) => Number(/经营范围/.test(a.label)) - Number(/经营范围/.test(b.label)));
  return { facts, pick, capital, registry, business };
}

/** What the company does, in one line: internal research first, then a manual description, then the freshest cited business line. */
export function companyIntro(detail: ProjectDetail): string {
  const { project, profile } = detail;
  const recentBusiness = dossierFacts(detail).business.find(f => f.kind === "business" && !/经营范围/.test(f.label) && sourceDay(f));
  const manualIntro = project.entity_source === "manual" ? project.description : "";
  return profile?.one_liner || manualIntro || recentBusiness?.text || project.description || "";
}

/** 公司简介: what the company does, in one line, with the fuller summary one click away. */
export function CompanyPortrait({ detail }: { detail: ProjectDetail }) {
  const { profile } = detail;
  const cutoff = (profile as { material_cutoff?: { date?: string } } | null)?.material_cutoff?.date;
  const source = profile?.origin ? `${profile.origin.producer} 整理 · 内部材料与公开来源` : profile ? `据 ${profile.documents ?? detail.documents.length} 份内部资料由 AI 整理`
    : detail.public_profile ? "据公开资料整理，未经公司确认" : "";
  const [more, setMore] = useState(false);
  const intro = companyIntro(detail);
  const summary = profile?.summary;
  return (
    <Reveal as="section" className="portrait" aria-label="公司简介">
      <div className="profile-kicker-row">
        <span className="profile-kicker">公司简介</span>
        {(cutoff || source) && <span className="muted small">{[cutoff && `资料截至 ${cutoff}`, source].filter(Boolean).join(" · ")}</span>}
      </div>
      {intro ? <p className="basic-intro portrait-intro">{intro}</p>
        : <p className="portrait-intro muted">还没有公司介绍。上传 BP 或年报，或补充公开档案。</p>}
      {summary && <div className="portrait-summary-wrap">
        <p className={`portrait-summary ${more ? "open" : ""}`}>{summary}</p>
        {summary.length > 140 && <button className="link small" onClick={() => setMore((v) => !v)}>{more ? "收起" : "阅读全文"}</button>}
      </div>}
    </Reveal>
  );
}

/** Milestones as one line in time: reported ones filled, planned ones hollow. */
function MilestoneLine({ detail }: { detail: ProjectDetail }) {
  const own = (detail.profile?.milestones ?? []).map((m, i) => ({ key: `doc-${i}`, date: m.date || "", planned: m.status === "planned", label: m.status === "planned" ? "计划" : "已完成", text: m.text, refs: m.refs as Ref[] | undefined, fact: undefined as PublicFact | undefined }));
  const pub = (detail.public_profile?.facts ?? []).filter((f) => f.kind === "milestone")
    .map((f, i) => ({ key: `pub-${i}`, date: f.date || "", planned: false, label: f.label || "公开报道", text: f.text, refs: undefined, fact: f }));
  const items = [...own, ...pub].sort((a, b) => Number(a.planned) - Number(b.planned) || (a.date || "9").localeCompare(b.date || "9")).slice(0, 10);
  if (!items.length) return null;
  return (
    <section className="basic-row dossier-milestones" id={anchorId("里程碑")}>
      <div className="row-title"><h3>里程碑</h3><span>● 已完成　○ 计划</span></div>
      <ol className="milestone-line">
        {items.map((m) => (
          <li key={m.key} className={m.planned ? "planned" : ""}>
            <i aria-hidden="true" />
            <time>{m.date || "日期未知"}</time>
            <strong>{m.label}</strong>
            <p>{m.text}</p>
            {m.refs && <RefChips refs={m.refs} />}{m.fact && <PublicRefs fact={m.fact} />}
          </li>
        ))}
      </ol>
    </section>
  );
}

/** 公司档案: business, customers, progress, team and financing — internal material first, cited public facts after. */
export function CompanyBasics({ detail, onRefreshDossier, refreshing, part = "all" }: {
  detail: ProjectDetail; onRefreshDossier: () => void; refreshing: boolean;
  /** "all": the classic stacked dossier; the others render one region for the tab frame. */
  part?: "all" | "highlights" | "main" | "side" | "milestones" | "sources";
}) {
  const { project, profile, public_profile: dossier } = detail;
  const { facts, pick, capital, registry: registryFacts, business: businessFacts } = dossierFacts(detail);
  const ruled = (title: string) => SECTION_RULES.find((r) => r.title === title)!.pattern;
  // Sections the rules do not cover (e.g. 经营进展) still appear, under their own titles.
  const other = (profile?.sections ?? []).filter((s) => s.points.length && !SECTION_RULES.some((r) => r.pattern.test(s.title)))
    .map((s) => ({ title: s.title, lines: s.points.map((p, i) => ({ key: `${s.title}-${i}`, text: p.text, refs: p.refs })), empty: "" }));
  const rows = [
    { title: "业务与产品", lines: [...sectionPoints(detail, ruled("业务与产品")), ...publicLines(businessFacts)], empty: "暂无业务资料。" },
    { title: "客户与市场", lines: sectionPoints(detail, ruled("客户与市场")), empty: "暂无客户资料。" },
    ...other,
    { title: "核心团队", lines: [...sectionPoints(detail, ruled("核心团队")), ...publicLines(pick("person"))], empty: "暂无团队资料。" },
    { title: "融资与股东", lines: [...sectionPoints(detail, ruled("融资与股东")), ...publicLines(capital)], empty: "暂无融资资料。" },
  ].filter((r) => r.lines.length || r.empty);
  const mainRows = rows.filter((r) => r.title !== "核心团队" && r.title !== "融资与股东");
  const sideRows = rows.filter((r) => r.title === "核心团队" || r.title === "融资与股东");
  const highlights = profile?.highlights ?? [];
  const nothing = !profile && !facts.length;
  if (part === "highlights") return highlights.length ? <ol className="profile-highlights" aria-label="亮点">
    {highlights.slice(0, 3).map((h, i) => <li key={i}><span className="hl-no">{String(i + 1).padStart(2, "0")}</span><p>{h.text} <RefChips refs={h.refs} compact /></p></li>)}
  </ol> : null;
  if (part === "main") return nothing ? <p className="muted small">上传 BP 或补充公开档案。</p>
    : <div className="basic-main">{mainRows.map((r) => <BasicRow key={r.title} {...r} limit={4} />)}</div>;
  if (part === "side") return nothing ? null : <>{sideRows.map((r) => <BasicRow key={r.title} {...r} limit={3} />)}</>;
  if (part === "milestones") return <MilestoneLine detail={detail} />;
  if (part === "sources") return <span className="ov-sources">
    {profile ? `内部资料 ${profile.documents ?? detail.documents.length} 份` : "暂无内部资料"}
    {dossier ? ` · 公开档案 ${relTime(dossier.updated_at)}更新` : ""}
    {" · "}<button className="link" disabled={refreshing || !project.activated} onClick={onRefreshDossier}>
      {refreshing ? "正在启动…" : dossier ? "更新公开档案" : "补充公开档案"}</button>
    {!nothing && <>{" · "}<BasicSources rows={[...rows, { title: "企业档案", lines: publicLines(registryFacts) }]} /></>}
  </span>;
  return (
    <Reveal as="section" className="company-basics dossier">
      <div className="dossier-head">
        <h3>公司档案</h3>
        <span className="muted small">
          {profile ? `内部资料 ${profile.documents ?? detail.documents.length} 份` : "暂无内部资料"}
          {dossier ? ` · 公开档案 ${relTime(dossier.updated_at)}更新` : ""}
          {" · "}<button className="link" disabled={refreshing || !project.activated} onClick={onRefreshDossier}>
            {refreshing ? "正在启动…" : dossier ? "更新公开档案" : "补充公开档案"}</button>
          {!nothing && <>{" · "}<BasicSources rows={[...rows, { title: "企业档案", lines: publicLines(registryFacts) }]} /></>}
        </span>
      </div>
      {highlights.length > 0 && <ol className="profile-highlights" aria-label="亮点">
        {highlights.slice(0, 3).map((h, i) => <li key={i}><span className="hl-no">{String(i + 1).padStart(2, "0")}</span><p>{h.text}</p><RefChips refs={h.refs} /></li>)}
      </ol>}
      {nothing ? (
        <p className="muted small">上传 BP 或补充公开档案。</p>
      ) : (
        <div className="basic-editorial">
          <div className="basic-main">{mainRows.map((r) => <BasicRow key={r.title} {...r} limit={4} />)}</div>
          <aside className="basic-aside" aria-label="团队与资本">{sideRows.map((r) => <BasicRow key={r.title} {...r} limit={3} />)}</aside>
        </div>
      )}
      <MilestoneLine detail={detail} />
      {!nothing && <p className="evidence-note">● 公司资料　○ 公开报道（未经公司确认）</p>}
    </Reveal>
  );
}

const REGISTERED_ADDRESS = /注册地址|注册地|住所/;

/** 工商档案: legal identity at a glance, always open — name, code, registered address, founding, site. */
function registryRows(detail: ProjectDetail): [string, ReactNode][] {
  const { project } = detail;
  const { pick } = dossierFacts(detail);
  const locations = pick("location");
  const address = locations.find((f) => REGISTERED_ADDRESS.test(f.label));
  const place = project.location || locations.find((f) => f !== address)?.text;
  const founded = project.founded || pick("founded")[0]?.text;
  const site = project.website ? (project.website.startsWith("http") ? project.website : `https://${project.website}`) : "";
  const rows: [string, ReactNode][] = ([
    ["工商全称", project.legal_name || project.name],
    ["统一社会信用代码", project.credit_code ? <span className="mono">{project.credit_code}</span> : ""],
    ["注册地址", address ? <>{address.text}{address.refs?.[0]?.url && <a className="registry-src muted small" href={address.refs[0].url} target="_blank" rel="noreferrer noopener">
      {address.refs[0].source || "来源"} ↗</a>}</> : <span className="muted">暂无（更新公开档案时查找）</span>],
    ["成立", founded],
    ["所在地", place],
    ["行业 / 赛道", project.industry],
    ["融资阶段", project.stage],
    ["官网", site ? <a href={site} target="_blank" rel="noreferrer noopener">{site.replace(/^https?:\/\//, "").replace(/\/$/, "")} ↗</a> : ""],
  ] as [string, ReactNode][]).filter(([, v]) => v);
  return rows;
}

/** The legal file as a compact side list (name, code, registered address, site). */
function RegistryList({ detail }: { detail: ProjectDetail }) {
  const rows = registryRows(detail).filter(([k]) => !["成立", "所在地", "行业 / 赛道", "融资阶段"].includes(k));
  return <dl className="side-dl">{rows.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl>;
}

export function CompanyRegistry({ detail }: { detail: ProjectDetail }) {
  const rows = registryRows(detail);
  const registryFacts = dossierFacts(detail).registry;
  return (
    <Reveal as="section" className="company-registry">
      <div className="dossier-head"><h3>工商档案</h3><span className="muted small">登记信息与公开来源</span></div>
      <dl className="basic-registry registry-open">{rows.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl>
      {registryFacts.length > 0 && <BasicRow title="登记摘录" lines={publicLines(registryFacts)} empty="" />}
    </Reveal>
  );
}

// ------------------------------------------------------------------ page

/** Company overview: an editorial lead, business narrative and a compact reference column. */
export function CompanyOverview({ detail, onReload }: { detail: ProjectDetail; onReload: () => void }) {
  const { project, profile } = detail;
  const [error, setError] = useState<unknown>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [more, setMore] = useState(false);
  const updateDossier = async () => {
    setRefreshing(true); setError(null);
    try { await api.refresh(project.id, "public_profile"); onReload(); }
    catch (e) { setError(e); }
    finally { setRefreshing(false); }
  };
  const basics = { detail, onRefreshDossier: updateDossier, refreshing };
  const { pick } = dossierFacts(detail);
  const founded = project.founded || pick("founded")[0]?.text;
  const place = project.location || pick("location").find((f) => !REGISTERED_ADDRESS.test(f.label))?.text;
  const intro = companyIntro(detail);
  const summary = profile?.summary;
  const cutoff = (profile as { material_cutoff?: { date?: string } } | null)?.material_cutoff?.date;
  return (
    <div className="tab-frame profile-frame">
      <section className="profile-lead" aria-labelledby="company-overview-title">
        <div className="profile-lead-copy">
          <div className="section-kicker"><span>公司概览</span><span aria-hidden="true">COMPANY PROFILE</span></div>
          <h2 id="company-overview-title">{intro || <span className="muted">公司介绍尚未补充</span>}</h2>
          {summary && summary !== intro && <div className="profile-abstract"><p className={!more && summary.length > 140 ? "clamp-3" : ""}>{summary}</p>
            {summary.length > 140 && <button className="link small" aria-expanded={more} onClick={() => setMore((v) => !v)}>{more ? "收起" : "阅读全文"}</button>}</div>}
          <div className="profile-provenance">{cutoff && <span>资料截至 {cutoff}</span>}<CompanyBasics {...basics} part="sources" /></div>
        </div>
        <dl className="profile-facts" aria-label="公司速览">
          <div><dt>成立时间</dt><dd>{founded || "未披露"}</dd></div>
          <div><dt>总部所在地</dt><dd>{place || "未披露"}</dd></div>
          <div><dt>融资阶段</dt><dd>{project.stage || "未披露"}</dd></div>
        </dl>
      </section>
      <Columns
        main={<>
          {!!profile?.highlights?.length && <Block title="关注要点" className="profile-notes"><CompanyBasics {...basics} part="highlights" /></Block>}
          <Block title="业务与进展" className="profile-business"><CompanyBasics {...basics} part="main" /></Block>
        </>}
        side={<div className="company-aside">
          <Block title="公司档案"><section aria-label="工商档案"><RegistryList detail={detail} /></section></Block>
          <CompanyBasics {...basics} part="side" />
        </div>} />
      <ResearchQuestions projectId={detail.project.id} revision={`${detail.project.last_external_at ?? ""}|${detail.project.last_internal_at ?? ""}`} />
      <CompanyBasics {...basics} part="milestones" />
      <ErrorNote error={error} />
      {!!detail.trials?.length && (
        <Block card title="临床研究进度" note="ClinicalTrials.gov · 申办方或合作方匹配本公司">
          <div className="trial-pipeline">{detail.trials.map((trial) => <div key={trial.id} className="trial-row"><a href={trial.url} target="_blank" rel="noreferrer noopener"><strong>{trial.id}</strong><span>{trial.title}</span><small>{trial.conditions.join(" / ")} · {trial.updated_at || "更新日期未知"}</small></a><div className="trial-phases">{["EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4"].map((phase, index) => <span key={phase} className={trial.phase.split("/").includes(phase) ? "trial-phase-current" : ""}>{["早期 I", "I 期", "II 期", "III 期", "IV 期"][index]}</span>)}</div><small>{trial.phase === "N/A" ? "阶段未注明" : trial.phase} · {trial.status}</small></div>)}</div>
          <p className="evidence-note">登记阶段不代表已完成或药品获批。</p>
        </Block>
      )}
      <p className="evidence-note">● 公司资料　○ 公开报道（未经公司确认）</p>
    </div>
  );
}

const QUESTION_TONE: Record<string, string> = { progress: "up", setback: "down", unchanged: "", insufficient: "muted" };

/** 研究问题 (8945 overview): the plan's questions with where each stands now — newest assessment first, unassessed after. */
function ResearchQuestions({ projectId, revision }: { projectId: string; revision: string }) {
  const { data } = useAsync(() => api.questions(projectId).catch(() => null), [projectId, revision]);
  const rows = (data?.questions ?? []).map((q) => {
    const latest = Object.values(q.latest ?? {}).filter(Boolean).sort((a, b) => b!.created_at.localeCompare(a!.created_at))[0];
    return { q, latest };
  }).sort((a, b) => (b.latest?.created_at ?? "").localeCompare(a.latest?.created_at ?? ""));
  if (!rows.length) return null;
  return <Block title="研究问题" note={`${rows.length} 个 · 最近判断在前`}
    action={<Link className="link small" to={`/projects/${projectId}?tab=plan`}>在监控范围维护 →</Link>}>
    <ol className="rq-list">{rows.slice(0, 6).map(({ q, latest }) => <li key={q.id}>
      <span className="rq-q">{q.text || q.label}</span>
      <span className={`rq-status ${latest ? QUESTION_TONE[latest.status] ?? "" : "muted"}`}>{latest ? latest.status_label : "尚未判断"}</span>
      {latest?.note && <span className="rq-note">{latest.note}</span>}
      {latest && <time className="rq-date">{relTime(latest.created_at)}</time>}
    </li>)}</ol>
  </Block>;
}

/** 当前判断: one overall judgment, then four judgments that follow the plan's four rings (公司本体 / 竞争 / 产业链 /
 * 环境), the three most important changes and the next things to watch. Everything else is reference, folded. */
function TopChanges({ changes, loading, projectId, projectName }: { changes: Change[]; loading: boolean; projectId: string; projectName: string }) {
  const [why, setWhy] = useState<Change | null>(null);
  if (loading) return <Skeleton lines={3} />;
  if (!changes.length) return <p className="muted small">近 60 天没有重要变化。</p>;
  return <><ol className="top3">{changes.slice(0, 3).map((c) => <li key={c.key}>
    <div className="top3-meta"><span>{c.origin === "资料" ? "内部资料" : c.tag}</span>{c.date && <time>{relTime(c.date)}</time>}
      {c.origin === "公开" && <button className="item-why" onClick={() => setWhy(c)}>关联与依据 →</button>}</div>
    <div className="top3-text">{c.url ? <a href={c.url} target="_blank" rel="noreferrer noopener">{c.text}</a> : c.text}</div>
    {c.meaning && <p className="top3-meaning">{c.meaning}</p>}
  </li>)}</ol>
  {why && <EventContextDrawer projectId={projectId} projectName={projectName} eventId={why.key} title={why.text} open onClose={() => setWhy(null)} />}</>;
}

const EVENT_STATE: Record<string, string> = { intent: "融资意向", agreement: "融资协议", received: "融资到账", signed: "签约", trial: "试用", delivered: "交付",
  accepted: "验收", recognized: "确认收入", paid: "回款", cancelled: "取消", delayed: "延期", withdrawn: "撤回", paused: "暂停", resumed: "恢复", unknown: "阶段待定" };
const ROLE: Record<string, string> = { customer: "客户", supplier: "供应商", partner: "合作方", pilot_partner: "试点合作方", investor: "投资方", unknown: "关系待定" };

export function CurrentBrief({ detail, goto, onReload, aside }: {
  detail: ProjectDetail; goto: (tab: Tab) => void; onReload: () => void; aside?: ReactNode;
}) {
  const { project, brief } = detail;
  const feed = useAsync(() => api.feed({ project_id: project.id, days: 60, limit: 80 }), [project.id, monitoringRevision(detail)]);
  const deep = useAsync(() => api.insight(project.id), [project.id, project.last_external_at]);
  const changes = collectChanges(detail, feed.data ?? []);
  const missingOCR = detail.documents.filter((d) => d.status === "needs_ocr" || d.status === "failed");
  const watch = brief?.watch ?? [];
  const record = deep.data?.record;
  const relations = detail.observations?.items.filter((r) => ["business_relation", "policy", "event"].includes(r.kind)) ?? [];

  return (
    <div className="tab-frame brief-frame">
      {/* The headline and key figures live in the project cover above every tab; here only their provenance. */}
      <div className="tf-band-top brief-provenance">
        <div className="tf-eyebrow">当前判断{!record && brief && brief.dependency?.status !== "stale" && <span className={`tone tone-${brief.tone}`}>{TONE_LABEL[brief.tone]}</span>}</div>
        <div className="tf-meta">{record ? `${record?.origin?.producer ? `${record.origin.producer} 写回` : "AI"} 交叉分析 · 依据 ${record.total} 条 · ${relTime(record.generated_at)}` : brief ? `${brief.origin?.producer || "AI"} 整理 · ${relTime(brief.updated_at)}` : ""}</div>
      </div>
      {brief?.dependency?.status === "stale" && <p className="muted small">资料或关系已更正，此版待更新。</p>}

      {missingOCR.length > 0 && (
        <div className="overview-attention">
          <div><strong>{missingOCR.length} 份资料未完整识别</strong><p>未识别页面不参与分析，已读内容保留。</p></div>
          <button className="btn" onClick={() => goto("documents")}>处理资料 →</button>
        </div>
      )}

      {deep.loading && !deep.data
        ? <div className="rj-tiles qa-tiles">{[1, 2, 3, 4, 5].map((n) => <div key={n} className="rj-tile empty"><Skeleton lines={2} /></div>)}</div>
        : <QuestionAnalysis detail={detail} view={deep.data ?? null} error={deep.error} goto={goto}
          onStart={() => api.refresh(project.id, "insight").then(() => deep.reload(), () => deep.reload())} />}

      <Columns
        main={<Block title="本期最重要的变化" note="近 60 天 · 内部与公开信息"
          action={<button className="link small" onClick={() => goto("external")}>全部公司动态 →</button>}>
          <TopChanges changes={changes} loading={feed.loading && !feed.data} projectId={project.id} projectName={project.short_name || project.name} />
          <ErrorNote error={feed.error} />
        </Block>}
        side={<>
          {watch.length > 0 && <Block title="接下来关注"><ul className="side-list">{watch.slice(0, 3).map((w, i) => <li key={i}>{w.text}</li>)}</ul></Block>}
          {aside}
        </>} />

      <Reference items={[
        { key: "evidence", title: "AI 判断的依据与缺口", note: record ? `引用 ${record.analysis.cited.length} 条 · 缺 ${record.analysis.gaps.length} 项` : undefined,
          body: record ? <EvidenceGaps view={deep.data ?? null} /> : null },
        { key: "questions", title: "跟踪问题与你的判断", note: "问题本身在监控方案里维护",
          body: <QuestionsCard projectId={project.id} revision={`${project.last_external_at ?? ""}|${brief?.updated_at ?? ""}`} /> },
        { key: "peers", title: "同行进展", body: detail.capabilities?.insight_projections === true
          ? <PositionLine projectId={project.id} revision={project.last_external_at ?? ""}
            more={<button className="link small" onClick={() => goto("competition")}>看竞争对手 →</button>} /> : null },
        { key: "relations", title: "关系、政策与事件依据", note: relations.length ? `${relations.length} 条` : undefined,
          body: relations.length ? <table className="table compact-table"><thead><tr><th>对象</th><th>资料中的关系</th><th>依据状态</th></tr></thead>
            <tbody>{relations.map((r) => <tr key={r.fact_id}><th scope="row">{r.peer_name || r.name}</th>
              <td>{r.kind === "event" ? `${EVENT_STATE[r.state || "unknown"]} · ${r.occurred_at || "日期未披露"}` : r.kind === "policy" ? "政策适用条件" : ROLE[r.role || "unknown"]}</td>
              <td>{r.support === "source_assertion" || r.applicability === "conditions_match_source_assertions" ? "有来源陈述" : r.applicability === "not_current" ? "非现行" : "依据不足"}
                {!!r.refs?.length && <details><summary className="muted small">依据</summary>{r.refs.map((ref, n) => <div key={n}><p>{ref.quote}</p><small className="muted">片段 {ref.part_index + 1} · 字符 {ref.start}–{ref.end}</small></div>)}</details>}
              </td>
            </tr>)}</tbody></table> : null },
        { key: "monitor", title: "监控运行细节", body: <MonitoringDetails detail={detail} onReload={onReload} /> },
      ]} />
    </div>
  );
}

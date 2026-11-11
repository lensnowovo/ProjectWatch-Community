import { motion } from "motion/react";
import { useState, type ReactNode } from "react";
import { useSearchParams } from 'react-router-dom';
import { api, type InsightPoint, type InsightView, type ProjectDetail, type RaceView, type TrackCell, type TrackView } from "./api";
import { Skeleton } from "./frame";
import { AnnotatedSummary, Confidence, Points, Scale, StatusLine, TechMap, verdictTone, type ScaleKey } from "./judgment";
import { EASE } from "./motion";
import { useAsync } from "./ui";
import { ChartHint } from "./chartHint";
import { SignalListDrawer } from "./signalList";
import { raceRevision, useRace } from './raceData';
import { jobWorkState, WORK_LABEL } from "./workState";
import { WorkIndicator } from "./Operational";
import { PageSummary, selectSummary, type SummarySubject } from './pageSummary';
import { TrackSignalRows } from './TrackSignalRows';

/**
 * 当前判断 organised by question (8945 分析): a strip of equal tiles, one per question; the chosen one shows a chart
 * made for that question beside its conclusion card, with every cited point available on demand.
 */
type Key = "operations" | "position" | "technology" | "chain" | "industry";
type Tab = "external" | "competition" | "industry" | "numbers" | "overview" | "tech-progress" | "chain";
interface Question { key: Key; label: string; ask: string; scale: ScaleKey; pro: string; con: string[]; proTitle: string; conTitle: string; goto?: Tab; more?: string }
const QUESTIONS: Question[] = [
  { key: "operations", label: "公司进展", ask: "公司近期推进到了哪里？", scale: "operations", pro: "for", con: ["against"],
    proTitle: "支持这个判断", conTitle: "相反的依据", goto: "external", more: "公司动态" },
  { key: "position", label: "竞争位置", ask: "和同路线对手比，领先还是落后？", scale: "position", pro: "for", con: ["against"],
    proTitle: "支持这个判断", conTitle: "相反的依据", goto: "competition", more: "竞争对手" },
  { key: "technology", label: "技术路线", ask: "押注的技术路线是前沿还是趋于落后？", scale: "maturity", pro: "academic", con: ["risks"],
    proTitle: "学术与产业信号", conTitle: "技术路线风险", goto: "tech-progress", more: "技术进展" },
  { key: "chain", label: "产业链", ask: "客户、供应与合作方是助力还是掣肘？", scale: "chain", pro: "for", con: ["against"],
    proTitle: "支持这个判断", conTitle: "相反的依据", goto: "chain", more: "产业链动态" },
  { key: "industry", label: "行业环境", ask: "赛道在扩张还是降温？", scale: "industry", pro: "drivers", con: ["headwinds"],
    proTitle: "驱动", conTitle: "阻力", goto: "industry", more: "行业环境" },
];

interface Judged { verdict: string; summary: string; confidence?: "high" | "medium" | "low"; change_if: string[]; pros: InsightPoint[]; cons: InsightPoint[];
  state?: string; reasons?: string[]; annotations?: { span: string; code: string; original?: string }[] }

function judged(view: InsightView | null, q: Question): Judged | null {
  const part = view?.record?.analysis?.[q.key] as Record<string, unknown> | undefined;
  if (!part || !part.summary) return null;
  const list = (k: string) => (part[k] as InsightPoint[] | undefined) ?? [];
  return {
    verdict: String(q.key === "technology" ? part.maturity : part.verdict), summary: String(part.summary),
    confidence: part.confidence as Judged["confidence"], change_if: (part.change_if as string[] | undefined) ?? [],
    pros: q.key === "technology" ? [...list("academic"), ...list("industry")] : list(q.pro), cons: q.con.flatMap(list),
    state: part.state as string | undefined, reasons: part.reason_codes as string[] | undefined,
    annotations: part.annotations as Judged["annotations"],
  };
}

/** `changes`: the period's most important company events, read under the 公司进展 narrative they support. */
export function QuestionAnalysis({ detail, view, error, onStart, goto, changes }: {
  detail: ProjectDetail; view: InsightView | null; error: unknown; onStart?: () => void; goto: (tab: Tab) => void; changes?: ReactNode;
}) {
  const analysis = view?.record?.analysis;
  const [params, setParams] = useSearchParams();
  const chosen = params.get('question') as Key | null;
  const all = params.get('evidence') === 'open';
  const setAll = (open: boolean) => setParams(previous => {
    const next = new URLSearchParams(previous);
    if (open) next.set('evidence', 'open'); else next.delete('evidence');
    return next;
  }, { replace: true, preventScrollReset: true });
  const selected = QUESTIONS.find(q => q.key === chosen);
  // 公司进展 is a reading, not a verdict: open on it so the period's key events come first.
  const active = selected?.key ?? "operations";
  const running = !!view?.job && ["queued", "running"].includes(view.job.status);
  const analysisJob = detail.jobs?.find(j => j.id === view?.job?.id);
  const analysisState = analysisJob ? jobWorkState(detail, analysisJob) : view?.job ? jobWorkState(detail, {
    ...view.job, kind: "analyze", stage: "", error: view.job.error || "", finished_at: null,
  }) : "idle";
  const q = QUESTIONS.find((x) => x.key === active);
  const j = q ? judged(view, q) : null;
  const summarySubject: Record<Key, SummarySubject> = {operations:'external',position:'competition',technology:'technology',chain:'chain',industry:'industry'};

  const stateText: Record<string, string> = {
    insufficient: '现有证据不足以支持本栏结论', missing_draft: '本轮未形成可复核的判断',
    review_blocked: '本栏判断未通过复核', unknown: '模型请求结果待确认，已保留进度',
    rate_limited: view?.job?.retry_after ? '模型限流，预计 ' + new Date(view.job.retry_after).toLocaleString('zh-CN', {month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit'}) + ' 继续' : '模型限流，等待恢复',
    pending: '本轮分析进行中', no_material: '本轮未筛到相关材料', waiting_model: '等待配置模型', unfinished: '本轮未完成本栏判断',
  };
  const labelsFor = (x: Question) => view?.labels?.[x.scale === "maturity" ? "maturity" : (x.key as "position")] ?? {};
  const canGenerate = view?.generation_available === true;
  if (view?.generation_available === false && !analysis && !QUESTIONS.some(x => selectSummary(detail, summarySubject[x.key]))) return <section className="qa" aria-label="判断记录">
    <p className="muted small">暂无判断记录。可由外部 Agent 写入，或启用分析扩展。</p>
    {changes && <div className="qa-company-changes"><h3>近 60 天重要变化</h3>{changes}</div>}
  </section>;
  return (
    <section className="qa" aria-label="按问题看判断">
      {(running || analysisState === "failed" || analysisState === "needs_confirmation") && <div className="op-local" role="status">
        <WorkIndicator state={analysisState} label={`综合判断 · ${analysisJob?.runtime?.label || WORK_LABEL[analysisState] || "状态待同步"}`} />
        {analysis && <span className="muted small">当前显示上次结果</span>}
      </div>}
      {view?.section_states && Object.keys(view.section_states).length > 0 && <p className="muted small" role="status">
        本轮已发布 {Object.entries(view.section_states).filter(([key,state]) => key !== 'operations' && state === 'published').length}/4 项判断
      </p>}
      <div className="rj-tiles qa-tiles" role="tablist" aria-label="选择问题">
        {QUESTIONS.map((x, i) => {
          const v = x.key === "operations" ? null : judged(view, x);
          const tileCopy = selectSummary(detail, summarySubject[x.key]);
          return <button key={x.key} role="tab" aria-selected={active === x.key}
            className={`rj-tile${active === x.key ? " on" : ""}${v || (x.key === "operations" && tileCopy) ? "" : " empty"}`} onClick={() => setParams(previous => {
              const next = new URLSearchParams(previous); next.set('question', x.key); next.delete('evidence'); return next;
            }, { replace: true, preventScrollReset: true })}>
            <span className="rj-tile-head"><span className="rj-tile-label"><span className="qa-no">0{i + 1}</span>{x.label}</span>
              {x.key !== "operations" && <span className={`rj-verdict ${v ? verdictTone(x.scale, v.verdict) : ""}`}>{v ? labelsFor(x)[v.verdict] ?? v.verdict : "—"}</span>}</span>
            <span className="rj-tile-summary">{v ? v.summary : tileCopy?.text || (x.key === "operations" ? "公司进展摘要整理后显示" : error ? "判断暂时无法读取" : !canGenerate ? "暂无判断记录" : view?.evidence_gaps?.[x.key] || (stateText[view?.section_states?.[x.key] ?? ''] ?? (analysis ? "本轮未发布本栏判断" : "还没有 AI 交叉分析")))}</span>
            <span className="rj-tile-foot">{v ? `支持 ${v.pros.length} · 相反 ${v.cons.length}` : ""}
              {v && view?.section_states?.[x.key] && !['published','retained'].includes(view.section_states[x.key]) && <small>{stateText[view.section_states[x.key]]} · 显示上次判断</small>}
              {x.key !== "operations" && view?.record?.section_versions?.[x.key] && <small> · {view.record.section_versions[x.key].generated_at?.slice(0, 10)}
                {view.record.section_versions[x.key].status !== 'current' ? ' · 待更新' : ''}</small>}
            </span>
            <i className="rj-tile-glow" aria-hidden="true" />
          </button>;
        })}
      </div>
      {!analysis && !running && !error && onStart && canGenerate && view?.model_ready && <p className="rj-empty">
        <button className="link small" onClick={onStart}>开始 AI 交叉分析 →</button></p>}
      {q?.key === "operations" && <motion.section key="company-progress" role="tabpanel" aria-label="公司进展" className="qa-panel qa-company-progress" initial={{opacity:0,y:6}} animate={{opacity:1,y:0}} transition={{duration:.28,ease:EASE}}>
        <PageSummary detail={detail} subject="external" />
        {changes && <div className="qa-company-changes"><h3>近 60 天重要变化</h3>{changes}</div>}
        <div className="qa-actions"><button className="link small" onClick={() => goto("external")}>查看公司动态 →</button></div>
      </motion.section>}
      {q && q.key !== "operations" && !j && <section role="tabpanel" className="qa-panel" aria-label={q.label}>
        {selectSummary(detail, summarySubject[q.key])
          ? <><PageSummary key={q.key} detail={detail} subject={summarySubject[q.key]}/><p className="muted small">已整理信息，综合判断尚未形成。</p></>
          : <p role="status" className="muted">{error ? '判断暂时无法读取，请稍后刷新。' : stateText[view?.section_states?.[q.key] ?? ''] || (running ? '正在分析本栏材料，完成后自动显示。' : '本栏尚未形成判断。')}</p>}
        {q.goto && <button className="link small" onClick={()=>goto(q.goto!)}>查看{q.more} →</button>}
      </section>}
      {q && q.key !== "operations" && j && <motion.div key={q.key} role="tabpanel" className={`qa-panel qa-panel-${q.key}`} initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }}
        transition={{ duration: .28, ease: EASE }}>
        <div className="qa-main">
          <div className="qa-visual"><QuestionVisual q={q.key} detail={detail} view={view!} /></div>
          <aside className="qa-card" aria-label="结论">
            <span className="qa-ask">{q.ask}</span>
            <strong className={`qa-verdict ${verdictTone(q.scale, j.verdict)}`}>{labelsFor(q)[j.verdict] ?? j.verdict}</strong>
            <Scale scale={q.scale} value={j.verdict} labels={labelsFor(q)} state={j.state} />
            <AnnotatedSummary text={j.summary} notes={j.annotations} className="qa-summary" />
            <StatusLine state={j.state} reasons={j.reasons} />
            <dl className="qa-facts">
              <div><dt>把握</dt><dd><Confidence value={j.confidence} /></dd></div>
              <div><dt>依据</dt><dd>支持 {j.pros.length} · 相反 {j.cons.length}</dd></div>
              {j.change_if[0] && <div><dt>会改变判断</dt><dd>{j.change_if[0]}</dd></div>}
            </dl>
            <div className="qa-actions">
              <button className="link small" aria-expanded={all} onClick={() => setAll(!all)}>{all ? "收起依据" : "查看全部依据 ↓"}</button>
              {q.goto && <button className="link small" onClick={() => goto(q.goto!)}>{q.more}明细 →</button>}
            </div>
          </aside>
        </div>
        {all && <motion.div className="jd-cols qa-evidence" initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: .2 }}>
          <Points title={q.proTitle} points={j.pros} tone="plus" empty="没有带出处的依据。" />
          <Points title={q.conTitle} points={j.cons} tone="minus" empty="没有带出处的相反依据。" />
          {j.change_if.length > 1 && <div className="jd-points"><h4>什么信息会改变判断</h4><ul>{j.change_if.map((t, i) => <li key={i}>{t}</li>)}</ul></div>}
        </motion.div>}
      </motion.div>}
    </section>
  );
}

function QuestionVisual({ q, detail, view }: { q: Key; detail: ProjectDetail; view: InsightView }) {
  if ((q === "position" || q === "industry") && detail.capabilities?.insight_projections !== true) {
    const question = QUESTIONS.find(x => x.key === q)!;
    const saved = judged(view, question);
    return <Visual title="已保存的判断"><Scale scale={question.scale} value={saved?.verdict ?? "unclear"}
      labels={view.labels[q]} /></Visual>;
  }
  if (q === "position") return <PeerStages projectId={detail.project.id} revision={raceRevision(detail)} view={view} />;
  if (q === "technology") return <Technology view={view} />;
  if (q === "chain") return <Chain detail={detail} />;
  return <Track projectId={detail.project.id} revision={detail.project.last_external_at ?? ""} />;
}

/** One frame for every question chart: title, the chart, then two or three plain lines on how to read it. */
function Visual({ title, note, children, reading }: { title: string; note?: ReactNode; children: ReactNode; reading?: string[] }) {
  return <figure className="qa-figure">
    <figcaption><strong>{title}</strong>{note && <span>{note}</span>}</figcaption>
    <div className="qa-figure-body">{children}</div>
    {!!reading?.length && <ul className="qa-reading">{reading.map((r, i) => <li key={i}>{r}</li>)}</ul>}
  </figure>;
}

// ------------------------------------------------------------------ 竞争位置

/** Each peer on the shared stage scale with its latest funding; the company's own row is marked. Attributed stages only. */
function PeerStages({ projectId, revision, view }: { projectId: string; revision: string; view: InsightView }) {
  const { data, loading, error, reload } = useRace(projectId, revision);
  const rows = (data?.rows ?? []).filter((r) => r.is_self || r.stage).slice(0, 7);
  const status = <div className="qa-read-status" role="status">
    {loading ? '正在读取同行记录…' : error ? <>{data ? '读取失败，保留上次记录。' : '同行记录读取失败。'} <button className="link small" onClick={() => void reload()}>重新读取</button></> : null}
  </div>;
  if (!data || !data.steps.length || rows.length < 2) return <CompetitionFallback data={data} view={view} status={status} />;
  const steps = data.steps;
  const self = rows.find((r) => r.is_self);
  const lead = rows.filter((r) => !r.is_self && r.stage).sort((a, b) => (b.stage!.index - a.stage!.index))[0];
  const reading = [
    self?.stage ? `本公司在「${self.stage.label}」${self.stage.date ? `（${self.stage.date}）` : ""}。` : "本公司在登记或报道中还没有可归属的阶段，无法直接比对。",
    lead ? `记录中阶段较靠前：${lead.name} · ${lead.stage!.label}。` : "",
    `展示 ${rows.length - (self ? 1 : 0)} 家同行的已收录阶段，不作为公司综合排名。`,
  ].filter(Boolean);
  return <Visual title="同行阶段" note={data.template_label} reading={reading}>
    {status}
    <div className="qa-stage" style={{ gridTemplateColumns: `minmax(8em, 1.3fr) repeat(${steps.length}, minmax(0, 1fr)) minmax(9.5em, 1.5fr)` }}>
      <span className="qa-stage-h" />{steps.map((s) => <span key={s} className="qa-stage-h">{s}</span>)}<span className="qa-stage-h">最近融资</span>
      {rows.map((r) => {
        const at = r.stage?.index ?? -1;
        return [<span key={`${r.name}-n`} className={`qa-stage-name${r.is_self ? " self" : ""}`} title={r.name}><b>{r.name}</b>{r.is_self && <em>本公司</em>}</span>,
          ...steps.map((s, i) => <span key={`${r.name}-${s}`} className="qa-stage-cell">
            <i className={`qa-stage-line${i <= at ? " done" : ""}${i === 0 ? " first" : ""}`} />
            {i === at && <ChartHint title={r.name} value={r.stage!.label} meta={[r.stage!.date, r.stage!.scope || r.stage!.title].filter(Boolean).join(' · ')} note={r.stage!.basis === 'title' ? '阶段来自报道标题，点击下方来源查看。' : '圆点表示已收录的产品阶段，不代表市场份额或综合排名。'}><motion.b className={`qa-stage-dot${r.is_self ? " self" : ""}`} initial={{ scale: 0 }} animate={{ scale: 1 }}
              transition={{ delay: .1, duration: .35, ease: EASE }} /></ChartHint>}</span>),
          <span key={`${r.name}-f`} className="qa-stage-date">{r.funding
            ? <><b>{r.funding.round || "融资"}</b>{r.funding.amount && <span className="qa-stage-amt">{r.funding.amount}</span>}
              {r.funding.date && <small>{r.funding.date}</small>}</>
            : <small>暂无记录</small>}</span>];
      })}
    </div>
    <ul className="qa-stage-sources">{rows.filter(r => r.stage).map(r => <li key={r.name}><strong>{r.name}</strong><span>{r.stage!.scope || r.stage!.title}</span>
      {r.stage!.url && <a href={r.stage!.url} target="_blank" rel="noreferrer noopener">{r.stage!.basis === 'title' ? '报道标题 ↗' : '来源 ↗'}</a>}</li>)}</ul>
  </Visual>;
}

export function CompetitionFallback({ data, view, status }: { data: RaceView | null; view: InsightView; status?: ReactNode }) {
  const position = view.record?.analysis.position;
  const points = [...(position?.against ?? []), ...(position?.for ?? [])].slice(0, 2);
  const names = (data?.rows ?? []).filter(r => !r.is_self).map(r => r.name);
  return <Visual title="竞争对照" note="已整理的依据">
    {status}
    {names.length > 0 && <div className="qa-peer-list">{names.map(name => <span key={name}>{name}</span>)}</div>}
    {points.length ? <Points title="已有对照" points={points} empty="" /> : <p className="muted small">暂无可展示的比较依据。</p>}
    {data && <p className="muted small qa-stage-missing">阶段记录不足，暂不排列进度。</p>}
  </Visual>;
}

// ------------------------------------------------------------------ 技术路线

const MATURITY_NOTE: Record<string, string> = { frontier: "仍在早期验证，领先但不确定", advanced: "已有临床或工程验证，尚未大规模商用",
  mainstream: "行业主流做法，竞争者多", mature: "成熟稳定，差异化空间小", dated: "正被新路线替代" };
const CONSENSUS_NOTE: Record<string, string> = { consensus: "行业与学术界普遍认可", forming: "支持者增多，但尚未定论", contrarian: "少数派押注，需要更多证据" };

function Technology({ view }: { view: InsightView }) {
  const t = view.record!.analysis.technology;
  const reading = [t.academic[0] ? `学术信号：${t.academic[0].text}` : "", t.risks[0] ? `主要风险：${t.risks[0].text}` : ""].filter(Boolean);
  return <Visual title="技术路线定位" note="横轴：共识程度 · 纵轴：技术成熟度" reading={reading}>
    <div className="qa-tech">
      <div className="qa-tech-map"><TechMap maturity={t.maturity} consensus={t.consensus} labels={view.labels} /></div>
      <dl className="qa-tech-side">
        <div><dt>押注的路线</dt><dd className="qa-tech-route">{t.route || "未识别"}</dd></div>
        <div><dt>成熟度 · {view.labels.maturity[t.maturity] ?? t.maturity}</dt><dd>{MATURITY_NOTE[t.maturity] ?? "依据不足，暂不定位"}</dd></div>
        <div><dt>共识 · {view.labels.consensus[t.consensus] ?? t.consensus}</dt><dd>{CONSENSUS_NOTE[t.consensus] ?? "依据不足，暂不定位"}</dd></div>
        <div className="qa-tech-counts"><span><b>{t.academic.length}</b>学术信号</span><span><b>{t.industry.length}</b>产业信号</span><span className="down"><b>{t.risks.length}</b>路线风险</span></div>
      </dl>
    </div>
  </Visual>;
}

// ------------------------------------------------------------------ 产业链

const ROLE_SIDE: Record<string, "up" | "partner" | "down" | "capital"> = { supplier: "up", partner: "partner", pilot_partner: "partner", customer: "down", investor: "capital" };

/** Upstream and partners → the company → customers, with capital beneath; names come from relationships in the materials. */
function Chain({ detail }: { detail: ProjectDetail }) {
  const rel = (detail.observations?.items ?? []).filter((r) => r.kind === "business_relation" && ROLE_SIDE[r.role ?? ""]);
  const names = (side: string) => [...new Set(rel.filter((r) => ROLE_SIDE[r.role ?? ""] === side).map((r) => r.peer_name || r.name || ""))].filter(Boolean);
  const up = names("up"), partner = names("partner"), down = names("down"), capital = names("capital");
  if (!rel.length) return <Visual title="上下游与合作"><p className="muted small">资料中还没有确认的客户、供应或合作关系。</p></Visual>;
  const col = (title: string, list: string[], cls = "") => <div className={`qa-chain-col ${cls}`}><span className="qa-chain-h">{title}<b>{list.length}</b></span>
    {list.length ? list.slice(0, 5).map((n) => <span key={n} className="qa-chain-node" title={n}>{n}</span>) : <span className="muted small">资料中未见</span>}
    {list.length > 5 && <span className="muted small">另 {list.length - 5} 家</span>}</div>;
  const reading = [
    down.length ? `资料中出现 ${down.length} 个客户关系。` : "资料中还没有确认的客户：商业化关系尚未形成或未披露。",
    partner.length || up.length ? `上游与合作方 ${up.length + partner.length} 家，是当前推进的主要依托。` : "",
    capital.length ? `资本方 ${capital.length} 家。` : "",
  ].filter(Boolean);
  return <Visual title="上下游与合作" note="来自资料中的商业关系" reading={reading}>
    <div className="qa-chain2">
      <div className="qa-chain-stack">{col("上游供应", up)}{col("合作方", partner)}</div>
      <span className="qa-chain-arrow" aria-hidden="true">→</span>
      <div className="qa-chain-core"><b>{detail.project.short_name || detail.project.name}</b><small>本公司</small></div>
      <span className="qa-chain-arrow" aria-hidden="true">→</span>
      {col("客户", down, "down")}
    </div>
    {capital.length > 0 && <div className="qa-chain-capital"><span className="qa-chain-h">资本方<b>{capital.length}</b></span>
      <div>{capital.slice(0, 8).map((n) => <span key={n} className="qa-chain-node">{n}</span>)}</div></div>}
  </Visual>;
}

// ------------------------------------------------------------------ 行业环境

/** The four sector signals as rows: direction, the number, its recent trend, and what it means for the company. */
function Track({ projectId, revision }: { projectId: string; revision: string }) {
  const { data, loading } = useAsync<TrackView | null>(() => api.track(projectId).catch(() => null), [projectId, revision]);
  const [open, setOpen] = useState<TrackCell | null>(null);
  if (loading && !data) return <Skeleton lines={5} />;
  if (!data?.cells.length) return <Visual title="赛道信号"><p className="muted small">赛道资料不足；见行业环境页签。</p></Visual>;
  return <>
    <SignalListDrawer projectId={projectId} cell={open} open={!!open} onClose={() => setOpen(null)} />
    <TrackSignalRows data={data} onOpen={setOpen} />
  </>;
}

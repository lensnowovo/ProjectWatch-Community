import { motion } from "motion/react";
import { useState, type ReactNode } from "react";
import { useSearchParams } from 'react-router-dom';
import { api, type InsightPoint, type InsightView, type ProjectDetail, type RaceView, type TrackCell, type TrackView } from "./api";
import { Skeleton } from "./frame";
import { Confidence, Points, Scale, TechMap, verdictTone, type ScaleKey } from "./judgment";
import { EASE } from "./motion";
import { formatAmount, useAsync } from "./ui";
import { financeSeries, periodKind } from "./financeAnalysis";
import { comparePeriods } from "./metrics";
import { ChartHint } from "./chartHint";
import { SignalListDrawer } from "./signalList";
import { raceRevision, useRace } from './raceData';
import { jobWorkState, WORK_LABEL } from "./workState";
import { WorkIndicator } from "./Operational";
import { TrackSignalRows } from './TrackSignalRows';

/**
 * 当前判断 organised by question (8945 分析): a strip of equal tiles, one per question; the chosen one shows a chart
 * made for that question beside its conclusion card, with every cited point available on demand.
 */
type Key = "operations" | "position" | "technology" | "chain" | "industry";
type Tab = "competition" | "industry" | "numbers" | "overview";
interface Question { key: Key; label: string; ask: string; scale: ScaleKey; pro: string; con: string[]; proTitle: string; conTitle: string; goto?: Tab; more?: string }
const QUESTIONS: Question[] = [
  { key: "operations", label: "公司本体", ask: "公司自己的经营与资金撑得住吗？", scale: "operations", pro: "for", con: ["against"],
    proTitle: "支持这个判断", conTitle: "相反的依据", goto: "numbers", more: "经营情况" },
  { key: "position", label: "竞争位置", ask: "和同路线对手比，领先还是落后？", scale: "position", pro: "for", con: ["against"],
    proTitle: "支持这个判断", conTitle: "相反的依据", goto: "competition", more: "竞争对手" },
  { key: "technology", label: "技术路线", ask: "押注的技术路线是前沿还是趋于落后？", scale: "maturity", pro: "academic", con: ["risks"],
    proTitle: "学术与产业信号", conTitle: "技术路线风险", goto: "industry", more: "行业环境" },
  { key: "chain", label: "产业链", ask: "客户、供应与合作方是助力还是掣肘？", scale: "chain", pro: "for", con: ["against"],
    proTitle: "支持这个判断", conTitle: "相反的依据", goto: "overview", more: "公司概览" },
  { key: "industry", label: "行业环境", ask: "赛道在扩张还是降温？", scale: "industry", pro: "drivers", con: ["headwinds"],
    proTitle: "驱动", conTitle: "阻力", goto: "industry", more: "行业环境" },
];

interface Judged { verdict: string; summary: string; confidence?: "high" | "medium" | "low"; change_if: string[]; pros: InsightPoint[]; cons: InsightPoint[] }

function judged(view: InsightView | null, q: Question): Judged | null {
  const part = view?.record?.analysis?.[q.key] as Record<string, unknown> | undefined;
  if (!part || !part.summary) return null;
  const list = (k: string) => (part[k] as InsightPoint[] | undefined) ?? [];
  return {
    verdict: String(q.key === "technology" ? part.maturity : part.verdict), summary: String(part.summary),
    confidence: part.confidence as Judged["confidence"], change_if: (part.change_if as string[] | undefined) ?? [],
    pros: q.key === "technology" ? [...list("academic"), ...list("industry")] : list(q.pro), cons: q.con.flatMap(list),
  };
}

export function QuestionAnalysis({ detail, view, error, onStart, goto }: {
  detail: ProjectDetail; view: InsightView | null; error: unknown; onStart?: () => void; goto: (tab: Tab) => void;
}) {
  const analysis = view?.record?.analysis;
  const first = QUESTIONS.find((q) => judged(view, q))?.key ?? null;
  const [params, setParams] = useSearchParams();
  const chosen = params.get('question') as Key | null;
  const all = params.get('evidence') === 'open';
  const setAll = (open: boolean) => setParams(previous => {
    const next = new URLSearchParams(previous);
    if (open) next.set('evidence', 'open'); else next.delete('evidence');
    return next;
  }, { replace: true, preventScrollReset: true });
  const selected = QUESTIONS.find(q => q.key === chosen);
  const active = selected && judged(view, selected) ? selected.key : first;
  const running = !!view?.job && ["queued", "running"].includes(view.job.status);
  const analysisJob = detail.jobs?.find(j => j.id === view?.job?.id);
  const analysisState = analysisJob ? jobWorkState(detail, analysisJob) : view?.job ? jobWorkState(detail, {
    ...view.job, kind: "insight", stage: "", error: view.job.error || "", finished_at: null,
  }) : "idle";
  const q = QUESTIONS.find((x) => x.key === active);
  const j = q ? judged(view, q) : null;
  const labelsFor = (x: Question) => view?.labels?.[x.scale === "maturity" ? "maturity" : (x.key as "position")] ?? {};
  const canGenerate = view?.generation_available === true;
  if (view?.generation_available === false && !analysis) return <section className="qa" aria-label="判断记录">
    <p className="muted small">暂无判断记录。可由外部 Agent 写入，或启用分析扩展。</p>
  </section>;
  return (
    <section className="qa" aria-label="按问题看判断">
      {(running || analysisState === "failed" || analysisState === "needs_confirmation") && <div className="op-local" role="status">
        <WorkIndicator state={analysisState} label={`综合判断 · ${analysisJob?.runtime?.label || WORK_LABEL[analysisState] || "状态待同步"}`} />
        {analysis && <span className="muted small">当前显示上次结果</span>}
      </div>}
      <div className="rj-tiles qa-tiles" role="tablist" aria-label="选择问题">
        {QUESTIONS.map((x, i) => {
          const v = judged(view, x);
          return <button key={x.key} role="tab" aria-selected={active === x.key} disabled={!v}
            className={`rj-tile${active === x.key ? " on" : ""}${v ? "" : " empty"}`} onClick={() => setParams(previous => {
              const next = new URLSearchParams(previous); next.set('question', x.key); next.delete('evidence'); return next;
            }, { replace: true, preventScrollReset: true })}>
            <span className="rj-tile-head"><span className="rj-tile-label"><span className="qa-no">0{i + 1}</span>{x.label}</span>
              <span className={`rj-verdict ${v ? verdictTone(x.scale, v.verdict) : ""}`}>{v ? labelsFor(x)[v.verdict] ?? v.verdict : "—"}</span></span>
            <span className="rj-tile-summary">{v ? v.summary : error ? "判断暂时无法读取" : !canGenerate ? "暂无判断记录" : analysis ? "下次 AI 交叉分析时生成" : "还没有 AI 交叉分析"}</span>
            <span className="rj-tile-foot">{v ? `支持 ${v.pros.length} · 相反 ${v.cons.length}` : "暂无结果"}</span>
            <i className="rj-tile-glow" aria-hidden="true" />
          </button>;
        })}
      </div>
      {!analysis && !running && !error && onStart && canGenerate && view?.model_ready && <p className="rj-empty">
        <button className="link small" onClick={onStart}>开始 AI 交叉分析 →</button></p>}
      {q && j && <motion.div key={q.key} role="tabpanel" className={`qa-panel qa-panel-${q.key}`} initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }}
        transition={{ duration: .28, ease: EASE }}>
        <div className="qa-main">
          <div className="qa-visual"><QuestionVisual q={q.key} detail={detail} view={view!} /></div>
          <aside className="qa-card" aria-label="结论">
            <span className="qa-ask">{q.ask}</span>
            <strong className={`qa-verdict ${verdictTone(q.scale, j.verdict)}`}>{labelsFor(q)[j.verdict] ?? j.verdict}</strong>
            <Scale scale={q.scale} value={j.verdict} labels={labelsFor(q)} />
            <p className="qa-summary">{j.summary}</p>
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
  if (q === "operations") return <Operations detail={detail} />;
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

// ------------------------------------------------------------------ 公司本体

const money = (v: number, unit: string) => {
  const a = unit === "元" ? formatAmount(v / 10000, "万元") : formatAmount(v, unit);
  return `${a.value} ${a.unit}`.trim();
};

function Operations({ detail }: { detail: ProjectDetail }) {
  const all = financeSeries(detail);
  const pick = (...names: string[]) => all.find((s) => names.includes(s.name));
  const candidates = [
    { key: "rev", label: "营业收入", s: pick("营业收入", "营业总收入") },
    { key: "rd", label: "研发投入", s: pick("研发投入", "研发费用") },
    { key: "np", label: "净利润", s: pick("净利润") },
  ].filter((x) => x.s && x.s.points.some((p) => p.basis === "actual"));
  // A shared axis requires a shared unit and currency. Do not visually compare unlike amounts.
  const series = candidates.filter((x) => x.s!.unit === candidates[0]?.s?.unit && x.s!.currency === candidates[0]?.s?.currency);
  const available = [...new Set(series.flatMap((x) => x.s!.points.filter((p) => p.basis === "actual").map((p) => p.period)))].sort(comparePeriods);
  const periods = available.filter((p) => periodKind(p) === periodKind(available[available.length - 1])).slice(-5);
  const k = detail.finance?.key_numbers;
  const months = k?.runway?.months != null ? Math.floor(k.runway.months) : null;
  if (!periods.length && !k?.cash) return <Visual title="经营与资金"><p className="muted small">还没有可比的财务数字；上传财务资料后显示。</p></Visual>;
  const val = (x: (typeof series)[number], p: string) => x.s!.points.find((pt) => pt.period === p && pt.basis === "actual")?.value;
  const max = Math.max(1, ...series.flatMap((x) => periods.map((p) => Math.abs(val(x, p) ?? 0))));
  const unit = series[0]?.s?.unit ?? "元";
  const np = series.find((x) => x.key === "np"), rev = series.find((x) => x.key === "rev");
  const lastP = periods[periods.length - 1], firstP = periods[0];
  const reading: string[] = [];
  if (rev && lastP && val(rev, lastP) !== undefined) reading.push(`${lastP} 收入 ${money(val(rev, lastP)!, unit)}${firstP !== lastP && val(rev, firstP) !== undefined ? `，${firstP} 为 ${money(val(rev, firstP)!, unit)}` : ""}。`);
  if (np) {
    const npPeriods = periods.filter((p) => val(np, p) !== undefined);
    const npPeriod = npPeriods[npPeriods.length - 1];
    if (npPeriod && val(np, npPeriod)! < 0) reading.push(`${npPeriod} 净亏损 ${money(-val(np, npPeriod)!, unit)}。`);
  }
  if (k?.cash && months != null) reading.push(`账上现金 ${formatAmount(k.cash.value, k.cash.unit).value} ${formatAmount(k.cash.value, k.cash.unit).unit}，按近期消耗约可支撑 ${months} 个月。`);
  const H = 150;
  return <Visual title="经营与资金" note={periods.length ? `${firstP}–${lastP} · 实际数字` : "按最近一期财务资料"} reading={reading}>
    <div className="qa-ops2">
      {periods.length > 0 && <div className="qa-groups" role="group" aria-label={series.map((x) => `${x.label}：${periods.map((p) => `${p} ${val(x, p) !== undefined ? money(val(x, p)!, unit) : "未披露"}`).join("，")}`).join("；")}>
        <div className="qa-groups-plot" style={{ height: H }}>
          <i className="qa-groups-zero" />
          {periods.map((p, pi) => <div key={p} className="qa-group">
            {series.map((x, si) => { const v = val(x, p); if (v === undefined) return <span key={x.key} className="qa-gbar none" />;
              const h = Math.abs(v) / max * (H / 2 - 4);
              return <ChartHint key={x.key} title={x.label} value={money(v,unit)} meta={`${p} · 实际 · ${x.s!.currency || "币种未标注"}`}
                note={v < 0 ? "横线下方为负值；柱高表示金额绝对值。" : "柱高表示金额，与同图同单位的其他期间对照。"}><motion.span className={`qa-gbar ${x.key}${v < 0 ? " neg" : ""}`}
                initial={{ height: 0 }} animate={{ height: Math.max(h, 1.5) }} transition={{ delay: pi * .06 + si * .03, duration: .5, ease: EASE }} /></ChartHint>; })}
            <small>{p}</small>
          </div>)}
        </div>
        <div className="qa-legend">{series.map((x) => <span key={x.key}><i className={`qa-gbar ${x.key}`} />{x.label}</span>)}</div>
      </div>}
      <div className="qa-ops-side">
        {k?.cash && <div><span>账上现金</span><b className="num-display">{formatAmount(k.cash.value, k.cash.unit).value}</b><small>{formatAmount(k.cash.value, k.cash.unit).unit} · {k.cash.period}</small></div>}
        {months != null && <div className="qa-runway">
          <span>现金跑道</span><b className="num-display">{months}<small> 个月</small></b>
          <ChartHint title="现金跑道" value={`约 ${months} 个月`} meta="按现有现金与近期消耗测算" note="刻度上限为36个月，超过部分不继续延长；不是确定的资金到期日。"><div className="qa-runway-track">{[12, 24].map((m) => <i key={m} style={{ left: `${m / 36 * 100}%` }} />)}
            <motion.span className={months < 12 ? "short" : ""} initial={{ width: 0 }} animate={{ width: `${Math.min(months, 36) / 36 * 100}%` }} transition={{ duration: .8, ease: EASE }} /></div></ChartHint>
          <small>0 · 12 · 24 · 36 个月</small>
        </div>}
        {k?.revenue_growth != null && <div><span>收入同比</span><b className={`num-display ${k.revenue_growth >= 0 ? "up" : "down"}`}>{k.revenue_growth >= 0 ? "+" : ""}{k.revenue_growth}%</b></div>}
      </div>
    </div>
  </Visual>;
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

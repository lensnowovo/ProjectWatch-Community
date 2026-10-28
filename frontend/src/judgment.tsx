import { motion } from "motion/react";
import type { ReactNode } from "react";
import { api, type Confidence, type InsightPoint, type InsightRef, type InsightView } from "./api";
import { Block } from "./frame";
import { EASE } from "./motion";
import { useAsync, useOpenSource } from "./ui";
import { ChartHint } from "./chartHint";

/**
 * AI cross-source judgments, shown where their subject lives: the four rings on 当前判断,
 * competitive position on 竞争对手, technology and industry on 行业环境. Every point keeps its numbered sources.
 */

// Ordered low → high so a verdict reads left to right.
export const SCALES = {
  operations: ["pressured", "steady", "improving"],
  position: ["lagging", "behind", "on_par", "ahead", "leading"],
  chain: ["pressured", "steady", "favorable"],
  industry: ["cooling", "consolidating", "stable", "growing", "expanding"],
  maturity: ["dated", "mature", "mainstream", "advanced", "frontier"],
  consensus: ["contrarian", "forming", "consensus"],
} as const;
export type ScaleKey = keyof typeof SCALES;
const SCALE_NOTES: Record<ScaleKey, string> = {
  operations: "从经营承压到经营改善。", position: "从落后到领先，对照可比公司的位置。",
  chain: "从供应、客户关系承压到形成助力。", industry: "从行业降温到加速扩张。",
  maturity: "从趋于落后到前沿探索；越靠右不代表商业化越成熟。", consensus: "从少数派路线到行业共识。",
};

/** Upper half of a scale reads as favourable, the bottom step as adverse. */
export function verdictTone(scale: ScaleKey, verdict?: string): "up" | "down" | "" {
  const steps = SCALES[scale] as readonly string[];
  const at = verdict ? steps.indexOf(verdict) : -1;
  if (at < 0) return "";
  return at > (steps.length - 1) / 2 ? "up" : at === 0 || (steps.length === 5 && at === 1) ? "down" : "";
}

const CONFIDENCE_LABEL: Record<Confidence, string> = { high: "把握高", medium: "把握中", low: "把握低" };

/** One insight request per project revision; every tab that shows a judgment reads the same record. */
export function useInsight(projectId: string, revision: string) {
  return useAsync(() => api.insight(projectId).catch(() => null), [projectId, revision]);
}

export function Confidence({ value }: { value?: Confidence }) {
  if (!value) return null;
  const n = value === "high" ? 3 : value === "medium" ? 2 : 1;
  return <span className="jd-conf" title={CONFIDENCE_LABEL[value]}>{[0, 1, 2].map((i) => <i key={i} className={i < n ? "on" : ""} />)}{CONFIDENCE_LABEL[value]}</span>;
}

export function Refs({ refs }: { refs: InsightRef[] }) {
  const open = useOpenSource();
  if (!refs.length) return null;
  return <span className="jd-refs">{refs.map((r) => r.kind === "internal" && r.source_id
    ? <button key={r.code} className="jd-ref" title={`${r.label}：${r.text}`} onClick={(e) => { e.stopPropagation(); open({ source_id: r.source_id!, source_name: r.source_name || "内部资料", locator: r.locator || "" }); }}>{r.code}</button>
    : r.url ? <a key={r.code} className="jd-ref" href={r.url} target="_blank" rel="noreferrer noopener" onClick={(e) => e.stopPropagation()}
      title={`${r.label}：${r.text}${r.date ? ` · ${r.date}` : ""}`}>{r.code}</a>
    : <span key={r.code} className="jd-ref" title={`${r.label}：${r.text}`}>{r.code}</span>)}</span>;
}

export function Points({ title, points, tone, empty }: { title: string; points: InsightPoint[]; tone?: "plus" | "minus"; empty?: string }) {
  if (!points.length && !empty) return null;
  return <div className="jd-points">
    <h4>{title}</h4>
    {points.length ? <ul>{points.map((p, i) => <motion.li key={i} className={tone ?? ""} initial={{ opacity: 0, x: -6 }} animate={{ opacity: 1, x: 0 }}
      transition={{ delay: .04 * i, duration: .3, ease: EASE }}>{p.text}<Refs refs={p.refs} /></motion.li>)}</ul>
      : <p className="jd-empty">{empty}</p>}
  </div>;
}

/** A labelled 1-D scale with an animated marker; "unclear" leaves the track empty. */
export function Scale({ scale, value, labels }: { scale: ScaleKey; value: string; labels: Record<string, string> }) {
  const steps = SCALES[scale] as readonly string[];
  const index = steps.indexOf(value);
  const tone = verdictTone(scale, value);
  return <div className={`jd-scale ${index < 0 ? "unclear" : ""}`} role="group" aria-label={labels[value] ?? value}>
    <div className="jd-scale-track">
      {steps.map((s, i) => <ChartHint key={s} title="判断档位" value={labels[s] ?? s}
        meta={i === index ? "● 当前判断" : index < 0 ? "尚无当前判断" : `当前判断：${labels[value] ?? value}`}
        note={`${SCALE_NOTES[scale]}这是定性档位，不是分数或百分比。`}><span className={i === index ? `on ${tone}` : ""} /></ChartHint>)}
      {index >= 0 && <motion.i className={`jd-scale-marker ${tone}`} initial={{ left: "50%", opacity: 0 }}
        animate={{ left: `${(index + .5) / steps.length * 100}%`, opacity: 1 }} transition={{ duration: .7, ease: EASE }} />}
    </div>
    <div className="jd-scale-labels">{steps.map((s) => <span key={s} className={s === value ? "on" : ""}>{labels[s]}</span>)}</div>
  </div>;
}

function ChangeIf({ items }: { items: string[] }) {
  if (!items.length) return null;
  return <div className="jd-points"><h4>什么信息会改变判断</h4><ul>{items.map((t, i) => <li key={i}>{t}</li>)}</ul></div>;
}

/** Shared body: scale on top, for/against side by side, then what would change it. */
export function JudgmentBody({ scale, verdict, labels, summary, pro, con, proTitle = "支持这个判断", conTitle = "相反的依据", changeIf = [], children, compact = false }: {
  scale: ScaleKey; verdict: string; labels: Record<string, string>; summary?: string;
  pro: InsightPoint[]; con: InsightPoint[]; proTitle?: string; conTitle?: string; changeIf?: string[]; children?: ReactNode; compact?: boolean;
}) {
  return <div className={`jd-body${compact ? ' jd-body-compact' : ''}`}>
    <div className={`jd-overview${summary || children ? ' jd-overview-split' : ''}`}>
    <Scale scale={scale} value={verdict} labels={labels} />
    {(summary || children) && <div className="jd-reading">
    {summary && <p className="jd-summary">{summary}</p>}
    {children}
    </div>}
    </div>
    <JudgmentEvidence compact={compact}>
    <div className="jd-cols">
      <Points title={proTitle} points={pro} tone="plus" empty="没有带出处的依据。" />
      <Points title={conTitle} points={con} tone="minus" empty="没有带出处的相反依据。" />
    </div>
    <ChangeIf items={changeIf} />
    </JudgmentEvidence>
  </div>;
}

function JudgmentEvidence({compact,children}:{compact:boolean;children:ReactNode}) {
  return compact ? <details className="chart-evidence"><summary>判断依据、反向信号与风险</summary>{children}</details> : <>{children}</>;
}

function Provenance({ view }: { view: InsightView }) {
  const record = view.record!;
  // An external Agent's write-back keeps its name; the analysis and source statements are stored separately.
  return <span className="jd-meta">{record?.origin?.producer ? `${record.origin.producer} 写回` : "AI"} 交叉分析 · 依据 {record.total} 条 · {new Date(record.generated_at).toLocaleDateString("zh-CN")}{record.status === "stale" || record.dependency?.status === "stale" ? " · 依据已变化，保留旧版待更新" : ""}</span>;
}

/** 竞争对手: where the company stands against its peers. */
export function PositionJudgment({ view, compact = false }: { view: InsightView | null; compact?: boolean }) {
  const p = view?.record?.analysis.position;
  if (!view?.record || !p) return null;
  return <Block card title="竞争位置判断" note={<Provenance view={view} />} action={<Confidence value={p.confidence} />}>
    <JudgmentBody compact={compact} scale="position" verdict={p.verdict} labels={view.labels.position} pro={p.for} con={p.against} changeIf={p.change_if}>
      {p.peers.length > 0 && <p className="jd-peers"><span>对比对象</span>{p.peers.map((n) => <b key={n}>{n}</b>)}</p>}
    </JudgmentBody>
  </Block>;
}

/** 行业环境: whether the industry is expanding or cooling, with drivers and headwinds. */
export function ChainJudgment({view}:{view:InsightView}) {
  const d=view.record?.analysis.chain;
  if(!d)return null;
  return <Block card title="产业链影响判断" note={<Provenance view={view}/>} action={<Confidence value={d.confidence}/>}>
    <JudgmentBody compact scale="chain" verdict={d.verdict} labels={view.labels.chain} summary={d.summary} pro={d.for} con={d.against} changeIf={d.change_if}/>
  </Block>;
}

export function IndustryJudgment({ view, compact = false }: { view: InsightView | null; compact?: boolean }) {
  const d = view?.record?.analysis.industry;
  if (!view?.record || !d) return null;
  return <Block card title="行业发展判断" note={<Provenance view={view} />} action={<Confidence value={d.confidence} />}>
    <JudgmentBody compact={compact} scale="industry" verdict={d.verdict} labels={view.labels.industry} summary={d.summary}
      pro={d.drivers} con={d.headwinds} proTitle="驱动" conTitle="阻力" />
  </Block>;
}

/** 行业环境: the company's technical bet placed on consensus × maturity, with academic and industry signals. */
export function TechnologyJudgment({ view, compact = false }: { view: InsightView | null; compact?: boolean }) {
  const t = view?.record?.analysis.technology;
  if (!view?.record || !t) return null;
  return <Block card title="技术方向判断" note={<Provenance view={view} />} action={<Confidence value={t.confidence} />}>
    <div className="jd-tech">
      <div className="jd-tech-copy">
        <span className="jd-label">押注的技术路线</span>
        <strong className="jd-route">{t.route || "未识别"}</strong>
        <div className="jd-badges"><b>成熟度：{view.labels.maturity[t.maturity] ?? t.maturity}</b><b>共识：{view.labels.consensus[t.consensus] ?? t.consensus}</b></div>
        {t.summary && <p className="jd-summary">{t.summary}</p>}
      </div>
      <TechMap maturity={t.maturity} consensus={t.consensus} labels={view.labels} />
    </div>
    <JudgmentEvidence compact={compact}><div className="jd-cols">
      <Points title="学术界信号（论文）" points={t.academic} empty="素材中没有可引用的论文信号。" />
      <Points title="产业界信号（融资、竞品、产品）" points={t.industry} empty="素材中没有可引用的产业信号。" />
    </div>
    <Points title="技术路线风险" points={t.risks} tone="minus" />
    </JudgmentEvidence>
  </Block>;
}

/** Consensus (x) against maturity (y); the company's bet is one point. */
export function TechMap({ maturity, consensus, labels }: { maturity: string; consensus: string; labels: InsightView["labels"] }) {
  const xs = SCALES.consensus as readonly string[], ys = SCALES.maturity as readonly string[];
  const xi = xs.indexOf(consensus), yi = ys.indexOf(maturity);
  const W = 400, H = 260, pad = { l: 72, r: 12, t: 12, b: 36 };
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  const x = (i: number) => pad.l + (i + .5) / xs.length * iw;
  const y = (i: number) => H - pad.b - (i + .5) / ys.length * ih;
  return <figure className="jd-map">
    <svg viewBox={`0 0 ${W} ${H}`} role="group" aria-label={`技术成熟度：${labels.maturity[maturity] ?? "未定"}；行业共识：${labels.consensus[consensus] ?? "未定"}`}>
      <rect x={pad.l} y={pad.t} width={iw} height={ih} className="jd-map-bg" rx="4" />
      {xs.map((c, i) => <g key={c}>
        {i > 0 && <line x1={pad.l + i / xs.length * iw} x2={pad.l + i / xs.length * iw} y1={pad.t} y2={H - pad.b} className="jd-map-grid" />}
        <text x={x(i)} y={H - pad.b + 20} className={`jd-map-tick ${c === consensus ? "on" : ""}`}>{labels.consensus[c]}</text></g>)}
      {ys.map((m, i) => <g key={m}>
        {i > 0 && <line x1={pad.l} x2={W - pad.r} y1={H - pad.b - i / ys.length * ih} y2={H - pad.b - i / ys.length * ih} className="jd-map-grid" />}
        <text x={pad.l - 8} y={y(i) + 4} className={`jd-map-tick end ${m === maturity ? "on" : ""}`}>{labels.maturity[m]}</text></g>)}
      {xi >= 0 && yi >= 0 ? <ChartHint title="技术路线定位" value={`${labels.maturity[maturity]} · ${labels.consensus[consensus]}`}
        meta="横轴：共识程度 / 纵轴：技术成熟度" note="圆点表示当前判断，不是精确评分；前沿不等于已经成熟商用。"><motion.g initial={{ opacity: 0, scale: .4 }} animate={{ opacity: 1, scale: 1 }} transition={{ duration: .6, delay: .2, ease: EASE }}
        style={{ transformOrigin: `${x(xi)}px ${y(yi)}px` }}>
        <circle cx={x(xi)} cy={y(yi)} r={16} className="jd-map-halo" />
        <circle cx={x(xi)} cy={y(yi)} r={6} className="jd-map-dot" />
      </motion.g></ChartHint> : <text x={pad.l + iw / 2} y={pad.t + ih / 2} className="jd-map-tick">依据不足，暂不定位</text>}
    </svg>
    <figcaption>横轴：行业与学术界的共识程度　纵轴：技术成熟度</figcaption>
  </figure>;
}

/** 当前判断 › 参考与依据: what the analysis read, what it cited and what it still lacks. */
export function EvidenceGaps({ view }: { view: InsightView | null }) {
  const record = view?.record;
  if (!view || !record) return null;
  const a = record.analysis;
  const max = Math.max(1, ...Object.values(record.counts));
  const used = new Set(a.cited.map((r) => r.code[0]));
  return <div className="jd-evidence">
    <div className="jd-cols">
      <div className="jd-points">
        <h4>素材构成</h4>
        <ul className="jd-bars">{Object.entries(record.counts).map(([k, n]) => <li key={k}>
          <span>{view.sources[k] ?? k}</span>
          <span className="jd-bar"><i style={{ width: `${n / max * 100}%` }} className={used.has(k) ? "used" : ""} /></span>
          <b>{n}</b></li>)}</ul>
        <p className="jd-empty">深色表示该类素材被判断引用；共引用 {a.cited.length} 条。</p>
      </div>
      <Points title="还缺什么信息" points={a.gaps.map((g) => ({ text: g, refs: [] }))} empty="暂无补充事项。" />
    </div>
    {a.cited.length > 0 && <div className="jd-points">
      <h4>被引用的素材</h4>
      <ol className="jd-cited">{a.cited.map((r) => <li key={r.code}><Refs refs={[r]} /><span className="jd-cited-label">{r.label}{r.date ? ` · ${r.date}` : ""}</span><span>{r.text}</span></li>)}</ol>
    </div>}
    {record.origin?.producer && <p className="jd-empty">{record.origin.producer} 根据已读材料写回，分析与来源陈述分开保存。</p>}
    <p className="jd-empty">以上为 AI 基于已收录素材的判断，不是事实结论；每条依据可点开核对原文。素材有实质变化时自动重新分析（每天最多一次）。</p>
  </div>;
}

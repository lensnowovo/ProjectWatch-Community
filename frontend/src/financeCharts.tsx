import { motion } from "motion/react";
import { useState } from "react";
import type { Metric, RatioRow, Ref, Runway } from "./api";
import { comparePeriods, consensus, mValue, periodKind, periodLabel, sourceTitle, type Row } from "./metrics";
import { EASE } from "./motion";
import { formatAmount, useOpenSource } from "./ui";
import { Seg } from "./widgets";

type Point2 = { period: string; metric: Metric; value: number };
type Ratio = { period: string; value: number; formula: string };
type Step = { label: string; from: number; to: number; kind: "total" | "down" | "up"; computed?: boolean; point?: Point2 };

const KIND_TEXT: Record<string, string> = { annual: "年度", half: "半年度", quarter: "季度", month: "月度" };
const amount = (v: number, unit: string) => { const a = formatAmount(v, unit); return `${a.value} ${a.unit}`.trim(); };
const refOf = (p: Point2): Ref => ({ source_id: p.metric.source_id, source_name: p.metric.source_name ?? "资料", locator: p.metric.locator });

function actual(row: Row | undefined, period: string): Point2 | undefined {
  if (!row) return undefined;
  const metric = consensus(row.cells.get(period) ?? [], row.unit);
  return metric ? { period, metric, value: mValue(metric) as number } : undefined;
}

/** Revenue → cost → gross profit → expenses & other (difference) → net profit, for one period of actuals. */
export function waterfallSteps(rows: { revenue?: Row; cost?: Row; gross?: Row; profit?: Row }, period: string): Step[] | null {
  const rev = actual(rows.revenue, period), net = actual(rows.profit, period);
  const cost = actual(rows.cost, period), grossP = actual(rows.gross, period);
  if (!rev || !net || (!cost && !grossP)) return null;
  const gross = grossP?.value ?? rev.value - cost!.value;
  return [
    { label: "营业收入", from: 0, to: rev.value, kind: "total", point: rev },
    { label: "营业成本", from: rev.value, to: gross, kind: "down", computed: !cost, point: cost },
    { label: "毛利", from: 0, to: gross, kind: "total", computed: !grossP, point: grossP },
    { label: "费用及其他", from: gross, to: net.value, kind: net.value <= gross ? "down" : "up", computed: true },
    { label: "净利润", from: 0, to: net.value, kind: "total", point: net },
  ];
}

export function ProfitWaterfall({ revenue, cost, gross, profit }: { revenue?: Row; cost?: Row; gross?: Row; profit?: Row }) {
  const open = useOpenSource();
  const [chosen, setChosen] = useState<string | null>(null);
  const periods = revenue ? [...revenue.cells.keys()].filter((p) => waterfallSteps({ revenue, cost, gross, profit }, p)).sort(comparePeriods).reverse() : [];
  const period = chosen && periods.includes(chosen) ? chosen : periods[0];
  if (!period || !revenue) return null;
  const steps = waterfallSteps({ revenue, cost, gross, profit }, period)!;
  const unit = revenue.unit;
  const levels = steps.flatMap((s) => [s.from, s.to]);
  const lo = Math.min(0, ...levels), hi = Math.max(0, ...levels);
  const W = 560, H = 240, pad = { t: 24, b: 34, l: 6, r: 6 };
  const y = (v: number) => pad.t + (hi - v) / (hi - lo || 1) * (H - pad.t - pad.b);
  const band = (W - pad.l - pad.r) / steps.length, bw = band * 0.56;
  return (
    <figure className="fi-chart">
      <div className="fi-chart-head">
        <h4>利润是怎么形成的</h4>
        {periods.length > 1 && <Seg id="fi-waterfall" value={period} onChange={setChosen} options={periods.slice(0, 4).map((p) => ({ value: p, label: periodLabel(p) }))} />}
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} role="img"
        aria-label={`${periodLabel(period)} 利润构成：${steps.map((s) => `${s.label} ${amount(s.kind === "total" ? s.to : s.to - s.from, unit)}`).join("，")}`}>
        <line x1={pad.l} x2={W - pad.r} y1={y(0)} y2={y(0)} className="axis" />
        {steps.map((s, i) => {
          const x = pad.l + i * band + (band - bw) / 2;
          const top = y(Math.max(s.from, s.to)), h = Math.max(1.5, Math.abs(y(s.from) - y(s.to)));
          const delta = s.kind === "total" ? s.to : s.to - s.from;
          const next = steps[i + 1];
          const negative = s.kind === "down" || (s.kind === "total" && s.to < 0);
          const point = s.point;
          const label = s.kind === "total" ? amount(s.to, unit) : `${delta >= 0 ? "+" : "−"}${amount(Math.abs(delta), unit)}`;
          return (
            <g key={`${period}-${s.label}`} className={point ? "fi-clickable" : undefined} role={point ? "button" : undefined} tabIndex={point ? 0 : undefined}
              onClick={() => point && open(refOf(point))}
              onKeyDown={(e) => { if (point && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); open(refOf(point)); } }}>
              <title>{point ? sourceTitle(point.metric) : `${s.label}：${label}（${s.label === "费用及其他" ? "净利润 − 毛利，差额计算" : "由其他科目差额计算"}）`}</title>
              <motion.rect x={x} width={bw} rx={3} className={negative ? "fi-wf-neg" : "fi-wf-total"}
                initial={{ y: y(s.from), height: 0 }} animate={{ y: top, height: h }}
                transition={{ duration: 0.55, ease: EASE, delay: 0.12 * i }} />
              {next && next.kind !== "total" && <motion.line x1={x + bw} x2={x + band} y1={y(s.to)} y2={y(s.to)} className="fi-wf-link"
                initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 0.12 * i + 0.45 }} />}
              <motion.text x={x + bw / 2} y={Math.max(13, top - 7)} className="bar-label" initial={{ opacity: 0 }} animate={{ opacity: 1 }}
                transition={{ delay: 0.12 * i + 0.35 }}>{label}</motion.text>
              <text x={x + bw / 2} y={H - 12} className="bar-axis">{s.label}{s.computed ? "*" : ""}</text>
            </g>
          );
        })}
      </svg>
      <figcaption className="muted small">{periodLabel(period)} · 实际数 · 点击柱子看原文。* 差额计算：“费用及其他” = 净利润 − 毛利，含期间费用、其他收益与一次性项目。</figcaption>
    </figure>
  );
}

/** One small chart per margin so a −300% net margin does not flatten the gross margin; one period length at a time. */
function MarginLine({ name, cells }: { name: string; cells: Ratio[] }) {
  const W = 360, H = 112, pad = { t: 20, b: 24, l: 34, r: 34 };
  const values = cells.map((c) => c.value);
  // A line needs no zero baseline: pad the observed range so small changes stay visible.
  const span = Math.max(...values) - Math.min(...values) || Math.abs(values[0]) || 1;
  const lo = Math.min(...values) - span * 0.25, hi = Math.max(...values) + span * 0.25;
  const x = (i: number) => pad.l + i * (W - pad.l - pad.r) / Math.max(1, cells.length - 1);
  const y = (v: number) => pad.t + (hi - v) / (hi - lo || 1) * (H - pad.t - pad.b);
  const path = cells.map((c, i) => `${i ? "L" : "M"}${x(i)},${y(c.value)}`).join(" ");
  const last = cells[cells.length - 1];
  return (
    <figure className="fi-mini">
      <figcaption><span>{name}</span><strong className={last.value < 0 ? "down" : ""}>{last.value.toFixed(1)}%</strong><small className="muted">{periodLabel(last.period)}</small></figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${name}：${cells.map((c) => `${periodLabel(c.period)} ${c.value.toFixed(1)}%`).join("，")}`}>
        {lo < 0 && hi > 0 && <line x1={pad.l - 8} x2={W - pad.r + 8} y1={y(0)} y2={y(0)} className="fi-zero" />}
        <motion.path d={path} className="fi-line" initial={{ pathLength: 0 }} animate={{ pathLength: 1 }} transition={{ duration: 0.9, ease: EASE }} />
        {cells.map((c, i) => (
          <g key={c.period}>
            <circle cx={x(i)} cy={y(c.value)} r={10} className="track-hit"><title>{`${periodLabel(c.period)}：${c.value.toFixed(1)}%\n${c.formula}`}</title></circle>
            <motion.circle cx={x(i)} cy={y(c.value)} r={4} className={c.value < 0 ? "fi-dot neg" : "fi-dot"}
              initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 0.5 + 0.1 * i }} />
            <text x={x(i)} y={H - 6} className="fi-tick">{periodLabel(c.period)}</text>
            {(i === 0 || i === cells.length - 1) && <text x={x(i)} y={y(c.value) - 10} className="fi-point-label">{c.value.toFixed(1)}%</text>}
          </g>
        ))}
      </svg>
    </figure>
  );
}

export function MarginTrends({ ratios }: { ratios: RatioRow[] }) {
  const [chosen, setChosen] = useState<string | null>(null);
  const series = (name: string): Ratio[] => Object.entries(ratios.find((r) => r.name === name)?.cells ?? {})
    .filter(([, c]) => c.basis === "actual").map(([period, c]) => ({ period, value: c.value, formula: c.formula }))
    .sort((a, b) => comparePeriods(a.period, b.period));
  const names = ["毛利率", "净利率"];
  const kinds = ["annual", "half", "quarter", "month"].filter((k) => names.some((n) => series(n).filter((c) => periodKind(c.period) === k).length >= 2));
  const kind = chosen && kinds.includes(chosen) ? chosen : kinds[0];
  if (!kind) return null;
  // A ratio beyond ±300% means revenue is too small for the ratio to say anything; such a series is not drawn.
  const all = names.map((n) => ({ n, cells: series(n).filter((c) => periodKind(c.period) === kind) })).filter((c) => c.cells.length >= 2);
  const charts = all.filter((c) => c.cells.every((x) => Math.abs(x.value) <= 300));
  const dropped = all.filter((c) => !charts.includes(c)).map((c) => c.n);
  if (!charts.length) return dropped.length ? <p className="muted small fi-dropped">收入基数过小，{dropped.join("、")}超过 ±300%，不作图；数值见下方财务分析表。</p> : null;
  return (
    <div className="fi-chart">
      <div className="fi-chart-head">
        <h4>利润率走势</h4>
        {kinds.length > 1 && <Seg id="fi-margin-kind" value={kind} onChange={setChosen} options={kinds.map((k) => ({ value: k, label: KIND_TEXT[k] }))} />}
      </div>
      <div className="fi-minis">{charts.map((c) => <MarginLine key={`${c.n}-${kind}`} name={c.n} cells={c.cells} />)}</div>
      <p className="muted small">每张图只连接同类期间，纵轴各自独立；悬停看公式。{dropped.length > 0 && `${dropped.join("、")}因收入基数过小（超过 ±300%）不作图。`}</p>
    </div>
  );
}

const METHOD_LABEL: Record<string, string> = { cash_change: "现金余额实际减少", operating_cash_flow: "经营活动现金流", net_profit: "净亏损（近似）" };

/** Month index of a balance date: 2025 → Dec 2025, 2025H1 → Jun, 2025M09 → Sep, 2025Q3 → Sep. */
function monthIndex(period: string): number | null {
  const m = /^(\d{4})(.*)$/.exec(period);
  if (!m) return null;
  const rest = m[2];
  const month = rest === "" || rest === "H2" ? 12 : rest === "H1" ? 6 : /^M(\d{2})$/.test(rest) ? Number(rest.slice(1))
    : /^Q([1-4])$/.test(rest) ? Number(rest[1]) * 3 : null;
  return month === null ? null : Number(m[1]) * 12 + month - 1;
}
const monthText = (idx: number) => `${Math.floor(idx / 12)}年${(idx % 12) + 1}月`;

/** 现金还能撑多久: reported cash balances, then a dashed straight line at today's burn to zero. */
export function RunwayChart({ cashRow, runway }: { cashRow?: Row; runway: Runway | null }) {
  const open = useOpenSource();
  const points = cashRow ? [...cashRow.cells.keys()]
    .map((p) => ({ p, idx: monthIndex(p), hit: actual(cashRow, p) }))
    .filter((x): x is { p: string; idx: number; hit: Point2 } => x.idx !== null && !!x.hit)
    .sort((a, b) => a.idx - b.idx).slice(-6) : [];
  const alts = (runway?.alternatives ?? []).filter((a) => a.months !== null);
  const endIdx = runway?.cash_period ? monthIndex(runway.cash_period) : null;
  const project = runway?.months != null && endIdx !== null && (runway.status === "ok" || runway.status === "short");
  if (points.length < 1 && !alts.length) return null;
  const zeroIdx = project ? endIdx! + Math.round(runway!.months!) : null;
  const W = 520, H = 170, pad = { t: 22, b: 26, l: 40, r: 54 };
  const x0 = points.length ? points[0].idx : endIdx ?? 0;
  const x1 = Math.max(zeroIdx ?? 0, points.length ? points[points.length - 1].idx : 0, x0 + 1);
  const hi = Math.max(1, ...points.map((pt) => pt.hit.value), runway?.cash ?? 0);
  const x = (i: number) => pad.l + (i - x0) / (x1 - x0) * (W - pad.l - pad.r);
  const y = (v: number) => pad.t + (hi - Math.max(0, v)) / hi * (H - pad.t - pad.b);
  const history = points.map((pt, i) => `${i ? "L" : "M"}${x(pt.idx)},${y(pt.hit.value)}`).join(" ");
  const last = points[points.length - 1];
  const unit = cashRow?.unit || runway?.unit || "万元";
  const maxMonths = Math.max(1, ...alts.map((a) => a.months!));
  return (
    <div className="fi-runway">
      {points.length > 0 && <figure className="fi-chart">
        <div className="fi-chart-head"><h4>现金还能撑多久</h4>{project && <span className="muted small">按当前消耗约 {monthText(zeroIdx!)} 用完</span>}</div>
        <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`账上现金：${points.map((pt) => `${periodLabel(pt.p)} ${amount(pt.hit.value, unit)}`).join("，")}${project ? `；按当前消耗约 ${monthText(zeroIdx!)} 用完` : ""}`}>
          <line x1={pad.l} x2={W - pad.r} y1={y(0)} y2={y(0)} className="axis" />
          {history && <motion.path d={history} className="fi-line" initial={{ pathLength: 0 }} animate={{ pathLength: 1 }} transition={{ duration: 0.8, ease: EASE }} />}
          {project && last && <motion.path d={`M${x(endIdx!)},${y(runway!.cash)} L${x(zeroIdx!)},${y(0)}`} className="fi-projection"
            initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: 0.6, delay: 0.7, ease: EASE }} />}
          {project && <motion.g initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 1.4 }}>
            <circle cx={x(zeroIdx!)} cy={y(0)} r={4} className="fi-dot neg" />
            <text x={x(zeroIdx!)} y={y(0) - 9} className="fi-point-label">{monthText(zeroIdx!)}</text>
          </motion.g>}
          {points.map((pt, i) => (
            <g key={pt.p} className="fi-clickable" role="button" tabIndex={0} onClick={() => open(refOf(pt.hit))}
              onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(refOf(pt.hit)); } }}>
              <title>{sourceTitle(pt.hit.metric)}</title>
              <circle cx={x(pt.idx)} cy={y(pt.hit.value)} r={11} className="track-hit" />
              <motion.circle cx={x(pt.idx)} cy={y(pt.hit.value)} r={4} className="fi-dot" initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 0.15 * i }} />
              {(i === points.length - 1 || i === 0) && <text x={x(pt.idx)} y={y(pt.hit.value) - 10} className="fi-point-label">{amount(pt.hit.value, unit)}</text>}
              <text x={x(pt.idx)} y={H - 8} className="fi-tick">{periodLabel(pt.p).replace(/^(\d{4})-(\d{2})$/, "$1.$2")}</text>
            </g>
          ))}
        </svg>
        <figcaption className="muted small">实线为资料中的期末现金（点击看原文）；虚线按当前月均消耗线性推算，不是预测，不含新增融资或借款到期。</figcaption>
      </figure>}
      {alts.length > 1 && <div className="fi-methods" role="group" aria-label="不同口径的现金续航">
        <h4>不同口径估算 <span className="muted small">取消耗最快的已测口径</span></h4>
        {alts.map((a) => (
          <div key={a.method} className={`fi-method ${a.method === runway?.method ? "on" : ""}`} title={a.text}>
            <span>{METHOD_LABEL[a.method] ?? a.label}{a.method === runway?.method ? " · 采用" : ""}</span>
            <span className="fi-bar-track"><motion.i initial={{ width: 0 }} animate={{ width: `${a.months! / maxMonths * 100}%` }} transition={{ duration: 0.7, ease: EASE }} /></span>
            <strong>{Math.floor(a.months!)} 个月</strong>
            <small className="muted">月均 {amount(a.burn, unit)}</small>
          </div>
        ))}
      </div>}
    </div>
  );
}

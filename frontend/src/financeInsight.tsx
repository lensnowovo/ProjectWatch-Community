import { motion } from "motion/react";
import type { FinanceView, Metric, Point, RatioRow, Ref } from "./api";
import { MarginTrends, ProfitWaterfall, RunwayChart } from "./financeCharts";
import { comparePeriods, consensus, mValue, periodLabel, sourceTitle, type Row } from "./metrics";
import { EASE } from "./motion";
import { RefChips, formatAmount, useOpenSource } from "./ui";

/**
 * 财务解读: rule-based reading of the extracted statements. Only like-for-like
 * periods are compared (H1 with the prior H1, a full year with the prior year);
 * nothing is estimated, and every number opens its source.
 */

type Tone = "ok" | "neutral" | "warn" | "none";
type Point2 = { period: string; metric: Metric; value: number };
type Ratio = { period: string; value: number; refs: Ref[]; formula: string };
interface Dimension {
  key: string; title: string; verdict: string; tone: Tone; summary: string;
  facts: string[]; hint?: string; pair?: { label: string; prev?: Point2; cur: Point2; unit: string }; refs?: Ref[];
}

const TONE_CLASS: Record<Tone, string> = { ok: "tag-ok", neutral: "", warn: "tag-warn", none: "" };
const RISK_TERMS = /现金|亏损|收入|营收|毛利|利润|负债|融资|借款|贷款|减值|应收|存货|流动性|续航|消耗/;

/** Same period one year earlier: 2026H1 → 2025H1, 2025 → 2024. */
export function priorPeriod(period: string): string | null {
  const match = /^(\d{4})(.*)$/.exec(period);
  return match ? `${Number(match[1]) - 1}${match[2]}` : null;
}
const isAnnual = (period: string) => /^\d{4}$/.test(period);

/** Latest reported actual for a period, in the row's main unit; forecasts never enter a comparison. */
function actual(row: Row | undefined, period: string | null): Point2 | undefined {
  if (!row || !period) return undefined;
  const metric = consensus(row.cells.get(period) ?? [], row.unit);
  return metric ? { period, metric, value: mValue(metric) as number } : undefined;
}

function latest(row: Row | undefined): Point2 | undefined {
  if (!row) return undefined;
  const periods = [...row.cells.keys()].sort(comparePeriods).reverse();
  for (const p of periods) { const hit = actual(row, p); if (hit) return hit; }
  return undefined;
}

function latestAnnualPair(row: Row | undefined, after?: string) {
  if (!row) return undefined;
  const years = [...row.cells.keys()].filter(isAnnual).sort(comparePeriods).reverse();
  for (const y of years) {
    if (after && y === after) continue;
    const cur = actual(row, y), prev = actual(row, priorPeriod(y));
    if (cur && prev) return { cur, prev };
  }
  return undefined;
}

const amount = (v: number, unit: string) => { const a = formatAmount(v, unit); return `${a.value} ${a.unit}`.trim(); };
const pct = (v: number) => `${v >= 0 ? "+" : ""}${v.toFixed(1)}%`;
const growth = (cur: number, prev: number) => (prev > 0 ? (cur / prev - 1) * 100 : null);
const refOf = (p: Point2): Ref => ({ source_id: p.metric.source_id, source_name: p.metric.source_name ?? "资料", locator: p.metric.locator });

function ratio(rows: RatioRow[], name: string, period: string | null): Ratio | undefined {
  const cell = period ? rows.find((r) => r.name === name)?.cells[period] : undefined;
  return cell && cell.basis === "actual" ? { period: period!, value: cell.value, refs: cell.refs, formula: cell.formula } : undefined;
}

function growthDimension(revenue?: Row): Dimension | null {
  const cur = latest(revenue);
  if (!revenue || !cur) return null;
  const prev = actual(revenue, priorPeriod(cur.period));
  const rate = prev ? growth(cur.value, prev.value) : null;
  const annual = !isAnnual(cur.period) ? latestAnnualPair(revenue) : undefined;
  const annualRate = annual ? growth(annual.cur.value, annual.prev.value) : null;
  const facts = [`${periodLabel(cur.period)} 营业收入 ${amount(cur.value, revenue.unit)}${prev && rate !== null ? `，同比 ${pct(rate)}（上年同期 ${amount(prev.value, revenue.unit)}）` : "；资料中没有上年同期数字，无法计算同比"}。`];
  if (annual && annualRate !== null) facts.push(`${periodLabel(annual.cur.period)} 全年 ${amount(annual.cur.value, revenue.unit)}，同比 ${pct(annualRate)}。`);
  const verdict = rate === null ? "缺少同期数据" : rate >= 15 ? "较快增长" : rate >= 0 ? "小幅增长" : "收入下滑";
  const hint = rate !== null && annualRate !== null && annualRate - rate > 20
    ? `本期增速明显低于 ${periodLabel(annual!.cur.period)} 全年，关注增长是否放缓及原因。`
    : rate !== null && rate < 0 ? "收入同比下降，关注主要产品销量、价格或客户变化。" : undefined;
  return { key: "growth", title: "增长", verdict, tone: rate === null ? "none" : rate >= 0 ? (rate >= 15 ? "ok" : "neutral") : "warn",
    summary: rate === null ? "收入缺少同期对比" : `收入${verdict === "收入下滑" ? "同比下滑" : verdict}`, facts, hint,
    pair: { label: "营业收入", prev, cur, unit: revenue.unit } };
}

function marginDimension(ratios: RatioRow[], revenue?: Row): Dimension | null {
  const anchor = latest(revenue)?.period
    ?? [...new Set(ratios.flatMap((r) => Object.keys(r.cells)))].sort(comparePeriods).reverse()[0];
  const cur = ratio(ratios, "毛利率", anchor ?? null);
  if (!cur) return null;
  const prev = ratio(ratios, "毛利率", priorPeriod(cur.period));
  const delta = prev ? cur.value - prev.value : null;
  const verdict = delta === null ? "缺少同期数据" : delta <= -3 ? "毛利率下降" : delta >= 3 ? "毛利率提升" : "毛利率基本持平";
  const facts = [`${periodLabel(cur.period)} 毛利率 ${cur.value.toFixed(1)}%${prev && delta !== null
    ? `，上年同期 ${prev.value.toFixed(1)}%，${delta >= 0 ? "上升" : "下降"} ${Math.abs(delta).toFixed(1)} 个百分点` : ""}。`];
  const hint = delta !== null && Math.abs(delta) >= 3
    ? `每 100 元收入${delta < 0 ? "少" : "多"}留下约 ${Math.abs(delta).toFixed(0)} 元毛利；需从资料中确认是价格、成本还是产品结构变化。` : undefined;
  return { key: "margin", title: "盈利能力", verdict, tone: delta === null ? "none" : delta <= -3 ? "warn" : delta >= 3 ? "ok" : "neutral",
    summary: verdict, facts, hint, refs: [...cur.refs, ...(prev?.refs ?? [])] };
}

function profitDimension(ratios: RatioRow[], profit?: Row, revenueRate?: number | null): Dimension | null {
  const cur = latest(profit);
  if (!profit || !cur) return null;
  const prev = actual(profit, priorPeriod(cur.period));
  const label = (v: number) => (v < 0 ? `净亏损 ${amount(-v, profit.unit)}` : `净利润 ${amount(v, profit.unit)}`);
  let verdict = "缺少同期数据", tone: Tone = "none", change: number | null = null;
  if (prev) {
    if (cur.value < 0 && prev.value < 0) {
      change = (1 - cur.value / prev.value) * 100;
      verdict = change >= 0 ? "亏损收窄" : "亏损扩大"; tone = change >= 0 ? "ok" : "warn";
    } else if (cur.value >= 0 && prev.value < 0) { verdict = "扭亏为盈"; tone = "ok"; }
    else if (cur.value < 0 && prev.value >= 0) { verdict = "由盈转亏"; tone = "warn"; }
    else { change = growth(cur.value, prev.value); verdict = (change ?? 0) >= 0 ? "利润增长" : "利润下降"; tone = (change ?? 0) >= 0 ? "ok" : "warn"; }
  }
  const margin = ratio(ratios, "净利率", cur.period), marginPrev = ratio(ratios, "净利率", priorPeriod(cur.period));
  const facts = [`${periodLabel(cur.period)} ${label(cur.value)}${prev ? `，上年同期${label(prev.value).replace(/^净/, "")}` : "；资料中没有上年同期数字"}${change !== null && (verdict === "亏损收窄" || verdict === "亏损扩大") ? `，亏损${verdict === "亏损收窄" ? "收窄" : "扩大"} ${Math.abs(change).toFixed(0)}%` : ""}。`];
  if (margin) facts.push(`净利率 ${margin.value.toFixed(1)}%${marginPrev ? `（上年同期 ${marginPrev.value.toFixed(1)}%）` : ""}。`);
  const hint = verdict === "亏损收窄" && change !== null && revenueRate != null && change - revenueRate > 30
    ? `亏损收窄幅度远大于收入增幅（${pct(revenueRate)}），可能来自费用下降、补偿收入或一次性项目，建议在利润表与附注中核对。`
    : verdict === "亏损扩大" ? "亏损扩大，关注费用投入与现金消耗是否随之加快。" : undefined;
  return { key: "profit", title: "盈亏", verdict, tone, summary: verdict, facts, hint,
    pair: { label: "净利润", prev, cur, unit: profit.unit } };
}

const METHOD_TEXT = { cash_change: "现金余额实际减少", operating_cash_flow: "经营活动现金流", net_profit: "净亏损（近似）" } as const;

function cashDimension(finance?: FinanceView): Dimension | null {
  const k = finance?.key_numbers;
  if (!k?.cash && !k?.runway) return null;
  const runway = k.runway;
  const months = runway?.months != null ? Math.floor(runway.months) : null;
  const verdict = runway?.status === "positive" ? "经营现金流非负" : months === null ? "无法估算续航"
    : runway?.status === "short" ? `续航约 ${months} 个月，偏短` : `可支撑约 ${months} 个月`;
  const facts: string[] = [];
  if (k.cash) facts.push(`${periodLabel(k.cash.period)} 账上现金 ${amount(k.cash.value, k.cash.unit)}。`);
  if (runway?.burn && runway.method && months !== null)
    facts.push(`按${METHOD_TEXT[runway.method]}计，月均消耗约 ${amount(runway.burn, runway.unit)}（${runway.text.replace(/^账面现金 ÷ 月均消耗（|）$/g, "").replace(/(\d{4})M(\d{2})/g, "$1-$2")}）。`);
  else if (runway?.text) facts.push(`${runway.text}。`);
  const loss = runway?.alternatives?.find((a) => a.method === "net_profit");
  const hint = runway?.status === "short" ? "按当前消耗速度不足一年，关注融资、借款续期或收入改善安排。"
    : loss?.months && runway?.months && runway.method !== "net_profit" && loss.months > runway.months * 1.3
      ? `若只按净亏损估算会得到约 ${Math.floor(loss.months)} 个月，明显偏乐观：现金实际流出还包括借款偿还、资本开支和营运资金变化。`
      : months !== null ? "续航按历史消耗线性推算，不含新增融资、借款到期或支出变化。" : "缺少现金或消耗数据，无法推算续航。";
  return { key: "cash", title: "现金与续航", verdict, tone: runway?.status === "short" ? "warn" : months !== null || runway?.status === "positive" ? "ok" : "none",
    summary: runway?.status === "positive" ? verdict : months === null ? "现金续航无法估算" : `现金可支撑约 ${months} 个月`,
    facts, hint, refs: [...(k.cash?.refs ?? []), ...(runway?.refs ?? [])] };
}

function PairBars({ pair }: { pair: NonNullable<Dimension["pair"]> }) {
  const open = useOpenSource();
  const items = [pair.prev, pair.cur].filter((p): p is Point2 => !!p);
  const max = Math.max(1, ...items.map((p) => Math.abs(p.value)));
  return (
    <div className="fi-bars" role="group" aria-label={`${pair.label}同期对比`}>
      {items.map((p) => (
        <button key={p.period} type="button" className="fi-bar" title={sourceTitle(p.metric)} onClick={() => open(refOf(p))}>
          <span className="fi-bar-label">{periodLabel(p.period)}</span>
          <span className="fi-bar-track"><motion.i className={p.value < 0 ? "neg" : ""} initial={{ width: 0 }}
            animate={{ width: `${Math.max(1.5, Math.abs(p.value) / max * 100)}%` }} transition={{ duration: 0.7, ease: EASE }} /></span>
          <strong className={p.value < 0 ? "down" : ""}>{amount(p.value, pair.unit)}</strong>
        </button>
      ))}
    </div>
  );
}

/** The rule-based reading: one sentence for the conclusion band, four dimensions, related risks from the materials. */
export function financeReading(rows: Row[], finance?: FinanceView, risks: Point[] = []) {
  const pick = (name: string) => rows.find((r) => r.std === name && r.category === "financial");
  const revenue = pick("营业收入"), profit = pick("净利润");
  const ratios = finance?.ratios ?? [];
  const revCur = latest(revenue), revPrev = revCur ? actual(revenue, priorPeriod(revCur.period)) : undefined;
  const revenueRate = revCur && revPrev ? growth(revCur.value, revPrev.value) : null;
  const dims = [growthDimension(revenue), marginDimension(ratios, revenue), profitDimension(ratios, profit, revenueRate), cashDimension(finance)]
    .filter((d): d is Dimension => !!d);
  return { dims, headline: dims.length ? `${dims.map((d) => d.summary).join("，")}。` : "",
    related: risks.filter((r) => RISK_TERMS.test(r.text)).slice(0, 3),
    revenue, profit, cost: pick("营业成本"), gross: pick("毛利"), cash: pick("货币资金"), ratios };
}

/** 财务解读: the four dimensions in an even 2×2 grid; charts live beside the trend, not inside a cell. */
export function FinanceInsight({ dims }: { dims: Dimension[] }) {
  if (!dims.length) return null;
  return (
    <div className="fi-grid fi-grid-2">
      {dims.map((d) => (
        <article key={d.key} className="fi-dim">
          <div className="fi-dim-head"><h4>{d.title}</h4><span className={`tag ${TONE_CLASS[d.tone]}`}>{d.verdict}</span></div>
          {d.facts.map((f, i) => <p key={i} className="fi-fact">{f}</p>)}
          {d.pair && d.pair.prev && <PairBars pair={d.pair} />}
          {d.hint && <p className="fi-hint"><b>意味着</b>{d.hint}</p>}
          {d.refs && d.refs.length > 0 && <RefChips refs={[...new Map(d.refs.map((r) => [`${r.source_id}|${r.locator}`, r])).values()]} />}
        </article>
      ))}
    </div>
  );
}

export { MarginTrends, ProfitWaterfall, RunwayChart };

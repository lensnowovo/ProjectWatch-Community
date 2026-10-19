import { motion } from "motion/react";
import type { Metric, ProjectDetail, Ref } from "./api";
import { Block, Figure, VerdictBand } from "./frame";
import { EASE } from "./motion";
import { Empty, RefChips, formatAmount } from "./ui";
import { comparePeriods, mName, mPeriod, mValue, mUnit, mScope, mRecency } from "./metrics";
import { ChartHint } from "./chartHint";

/**
 * 内部信息 › 财务分析: what the extracted statements say over time — each line's progression, the ratios built from
 * them, and rule-based quality checks. Rules only read numbers already in the materials; anything missing says so.
 */

export interface FinPoint { period: string; key: string; value: number; basis: Metric["basis"]; metric: Metric }
export interface FinSeries { name: string; section: string; unit: string; currency?: string; points: FinPoint[] }

const STATEMENT = new Set(["income", "balance", "cashflow"]);

/** Statement lines with their periods, actual figures first; one series per standard name and unit. */
export function financeSeries(detail: ProjectDetail): FinSeries[] {
  const sections = detail.finance?.sections ?? {};
  const by = new Map<string, FinSeries>();
  for (const m of [...detail.metrics].sort((a,b) => mRecency(b).localeCompare(mRecency(a)))) {
    const section = sections[m.id];
    const value = mValue(m);
    if (!STATEMENT.has(section) || value == null || !Number.isFinite(value) || mScope(m)) continue;
    const name = mName(m), unit = mUnit(m), period = mPeriod(m);
    const key = `${section}|${name}|${unit}|${m.currency || ''}`;
    const s = by.get(key) ?? { name, section, unit, currency: m.currency || '', points: [] };
    if (!s.points.some((p) => p.period === period && p.basis === m.basis)) {
      s.points.push({ period, key: period, value, basis: m.basis, metric: m });
    }
    by.set(key, s);
  }
  for (const s of by.values()) s.points.sort((a, b) => comparePeriods(a.key,b.key));
  return [...by.values()];
}

const actual = (s?: FinSeries) => (s?.points ?? []).filter((p) => p.basis === "actual");
const find = (all: FinSeries[], ...names: string[]) => all.find((s) => names.includes(s.name));
const at = (s: FinSeries | undefined, period: string) => actual(s).find((p) => p.period === period);
const paired = (s: FinSeries | undefined, base: FinSeries | undefined, period: string) => s && base && s.unit === base.unit && s.currency === base.currency ? at(s,period) : undefined;
export const periodKind = (p:string) => /^\d{4}$/.test(p) ? 'year' : /^\d{4}Q[1-4]$/.test(p) ? 'quarter' : /^\d{4}H[12]$/.test(p) ? 'half' : /^\d{4}M\d{2}$/.test(p) ? 'month' : p.replace(/^\d{4}/,'');
const comparable = (s?: FinSeries) => {const pts=actual(s);const latest=pts[pts.length-1];return latest?pts.filter(p=>periodKind(p.key)===periodKind(latest.key)):[]};
const money = (v: number, unit: string) => {
  const a = unit === "元" ? formatAmount(v / 10000, "万元") : formatAmount(v, unit);
  return `${a.value} ${a.unit}`.trim();
};
const pct = (v: number) => `${v >= 0 ? "" : "−"}${Math.abs(v) >= 100 ? Math.round(Math.abs(v)).toLocaleString() : Math.abs(v).toFixed(1)}%`;
const refOf = (p?: FinPoint): Ref[] => (p?.metric.source_id ? [{ source_id: p.metric.source_id, source_name: p.metric.source_name ?? "内部资料", locator: p.metric.locator }] : []);

type Tone = "good" | "watch" | "warn" | "unknown";
interface Check { title: string; value: string; tone: Tone; why: string; basis: string; refs: Ref[] }

/** Rule checks over the extracted lines. Each states what it compared and leaves the verdict to the reader when data is missing. */
export function qualityChecks(all: FinSeries[]): Check[] {
  const revenue = find(all, "营业收入", "营业总收入"), profit = find(all, "净利润"), rd = find(all, "研发投入", "研发费用");
  const salesCash = find(all, "销售商品、提供劳务收到的现金"), opCash = find(all, "经营活动现金流净额"), invest = find(all, "吸收投资收到的现金");
  const out: Check[] = [];
  const last = (s?: FinSeries) => actual(s).slice(-1)[0];

  const r = last(revenue), sc = r && paired(salesCash,revenue,r.period);
  out.push(r && sc && r.value
    ? (() => { const v = sc.value / r.value * 100; return { title: "收入现金含量", value: pct(v), tone: v >= 90 ? "good" : v >= 70 ? "watch" : "warn",
      why: "比较销售收现与收入规模；含税、预收及跨期回款会影响比值", basis: `${r.period} 销售收到的现金 ÷ 营业收入`, refs: [...refOf(sc), ...refOf(r)] } as Check; })()
    : { title: "收入现金含量", value: "—", tone: "unknown", why: "资料没有同一期间的营业收入和销售收到的现金", basis: "销售收到的现金 ÷ 营业收入", refs: [] });

  const p = last(profit), oc = p && paired(opCash,profit,p.period);
  out.push(p && oc && p.value
    ? (() => { const v = oc.value / p.value * 100; const loss = p.value < 0; return { title: "利润现金含量", value: pct(v),
      tone: loss ? (oc.value < p.value ? "warn" : "watch") : v >= 80 ? "good" : "warn",
      why: loss ? (oc.value >= 0 ? "账面亏损，但经营现金流为正" : oc.value < p.value ? "经营现金净流出大于账面亏损" : "经营现金净流出小于账面亏损") : v >= 80 ? "经营现金流与利润规模相近或更高" : "利润高于经营现金流，关注利润质量",
      basis: `${p.period} 经营现金流净额 ÷ 净利润`, refs: [...refOf(oc), ...refOf(p)] } as Check; })()
    : { title: "利润现金含量", value: "—", tone: "unknown", why: "资料未披露同期经营活动现金流净额", basis: "经营现金流净额 ÷ 净利润", refs: [] });

  const pts = comparable(profit);
  if (pts.length >= 2) {
    let widening = 0;
    for (let i = pts.length - 1; i > 0 && pts[i].value < 0 && pts[i].value < pts[i - 1].value; i--) widening++;
    const lossYears = pts.filter((x) => x.value < 0).length;
    out.push({ title: "盈利趋势", value: lossYears ? `亏损 ${lossYears}/${pts.length} 期` : "持续盈利",
      tone: widening >= 2 ? "warn" : lossYears ? "watch" : "good",
      why: widening ? `亏损已连续 ${widening} 期扩大` : pts[pts.length-1].value >= 0 ? "最新可比期间已盈利" : lossYears ? "最新期间亏损，未连续扩大" : "各期均盈利",
      basis: `${pts[0].period}–${pts[pts.length - 1].period} 净利润`, refs: refOf(pts[pts.length - 1]) });
  }

  const inv = last(invest), loss = inv && paired(profit,invest,inv.period);
  if (inv && loss && loss.value < 0) {
    const v = inv.value / Math.abs(loss.value) * 100;
    out.push({ title: "融资与亏损规模", value: pct(v), tone: "watch",
      why: "融资流入与账面亏损的规模对照，不代表现金缺口或融资依赖程度", basis: `${inv.period} 吸收投资收到的现金 ÷ 净亏损`, refs: [...refOf(inv), ...refOf(loss)] });
  }

  const d = last(rd), lp = d && paired(profit,rd,d.period);
  if (d && lp && lp.value < 0) {
    const v = d.value / Math.abs(lp.value) * 100;
    out.push({ title: "研发与亏损规模", value: pct(v), tone: "watch",
      why: "研发支出与净亏损的规模对照，不是亏损原因分解", basis: `${d.period} 研发投入 ÷ 净亏损`, refs: [...refOf(d), ...refOf(lp)] });
  }

  const years = (section: string) => new Set(all.filter((s) => s.section === section).flatMap((s) => actual(s).map((x) => x.period))).size;
  const cover = { income: years("income"), balance: years("balance"), cashflow: years("cashflow") };
  out.push({ title: "报表覆盖", value: `${cover.income}/${cover.balance}/${cover.cashflow} 期`, tone: cover.income && cover.balance && cover.cashflow ? "watch" : "warn",
    why: !cover.income || !cover.cashflow || !cover.balance ? "部分报表尚无已提取数字" : "三类报表均有数字，不代表科目完整或已经勾稽核对",
    basis: "利润表 / 资产负债表 / 现金流量表 的实际期数", refs: [] });
  return out;
}

const TONE_LABEL: Record<Tone, string> = { good: "正常", watch: "留意", warn: "警示", unknown: "未披露" };

/** Ratios per period: the backend's set, plus balance-sheet ratios when the lines exist. */
function indicators(detail: ProjectDetail, all: FinSeries[]) {
  const rows = (detail.finance?.ratios ?? []).map((r) => ({ name: r.name, unit: r.unit,
    cells: Object.entries(r.cells).filter(([, c]) => c.basis === "actual").map(([period, c]) => ({ period, value: c.value })).sort((a, b) => a.period.localeCompare(b.period)) }));
  const assets = find(all, "总资产"), equity = find(all, "净资产", "所有者权益"), debt = find(all, "总负债"), cash = find(all, "货币资金");
  const derive = (name: string, f: (period: string) => number | null) => {
    const periods = actual(assets).map((p) => p.period);
    const cells = periods.map((period) => ({ period, value: f(period) })).filter((c): c is { period: string; value: number } => c.value !== null && Number.isFinite(c.value));
    if (cells.length) rows.push({ name, unit: "%", cells });
  };
  const growth = (name: string, series?: FinSeries) => {
    const pts = comparable(series);
    const cells = pts.slice(1).map((p, i) => ({ period: p.period, value: pts[i].value ? (p.value - pts[i].value) / Math.abs(pts[i].value) * 100 : NaN }))
      .filter((c) => Number.isFinite(c.value));
    if (cells.length) rows.push({ name, unit: "%", cells });
  };
  growth("研发投入同比", find(all, "研发投入", "研发费用"));
  growth("净利润同比", find(all, "净利润"));
  derive("资产负债率", (period) => { const a = at(assets, period)?.value; const eq = paired(equity,assets,period); const d = paired(debt,assets,period)?.value ?? (a !== undefined && eq ? a - eq.value : undefined);
    return a && d !== undefined ? d / a * 100 : null; });
  derive("货币资金占总资产", (period) => { const a = at(assets, period)?.value, c = paired(cash,assets,period)?.value; return a && c !== undefined ? c / a * 100 : null; });
  return rows.filter((r) => r.cells.length);
}

/** Amount bars on the left scale with year-on-year change as a line on the right scale. */
function Bars({ s }: { s: FinSeries }) {
  const pts = s.points.slice(-6);
  const max = Math.max(...pts.map((p) => Math.abs(p.value)), 1);
  const neg = pts.some((p) => p.value < 0), pos = pts.some((p) => p.value > 0);
  const W = 300, H = 112, padL = 4, padR = 44, padB = 16;
  const zero = neg && pos ? H / 2 : neg ? 2 : H - 2, span = neg && pos ? H / 2 - 2 : H - 4;
  const inner = W - padL - padR;
  const bw = inner / pts.length;
  const barW = Math.min(16, bw * .42);
  const centre = (i: number) => padL + i * bw + bw / 2;
  // Growth only across same-kind periods, and only where the previous period is a usable base.
  const kind = pts.length ? periodKind(pts[pts.length - 1].key) : "";
  const growth = pts.map((p, i) => {
    const prev = pts[i - 1];
    if (!prev || !prev.value || periodKind(p.key) !== kind || periodKind(prev.key) !== kind || p.basis !== "actual" || prev.basis !== "actual") return null;
    return { i, period: p.period, value: (p.value - prev.value) / Math.abs(prev.value) * 100 };
  }).filter((g): g is { i: number; period: string; value: number } => !!g);
  const gLo = Math.min(0, ...growth.map((g) => g.value)), gHi = Math.max(0, ...growth.map((g) => g.value));
  const gSpan = gHi - gLo || 1;
  const gy = (v: number) => 4 + (gHi - v) / gSpan * (H - padB - 8);
  return <div className="fa-chart"><svg viewBox={`0 0 ${W} ${H + 4}`} className="fa-bars" role="group"
    aria-label={`${pts.map((p) => `${p.period} ${money(p.value, s.unit)}`).join("，")}${growth.length ? `；同比：${growth.map((g) => `${g.period} ${pct(g.value)}`).join("，")}` : ""}`}>
    <line x1={padL} x2={W - padR} y1={zero} y2={zero} className="fa-zero" />
    {pts.map((p, i) => {
      const h = Math.abs(p.value) / max * span;
      const y = p.value >= 0 ? zero - h : zero;
      return <ChartHint key={`${p.period}-${p.basis}`} title={s.name} value={money(p.value, s.unit)}
        meta={`${p.period} · ${{ actual: "实际", forecast: "预测", budget: "预算", unknown: "口径未明" }[p.basis]} · ${s.currency || "币种未标注"}`}
        note={`${p.basis === "actual" ? "实心柱为实际值。" : "虚线柱不是已实现结果。"}${p.metric.source_name ? `来源：${p.metric.source_name} · ${p.metric.locator}` : ""}`}><g>
        <rect x={padL + i * bw} y={0} width={bw} height={H + 4} fill="transparent" />
        <motion.rect x={centre(i) - barW / 2} width={barW} initial={{ height: 0, y: zero }} animate={{ height: h, y }}
          transition={{ delay: i * .05, duration: .5, ease: EASE }} rx={2} className={`${p.value < 0 ? "neg" : "pos"}${p.basis === "actual" ? "" : " plan"}`} />
        <text x={centre(i)} y={H + 1} className="fa-tick">{p.period.replace(/^20/, "’")}</text>
      </g></ChartHint>;
    })}
    {growth.length > 1 && <g className="fa-growth">
      <motion.path initial={{ pathLength: 0 }} animate={{ pathLength: 1 }} transition={{ duration: .6, delay: .25, ease: EASE }} fill="none"
        d={growth.map((g, n) => `${n ? "L" : "M"}${centre(g.i)},${gy(g.value)}`).join(" ")} />
      {growth.map((g) => <ChartHint key={g.period} title={`${s.name} 同比`} value={pct(g.value)} meta={g.period}
        note="与上一同口径期间相比；负值表示比上期减少。"><circle cx={centre(g.i)} cy={gy(g.value)} r={3} /></ChartHint>)}
      <text x={W - padR + 5} y={gy(gHi) + 3} className="fa-tick fa-growth-label">{pct(gHi)}</text>
      {gLo !== gHi && <text x={W - padR + 5} y={gy(gLo) + 3} className="fa-tick fa-growth-label">{pct(gLo)}</text>}
    </g>}
  </svg></div>;
}

/** A ratio over the same periods: one line on its own scale, because margins and cost shares never share a range. */
function RatioChart({ cells }: { cells: { period: string; value: number }[] }) {
  const pts = cells.slice(-6);
  const lo = Math.min(0, ...pts.map((p) => p.value)), hi = Math.max(0, ...pts.map((p) => p.value));
  const span = hi - lo || 1;
  const W = 300, H = 112, padL = 4, padR = 52, padB = 16;
  const inner = W - padL - padR;
  const x = (i: number) => padL + (pts.length === 1 ? inner / 2 : i / (pts.length - 1) * inner);
  const y = (v: number) => 6 + (hi - v) / span * (H - padB - 10);
  return <div className="fa-chart"><svg viewBox={`0 0 ${W} ${H + 4}`} className="fa-bars fa-ratio" role="group"
    aria-label={pts.map((p) => `${p.period} ${pct(p.value)}`).join("，")}>
    {lo < 0 && hi > 0 && <line x1={padL} x2={W - padR} y1={y(0)} y2={y(0)} className="fa-zero" />}
    <motion.path initial={{ pathLength: 0 }} animate={{ pathLength: 1 }} transition={{ duration: .6, ease: EASE }} fill="none"
      d={pts.map((p, i) => `${i ? "L" : "M"}${x(i)},${y(p.value)}`).join(" ")} />
    {pts.map((p, i) => <ChartHint key={p.period} title="按期比率" value={pct(p.value)} meta={p.period} note="按同口径期间计算。">
      <g><rect x={x(i) - 12} y={0} width={24} height={H} fill="transparent" /><circle cx={x(i)} cy={y(p.value)} r={3} /></g></ChartHint>)}
    {pts.map((p, i) => <text key={`t${p.period}`} x={x(i)} y={H + 1} className="fa-tick">{p.period.replace(/^20/, "’")}</text>)}
    <text x={W - padR + 5} y={y(hi) + 3} className="fa-tick fa-growth-label">{pct(hi)}</text>
    {lo !== hi && <text x={W - padR + 5} y={y(lo) + 3} className="fa-tick fa-growth-label">{pct(lo)}</text>}
  </svg></div>;
}

export function FinanceAnalysisView({ detail, go }: { detail: ProjectDetail; go: (page: "documents" | "numbers") => void }) {
  const all = financeSeries(detail);
  const multi = all.filter((s) => s.points.length >= 2).sort((a, b) =>
    ["income", "cashflow", "balance"].indexOf(a.section) - ["income", "cashflow", "balance"].indexOf(b.section)
    || lineRank(a.name) - lineRank(b.name) || b.points.length - a.points.length || a.name.localeCompare(b.name));
  const checks = qualityChecks(all);
  const rows = indicators(detail, all);
  const margins = marginRows(detail, all);
  const revenueSeries = find(all, "营业收入", "营业总收入"), profitSeries = find(all, "净利润");
  const revenue = comparable(revenueSeries), profit = comparable(profitSeries);
  if (!all.length) return <div className="tab-frame internal-view"><Empty title="还没有可分析的财务数字"
    action={<button className="btn" onClick={() => go("documents")}>去上传财务资料 →</button>}>上传年报、审计报告或管理报表后，这里显示多年趋势、财务指标与质量检查。</Empty></div>;
  const headline = revenue.length >= 2 && profit.length
    ? `收入从 ${money(revenue[0].value, revenueSeries!.unit)}（${revenue[0].period}）到 ${money(revenue[revenue.length - 1].value, revenueSeries!.unit)}（${revenue[revenue.length - 1].period}）；${profit[profit.length-1].period} ${profit[profit.length - 1].value < 0 ? `亏损 ${money(-profit[profit.length - 1].value, profitSeries!.unit)}` : `盈利 ${money(profit[profit.length - 1].value, profitSeries!.unit)}`}`
    : "已提取的财务数字";
  const warn = checks.filter((c) => c.tone === "warn").length;
  return <div className="tab-frame internal-view">
    <VerdictBand eyebrow="财务分析" meta="规则计算 · 只用资料中已有的数字 · 点数字看原文" title={headline}
      figures={<>
        <Figure label="覆盖期间" value={new Set(all.flatMap((s) => actual(s).map((p) => p.period))).size} unit="期" note="有实际数字的期间" />
        <Figure label="多期科目" value={multi.length} unit="项" note="可看趋势" />
        <Figure label="财务指标" value={rows.length} unit="项" note="按期计算" />
        <Figure label="质量警示" value={warn} unit="项" note={`共检查 ${checks.length} 项`} tone={warn ? "warn" : undefined} />
      </>} />

    {(multi.length > 0 || margins.some((m) => m.cells.length >= 2)) && <Block card title="各期走势"
      note="柱为金额，折线为同比；虚线柱是预测或预算。比率单独成卡，各有自己的刻度">
      <div className="fa-grid">{multi.slice(0, 9).map((s) => {
        const a = comparable(s), last = a[a.length - 1] ?? s.points[s.points.length - 1], prev = a[a.length - 2];
        const yoy = prev && prev.value ? (last.value - prev.value) / Math.abs(prev.value) * 100 : null;
        return <article key={`${s.section}|${s.name}|${s.unit}|${s.currency}`} className="fa-cell">
          <header><span>{s.name}</span>{yoy !== null && <i className={(s.name.includes("利润") ? last.value >= prev!.value : yoy >= 0) ? "up" : "down"}>{yoy >= 0 ? "+" : "−"}{Math.abs(yoy) >= 1000 ? `${Math.round(Math.abs(yoy) / 100) / 10}k` : Math.abs(yoy).toFixed(0)}%</i>}</header>
          <strong className="num-display">{money(last.value, s.unit)}</strong><small>{last.period}{prev ? ` · 上期 ${money(prev.value, s.unit)}` : ""}</small>
          <Bars s={s} />
          <RefChips refs={refOf(last)} compact />
        </article>;
      })}
        {margins.filter((m) => m.cells.length >= 2).map((m) => {
          const last = m.cells[m.cells.length - 1], prev = m.cells[m.cells.length - 2];
          return <article key={m.name} className="fa-cell fa-cell-ratio">
            <header><span>{m.name}</span><i className="fa-ratio-tag">比率</i></header>
            <strong className="num-display">{pct(last.value)}</strong>
            <small>{last.period}{prev ? ` · 上期 ${pct(prev.value)}` : ""}</small>
            <RatioChart cells={m.cells} />
          </article>;
        })}
      </div>
      {margins.filter((m) => m.cells.length < 2).length > 0 && <p className="muted small fa-missing">
        {margins.filter((m) => m.cells.length < 2).map((m) => `${m.name}：${m.missing}`).join("；")}。</p>}
      {margins.some((m) => m.cells.some((c) => Math.abs(c.value) > 500)) && <p className="muted small fa-missing">
        收入基数极小的期间，比率绝对值会失真，只看方向。</p>}
    </Block>}

    <div className="research-pair">
      <Block card title="财务指标" note="按期计算 · 收入很小时比率会失真">
        {rows.length ? <table className="table fa-table"><thead><tr><th>指标</th>{[...new Set(rows.flatMap((r) => r.cells.map((c) => c.period)))].sort().slice(-4).map((p) => <th key={p}>{p}</th>)}</tr></thead>
          <tbody>{rows.map((r) => {
            const periods = [...new Set(rows.flatMap((x) => x.cells.map((c) => c.period)))].sort().slice(-4);
            return <tr key={r.name}><th scope="row">{r.name}</th>{periods.map((p) => { const c = r.cells.find((x) => x.period === p);
              return <td key={p} className={c && c.value < 0 ? "neg" : ""}>{c ? (r.unit === "%" ? pct(c.value) : c.value.toLocaleString()) : "—"}</td>; })}</tr>;
          })}</tbody></table> : <p className="muted small">资料中的数字不足以计算财务指标。</p>}
        <button className="link small" onClick={() => go("numbers")}>看三张报表 →</button>
      </Block>
      <Block card title="财务质量" note="规则检查，不是审计结论">
        <ul className="fa-checks">{checks.map((c) => <li key={c.title} className={c.tone}>
          <span className="fa-check-tone">{TONE_LABEL[c.tone]}</span>
          <div><p><b>{c.title}</b><em>{c.value}</em></p><p className="fa-why">{c.why}</p><small>{c.basis}</small>{c.refs.length > 0 && <RefChips refs={c.refs.slice(0, 2)} compact />}</div>
        </li>)}</ul>
      </Block>
    </div>
  </div>;
}

/** Statement reading order: revenue first, then what it costs, then what is left, then cash, then the balance sheet. */
const LINE_ORDER = ["营业总收入", "营业收入", "营业成本", "毛利", "毛利润", "税金及附加", "销售费用", "管理费用", "研发投入", "研发费用",
  "财务费用", "营业利润", "利润总额", "净利润", "扣非净利润",
  "销售商品、提供劳务收到的现金", "经营活动现金流净额", "投资活动现金流净额", "筹资活动现金流净额", "吸收投资收到的现金",
  "货币资金", "应收账款", "存货", "总资产", "总负债", "净资产", "所有者权益", "未分配利润"];
const lineRank = (name: string) => { const i = LINE_ORDER.indexOf(name); return i < 0 ? LINE_ORDER.length : i; };

/** Margin trends: ratios that are shares of revenue, read together on one scale. */
function marginRows(detail: ProjectDetail, all: FinSeries[]) {
  const named = (name: string) => (detail.finance?.ratios ?? []).find((r) => r.name === name);
  const fromRatio = (name: string) => { const r = named(name); return r ? Object.entries(r.cells).filter(([, c]) => c.basis === "actual")
    .map(([period, c]) => ({ period, value: c.value })).sort((a, b) => a.period.localeCompare(b.period)) : []; };
  const revenue = find(all, "营业收入", "营业总收入"), cost = find(all, "营业成本"), gross = find(all, "毛利", "毛利润");
  const grossCells = actual(revenue).map((p) => {
    const g = paired(gross, revenue, p.period)?.value ?? (() => { const c = paired(cost, revenue, p.period)?.value; return c === undefined ? undefined : p.value - c; })();
    return g === undefined || !p.value ? null : { period: p.period, value: g / p.value * 100 };
  }).filter((x): x is { period: string; value: number } => !!x);
  return [
    { name: "毛利率", cells: grossCells, missing: "资料未披露营业成本或毛利" },
    { name: "净利率", cells: fromRatio("净利率"), missing: "缺同期净利润或营业收入" },
    { name: "研发投入占收入", cells: fromRatio("研发投入占收入"), missing: "缺同期研发投入或营业收入" },
  ].filter((r) => r.cells.length > 0 || r.name === "毛利率");
}


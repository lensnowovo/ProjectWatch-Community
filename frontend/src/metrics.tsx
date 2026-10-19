import { motion } from "motion/react";
import { useMemo, useState, type ReactNode } from "react";
import { EASE } from "./motion";
import type { FinanceView, Metric, MetricIssue, Point, RatioRow } from "./api";
import { FinanceInsight, MarginTrends, ProfitWaterfall, RunwayChart, financeReading } from "./financeInsight";
import { Block, Reference, VerdictBand } from "./frame";
import { MoreNumbers, Statements } from "./statements";
import { BASIS_LABEL, Empty, RefChips, formatNumber, useOpenSource } from "./ui";
import { Seg } from "./widgets";

const PRIORITY = ["营业收入", "收入", "毛利", "净利润", "经营活动现金流", "货币资金", "现金"];

export interface Row {
  name: string;
  aliases?: string[];
  std: string;
  unit: string;
  category: Metric["category"];
  cells: Map<string, Metric[]>;
  ids: string[];
}

/** Normalised accessors: fall back to the raw extraction when the server had no rule. */
export const mName = (m: Metric) => m.std_name || m.name;
export const mPeriod = (m: Metric) => m.period_key || m.period;
export const mValue = (m: Metric) => (m.value_norm ?? m.value);
export const mUnit = (m: Metric) => (m.value_norm != null ? m.norm_unit ?? m.unit : m.unit);
export const mRecency = (m: Metric) => m.source_created_at || m.created_at || "";

/** Among the newest actuals for one period, the value most citations agree on (a note or parent-company figure under the same label loses). */
export function consensus(values: Metric[], unit: string): Metric | undefined {
  const pool = values.filter((m) => m.basis === "actual" && mUnit(m) === unit && mValue(m) !== null);
  if (!pool.length) return undefined;
  const newest = pool.reduce((a, m) => (mRecency(m) > a ? mRecency(m) : a), "");
  const latest = pool.filter((m) => mRecency(m) === newest);
  const agree = (a: Metric, b: Metric) => { const x = mValue(a)!, y = mValue(b)!; return Math.abs(x - y) <= Math.max(Math.abs(x), Math.abs(y), 1e-9) * 0.005; };
  return latest.reduce((best, m) => (latest.filter((o) => agree(m, o)).length > latest.filter((o) => agree(best, o)).length ? m : best), latest[0]);
}

const DEFAULT_SCOPES = new Set(["合并", "合并口径", "合并报表", "集团", "集团合并", "公司", "公司整体", "整体", "全公司", "consolidated", "group", "total"]);
/** Mirrors normalize.scope_key: consolidated scope is the default and is not shown. */
export const mScope = (m: Metric) => {
  const s = (m.scope || "").replace(/\s+/g, "").replace(/^[（(]|[）)]$/g, "");
  return DEFAULT_SCOPES.has(s.toLowerCase()) ? "" : s;
};

/** Mirrors normalize.sort_key: 2025Q1 < 2025H1 < 2025-1-9M < 2025 (full year last). */
export function periodSort(key: string): [number, number, string] {
  const match = /^(\d{4})(.*)$/.exec(key);
  if (!match) return [9999, 0, key];
  const rest = match[2];
  let sub = 98;
  if (rest === "") sub = 99;
  else if (rest === "H1") sub = 6.5;
  else if (rest === "H2") sub = 12.5;
  else if (/^Q[1-4]$/.test(rest)) sub = Number(rest[1]) * 3 + 0.2;
  else if (/^M\d{2}$/.test(rest)) sub = Number(rest.slice(1));
  else if (/^-1-\d+M$/.test(rest)) sub = Number(rest.slice(3, -1)) + 0.4;
  return [Number(match[1]), sub, rest];
}
export const comparePeriods = (a: string, b: string) => {
  const x = periodSort(a); const y = periodSort(b);
  return x[0] - y[0] || x[1] - y[1] || x[2].localeCompare(y[2]);
};

export function periodLabel(key: string) {
  return key
    .replace(/^(\d{4})M(\d{2})$/, "$1-$2")
    .replace(/^(\d{4})-1-(\d+)M$/, "$1 1-$2月")
    .replace(/^(\d{4})(H[12]|Q[1-4])$/, "$1 $2");
}

const rank = (n: string) => { const i = PRIORITY.findIndex((p) => n.includes(p)); return i < 0 ? 99 : i; };

export function useMetricTable(metrics: Metric[]) {
  return useMemo(() => {
    const periods = [...new Set(metrics.map(mPeriod))].sort(comparePeriods);
    const rows = new Map<string, Row & { units: Map<string, number> }>();
    for (const m of metrics) {
      const scope = mScope(m);
      const key = `${m.category}|${mName(m)}|${scope}|${mUnit(m)}|${m.currency || ""}`;
      const row = rows.get(key) ?? { name: mName(m) + (scope ? `（${scope}）` : ""), std: mName(m), unit: "", category: m.category, cells: new Map(), units: new Map(), ids: [] as string[], aliases: [] as string[] };
      if (m.name && !row.aliases!.includes(m.name)) row.aliases!.push(m.name);
      row.cells.set(mPeriod(m), [...(row.cells.get(mPeriod(m)) ?? []), m]);
      row.ids.push(m.id);
      row.units.set(mUnit(m), (row.units.get(mUnit(m)) ?? 0) + 1);
      rows.set(key, row);
    }
    const list = [...rows.values()].map(({ units, ...row }) => ({
      ...row, unit: [...units.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] ?? "",
    }));
    return { periods, rows: list.sort((a, b) => rank(a.name) - rank(b.name)) };
  }, [metrics]);
}

function differs(a: number | null, b: number | null) {
  if (a === null || b === null) return false;
  return Math.abs(a - b) / Math.max(Math.abs(a), Math.abs(b), 1e-9) > 0.01;
}

export function sourceTitle(v: Metric) {
  const original = `原文：${formatNumber(v.value)}${v.unit ? ` ${v.unit}` : ""}${v.period ? ` · ${v.period}` : ""}`;
  return `${original}\n${v.quote || ""}\n来源：${v.source_name ?? ""} · ${v.locator}`.replace(/\n\n/g, "\n");
}

export function Cell({ values, unit, planCol = false }: { values?: Metric[]; unit: string; planCol?: boolean }) {
  const open = useOpenSource();
  if (!values?.length) return <td className="num muted">—</td>;
  values = [...values].sort((a, b) => mRecency(b).localeCompare(mRecency(a)));
  // Prefer actuals; a forecast and a later actual for the same period are not a conflict.
  const actuals = values.filter((v) => v.basis === "actual");
  const shown = actuals.length ? actuals : values;
  const conflict = shown.some((v) => mUnit(v) === mUnit(shown[0]) && differs(mValue(v), mValue(shown[0])));
  const plan = actuals.length ? values.find((v) => v.basis === "forecast" || v.basis === "budget") : undefined;
  const list = conflict ? shown.filter((v, i, all) => all.findIndex((x) => !differs(mValue(x), mValue(v))) === i).slice(0, 3) : shown.slice(0, 1);
  return (
    <td className={`num ${conflict ? "cell-conflict" : ""} ${planCol ? "col-plan" : ""}`}>
      {list.map((v) => (
        <button key={v.id} className={`metric-val basis-${v.basis}`} title={sourceTitle(v)}
          onClick={() => open({ source_id: v.source_id, source_name: v.source_name ?? "资料", locator: v.locator })}>
          {formatNumber(mValue(v))}
          {mUnit(v) !== unit && <small className="muted"> {mUnit(v)}</small>}
          {v.basis !== "actual" && !planCol && <sup>{BASIS_LABEL[v.basis]}</sup>}
        </button>
      ))}
      {conflict && <div className="conflict-note">不同资料数字不一致</div>}
      {!conflict && plan && mValue(plan) !== null && differs(mValue(plan), mValue(shown[0])) && (
        <div className="plan-note" title={sourceTitle(plan)}>原{BASIS_LABEL[plan.basis]} {formatNumber(mValue(plan))}</div>
      )}
    </td>
  );
}

export function IssueList({ issues, compact = false }: { issues: MetricIssue[]; compact?: boolean }) {
  if (!issues.length) return null;
  return (
    <div className={`card card-pad issues ${compact ? "issues-compact" : ""}`} role="note">
      <div className="card-head">
        <h3>需要核对的数字 <span className="count-badge">{issues.length}</span></h3>
        <span className="muted small">资料差异与数字异常 · 规则检查</span>
      </div>
      <ul className="bullets">
        {issues.slice(0, compact ? 3 : 20).map((i, n) => (
          <li key={n} className={`issue sev-${i.severity}`}>{i.text} <RefChips refs={i.refs} /></li>
        ))}
      </ul>
    </div>
  );
}

function BarChart({ row, periods, height = 200 }: { row: Row; periods: string[]; height?: number }) {
  const open = useOpenSource();
  const points = periods
    .map((p) => {
      const cell = (row.cells.get(p) ?? []).filter((m) => mUnit(m) === row.unit && mValue(m) !== null).sort((a, b) => mRecency(b).localeCompare(mRecency(a)));
      return { period: p, metric: consensus(cell, row.unit) ?? cell[0] };
    })
    .filter((x): x is { period: string; metric: Metric } => !!x.metric);
  if (points.length < 2) return null;
  const values = points.map((p) => mValue(p.metric) as number);
  const max = Math.max(0, ...values);
  const min = Math.min(0, ...values);
  const span = max - min || 1;
  const width = 560;
  const pad = { top: 18, bottom: 28, left: 8, right: 8 };
  const inner = height - pad.top - pad.bottom;
  const band = (width - pad.left - pad.right) / points.length;
  const bw = Math.min(band * 0.5, 34);
  const y = (v: number) => pad.top + ((max - v) / span) * inner;
  return (
    <figure className="chart">
      <figcaption>
        {row.name}<span className="muted">（{row.unit || "单位未注明"}）</span>
        <span className="legend"><i className="lg-actual" />实际 <i className="lg-forecast" />预测/预算</span>
      </figcaption>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${row.name} 趋势`}>
        <line x1={pad.left} x2={width - pad.right} y1={y(0)} y2={y(0)} className="axis" />
        {points.map((p, i) => {
          const v = mValue(p.metric) as number;
          const x = pad.left + i * band + (band - bw) / 2;
          const top = Math.min(y(v), y(0));
          const h = Math.max(1, Math.abs(y(v) - y(0)));
          return (
            <g key={p.period} role="button" tabIndex={0} aria-label={`${periodLabel(p.period)} ${formatNumber(v)}${row.unit}，查看出处`}
              onClick={() => open({ source_id: p.metric.source_id, source_name: p.metric.source_name ?? "资料", locator: p.metric.locator })}
              onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open({ source_id: p.metric.source_id, source_name: p.metric.source_name ?? "资料", locator: p.metric.locator }); } }}>
              <title>{sourceTitle(p.metric)}</title>
              <motion.rect x={x} width={bw} rx={3}
                initial={{ y: y(0), height: 0 }} animate={{ y: top, height: h }}
                transition={{ duration: 0.7, ease: EASE, delay: 0.08 * i }}
                className={`bar${p.metric.basis === "actual" ? "" : " bar-proj"}${i === points.length - 1 ? " bar-latest" : ""}${v < 0 ? " bar-neg" : ""}`} />
              <motion.text x={x + bw / 2} y={v >= 0 ? top - 6 : top + h + 14} className="bar-label"
                initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 0.5 + 0.08 * i }}>{formatNumber(v)}</motion.text>
              <text x={x + bw / 2} y={height - 8} className="bar-axis">{periodLabel(p.period)}</text>
            </g>
          );
        })}
      </svg>
    </figure>
  );
}

function PlanComparison({ row }: { row: Row }) {
  const open = useOpenSource();
  const pairs = [...row.cells.entries()].map(([period, values]) => {
    const ordered = [...values].filter((m) => mValue(m) !== null).sort((a, b) => mRecency(b).localeCompare(mRecency(a)));
    return { period, actual: ordered.find((m) => m.basis === "actual"), plan: ordered.find((m) => m.basis === "forecast" || m.basis === "budget") };
  }).filter((p) => p.actual && p.plan);
  const max = Math.max(1, ...pairs.flatMap((p) => [Math.abs(mValue(p.actual!)!), Math.abs(mValue(p.plan!)!)]));
  return <div className="plan-comparison"><h4>{row.name} · 实际对比计划 <span>{row.unit}</span></h4>{pairs.map((p) => <div className="comparison-period" key={p.period}><span>{periodLabel(p.period)}</span>{[p.actual!, p.plan!].map((m) => <button key={m.id} title={sourceTitle(m)} onClick={() => open({ source_id: m.source_id, source_name: m.source_name ?? "资料", locator: m.locator })}><span>{BASIS_LABEL[m.basis]}</span><i className={m.basis === "actual" ? "comparison-actual" : "comparison-plan"} style={{ width: `${Math.max(1, Math.abs(mValue(m)!) / max * 100)}%` }} /><strong>{formatNumber(mValue(m))}</strong></button>)}</div>)}</div>;
}

export function MetricsPreview({ metrics }: { metrics: Metric[] }) {
  const { periods, rows } = useMetricTable(metrics);
  const [selected, setSelected] = useState(0);
  const comparable = (r: Row) => [...r.cells.values()].some((values) => values.some((v) => v.basis === "actual") && values.some((v) => v.basis === "forecast" || v.basis === "budget"));
  const available = rows.filter((r) => r.cells.size >= 2 || comparable(r)).slice(0, 6);
  const row = available[Math.min(selected, available.length - 1)];
  if (!metrics.length) return null;
  return <div className="metrics-preview">
    {row ? <>{available.length > 1 && <Seg id="overview-metric" value={selected} onChange={setSelected} options={available.map((r, i) => ({ value: i, label: r.name }))} />}{comparable(row) ? <PlanComparison row={row} /> : <BarChart row={row} periods={periods.filter((p) => row.cells.has(p))} />}</> : <div className="single-period-metrics">{rows.slice(0, 4).map((r) => { const m = [...r.cells.values()].flat().filter((v) => mValue(v) !== null).sort((a, b) => mRecency(b).localeCompare(mRecency(a)))[0]; return m && <div key={`${r.name}|${r.unit}`}><span>{r.name} · {periodLabel(mPeriod(m))}</span><strong>{formatNumber(mValue(m))}<small>{r.unit}</small></strong><span>{BASIS_LABEL[m.basis]}</span><RefChips refs={[{ source_id: m.source_id, source_name: m.source_name ?? "资料", locator: m.locator }]} /></div>; })}</div>}
    <p className="evidence-note">仅比较同单位、币种与口径；点击查看原文。</p>
  </div>;
}

const SECTIONS: { key: string; title: string; order: string[] }[] = [
  { key: "income", title: "利润表", order: ["营业收入", "营业成本", "毛利", "销售费用", "管理费用", "研发投入", "财务费用", "营业利润", "净利润", "扣非净利润"] },
  { key: "balance", title: "资产负债表", order: ["货币资金", "应收账款", "存货", "总资产", "总负债", "净资产"] },
  { key: "cashflow", title: "现金流量表", order: ["经营活动现金流净额", "投资活动现金流净额", "筹资活动现金流净额"] },
  { key: "financial_other", title: "其他财务数据", order: [] },
  { key: "operating", title: "经营指标", order: [] },
];

export function sectionOf(row: Row, finance?: FinanceView): string {
  const fromServer = row.ids.map((id) => finance?.sections[id]).find(Boolean);
  if (fromServer) return fromServer;
  return row.category === "operating" ? "operating" : "financial_other";
}

/** A column is a plan column when every value shown in it is a forecast or budget. */
function planColumns(rows: Row[], periods: string[]) {
  return new Set(periods.filter((p) => {
    const values = rows.flatMap((r) => r.cells.get(p) ?? []);
    return values.length > 0 && values.every((v) => v.basis === "forecast" || v.basis === "budget");
  }));
}

export function StatementTable({ title, rows, note }: { title: string; rows: Row[]; note?: string }) {
  const periods = useMemo(() => [...new Set(rows.flatMap((r) => [...r.cells.keys()]))].sort(comparePeriods), [rows]);
  const plans = planColumns(rows, periods);
  const units = [...new Set(rows.map((r) => r.unit))];
  const shared = units.length === 1 ? units[0] : "";
  return (
    <section className="statement">
      <div className="statement-head"><h3>{title}</h3><span className="muted small">{shared ? `单位：${shared}` : "单位见各行"}{note ? ` · ${note}` : ""}</span></div>
      <div className="table-wrap">
        <table className="table statement-table">
          <thead><tr><th>科目</th>{periods.map((p) => <th key={p} className={`num ${plans.has(p) ? "col-plan" : ""}`}>{periodLabel(p)}{plans.has(p) && <small>预测</small>}</th>)}</tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.name}|${r.unit}`}>
                <td className="statement-name">{r.name}{!shared && r.unit && <small>{r.unit}</small>}</td>
                {periods.map((p) => <Cell key={p} values={r.cells.get(p)} unit={r.unit} planCol={plans.has(p)} />)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function RatioTable({ rows, reported }: { rows: RatioRow[]; reported: Row[] }) {
  const open = useOpenSource();
  const periods = [...new Set([...rows.flatMap((r) => Object.keys(r.cells)), ...reported.flatMap((r) => [...r.cells.keys()])])].sort(comparePeriods);
  if (!rows.length && !reported.length) return null;
  return (
    <section className="statement">
      <div className="statement-head"><h3>财务分析</h3><span className="muted small">公式计算 · 斜体为预测 · 悬停查看公式</span></div>
      <div className="table-wrap">
        <table className="table statement-table">
          <thead><tr><th>指标</th>{periods.map((p) => <th key={p} className="num">{periodLabel(p)}</th>)}</tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.name}>
                <td className="statement-name">{r.name}</td>
                {periods.map((p) => {
                  const c = r.cells[p];
                  if (!c) return <td key={p} className="num muted">—</td>;
                  const refs = [...new Map(c.refs.map((ref) => [`${ref.source_id}|${ref.locator}`, ref])).values()];
                  return <td key={p} className={`num ${c.basis === "forecast" ? "col-plan" : ""}`}>
                    <button className={`metric-val ${c.basis === "forecast" ? "basis-forecast" : ""} ${c.value < 0 ? "neg" : ""}`} title={c.formula}
                      onClick={() => c.refs[0] && open(c.refs[0])}>{c.value.toFixed(1)}%</button>
                    {c.basis === "unknown" && <div className="plan-note">口径待核对</div>}
                    <details className="ratio-sources"><summary>查看依据（{refs.length}）</summary>
                      {refs.map((ref) => <RefChips key={`${ref.source_id}|${ref.locator}`} refs={[ref]} />)}
                    </details>
                  </td>;
                })}
              </tr>
            ))}
            {reported.map((r) => (
              <tr key={`rep-${r.name}`}>
                <td className="statement-name">{r.name}<small>资料给出</small></td>
                {periods.map((p) => <Cell key={p} values={r.cells.get(p)} unit={r.unit} />)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

const TREND_NAMES = ["营业收入", "毛利", "净利润", "经营活动现金流净额", "货币资金"];

export const KIND_LABEL = { annual: "年度", half: "半年度", quarter: "季度", month: "期末时点", other: "其他期间" } as const;
export type PeriodKind = keyof typeof KIND_LABEL;
/** Annual, half-year and quarterly values are not drawn side by side: different lengths are not a trend. */
export function periodKind(period: string): PeriodKind {
  if (/^\d{4}$/.test(period)) return "annual";
  if (/^\d{4}H[12]$/.test(period)) return "half";
  if (/^\d{4}Q[1-4]$/.test(period)) return "quarter";
  if (/^\d{4}M\d{2}$/.test(period)) return "month";
  return "other";
}

export function MetricsView({ metrics, issues = [], finance, figures, risks }: {
  metrics: Metric[]; issues?: MetricIssue[]; finance?: FinanceView; figures?: ReactNode; risks?: Point[];
}) {
  const { periods, rows } = useMetricTable(metrics);
  const [selected, setSelected] = useState(0);
  const [kindChoice, setKind] = useState<PeriodKind | null>(null);

  if (!metrics.length) {
    return (
      <Empty title="还没有提取到数字">
        添加 BP、财务报表或经营月报。
      </Empty>
    );
  }
  const bySection = (key: string) => {
    const order = SECTIONS.find((s) => s.key === key)?.order ?? [];
    const rank = (r: Row) => { const i = order.indexOf(r.std); return i < 0 ? 99 : i; };
    return rows.filter((r) => sectionOf(r, finance) === key).sort((a, b) => rank(a) - rank(b));
  };
  const trend = TREND_NAMES.map((n) => rows.find((r) => r.std === n && r.cells.size >= 2)).filter((r): r is Row => !!r);
  const trendRow = trend[Math.min(selected, trend.length - 1)];
  const kindCount = (r: Row | undefined, k: PeriodKind) => r ? [...r.cells.keys()].filter((p) => periodKind(p) === k).length : 0;
  // Offered kinds come from all trend metrics, so switching metric never adds or removes buttons.
  const allKinds = (Object.keys(KIND_LABEL) as PeriodKind[]).filter((k) => trend.some((r) => kindCount(r, k) >= 2));
  const kinds = allKinds.filter((k) => kindCount(trendRow, k) >= 2);
  const kind = kindChoice && kinds.includes(kindChoice) ? kindChoice : kinds[0];
  const comparable = rows.filter((r) => [...r.cells.values()].some((values) => values.some((v) => v.basis === "actual") && values.some((v) => v.basis === "forecast" || v.basis === "budget")));
  const reportedRatios = bySection("ratio").filter((r) => !(finance?.ratios ?? []).some((x) => x.name === r.std));

  const reading = financeReading(rows, finance, risks);
  const other = [...bySection("financial_other"), ...["income", "balance", "cashflow"].flatMap((k) => bySection(k).filter((r) => r.name !== r.std))];
  const operating = bySection("operating");
  return (
    <div className="tab-frame metrics">
      <VerdictBand eyebrow="经营情况" meta="规则计算 · 只做同口径对比 · 点击数字看原文"
        title={reading.headline || "已提取的经营与财务数字"} figures={figures}>
        <p className="tf-sub">报表数字由 AI 从原件抓取，可能有误；点开原文核验。</p>
      </VerdictBand>
      <IssueList issues={issues} />
      <div className="chart-pair">
        {trendRow && <figure className="chart-panel">
          <div className="chart-head">
            <h3>趋势</h3>
            {trend.length > 1 && <Seg id="metric" value={trend.indexOf(trendRow)} onChange={setSelected} options={trend.map((r, i) => ({ value: i, label: r.std }))} />}
            {allKinds.length > 1 && <Seg id="metric-kind" value={kind} onChange={setKind}
              options={allKinds.map((k) => ({ value: k, label: KIND_LABEL[k], disabled: !kinds.includes(k),
                title: kinds.includes(k) ? undefined : `${trendRow.std}没有两个以上的${KIND_LABEL[k]}数字` }))} />}
          </div>
          {kind ? <BarChart key={`${trendRow.name}|${trendRow.unit}|${kind}`} row={trendRow} periods={periods.filter((p) => periodKind(p) === kind)} height={170} />
            : <p className="muted small">同类期间（如连续两个年度）不足两个，暂不画趋势；数字见下方报表。</p>}
        </figure>}
        <div className="chart-panel"><RunwayChart cashRow={reading.cash} runway={finance?.key_numbers?.runway ?? null} /></div>
      </div>
      <Block title="财务解读" note="每项只与上年同期比较">
        <FinanceInsight dims={reading.dims} />
      </Block>
      <div className="chart-pair">
        <div className="chart-panel"><ProfitWaterfall revenue={reading.revenue} cost={reading.cost} gross={reading.gross} profit={reading.profit} /></div>
        <div className="chart-panel"><MarginTrends ratios={reading.ratios} /></div>
      </div>
      <section className="statements">
        <Statements rows={rows} finance={finance} />
        <RatioTable rows={finance?.ratios ?? []} reported={reportedRatios} />
      </section>
      <Reference items={[
        { key: "risks", title: "资料中的相关风险", note: "AI 整理，非规则计算",
          body: reading.related.length ? <ul className="bullets">{reading.related.map((r, i) => <li key={i}>{r.text} <RefChips refs={r.refs} /></li>)}</ul> : null },
        { key: "plan", title: "实际对比计划", note: "同期间实际 × 预测 / 预算",
          body: comparable.length ? <div className="plan-grid">{comparable.slice(0, 4).map((r) => <PlanComparison key={`${r.name}|${r.unit}`} row={r} />)}</div> : null },
        { key: "other", title: "其他披露数字", note: `${other.length} 项 · 附注明细、非IFRS、母公司口径等`,
          body: other.length ? <MoreNumbers title="其他披露数字" hint="未归入三张报表" rows={other} /> : null },
        { key: "operating", title: "经营指标", note: `${operating.length} 项 · 客户、订单、产能、人员等`,
          body: operating.length ? <MoreNumbers title="经营指标" hint="客户、订单、产能、人员等" rows={operating} /> : null },
      ]} />
      <p className="muted small">AI 提取数字；人民币统一为万元，比率与现金跑道按公式计算。悬停看原始数值，点击看出处。解读不替代审计或管理层说明。</p>
    </div>
  );
}

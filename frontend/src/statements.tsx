import { useState } from "react";
import type { FinanceView, Metric, StatementLayout } from "./api";
import { KIND_LABEL, comparePeriods, consensus, mUnit, mValue, periodKind, sectionOf, sourceTitle, StatementTable, type PeriodKind, type Row } from "./metrics";
import { formatNumber, useOpenSource } from "./ui";
import { Seg } from "./widgets";

/**
 * 三张报表 in the Chinese Accounting Standards order: grouped lines, subtotals in bold, one period
 * length at a time for flows, and balance-sheet columns by date. HK/IFRS wording is mapped to CAS
 * lines by the server; the original wording stays visible on hover.
 */

// Used only when an older server sends no layout.
const FALLBACK: StatementLayout[] = [
  { key: "income", title: "利润表", groups: [{ title: "", items: ["营业收入", "营业成本", "毛利", "销售费用", "管理费用", "研发投入", "财务费用", "营业利润", "净利润", "扣非净利润"].map((n) => ({ name: n, label: n === "研发投入" ? "研发费用" : n, total: ["营业收入", "毛利", "营业利润", "净利润"].includes(n) })) }] },
  { key: "balance", title: "资产负债表", groups: [{ title: "", items: ["货币资金", "应收账款", "存货", "总资产", "总负债", "净资产"].map((n) => ({ name: n, label: ({ 总资产: "资产总计", 总负债: "负债合计", 净资产: "所有者权益合计" } as Record<string, string>)[n] ?? n, total: ["总资产", "总负债", "净资产"].includes(n) })) }] },
  { key: "cashflow", title: "现金流量表", groups: [{ title: "", items: ["经营活动现金流净额", "投资活动现金流净额", "筹资活动现金流净额"].map((n) => ({ name: n, label: n, total: true })) }] },
];

/** Balance dates as month numbers: 2025 → Dec 2025, 2025H1 → Jun, 2025M09 → Sep. */
function pointIndex(period: string): number | null {
  const m = /^(\d{4})(.*)$/.exec(period);
  if (!m) return null;
  const rest = m[2];
  const month = rest === "" || rest === "H2" ? 12 : rest === "H1" ? 6 : /^M(\d{2})$/.test(rest) ? Number(rest.slice(1))
    : /^Q([1-4])$/.test(rest) ? Number(rest[1]) * 3 : null;
  return month === null ? null : Number(m[1]) * 12 + month - 1;
}
const pointLabel = (idx: number) => `${Math.floor(idx / 12)}-${String((idx % 12) + 1).padStart(2, "0")}`;
const periodText = (p: string) => p.replace(/^(\d{4})(H[12]|Q[1-4])$/, "$1 $2");

type Column = { key: string; label: string; cells: (row: Row) => Metric[] | undefined };

function flowColumns(rows: Row[], kind: PeriodKind): Column[] {
  const periods = [...new Set(rows.flatMap((r) => [...r.cells.keys()]))].filter((p) => periodKind(p) === kind).sort(comparePeriods).slice(-4);
  return periods.map((p) => ({ key: p, label: periodText(p), cells: (row) => row.cells.get(p) }));
}

function pointColumns(rows: Row[]): Column[] {
  const points = new Map<number, string[]>();
  for (const p of new Set(rows.flatMap((r) => [...r.cells.keys()]))) {
    const idx = pointIndex(p);
    if (idx !== null) points.set(idx, [...(points.get(idx) ?? []), p]);
  }
  return [...points.keys()].sort((a, b) => a - b).slice(-4).map((idx) => ({
    key: String(idx), label: pointLabel(idx),
    cells: (row) => { const all = points.get(idx)!.flatMap((p) => row.cells.get(p) ?? []); return all.length ? all : undefined; },
  }));
}

/** Like-for-like change of the newest column against the same period one year earlier. */
function yoy(row: Row, cols: Column[], kind: PeriodKind | "point"): string | null {
  const last = cols[cols.length - 1];
  if (!last) return null;
  const prevKey = kind === "point" ? String(Number(last.key) - 12) : last.key.replace(/^(\d{4})/, (y) => String(Number(y) - 1));
  const prev = cols.find((c) => c.key === prevKey);
  const a = consensus(last.cells(row) ?? [], row.unit), b = prev ? consensus(prev.cells(row) ?? [], row.unit) : undefined;
  if (!a || !b) return null;
  const x = mValue(a)!, y = mValue(b)!;
  if (y === 0) return null;
  if (y < 0 || x < 0) return x === y ? "持平" : `${x > y ? "改善" : "恶化"} ${Math.abs(x - y).toLocaleString("zh-CN", { maximumFractionDigits: 0 })}`;
  const pct = (x / y - 1) * 100;
  return `${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%`;
}

/** Presentation sign differs between reports (expenses in brackets); magnitude decides whether two readings disagree. */
const sameMagnitude = (a: number, b: number) => Math.abs(Math.abs(a) - Math.abs(b)) <= Math.max(Math.abs(a), Math.abs(b), 1e-9) * 0.01;

function StatementCell({ values, unit }: { values?: Metric[]; unit: string }) {
  const open = useOpenSource();
  const shown = consensus(values ?? [], unit);
  if (!shown) return <td className="num muted">—</td>;
  const v = mValue(shown)!;
  const others = [...new Map((values ?? [])
    .filter((m) => m.basis === "actual" && mUnit(m) === unit && mValue(m) !== null && !sameMagnitude(mValue(m)!, v))
    .map((m) => [Math.round(Math.abs(mValue(m)!) * 10), m])).values()];
  return (
    <td className={`num ${others.length ? "cell-conflict" : ""}`}>
      <button className="metric-val basis-actual" title={sourceTitle(shown)}
        onClick={() => open({ source_id: shown.source_id, source_name: shown.source_name ?? "资料", locator: shown.locator })}>
        {formatNumber(v)}
      </button>
      {others.length > 0 && <div className="conflict-note" title={others.map((m) => `${formatNumber(mValue(m))}：${m.source_name ?? "资料"} · ${m.locator}${m.name ? `（${m.name}）` : ""}`).join("\n")}>
        另有 {others.length} 个不同数字</div>}
    </td>
  );
}

type Item = StatementLayout["groups"][number]["items"][number];
type Group = { title: string; items: { item: Item; row: Row }[] };

/** Lines of one statement that actually have numbers in the shown columns (consolidated scope only). */
function linesOf(layout: StatementLayout, rows: Row[], cols: Column[]): Group[] {
  return layout.groups.map((g) => ({
    title: g.title,
    items: g.items.flatMap((item) => rows.filter((r) => r.std === item.name && r.category === "financial" && r.name === r.std)
      .map((row) => ({ item, row })))
      .filter(({ row }) => cols.some((c) => c.cells(row)?.length)),
  })).filter((g) => g.items.length);
}

const isKey = (item: Item) => item.total || item.key !== false; // older servers send no flag: everything counts as key

function Statement({ layout, groups, hidden, flat, cols, kind, note }: {
  layout: StatementLayout; groups: Group[]; hidden: number; flat: boolean; cols: Column[]; kind: PeriodKind | "point"; note?: string;
}) {
  const showYoy = cols.length >= 2;
  const counts = new Map<string, number>();
  for (const g of groups) for (const i of g.items) counts.set(i.row.unit, (counts.get(i.row.unit) ?? 0) + 1);
  const main = [...counts.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] ?? "";
  return (
    <section className={`statement cas-statement ${flat ? "cas-flat" : ""}`}>
      <div className="statement-head">
        <h3>{layout.title}</h3>
        <span className="muted small">{main ? `单位：${main}` : "单位见各行"}{note ? ` · ${note}` : ""}
          {hidden > 0 && ` · 另有 ${hidden} 个明细科目`}</span>
      </div>
      <div className="table-wrap">
        <table className="table statement-table">
          <thead><tr><th>项目</th>{cols.map((c) => <th key={c.key} className="num">{c.label}</th>)}{showYoy && <th className="num st-yoy">同比</th>}</tr></thead>
          <tbody>
            {groups.map((g, gi) => [
              g.title && !flat ? <tr key={`g-${gi}`} className="st-group"><th colSpan={cols.length + 1 + (showYoy ? 1 : 0)} scope="colgroup">{g.title}</th></tr> : null,
              ...g.items.map(({ item, row }) => {
                const change = showYoy ? yoy(row, cols, kind) : null;
                const aliases = row.aliases ?? [];
                const original = aliases.some((a) => a === item.label || a === row.std) ? [] : aliases;
                return (
                  <tr key={`${row.name}|${row.unit}`} className={item.total && !flat ? "st-total" : g.title && !flat ? "st-item" : ""}>
                    <td className="statement-name" title={original.length ? `资料原文科目：${original.join("、")}` : undefined}>
                      {item.label}{row.unit !== main && <small>{row.unit || "单位未注明"}</small>}{original.length > 0 && <sup className="st-mapped" aria-label="已按国内科目归类">*</sup>}
                    </td>
                    {cols.map((c) => <StatementCell key={c.key} values={c.cells(row)} unit={row.unit} />)}
                    {showYoy && <td className={`num st-yoy ${change?.startsWith("-") || change?.startsWith("恶化") ? "down" : ""}`}>{change ?? <span className="muted">—</span>}</td>}
                  </tr>
                );
              }),
            ])}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/** Above this many lines the statements open on key lines; below it everything disclosed is shown. */
const CONDENSE_ABOVE = 45;

export function Statements({ rows, finance }: { rows: Row[]; finance?: FinanceView }) {
  const layouts = finance?.layout?.length ? finance.layout : FALLBACK;
  const of = (key: string) => rows.filter((r) => sectionOf(r, finance) === key);
  const flowRows = [...of("income"), ...of("cashflow")];
  const kinds = (["annual", "half", "quarter"] as PeriodKind[]).filter((k) => flowRows.some((r) => [...r.cells.keys()].some((p) => periodKind(p) === k)));
  const [chosen, setChosen] = useState<PeriodKind | null>(null);
  const [detail, setDetail] = useState<"key" | "all" | null>(null);
  const annualCount = new Set(flowRows.flatMap((r) => [...r.cells.keys()]).filter((p) => periodKind(p) === "annual")).size;
  const kind = chosen && kinds.includes(chosen) ? chosen : annualCount >= 2 ? "annual" : kinds[kinds.length - 1] ?? "annual";
  const cols = { income: flowColumns(of("income"), kind), cashflow: flowColumns(of("cashflow"), kind), balance: pointColumns(of("balance")) };
  const statements = layouts.map((l) => ({ layout: l, groups: linesOf(l, of(l.key), cols[l.key]) }));
  const total = statements.reduce((n, s) => n + s.groups.reduce((m, g) => m + g.items.length, 0), 0);
  const detailLines = statements.reduce((n, s) => n + s.groups.reduce((m, g) => m + g.items.filter((i) => !isKey(i.item)).length, 0), 0);
  const condensable = total > CONDENSE_ABOVE && detailLines > 0;
  const view = detail ?? (condensable ? "key" : "all");
  const present = statements.filter((s) => s.groups.length);
  const missing = statements.filter((s) => !s.groups.length).map((s) => s.layout.title);
  const mapped = rows.some((r) => ["income", "balance", "cashflow"].includes(sectionOf(r, finance)) && (r.aliases ?? []).length > 0
    && !(r.aliases ?? []).includes(r.std));
  return (
    <div className="cas-statements">
      <div className="cas-head">
        <div>
          <h3>三张报表</h3>
          <p className="muted small">
            {total <= 6 ? "资料中披露的财务数字较少，仅列出已有项目" : "按企业会计准则科目排列"}
            {missing.length > 0 && `；资料中暂无${missing.join("、")}${kind === "annual" ? "" : `（${KIND_LABEL[kind]}）`}数字`}
            {mapped ? "；部分科目名称已按准则统一（如港股／IFRS 写法），带 * 的行悬停可看原文科目名" : ""}。点击数字看原文。
          </p>
        </div>
        <div className="cas-controls">
          {condensable && <Seg id="cas-detail" value={view} onChange={setDetail}
            options={[{ value: "key", label: "主要科目" }, { value: "all", label: `全部科目（${total}）` }]} />}
          {kinds.length > 1 && <Seg id="cas-kind" value={kind} onChange={setChosen} options={kinds.map((k) => ({ value: k, label: KIND_LABEL[k] }))} />}
        </div>
      </div>
      {!present.length && <p className="muted small">资料中还没有可归入三张报表的数字。</p>}
      {present.map(({ layout, groups }) => {
        const shown = view === "key" ? groups.map((g) => ({ ...g, items: g.items.filter((i) => isKey(i.item)) })).filter((g) => g.items.length) : groups;
        const disclosed = groups.reduce((n, g) => n + g.items.length, 0);
        const hidden = disclosed - shown.reduce((n, g) => n + g.items.length, 0);
        // A few disclosed lines read best as a plain list; headings help once there is structure to show.
        return <Statement key={layout.key} layout={layout} groups={shown} hidden={hidden} flat={disclosed <= 3} kind={layout.key === "balance" ? "point" : kind}
          cols={cols[layout.key]} note={layout.key === "balance" ? "按期末日期" : `${KIND_LABEL[kind]}数`} />;
      })}
    </div>
  );
}

/** Long tail of disclosed numbers (notes, non-IFRS, licence terms): folded, searchable, not mixed into the statements. */
export function MoreNumbers({ title, hint, rows }: { title: string; hint: string; rows: Row[] }) {
  const [query, setQuery] = useState("");
  if (!rows.length) return null;
  const q = query.trim().toLowerCase();
  const shown = q ? rows.filter((r) => [r.name, ...(r.aliases ?? [])].join(" ").toLowerCase().includes(q)) : rows;
  return (
    <details className="card card-pad more-numbers">
      <summary><span className="more-title">{title}</span><span className="muted small">{rows.length} 项 · {hint}</span></summary>
      <input className="search more-search" placeholder="搜索科目，如 借款、减值、薪酬" value={query} onChange={(e) => setQuery(e.target.value)} />
      {shown.length ? <StatementTable title={q ? `匹配 ${shown.length} 项` : "全部"} rows={shown} /> : <p className="muted small">没有匹配的科目。</p>}
    </details>
  );
}

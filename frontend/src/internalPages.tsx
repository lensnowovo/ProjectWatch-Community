import { motion } from "motion/react";
import type { ReactNode } from "react";
import { api, type Metric, type Point, type ProjectDetail, type Ref } from "./api";
import { Block, Columns, Figure, VerdictBand } from "./frame";
import { IssueList } from "./metrics";
import { EASE } from "./motion";
import { financeSeries, qualityChecks } from "./financeAnalysis";
import { ChartHint } from "./chartHint";
import { SignalsPanel } from "./signals";
import { Empty, RefChips, formatAmount, relTime, useAsync } from "./ui";

/**
 * 内部信息 (8945 five modules): what the uploaded materials say about operations, business and R&D, risks and what
 * to verify. Everything here is read from internal documents; every figure and point links back to its page.
 */

type Go = (page: "documents") => void;

function NoMaterial({ go, what }: { go: Go; what: string }) {
  return <Empty title={`还没有内部资料可读出${what}`} action={<button className="btn" onClick={() => go("documents")}>去上传资料 →</button>}>
    上传 BP、经营月报、财务报表或会议纪要后，这里从原文整理并标注页码。
  </Empty>;
}

const metricRef = (m: Metric): Ref[] => (m.source_id ? [{ source_id: m.source_id, source_name: m.source_name ?? "内部资料", locator: m.locator }] : []);
const cutoff = (detail: ProjectDetail) => (detail.profile as { material_cutoff?: string } | null)?.material_cutoff;

// ------------------------------------------------------------------ 经营概览

interface Series { name: string; unit: string; points: Metric[] }
const PERIOD_SUFFIX = /\s*[（(]\s*(?:\d{4}\s*(?:年|年度|Q[1-4]|H[12]|[上下]半年|年?\d{1,2}月)?|\d{4}[-/.]\d{1,2})\s*[）)]\s*$/i;

function operatingSeries(detail: ProjectDetail): Series[] {
  const sections = detail.finance?.sections ?? {};
  const by = new Map<string, Metric[]>();
  for (const m of detail.metrics) {
    if (sections[m.id] !== "operating" && !(m.category === "operating" && !sections[m.id])) continue;
    if (m.value === null || m.value === undefined) continue;
    // Extraction sometimes writes the period into the name ("单批次产量（2025Q4）"); strip it so periods line up as one series.
    const key = `${(m.std_name || m.name).replace(PERIOD_SUFFIX, "").trim()}|${m.unit}`;
    by.set(key, [...(by.get(key) ?? []), m]);
  }
  return [...by.entries()].map(([key, points]) => ({ name: key.split("|")[0], unit: points[0].unit,
    points: points.sort((a, b) => (a.period_key || a.period).localeCompare(b.period_key || b.period)) }))
    .sort((a, b) => b.points.length - a.points.length || a.name.localeCompare(b.name));
}

/** Keep the overview grounded in uploaded material, rather than copying the public brief. */
export function operatingHighlights(detail: ProjectDetail) {
  const seen = new Set<string>();
  return (detail.profile?.sections ?? [])
    .filter((s) => /业务|产品|客户|订单|经营|产能|扩产|交付|生产|研发/.test(s.title) && !/边界|缺口/.test(s.title))
    .map((s) => ({ ...s, points: s.points.filter((p) => {
      if (!p.refs?.some((r) => r.source_id && !r.url) || seen.has(p.text)) return false;
      seen.add(p.text); return true;
    }) }))
    .filter((s) => s.points.length).slice(0, 4);
}

export function OperationsView({ detail, go }: { detail: ProjectDetail; go: (page: "documents" | "business") => void }) {
  const series = operatingSeries(detail);
  const change = detail.doc_changes?.[0];
  const hasMaterial = detail.documents.length > 0;
  const highlights = operatingHighlights(detail);
  const milestones = (detail.profile?.milestones ?? []).filter((p) => p.refs?.some((r) => r.source_id && !r.url)).slice(-4);
  const documents = [...detail.documents].sort((a,b) => b.created_at.localeCompare(a.created_at)).slice(0,3);
  const changes = change ? [...change.metrics, ...change.texts] : [];
  const LEAD = 6;
  const card = (s: Series) => {
          const last = s.points[s.points.length - 1], prev = s.points[s.points.length - 2];
          const v = formatAmount(last.value, s.unit);
          const delta = prev && prev.value && last.value !== null ? (last.value - prev.value) / Math.abs(prev.value) * 100 : null;
          const max = Math.max(...s.points.map((p) => Math.abs(p.value ?? 0)), 1);
          return <article key={`${s.name}|${s.unit}`} className="op-card">
            <span className="op-name" title={s.name}>{s.name}</span>
            <span className="op-value"><b className="num-display">{v.value}</b><small>{v.unit}</small></span>
            <span className="op-meta">{last.period}{last.basis !== "actual" && <em>{last.basis === "forecast" ? "预测" : last.basis === "budget" ? "预算" : "口径未明"}</em>}
              {delta !== null && Number.isFinite(delta) && <i className={delta >= 0 ? "up" : "down"}>{delta >= 0 ? "+" : ""}{delta.toFixed(0)}% 较 {prev!.period}</i>}</span>
            {s.points.length > 1 && <span className="op-spark">{s.points.slice(-8).map((p, i) =>
              <ChartHint key={p.id} title={s.name} value={`${formatAmount(p.value,s.unit).value} ${formatAmount(p.value,s.unit).unit}`} meta={`${p.period} · ${{actual:"实际",forecast:"预测",budget:"预算",unknown:"口径未明"}[p.basis]}`}
                note={`柱高表示数值绝对值；虚线表示非实际值。${p.source_name ? `来源：${p.source_name}` : ""}`}><motion.i initial={{ height: 0 }} animate={{ height: `${Math.abs(p.value ?? 0) / max * 100}%` }} transition={{ delay: i * .03, duration: .4, ease: EASE }}
                className={p.basis === "actual" ? "" : "plan"} /></ChartHint>)}</span>}
            <RefChips refs={metricRef(last)} compact />
          </article>;
        };
  const metrics = <>
    <div className="op-grid">{series.slice(0, LEAD).map(card)}</div>
    {series.length > LEAD && <details className="ops-all-metrics"><summary>其余 {series.length - LEAD} 项经营数字</summary>
      <div className="op-grid">{series.slice(LEAD).map(card)}</div></details>}
  </>;
  return <div className="tab-frame internal-view ops-editorial">
    <header className="ops-heading"><div><span className="ops-kicker">内部信息 / 经营</span><h2>经营概览</h2></div>
      <div className="ops-heading-meta">{cutoff(detail) && <span>材料截至 {cutoff(detail)}</span>}<button onClick={() => go("documents")}>资料库 <span aria-hidden="true">↗</span></button></div>
    </header>
    {!hasMaterial && !highlights.length && !series.length ? <section className="ops-start">
      <svg viewBox="0 0 80 80" aria-hidden="true"><rect x="19" y="10" width="43" height="58" rx="4"/><path d="M29 25h23M29 34h17M29 52v-8m10 8V40m10 12V36"/></svg>
      <div><h3>从一份经营材料开始</h3><p>上传 BP 或经营月报，整理业务进展、经营数字与版本变化。</p><button className="btn" onClick={() => go("documents")}>上传资料</button></div>
    </section> : <>
      {!!series.length && <section className="ops-numbers" aria-label="经营指标"><div className="ops-section-head"><h3>经营数字</h3><span>{series.length} 项 · 点来源看原文</span></div>{metrics}</section>}
      <div className="ops-layout">
        <section className="ops-report" aria-label="经营摘要">
          <div className="ops-section-head"><h3>经营摘要</h3><button onClick={() => go("business")}>业务与研发 ↗</button></div>
          {highlights.length ? <div className="ops-topics">{highlights.map((s,i) => <article key={s.title} className="ops-topic">
            <div className="ops-topic-label"><span aria-hidden="true">{String(i+1).padStart(2,"0")}</span><h4>{s.title}</h4></div>
            <div><p>{s.points[0].text}</p><RefChips refs={s.points[0].refs} compact />
              {s.points.length > 1 && <details><summary>另 {s.points.length-1} 条</summary><ul>{s.points.slice(1).map((p,j) => <li key={j}>{p.text} <RefChips refs={p.refs} compact /></li>)}</ul></details>}
            </div>
          </article>)}</div> : <p className="ops-quiet">{series.length ? "经营数字已列于上方，暂无业务文字摘要。" : "已收录材料，经营摘要尚未形成。"}</p>}
          {!series.length && <footer className="ops-report-note">经营数字尚未结构化 · 已有材料摘录保留在此</footer>}
        </section>
        <aside className="ops-aside">
          {!!milestones.length && <section className="ops-nodes"><div className="ops-section-head"><h3>经营节点</h3><span>材料记录</span></div>
            <ol>{milestones.map((m,i) => <li key={i}><div><time>{m.date || "日期未注明"}</time><span>{m.status === "planned" ? "计划" : m.status === "completed" || m.status === "done" ? "已完成" : "材料记录"}</span></div><p>{m.text}</p><RefChips refs={m.refs} compact /></li>)}</ol>
          </section>}
          <section className="ops-materials"><div className="ops-section-head"><h3>材料基础</h3><span>{detail.documents.length} 份</span></div>
            <ul>{documents.map((d) => <li key={d.id}><span className="ops-doc-icon" aria-hidden="true">↳</span><div><button onClick={() => go("documents")}>{d.name}</button><small>加入于 {d.created_at.slice(0,10)}</small></div></li>)}</ul>
            <button className="ops-material-link" onClick={() => go("documents")}>查看资料与版本 →</button>
          </section>
        </aside>
      </div>
      {changes.length ? <section className="ops-changes"><div className="ops-section-head"><h3>本期资料变化</h3><span>{relTime(change!.created_at)}</span></div><ul className="change-list">{changes.map((c,i) => <li key={i} className={c.direction ?? ""}><span className="change-kind">{CHANGE_LABEL[c.kind] ?? c.kind}</span>{c.text}<RefChips refs={c.refs} compact /></li>)}</ul></section>
        : <div className="ops-change-note"><span>版本对比</span><p>加入新版材料后，显示本期变化。</p><button onClick={() => go("documents")}>添加资料 ↗</button></div>}
    </>}
  </div>;
}

const CHANGE_LABEL: Record<string, string> = {
  actual_vs_plan: "实际对比计划", restated: "数字被改写", plan_revised: "预测调整", new_period: "新增数据",
  新进展: "新进展", 不一致: "前后不一致", 淡化: "不再提及", 新风险: "新风险",
};

// ------------------------------------------------------------------ 业务与研发

const TEAM = /团队|人员|管理层|创始/;
const MONEY = /融资|股权|股东|资本/;

function Points({ points }: { points: Point[] }) {
  return <ul className="dossier-points">{points.map((p, i) => <li key={i}>{p.text} <RefChips refs={p.refs} compact /></li>)}</ul>;
}

function overdue(m: Point): boolean {
  if (m.status !== "planned" || !m.date) return false;
  const hit = m.date.match(/(\d{4})\D?(\d{1,2})?/), q = m.date.match(/(\d{4})Q([1-4])/i);
  const end = q ? new Date(+q[1], +q[2] * 3, 0) : hit ? new Date(+hit[1], hit[2] ? +hit[2] : 12, 0) : null;
  return !!end && end.getTime() < Date.now();
}

export function BusinessView({ detail, go }: { detail: ProjectDetail; go: Go }) {
  const profile = detail.profile;
  const sections = profile?.sections ?? [];
  const team = sections.filter((s) => TEAM.test(s.title));
  const capital = sections.filter((s) => MONEY.test(s.title));
  const main = sections.filter((s) => !TEAM.test(s.title) && !MONEY.test(s.title));
  const milestones = profile?.milestones ?? [];
  const done = milestones.filter((m) => m.status !== "planned"), planned = milestones.filter((m) => m.status === "planned");
  const late = planned.filter(overdue);
  return <div className="tab-frame internal-view">
    <VerdictBand eyebrow="业务与研发" meta={profile?.origin ? `${profile.origin.producer} 整理写回 · ${relTime(profile.origin.received_at)}`
      : cutoff(detail) ? `内部资料截至 ${cutoff(detail)}` : "内部资料"}
      title={profile?.one_liner || (detail.documents.length ? "资料中的业务与研发进展" : "还没有内部资料")}
      figures={profile ? <>
        <Figure label="已完成节点" value={done.length} unit="个" note="资料中写明已完成" />
        <Figure label="计划节点" value={planned.length} unit="个" note={late.length ? `${late.length} 个已过计划时间` : "按资料中的计划时间"} tone={late.length ? "warn" : undefined} />
        <Figure label="业务要点" value={main.reduce((n, s) => n + s.points.length, 0)} unit="条" note="均带原文页码" />
      </> : undefined} />
    {!profile ? <NoMaterial go={go} what="业务与研发" /> : <>
      {milestones.length > 0 && <Block card title="研发与经营节点" note="资料中的计划与完成情况；过期未见完成的标出">
        <ol className="ms-track">{milestones.map((m, i) => {
          const late = overdue(m);
          return <li key={i} className={m.status === "planned" ? late ? "late" : "planned" : "done"}>
            <span className="ms-date">{m.date || "时间未写"}</span>
            <span className="ms-dot" aria-hidden="true" />
            <span className="ms-text">{m.text}<span className="ms-state">{m.status === "planned" ? late ? "已过计划时间，资料中未见完成" : "计划" : "已完成"}</span>
              <RefChips refs={m.refs} compact /></span>
          </li>;
        })}</ol>
      </Block>}
      <Columns
        main={main.length ? <>{main.map((s) => <Block card key={s.title} title={s.title}><Points points={s.points} /></Block>)}</>
          : <p className="muted small">资料中没有整理出产品与业务要点。</p>}
        side={team.length || capital.length ? <>{[...team, ...capital].map((s) => <Block key={s.title} title={s.title}><Points points={s.points} /></Block>)}</> : undefined} />
    </>}
  </div>;
}

// ------------------------------------------------------------------ 风险与核对

export function RisksView({ detail, go, extra }: { detail: ProjectDetail; go: Go; extra?: ReactNode }) {
  const signals = useAsync(() => api.signals(detail.project.id), [detail.project.id, detail.project.last_external_at, detail.brief?.updated_at]);
  const risks = detail.profile?.risks ?? [];
  const questions = detail.profile?.questions ?? [];
  const issues = detail.issues ?? [];
  const crosschecks = signals.data?.crosschecks?.filter((c) => c.status === "open").length ?? 0;
  return <div className="tab-frame internal-view">
    <VerdictBand eyebrow="风险与核对" meta="内部资料 · 内外对照为规则检查"
      title={!detail.documents.length ? "还没有内部资料" : `${risks.length} 条资料中的风险，${crosschecks + issues.length} 处需要核对`}
      figures={detail.documents.length ? <>
        <Figure label="资料中的风险" value={risks.length} unit="条" note="AI 从原文整理" tone={risks.length ? "warn" : undefined} />
        <Figure label="内外不一致" value={crosschecks} unit="处" note="内部资料对照公开信息" />
        <Figure label="数字待核对" value={issues.length} unit="处" note="同口径数字冲突或异常" />
        <Figure label="值得追问" value={questions.length} unit="个" note="下次沟通可问" />
      </> : undefined} />
    {!detail.documents.length ? <NoMaterial go={go} what="风险与核对" /> : <>
      {risks.length > 0 && <Block card title="资料中的风险" note="原文写到或可从原文数字读出">
        <ul className="bullets bullets-risk">{risks.map((r, i) => <li key={i}>{r.text} <RefChips refs={r.refs} /></li>)}</ul>
      </Block>}
      <Block card title="内外核对" note="内部说法与公开信息对不上的地方；处理后从这里移走">
        {issues.length > 0 && <IssueList issues={issues} compact />}
        <SignalsPanel data={signals.data ?? null} hideSilence onChange={(next) => signals.setData(() => next)} />
      </Block>
      {questions.length > 0 && <Block title="值得追问" note="由资料缺口整理，可直接用于下次沟通">
        <ol className="questions">{questions.map((q, i) => <li key={i}>{q}</li>)}</ol>
      </Block>}
      {extra}
    </>}
  </div>;
}

/** Counts for the 内部信息 strip: what each page reads from the materials. */
export function internalCounts(detail: ProjectDetail): { page: "ops" | "business" | "numbers" | "fin" | "risks" | "documents"; label: string; n: number }[] {
  const statements = financeSeries(detail);
  const profile = detail.profile;
  return [
    { page: "ops", label: "经营概览", n: operatingSeries(detail).length + operatingHighlights(detail).length },
    { page: "business", label: "业务与研发", n: (profile?.milestones.length ?? 0) + (profile?.sections ?? []).reduce((n, s) => n + s.points.length, 0) },
    { page: "numbers", label: "财务报表", n: statements.reduce((n, s) => n + s.points.length, 0) },
    { page: "fin", label: "财务分析", n: statements.length ? qualityChecks(statements).filter((c) => c.tone !== "unknown").length : 0 },
    { page: "risks", label: "风险与核对", n: (profile?.risks.length ?? 0) + (detail.issues?.length ?? 0) },
    { page: "documents", label: "资料与版本", n: detail.documents.length },
  ];
}

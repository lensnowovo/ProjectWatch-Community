import { motion } from "motion/react";
import { useState } from "react";
import { type ResearchSource as Item, type ProjectDetail } from "./api";
import { Block, Figure, Skeleton, VerdictBand } from "./frame";
import { RELATION, ResearchSourceList, ResearchMore, useResearchSources } from './researchSources';
import { EASE } from "./motion";
import { Empty, ErrorNote, byNewest } from "./ui";
import { externalCollection } from './externalCollection';

export function ResearchEmpty({detail,label}: {detail:ProjectDetail;label:string}) {
  const channel = ({'临床':'trial', '论文':'paper', '专利与注册':'patent'} as Record<string,string>)[label];
  const state=externalCollection(detail, channel ? [channel] : undefined);
  return <div className="tab-frame research-view"><VerdictBand eyebrow={label} meta={state.label} title={`${label} · 暂无收录`} />
    <Empty title={state.label}>{state.description}{label==='专利与注册' && <p>本栏目来自通用网页检索，不是完整的专利数据库。</p>}</Empty></div>;
}

/**
 * 外部信息 › 临床 / 论文 / 专利: related public records, read from the structured fields the sources carry
 * (ClinicalTrials.gov registrations, OpenAlex papers). Counts are of what was collected, never of what exists.
 */

type Extra = Record<string, unknown>;
const extras = (item: Item): Extra[] => [item.extra];
const str = (v: unknown) => (typeof v === "string" ? v : "");
const list = (v: unknown) => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);

function useCategory(detail: ProjectDetail, category: "trial" | "paper" | "patent") {
  return useResearchSources(detail.project.id, category);
}

// ------------------------------------------------------------------ 临床

const PHASES = [["EARLY_PHASE1", "早期 I"], ["PHASE1", "I 期"], ["PHASE2", "II 期"], ["PHASE3", "III 期"], ["PHASE4", "IV 期"]] as const;
const STATUS: Record<string, [string, "on" | "done" | "stop" | ""]> = {
  RECRUITING: ["招募中", "on"], NOT_YET_RECRUITING: ["尚未招募", ""], ENROLLING_BY_INVITATION: ["邀请入组", "on"],
  ACTIVE_NOT_RECRUITING: ["进行中（不再招募）", "on"], COMPLETED: ["已完成", "done"], TERMINATED: ["已终止", "stop"],
  WITHDRAWN: ["已撤回", "stop"], SUSPENDED: ["暂停", "stop"], UNKNOWN: ["状态未知", ""],
};

interface Trial { id: string; title: string; phase: string; status: string; sponsor: string; collaborators: string[]; conditions: string[]; url: string; date: string; news: number }

export function ClinicalView({ detail }: { detail: ProjectDetail }) {
  const { data, loading, error, reload, page, more, note } = useCategory(detail, "trial");
  const items = (data ?? []).slice().sort(byNewest);
  const trials = new Map<string, Trial>();
  for (const item of items) for (const x of extras(item)) {
    const id = str(x.nct_id);
    if (!id) continue;
    const known = trials.get(id);
    trials.set(id, { id, title: known?.title || item.title, phase: str(x.phase) || known?.phase || "", status: str(x.status) || known?.status || "",
      sponsor: str(x.sponsor) || known?.sponsor || "", collaborators: list(x.collaborators), conditions: list(x.conditions).length ? list(x.conditions) : known?.conditions ?? [],
      url: known?.url || `https://clinicaltrials.gov/study/${id}`, date: (item.published_at ?? "").slice(0, 10) || known?.date || "",
      news: (known?.news ?? 0) + 1 });
  }
  const rows = [...trials.values()];
  if(error)return <div><ErrorNote error={error}/><button className="link small" onClick={reload}>重新读取已保存的临床信息</button></div>;
  if(!loading && !rows.length && !items.length)return <ResearchEmpty detail={detail} label="临床"/>;
  const rank = (p: string) => Math.max(-1, ...p.split("/").map((x) => PHASES.findIndex(([k]) => k === x)));
  const top = rows.reduce((m, r) => Math.max(m, rank(r.phase)), -1);
  const active = rows.filter((r) => STATUS[r.status]?.[1] === "on").length;
  return <div className="tab-frame research-view">
    <VerdictBand eyebrow="临床" meta={note}
      title={loading && !data ? "正在读取临床信息…" : rows.length ? `已收录 ${rows.length} 项登记试验，最高到 ${top >= 0 ? PHASES[top][1] : "阶段未注明"}` : items.length ? `${items.length} 条临床相关报道，尚未收录登记号` : "还没有收录到临床信息"}
      figures={<>
        <Figure label="登记试验" value={rows.length} unit="项" note="按登记号去重" />
        <Figure label="进行中" value={active} unit="项" note="招募中或不再招募" />
        <Figure label="最高阶段" value={top >= 0 ? PHASES[top][1] : "—"} note="登记阶段，非获批" />
        <Figure label="临床报道" value={items.length} unit="条" note="含会议与研究进展" />
      </>} />
    {rows.length > 0 && <Block card title="登记试验" note="阶段与状态以登记平台为准；登记不代表已完成或获批">
      <div className="table-wrap"><table className="table trial-table">
        <thead><tr><th>登记号与题目</th><th>阶段</th><th>状态</th><th>申办方 / 合作方</th><th>适应症</th></tr></thead>
        <tbody>{rows.map((r) => {
          const at = new Set(r.phase.split("/"));
          const [label, tone] = STATUS[r.status] ?? [r.status || "—", ""];
          return <tr key={r.id}>
            <td><a href={r.url} target="_blank" rel="noreferrer noopener" className="trial-id">{r.id} ↗</a><span className="trial-title">{r.title}</span>
              {r.date && <small className="muted">{r.date}{r.news > 1 ? ` · ${r.news} 条报道` : ""}</small>}</td>
            <td><span className="trial-steps" aria-label={r.phase || "阶段未注明"}>{PHASES.map(([k, l]) => <i key={k} className={at.has(k) ? "on" : ""} title={l} />)}</span>
              <small>{!r.phase || /^N\/?A$/i.test(r.phase) ? "阶段未注明" : r.phase.split("/").map((p) => PHASES.find(([k]) => k === p)?.[1] ?? p).join(" / ")}</small></td>
            <td><span className={`trial-status ${tone}`}>{label}</span></td>
            <td>{r.sponsor || "—"}{r.collaborators.length > 0 && <small className="muted">合作：{r.collaborators.slice(0, 3).join("、")}</small>}</td>
            <td className="trial-cond">{r.conditions.slice(0, 3).join("；") || "—"}{r.conditions.length > 3 && <small className="muted">等 {r.conditions.length} 项</small>}</td>
          </tr>;
        })}</tbody>
      </table></div>
    </Block>}
    <Block title="临床相关报道" note="按时间">
      {loading && !data ? <Skeleton lines={4} /> : items.length ? <ResearchSourceList items={items} />
        : <Empty title="暂无临床相关报道" />}
    </Block>
    <ResearchMore page={page} loading={loading} more={more}/>
  </div>;
}

// ------------------------------------------------------------------ 论文

interface Paper { item: Item; authors: string[]; institutions: string[]; cited: number | null; year: string; quarter: string; structured: boolean }

function quarterOf(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : `${String(d.getFullYear()).slice(2)}Q${Math.floor(d.getMonth() / 3) + 1}`;
}

export function PapersView({ detail }: { detail: ProjectDetail }) {
  const { data, loading, error, reload, page, more, note } = useCategory(detail, "paper");
  const [all, setAll] = useState(false);
  if(error)return <div><ErrorNote error={error}/><button className="link small" onClick={reload}>重新读取已保存的论文信息</button></div>;
  if(!loading && !data?.length)return <ResearchEmpty detail={detail} label="论文"/>;
  // A verdict that could not be matched to its source may belong to another paper: list it apart, count nothing from it.
  const unreviewed = (data ?? []).filter((item) => item.review === 'unreviewed').sort(byNewest);
  const items = (data ?? []).filter((item) => item.review !== 'unreviewed').sort(byNewest);
  const relationOf = (item: Item) => { const r = item.extra?.research_relation; return typeof r === 'string' && RELATION[r] && r !== 'unrelated' ? r : 'other'; };
  const ORDER = ['subject', 'competitor', 'route', 'field', 'other'];
  const LABEL: Record<string, string> = { ...RELATION, subject: '公司署名', other: '关系未标注' };
  const byRelation = ORDER.map((key) => ({ key, items: items.filter((item) => relationOf(item) === key) })).filter((g) => g.items.length);
  const papers: Paper[] = items.map((item) => {
    const x = extras(item).find((e) => e.openalex_id) ?? {};
    const when = item.published_at && Date.parse(item.published_at) <= Date.now() ? item.published_at : "";
    return { item, authors: list(x.authors), institutions: list(x.institutions), cited: typeof x.cited_by === "number" ? x.cited_by : null,
      year: when.slice(0, 4), quarter: quarterOf(when), structured: !!x.openalex_id };
  });
  const structured = papers.filter((p) => p.structured);
  const year = papers.filter((p) => { const age = Date.now() - Date.parse(p.item.published_at ?? ''); return age >= 0 && age < 365 * 86400_000; }).length;
  const inst = new Map<string, number>();
  for (const p of structured) for (const name of new Set(p.institutions)) inst.set(name, (inst.get(name) ?? 0) + 1);
  const topInst = [...inst.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8);
  const cited = structured.filter((p) => p.cited !== null).sort((a, b) => (b.cited ?? 0) - (a.cited ?? 0)).slice(0, 5);
  const quarters = lastQuarters(8);
  const perQ = quarters.map((q) => ({ q, n: papers.filter((p) => p.quarter === q).length }));
  const maxQ = Math.max(1, ...perQ.map((x) => x.n));
  return <div className="tab-frame research-view">
    <VerdictBand eyebrow="论文" meta={note}
      title={loading && !data ? "正在读取论文…" : papers.length ? `已收录 ${papers.length} 篇相关论文：${byRelation.map((g) => `${LABEL[g.key]} ${g.items.length} 篇`).join('、')}` : "还没有收录到相关论文"}
      figures={<>
        <Figure label="已收录论文" value={papers.length} unit="篇" note={`${structured.length} 篇有作者与机构`} />
        <Figure label="近一年" value={year} unit="篇" note="按发表时间" />
        <Figure label="发文机构" value={inst.size} unit="家" note="来自结构化记录" />
        <Figure label="最高被引" value={cited[0]?.cited ?? "—"} unit={cited[0] ? "次" : undefined} note="OpenAlex 统计" />
      </>} />
    {papers.length > 0 && <Block card title="与公司的关系" note="AI 按署名与内容判断，署名以原文为准">
      <ul className="inst-bars">{byRelation.map((g) => <li key={g.key}><span>{LABEL[g.key]}</span>
        <i><motion.b initial={{ width: 0 }} animate={{ width: `${g.items.length / Math.max(...byRelation.map((x) => x.items.length)) * 100}%` }} transition={{ duration: .5, ease: EASE }} /></i><em>{g.items.length}</em></li>)}</ul>
    </Block>}
    {papers.length > 0 && <div className="research-pair">
      <Block card title="发文节奏" note="近 8 个季度">
        <div className="q-bars" role="img" aria-label={perQ.map((x) => `${x.q} ${x.n} 篇`).join("，")}>
          {perQ.map((x, i) => <div key={x.q} className="q-bar"><b>{x.n || ""}</b>
            <motion.span initial={{ height: 0 }} animate={{ height: `${x.n / maxQ * 100}%` }} transition={{ delay: i * .04, duration: .5, ease: EASE }} />
            <small>{x.q}</small></div>)}
        </div>
      </Block>
      <Block card title="谁在发" note={structured.length ? `${structured.length} 篇有机构信息` : "暂无机构信息"}>
        {topInst.length ? <ul className="inst-bars">{topInst.map(([name, n]) => <li key={name}><span title={name}>{name}</span>
          <i><motion.b initial={{ width: 0 }} animate={{ width: `${n / topInst[0][1] * 100}%` }} transition={{ duration: .5, ease: EASE }} /></i><em>{n}</em></li>)}</ul>
          : <p className="muted small">收录的论文没有机构字段。</p>}
      </Block>
    </div>}
    {cited.length > 0 && <Block card title="被引最多" note="OpenAlex 被引次数，非影响力结论">
      <ol className="cited-list">{cited.map((p) => <li key={p.item.id}>
        <span className="cited-n">{p.cited}</span>
        <div><a href={p.item.url} target="_blank" rel="noreferrer noopener">{p.item.title}</a>
          <small className="muted">{[p.authors.slice(0, 3).join(", ") + (p.authors.length > 3 ? " 等" : ""), p.institutions[0], p.year].filter(Boolean).join(" · ")}</small></div>
      </li>)}</ol>
    </Block>}
    {loading && !data ? <Skeleton lines={4} /> : !items.length ? <Block title="全部论文"><Empty title="暂无相关论文" /></Block>
      : byRelation.map((g) => <Block key={g.key} title={LABEL[g.key]} note={`${g.items.length} 篇`}
          action={g.items.length > 5 ? <button className="link small" onClick={() => setAll((v) => !v)}>{all ? "收起" : `展开全部 ${g.items.length} 篇`}</button> : undefined}>
          <ResearchSourceList items={all ? g.items : g.items.slice(0, 5)} />
        </Block>)}
    {unreviewed.length > 0 && <details className="item-reading-detail"><summary className="muted small">
      另有 {unreviewed.length} 篇待核实：AI 判断未能与原文对应，未计入上方统计</summary>
      <ResearchSourceList items={unreviewed} /></details>}
    <ResearchMore page={page} loading={loading} more={more}/>
  </div>;
}

function lastQuarters(n: number): string[] {
  const d = new Date();
  let y = d.getFullYear(), q = Math.floor(d.getMonth() / 3) + 1;
  const out: string[] = [];
  for (let i = 0; i < n; i++) { out.unshift(`${String(y).slice(2)}Q${q}`); q -= 1; if (!q) { q = 4; y -= 1; } }
  return out;
}

// ------------------------------------------------------------------ 专利

export function PatentsView({ detail }: { detail: ProjectDetail }) {
  const { data, loading, error, reload, page, more, note } = useCategory(detail, "patent");
  if(error)return <div><ErrorNote error={error}/><button className="link small" onClick={reload}>重新读取已保存的专利信息</button></div>;
  if(!loading && !data?.length)return <ResearchEmpty detail={detail} label="专利与注册"/>;
  const items = (data ?? []).slice().sort(byNewest);
  const own = companyNames(detail);
  // Patent search returns sector news by keyword, so most records are other companies' patents. Say whose they are.
  const mine = items.filter((i) => own.some((n) => i.title.includes(n) || i.snippet.includes(n)));
  const others = items.filter((i) => !mine.includes(i));
  // What can be read from the records themselves: relation, filing office and document kind of each number.
  const relationOf = (item: Item) => { const r = item.extra?.research_relation; return typeof r === 'string' && RELATION[r] && r !== 'unrelated' ? r : 'other'; };
  const LABEL: Record<string, string> = { ...RELATION, subject: '本公司申请', other: '关系未标注' };
  const relations = ['subject', 'competitor', 'route', 'field', 'other']
    .map((key) => ({ key, n: items.filter((i) => relationOf(i) === key).length })).filter((g) => g.n);
  const numbers = items.map((i) => patentNumber(i)).filter((x): x is { office: string; kind: string } => !!x);
  const offices = [...numbers.reduce((m, x) => m.set(x.office, (m.get(x.office) ?? 0) + 1), new Map<string, number>())].sort((a, b) => b[1] - a[1]);
  const granted = numbers.filter((x) => x.kind === 'B').length;
  return <div className="tab-frame research-view">
    <VerdictBand eyebrow="专利与注册" meta={`通用网页检索 · ${note ?? ''}`}
      title={loading && !data ? "正在读取专利信息…" : !items.length ? "还没有检索到专利或注册信息"
        : mine.length ? `检索到 ${items.length} 条，其中 ${mine.length} 条提到本公司` : `检索到 ${items.length} 条，尚未匹配到本公司名称`}
      figures={items.length ? <>
        <Figure label="检索到" value={items.length} unit="条" note="专利或注册相关报道" />
        <Figure label="提到本公司" value={mine.length} unit="条" note={mine.length ? "仍需到专利库核实" : "本公司专利未被检索到"} />
        <Figure label="其他相关线索" value={others.length} unit="条" note="未匹配到本公司名称" />
      </> : undefined}>
      <p className="tf-sub">专利只来自通用网页检索，没有接入国家知识产权局或专利数据库，<b>既不能代表本公司的专利布局，也不是完整的同行专利清单</b>。下方按是否提到本公司分开列出。</p>
    </VerdictBand>
    {items.length > 0 && <div className="research-pair">
      <Block card title="与公司的关系" note="AI 按申请人与内容判断，权利人以专利登记为准">
        <ul className="inst-bars">{relations.map((g) => <li key={g.key}><span>{LABEL[g.key]}</span>
          <i><motion.b initial={{ width: 0 }} animate={{ width: `${g.n / Math.max(...relations.map((x) => x.n)) * 100}%` }} transition={{ duration: .5, ease: EASE }} /></i><em>{g.n}</em></li>)}</ul>
      </Block>
      <Block card title="布局地域与状态" note={numbers.length ? `${numbers.length} 条带公开号 · 按公开号判断` : '记录中没有公开号'}>
        {numbers.length ? <>
          <ul className="inst-bars">{offices.slice(0, 6).map(([office, n]) => <li key={office}><span>{OFFICE[office] ?? office}</span>
            <i><motion.b initial={{ width: 0 }} animate={{ width: `${n / offices[0][1] * 100}%` }} transition={{ duration: .5, ease: EASE }} /></i><em>{n}</em></li>)}</ul>
          <p className="muted small">其中授权文本 {granted} 条、申请公开 {numbers.length - granted} 条（按公开号末尾 A/B 判断；法律状态以专利登记为准）。</p>
        </> : <p className="muted small">检索到的记录没有可识别的公开号。</p>}
      </Block>
    </div>}
    {!items.length ? <Block title="专利与注册信息"><Empty title="暂无专利或注册信息">需要接入专利数据库才能做专利布局分析。</Empty></Block> : <>
      <Block title="提到本公司的专利动态" note={mine.length ? "仍需到专利库核实权利人与法律状态" : "检索未命中"}>
        {mine.length ? <ResearchSourceList items={mine} />
          : <p className="muted small">暂无收录的公司专利记录。</p>}
      </Block>
      {others.length > 0 && <Block title="其他专利与注册线索" note={`${others.length} 条 · 未匹配到本公司名称`}>
        <ResearchSourceList items={others} />
      </Block>}
    </>}
    <ResearchMore page={page} loading={loading} more={more}/>
  </div>;
}

const OFFICE: Record<string, string> = { CN: '中国', US: '美国', EP: '欧洲', WO: 'PCT 国际', JP: '日本', KR: '韩国', DE: '德国', TW: '中国台湾' };

/** Publication number in the record (CN117878383B, US2024123456A1): office and A/B document kind. */
function patentNumber(item: Item): { office: string; kind: string } | null {
  const raw = `${str(item.extra?.patent_number)} ${item.title} ${item.url}`;
  const m = raw.match(/(?<![A-Za-z])(CN|US|EP|WO|JP|KR|DE|TW)\s?\d{6,}\s?([AB])\d?(?![A-Za-z\d])/);
  return m ? { office: m[1], kind: m[2] } : null;
}

/** Match saved names only. Stripping company suffixes can turn a name into an unrelated common word. */
function companyNames(detail: ProjectDetail): string[] {
  const p = detail.project;
  const raw = [p.short_name, p.name, p.legal_name].filter((x): x is string => !!x && x.length >= 2);
  return [...new Set(raw)];
}

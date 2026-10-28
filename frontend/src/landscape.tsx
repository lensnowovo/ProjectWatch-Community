import { motion } from "motion/react";
import { ChartHint } from "./chartHint";
import { useState, type ReactNode } from "react";
import { PositionJudgment } from "./judgment";
import { ExternalNews, unmatchedReports, useExternalNews } from "./ExternalNews";
import { api, type Category, type BriefEntry, type Item, type Landscape, type ProjectDetail, type RegistryOverlap, type ScopedRelationship } from "./api";
import { ItemList } from "./items";
import { Storylines, WatchBar } from "./monitor";
import { EventStates, StoredEvidence } from "./MonitoringDetails";
import { Commitments } from "./commitments";
import { RedFlags } from "./flags";
import { EASE, useToast } from "./motion";
import { Empty, ErrorNote, EvidenceLinks, byNewest, seenAt, errorMessage, relTime, useAsync } from "./ui";
import { insightRevision, jobWorkState, monitoringRevision, WORK_LABEL } from "./workState";
import { companyName, competitionIdentity } from "./peerNames";
import { Block, Figure, Reference, Skeleton, VerdictBand } from "./frame";
import { useStagedRead, WorkIndicator } from "./Operational";

// ------------------------------------------------------------------ 公司动态

function OutlookColumn({ title, tone, entries }: { title: string; tone: string; entries?: BriefEntry[] }) {
  return (
    <div className={`outlook-col outlook-${tone}`}>
      <h4>{title}</h4>
      {entries?.length ? (
        <ul>{entries.map((e, i) => (
          <li key={i}>{e.text}{e.refs.map((r) => <a key={r.item_id} className="ref" href={r.url} target="_blank" rel="noreferrer noopener" title={r.title}>依据 ↗</a>)}</li>
        ))}</ul>
      ) : <p className="muted small">本期没有</p>}
    </div>
  );
}

/** 公司进展: the company's own events followed by a continuous, stable reading list.
 * Clinical trials, papers and patents have their own pages. */
export function NewsView({ detail, financing }: { detail: ProjectDetail; financing?: ReactNode }) {
  const [major, setMajor] = useState(false);
  const [watch, setWatch] = useState<string | null>(null);
  const projectId = detail.project.id;
  const revision = monitoringRevision(detail);
  const monitor = useAsync(() => api.monitor(projectId), [projectId, revision]);
  const { data, loading, error, reload, setData } = useAsync(
    () => (watch ? api.watchEvents(watch)
      : api.feed({ project_id: projectId, category: "company", importance: major ? "major" : undefined, limit: 200 })),
    [projectId, major, watch],
  );
  const staged = useStagedRead(`${projectId}:${major}:${watch}`, () => watch ? api.watchEvents(watch)
    : api.feed({ project_id: projectId, category: "company", importance: major ? "major" : undefined, limit: 200 }),
    next => next.some(item => !(data ?? []).some(old => old.id === item.id)), !!data);
  const added = staged.pending?.filter(item => !(data ?? []).some(old => old.id === item.id)).length ?? 0;
  const own = (watch ? data ?? [] : (data ?? []).filter((i) => i.category === "company")).slice().sort(byNewest);
  const outlook = detail.brief?.outlook;
  const stale = detail.brief?.dependency?.status === "stale";
  const hasOutlook = !stale && !!outlook && ["opportunities", "threats", "verify"].some((k) => (outlook as Record<string, BriefEntry[] | undefined>)[k]?.length);
  const update = (changed: Item) =>
    setData((items) => (items ?? []).map((i) => (i.id === changed.id ? changed : i)).filter((i) => i.status !== "dismissed"));
  const watchpoints = monitor.data?.watchpoints ?? [];
  const activeWatch = watchpoints.find((w) => w.id === watch);
  const month = own.filter((i) => Date.now() - Date.parse(i.published_at ?? i.collected_at) < 30 * 86400_000);
  const majors = month.filter((i) => i.importance === "major").length;
  const notable = month.filter((i) => i.importance === "notable").length;
  const [limit, setLimit] = useState(12);

  if (error && !data) return <div className="tab-frame news-view">
    <VerdictBand eyebrow="公司进展" title="暂时无法读取公司动态" />
    <ErrorNote error={error} />
    <button className="link small" onClick={reload}>重新读取已保存的公司动态</button>
  </div>;

  return (
    <div className="tab-frame news-view">
      <VerdictBand eyebrow="公司进展" meta={detail.brief ? `${detail.brief.origin?.producer ?? "AI"} 整理 · ${relTime(detail.brief.updated_at)}` : "等待首次检索"}
        title={loading && !data ? "正在读取公司动态…" : month.length ? `近 30 天 ${majors ? `${majors} 条重大、` : ""}${notable} 条值得关注的公司动态` : "近 30 天没有新的公司自身动态"}
        figures={<>
          <Figure label="重大" value={majors} unit="条" note="近 30 天" tone={majors ? "loss" : undefined} />
          <Figure label="值得关注" value={notable} unit="条" note="近 30 天" />
          <Figure label="公司动态" value={detail.counts.company ?? 0} unit="条" note="已收录 · 同一事件合并" />
          <Figure label="来源" value={new Set(own.map((i) => i.source)).size} unit="家" note="同一稿件转载合并" />
        </>} />

      <NewsStats items={own} counts={detail.counts} />
      {!!error && <p role="alert" className="muted small">公司动态读取失败，已读取的内容保留。 <button className="link" onClick={reload}>重新读取</button></p>}
      {staged.failed && <p className="muted small">暂时无法检查更新，当前动态保留。</p>}
      {added > 0 && <button className="link op-new" onClick={() => { setData(staged.pending); staged.clear(); }}>新增 {added} 条 · 点击查看</button>}
      <Commitments projectId={projectId} revision={revision} progress={detail.monitoring_progress} />
      {detail.capabilities?.insight_projections === true && <RedFlags projectId={projectId} revision={revision} progress={detail.monitoring_progress} />}

      <Block card title="本期解读">
        {hasOutlook ? <div className="outlook-grid">
          <OutlookColumn title="机会" tone="up" entries={outlook?.opportunities} />
          <OutlookColumn title="威胁" tone="down" entries={outlook?.threats} />
          <OutlookColumn title="值得追问" tone="verify" entries={outlook?.verify} />
        </div> : !stale && detail.brief?.summary ? <p className="side-text">{detail.brief.summary}</p>
          : <p className="muted small">{stale ? "资料或关系已更正，解读待更新。" : detail.brief ? "等待更新解读。" : "暂无本期解读，可先阅读已收录动态。"}</p>}
      </Block>

      <section className="company-news-stream" aria-label="公司新闻">
          <div className="filters">
            {activeWatch ? (
              <>
                <span className="watch-filter">只看命中“{activeWatch.label}”的动态 <button className="link" onClick={() => {setWatch(null);setLimit(12);}}>清除</button></span>
                <span className="spacer" />
                <span className="muted small" title={activeWatch.criteria}>判断标准：{activeWatch.criteria}</span>
              </>
            ) : (
              <>
                <label className="check check-inline"><input type="checkbox" checked={major} onChange={(e) => {setMajor(e.target.checked);setLimit(12);}} />只看重大</label>
                <span className="muted small">{own.length} 条已读取 · 日期倒序</span>
              </>
            )}
          </div>
          {loading && !data && <Skeleton lines={6} />}
          {data && own.length === 0 && (watch
            ? <Empty title="这个关注点还没有命中" />
            : <Empty title="暂无公司自身动态" />)}
          {own.length>0 && <ItemList items={own.slice(0,limit)} onChange={update} grouped={false} watchLabels={monitor.data?.watch_labels} />}
          {own.length>limit && <button className="btn btn-ghost btn-sm news-older" onClick={() => setLimit(n=>n+20)}>再看 {Math.min(20,own.length-limit)} 条（共 {own.length} 条）</button>}
      </section>

      <Reference items={[
        { key: "financing", title: "融资记录（专业数据源）", body: financing ?? null },
        { key: "events", title: "事件进展", note: "融资、签约、交付等事件的最近有依据阶段", body: <EventStates detail={detail} /> },
        { key: "threads", title: "事件脉络", body: monitor.data ? <Storylines threads={monitor.data.threads} /> : null },
        { key: "watch", title: "按关注点筛选", note: watchpoints.length ? `${watchpoints.length} 个` : undefined,
          body: monitor.data ? <WatchBar projectId={projectId} watchpoints={watchpoints} active={watch} onSelect={value=>{setWatch(value);setLimit(12);}} onChanged={monitor.reload}
            suggestions={watchSuggestions(monitor.data.threads, detail.brief?.watch ?? [], watchpoints)} /> : null },
      ]} />
    </div>
  );
}

const WEIGHT = [["major", "重大"], ["notable", "关注"], ["routine", "一般"]] as const;

/** 公司进展 opener: how much the company actually moved, month by month, split by how heavy each item was.
 * Dates come from seenAt, so an item dated in the future counts when it was collected, never ahead of today. */
export function NewsStats({ items, counts }: { items: Item[]; counts: { company?: number } }) {
  const now = new Date();
  const months: { key: string; month: number; year: number; major: number; notable: number; routine: number }[] = [];
  for (let i = 11; i >= 0; i--) {
    const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
    months.push({ key: `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`, month: d.getMonth() + 1, year: d.getFullYear(), major: 0, notable: 0, routine: 0 });
  }
  // An item published ahead of today (a scheduled talk, a dated filing) has not happened: it stays in the
  // collected totals but never in a "what moved recently" window.
  const nowIso = new Date().toISOString();
  const scheduled = (i: Item) => !!i.published_at && i.published_at > nowIso;
  const byKey = new Map(months.map((m) => [m.key, m]));
  for (const item of items) {
    if (scheduled(item)) continue;
    const m = byKey.get(seenAt(item).slice(0, 7));
    if (m) m[item.importance] += 1;
  }
  const total = (m: (typeof months)[number]) => m.major + m.notable + m.routine;
  const peak = Math.max(1, ...months.map(total));
  // A single spike would flatten every other month; the scale tops at the peak but marks a readable middle line.
  const mid = peak > 3 ? Math.round(peak / 2) : 0;
  const recent = items.filter((i) => !scheduled(i) && Date.now() - Date.parse(seenAt(i)) < 90 * 86400_000);
  const recentMajor = recent.filter((i) => i.importance === "major").length;
  const major = items.filter((i) => i.importance === "major").length;
  const confirmed = items.filter((i) => (i.source_count ?? 1) > 1).length;
  const sources = new Set(items.flatMap((i) => ((i.sources ?? []).map((s) => s.source).filter(Boolean) as string[]).concat(i.source ? [i.source] : []))).size;
  const busiest = months.reduce((a, b) => (total(b) > total(a) ? b : a), months[0]);
  const active = months.filter((m) => total(m) > 0).length;
  const quiet = months.slice(-3).every((m) => !total(m));
  const H = 168;
  return <Block card title="动态节奏" note={`近 12 个月 · 已收录 ${counts.company ?? items.length} 条公司动态 · 柱高按条数，刻度上限 ${peak} 条`}>
    <div className="ns">
      <div className="ns-plot">
        <div className="ns-axis" aria-hidden="true">
          <span style={{ bottom: "100%" }}>{peak}</span>
          {mid > 0 && <span style={{ bottom: `${mid / peak * 100}%` }}>{mid}</span>}
          <span style={{ bottom: 0 }}>0</span>
        </div>
        <div className="ns-chart" style={{ height: H }} role="img"
          aria-label={months.map((m) => `${m.year} 年 ${m.month} 月 ${total(m)} 条`).join("，")}>
          <i className="ns-grid" style={{ bottom: "100%" }} />
          {mid > 0 && <i className="ns-grid" style={{ bottom: `${mid / peak * 100}%` }} />}
          {months.map((m, i) => {
            const n = total(m);
            const bar = <div className={`ns-col${n ? "" : " empty"}`}>
              <span className="ns-n">{n || ""}</span>
              <span className="ns-stack">{WEIGHT.map(([k]) => m[k] ? <motion.i key={k} className={`ns-${k}`}
                initial={{ height: 0 }} animate={{ height: `${m[k] / peak * (H - 22)}px` }} transition={{ delay: i * .03, duration: .45, ease: EASE }} /> : null)}</span>
            </div>;
            return n ? <ChartHint key={m.key} title={`${m.year} 年 ${m.month} 月`} value={`${n} 条`}
              meta={WEIGHT.filter(([k]) => m[k]).map(([k, label]) => `${label} ${m[k]}`).join(" · ")}>{bar}</ChartHint>
              : <div key={m.key} className="ns-slot">{bar}</div>;
          })}
        </div>
        {/* Labels live in their own rows, so a month without bars still lines up with the rest. */}
        <div className="ns-months" aria-hidden="true">{months.map((m) => <small key={m.key}>{m.month}月</small>)}</div>
        <div className="ns-years" aria-hidden="true">{months.map((m, i) => <u key={m.key}>{m.month === 1 || i === 0 ? m.year : ""}</u>)}</div>
        <div className="ns-legend">{WEIGHT.map(([k, label]) => <span key={k}><i className={`ns-${k}`} />{label}</span>)}
</div>
      </div>
      <dl className="ns-side">
        <div><dt>近 90 天</dt><dd><b className="num-display">{recent.length}</b> 条{recentMajor > 0 && <em>含 {recentMajor} 条重大</em>}</dd></div>
        <div><dt>重大动态</dt><dd><b className="num-display">{major}</b> 条<em>占已收录 {items.length ? Math.round(major / items.length * 100) : 0}%</em></dd></div>
        <div><dt>多来源印证</dt><dd><b className="num-display">{confirmed}</b> 条<em>共 {sources} 家来源</em></dd></div>
        <div><dt>有动态的月份</dt><dd><b className="num-display">{active}</b> / 12<em>其余月份没有收录到</em></dd></div>
      </dl>
    </div>
    <ul className="qa-reading ns-reading">
      {items.length >= 6 && total(busiest) > 1 && <li>{busiest.year} 年 {busiest.month} 月最密集，当月 {total(busiest)} 条；12 个月里只有 {active} 个月有动态。</li>}
      {quiet ? <li>近三个月没有收录到公司自身动态：可能确实安静，也可能是公开渠道没有报道。</li>
        : <li>近 90 天 {recent.length} 条，其中 {recentMajor} 条判为重大。</li>}
      {items.length >= 6 && <li>{confirmed ? `${confirmed} 条有两家以上来源互相印证，其余为单一来源。` : "全部为单一来源报道，尚无多来源互相印证。"}</li>}
    </ul>
  </Block>;
}

// ------------------------------------------------------------------ shared

function LandscapeStatus({ detail, landscape, what }: { detail: ProjectDetail; landscape?: Landscape | null; what: string }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const running = detail.jobs.some((j) => j.kind === "landscape" && (j.status === "queued" || j.status === "running"));
  const last = detail.jobs.find((j) => j.kind === "landscape");
  const work = last && (last.status === "running" || last.status === "queued") ? jobWorkState(detail, last) : null;
  const failed = !running && last?.status === "failed" && (!landscape || last.created_at > landscape.generated_at) ? last : null;
  const refresh = async () => {
    setBusy(true);
    try { await api.refresh(detail.project.id, "landscape"); toast(`${what}已加入更新队列`); }
    catch (e) { toast(errorMessage(e), "error"); }
    finally { setBusy(false); }
  };
  return (
    <div className="landscape-status muted small">
      {landscape ? <>上次 {relTime(landscape.generated_at)} · {landscape.evidence_count} 条依据{landscape.with_search ? " · 含搜索结果" : " · 仅收录新闻"}</>
        : "资料摘要尚未整理"}
      {running ? <WorkIndicator state={work || "queued"} label={last?.runtime?.label || (work ? WORK_LABEL[work] : "等待更新")} /> : <button className="link" disabled={busy || !detail.model_ready || !detail.project.activated} onClick={refresh}>更新资料摘要 ↻</button>}
      {landscape?.errors?.length ? <span className="text-warn"> · 部分未完成：{landscape.errors.map(errorMessage).join("；")}</span> : null}
      {failed && <span className="text-warn"> · 上次整理没有完成{failed.error ? `：${errorMessage(failed.error)}` : ""}</span>}
    </div>
  );
}

// ------------------------------------------------------------------ 竞争对手

function BilateralBasis({ projectId, row }: { projectId: string; row: ScopedRelationship }) {
  const facts = row.basis_facts ?? [];
  if (!facts.length) return null;
  const dimensions: Record<string, string> = { use_case: "用途", buyer_type: "购买方类型", delivery: "交付方式" };
  return <details><summary className="muted small">双方依据{row.created_at ? ` · ${row.created_at.slice(0, 10)}` : ""}</summary>
    {facts.map(f => <div key={f.fact_id}><p><strong>{f.subject_id === row.subject_product_id ? row.subject_product_name : row.peer_product_name}</strong>
      {` · ${dimensions[f.predicate] ?? "资料陈述"}：${f.value}`}</p>
      <StoredEvidence key={`${projectId}:${f.fact_id}`} projectId={projectId} refs={f.refs} /></div>)}
    {row.basis_facts_truncated && <p className="muted small">仅显示部分依据。</p>}
  </details>;
}

/** Only located, actual assertions for this exact product enter a comparison cell. */
export function comparisonValue(row: ScopedRelationship, productId: string | null | undefined, predicate: string) {
  if (!productId) return "—";
  const values = (row.basis_facts ?? []).filter(f => f.subject_id === productId && f.predicate === predicate
    && f.basis === "actual" && f.refs.length > 0).map(f => f.value.trim()).filter(Boolean);
  return [...new Set(values)].join("；") || "—";
}

function ProductComparison({ detail, rows, action }: {
  detail: ProjectDetail; rows: ScopedRelationship[]; action: (row: ScopedRelationship, decision: "exclude" | "reset") => ReactNode;
}) {
  return <div className="table-scroll comparison-scroll"><table className="table product-comparison">
    <thead><tr><th scope="col">公司 / 产品</th><th scope="col">用途</th><th scope="col">购买方类型</th><th scope="col">交付方式</th></tr></thead>
    {rows.map(row => <tbody key={row.scope_key}>
      <tr className="comparison-own"><th scope="row"><span className="comparison-side">本公司</span>{detail.project.name}
        <small>{row.subject_product_name}</small></th>
        {["use_case", "buyer_type", "delivery"].map(p => <td key={p}>{comparisonValue(row, row.subject_product_id, p)}</td>)}</tr>
      <tr><th scope="row"><span className="comparison-side">对照公司</span>{row.peer_name}<small>{row.peer_product_name}</small></th>
        {["use_case", "buyer_type", "delivery"].map(p => <td key={p}>{comparisonValue(row, row.peer_product_id, p)}</td>)}</tr>
      <tr className="comparison-basis"><td colSpan={4}>
        <div className="comparison-conclusion"><span className="cf-tag">{RELATION_LABEL[row.relation_type]}</span>
          <span>{row.rationale || row.reason}</span>{action(row, "exclude")}</div>
        <BilateralBasis projectId={detail.project.id} row={row} />
      </td></tr>
    </tbody>)}
  </table></div>;
}

const RELATION_LABEL = { direct: "直接竞争", substitute: "替代方案", adjacent: "邻近业务", unknown: "关系待定", unrelated: "无关" };

export const REGISTRY_LEVEL: Record<RegistryOverlap["level"], string> = {
  same_target_and_modality: "同靶点、同技术形式", same_modality: "同技术形式", same_target: "同靶点",
};
const CELL_SOURCE: Record<RegistryOverlap["cell_source"], string> = { same: "细胞来源相同", different: "细胞来源不同", unknown: "" };
export const INDICATION_OVERLAP: Record<NonNullable<RegistryOverlap["indication_overlap"]>, string> = {
  same: "同一疾病类别", different: "不同疾病类别", unknown: "无法比较",
};
export const FAMILY_LABEL: Record<string, string> = {
  lymphoma: "淋巴瘤", "b-all": "B细胞急性淋巴细胞白血病", cll: "慢性淋巴细胞白血病", "multiple-myeloma": "多发性骨髓瘤",
  "al-amyloidosis": "AL淀粉样变", myeloid: "髓系肿瘤", autoimmune: "自身免疫病", "t-cell-malignancy": "T细胞肿瘤",
};
const families = (values?: string[]) => (values ?? []).map(v => FAMILY_LABEL[v] ?? v).join("、");

// One row per peer product: same disease area first, then target/modality overlap.
export function registryRows(detail: ProjectDetail) {
  const indication = ["same", "unknown", "different"];
  const order = ["same_target_and_modality", "same_modality", "same_target"];
  const removed = new Set((detail.relationships?.exclusions ?? []).filter(r => r.company_wide)
    .flatMap(r => r.peer_names ?? [r.peer_name]).map(n => n.toLocaleLowerCase()));
  return (detail.competition_research?.registry?.peers ?? [])
    .filter(peer => companyName(peer.name) && !removed.has(peer.name.toLocaleLowerCase()))
    .flatMap(peer => peer.products.map(product => ({ peer, product })))
    .sort((a, b) => indication.indexOf(a.product.indication_overlap ?? "unknown") - indication.indexOf(b.product.indication_overlap ?? "unknown")
      || order.indexOf(a.product.level) - order.indexOf(b.product.level));
}

function RegistryComparison({ detail }: { detail: ProjectDetail }) {
  const registry = detail.competition_research?.registry;
  const rows = registryRows(detail);
  if (!registry || registry.status === "not_applicable" || (!rows.length && registry.status !== "failed")) return null;
  return <section className="card card-pad registry-panel">
    <div className="card-head"><h3>临床登记对照</h3>
      <span className="muted small">{rows.length} 个在研产品 · 来源 ClinicalTrials.gov</span></div>
    <p className="muted small">按公开试验登记比较靶点、技术形式和疾病类别，不等于已确认的竞争关系；申办方是登记信息，不等于已核实的产品所有人。
      {registry.excluded.non_industry > 0 && ` 已略过医院、大学等非企业申办方 ${registry.excluded.non_industry} 项。`}
      {(registry.excluded.grouped_alternatives ?? 0) > 0 && ` 另有 ${registry.excluded.grouped_alternatives} 项把多个备选产品登记在一起，未当作单一产品。`}
      {registry.status === "failed" && " 本轮登记查询未成功，下次更新会重试。"}</p>
    {(registry.target_products ?? []).map(t => <p className="muted small" key={t.product_id}>本公司对照依据：{t.product_name}
      （{[...Object.values(t.targets), ...Object.values(t.modalities)].join("、")}
      {t.indication_families?.length ? `；疾病类别：${families(t.indication_families)}` : "；疾病类别未知"}；
      {t.basis === "own_registry_record" || t.indication_basis?.includes("own_registry_record")
        ? `参考本公司登记记录 ${t.anchor_nct_ids.join("、")}` : "取自已引用的产品描述"}）</p>)}
    {rows.length > 0 && <div className="table-scroll"><table className="registry-table">
      <thead><tr><th>公司</th><th>在研产品</th><th>与本公司的共同点</th><th>疾病类别</th><th>登记试验</th></tr></thead>
      <tbody>{rows.map(({ peer, product }) => <tr key={product.product_id}>
        <td>{peer.name}</td>
        <td>{product.name}{!!product.aliases?.length && <span className="muted small"><br />又名 {product.aliases.join("、")}</span>}</td>
        <td>{REGISTRY_LEVEL[product.level]}{product.shared_targets.length > 0 && <span className="muted small"> · {product.shared_targets.join("、")}</span>}
          {CELL_SOURCE[product.cell_source] && <span className="muted small"> · {CELL_SOURCE[product.cell_source]}</span>}</td>
        <td>{INDICATION_OVERLAP[product.indication_overlap ?? "unknown"]}
          {!!product.indication_families?.length && <span className="muted small"><br />{families(product.indication_families)}</span>}</td>
        <td>{(product.trials ?? product.nct_ids.map((nct, i) => ({ nct, url: product.urls[i], phase: "", status: "", conditions: product.indications,
            indication_basis: "study" as const, product_name: product.name })))
          .map(t => <div key={t.nct} className="registry-trial">
            <a href={t.url} target="_blank" rel="noreferrer noopener">{t.nct}</a>
            <span className="muted small"> · {[t.phase, t.status].filter(Boolean).join(" · ") || "—"}</span>
            <div className="small">{t.conditions.join("；") || (t.indication_basis === "unattributed" ? "试验组未注明适应症" : "—")}</div>
          </div>)}</td>
      </tr>)}</tbody></table></div>}
    {(() => {
      const known = new Set(rows.map(r => r.peer.name.toLocaleLowerCase()));
      const removed = new Set((detail.relationships?.exclusions ?? []).filter(r => r.company_wide)
        .flatMap(r => r.peer_names ?? [r.peer_name]).map(n => n.toLocaleLowerCase()));
      const leads = (detail.competition_research?.candidates ?? []).filter(c => c.origin === "search" && companyName(c.name)
        && !known.has(c.name.toLocaleLowerCase()) && !removed.has(c.name.toLocaleLowerCase()));
      return leads.length > 0 && <p className="small registry-leads"><span className="muted">其他线索（来自搜索摘要，未核实）：</span>
        {leads.map(c => c.name).join("、")}</p>;
    })()}
  </section>;
}

export function CompetitionView({ detail, onReload, financing, race, expandAll = false }: {
  detail: ProjectDetail; onReload?: () => void; financing?: ReactNode; race?: ReactNode;
  /** Render every row and reference block open (print, review and tests); the page folds them by default. */
  expandAll?: boolean;
}) {
  const [openPeer, setOpenPeer] = useState<string | null>(null);
  const deep = useAsync(() => api.insight(detail.project.id).catch(() => null), [detail.project.id, insightRevision(detail)]);
  const toast = useToast();
  const [saving, setSaving] = useState(false);
  const [peerName, setPeerName] = useState("");
  const select = async (name: string, decision: "include" | "exclude", peerId?: string) => {
    if (!detail.project.config_version || saving) return;
    setSaving(true);
    try {
      await api.selectCompetitor(detail.project.id, detail.project.config_version, name, decision, peerId);
      toast(decision === "include" ? "已确认跟踪" : "已移除");
      setPeerName(""); onReload?.();
    } catch (e) { toast(e instanceof Error ? e.message : "未保存，请重试", "error"); }
    finally { setSaving(false); }
  };
  const correct = async (row: ScopedRelationship, decision: "exclude" | "reset") => {
    if (!row.subject_id || !row.peer_id || !detail.project.config_version) return;
    setSaving(true);
    try {
      await api.correctRelationship(detail.project.id, detail.project.config_version, row, decision);
      toast(decision === "exclude" ? "已排除，后续更新不会重加" : "已撤销排除");
      onReload?.();
    } catch { toast("未保存，请刷新后再试", "error"); }
    finally { setSaving(false); }
  };
  const research = async () => {
    setSaving(true);
    try { await api.refresh(detail.project.id, "competition"); toast("已开始更新竞对资料"); onReload?.(); }
    catch (e) { toast(e instanceof Error ? e.message : "暂时无法更新", "error"); }
    finally { setSaving(false); }
  };
  const action = (row: ScopedRelationship, decision: "exclude" | "reset") => row.subject_id && row.peer_id && detail.project.config_version ?
    <button className="link" disabled={saving} onClick={() => correct(row, decision)}>{decision === "exclude" ? "移除这项对照" : "恢复"}</button> : null;
  const landscape = detail.landscape;
  const comp = landscape?.competition;
  const moves = landscape?.moves ?? [];
  const feed = useExternalNews(detail.project.id, monitoringRevision(detail), ['competitor']);
  const news = feed.data?.items ?? [];
  const reports = [...news.filter(i => i.status !== 'dismissed').map((item) => {
    const source = item.sources?.[0];
    return { id: item.id, title: source?.title || item.title, url: source?.url || item.url,
      date: (source?.published_at || item.published_at || item.collected_at).slice(0, 10),
      type: "其他", topic: "其他", importance: item.importance };
  }), ...unmatchedReports(news, moves)];
  // Defense in depth for an older API/cache. Legacy peer rows have no verified
  // relation contract; never show their threats or field assignments as facts.
  const candidates = competitionCandidates(detail);
  const peerKey = competitionIdentity(detail);
  for (const row of detail.competition_research?.candidates ?? []) {
    if (!companyName(row.name)) continue;
    if (!(detail.relationships?.items ?? []).some((r) => r.peer_id === row.id)
        && !(detail.relationships?.exclusions ?? []).some((r) => r.peer_id === row.id && r.company_wide)
        && !candidates.some((r) => peerKey(r.name, r.peerId) === peerKey(row.name, row.id)))
      candidates.push({ name: row.name, refs: [], peerId: row.id });
  }
  const manual = (detail.relationships?.items ?? []).filter((r) => r.origin === "user_override" && companyName(r.peer_name));
  const automatic = (detail.relationships?.items ?? []).filter((r) => r.origin === "auto_research" && companyName(r.peer_name));
  const supported = automatic.filter((r) => r.support === "supported" && r.status === "current" && !r.scope_identity_changed);
  const exclusions = detail.relationships?.exclusions ?? [];
  const peers = new Map(candidates.map(c => [peerKey(c.name, c.peerId), { ...c, aliases: [c.name], rows: [] as ScopedRelationship[] }]));
  for (const row of [...manual, ...automatic]) {
    if (!companyName(row.peer_name)) continue;
    const key = peerKey(row.peer_name, row.peer_id);
    const peer = peers.get(key) ?? { name: row.peer_name, refs: [], aliases: [], rows: [], peerId: row.peer_id };
    peer.aliases = [...new Set([...peer.aliases, row.peer_name, ...(row.peer_names ?? [])])];
    if (!peer.rows.length || row.origin === "user_override") peer.name = row.peer_name;
    peer.peerId = row.peer_id ?? peer.peerId;
    peer.rows.push(row); peers.set(key, peer);
  }
  const removedNames = new Set(exclusions.filter(r => r.company_wide)
    .flatMap(r => (r.peer_names ?? [r.peer_name]).map(n => peerKey(n, r.peer_id))));
  for (const key of peers.keys()) if (removedNames.has(key)) peers.delete(key);
  const self = comp?.companies.find((c) => c.is_self);
  const ownCells = (comp?.dimensions ?? []).filter((dim) => self?.cells[dim]?.text);
  // Registry programmes summarise a card; unverified search leads go last.
  const registryByPeer = new Map<string, RegistryOverlap[]>();
  for (const { peer, product } of registryRows(detail)) registryByPeer.set(peerKey(peer.name), [...(registryByPeer.get(peerKey(peer.name)) ?? []), product]);
  const isLead = (name: string) => !!detail.competition_research?.candidates?.some(c => peerKey(c.name, c.id) === peerKey(name) && c.origin === "search");
  const orderedPeers = [...peers.values()].sort((a, b) =>
    Number(isLead(a.name) && !a.rows.length) - Number(isLead(b.name) && !b.rows.length)
    || Number(!registryByPeer.has(peerKey(a.name, a.peerId))) - Number(!registryByPeer.has(peerKey(b.name, b.peerId))));

  const peerView = (peer: (typeof orderedPeers)[number]) => {
    const selected = peer.rows.some(r => r.origin === "user_override");
    const peerId = peer.peerId;
    const profile = comp?.profiles?.find(p => peerKey(p.name) === peerKey(peer.name, peerId))
      ?? comp?.companies.find(p => !p.is_self && peerKey(p.name) === peerKey(peer.name, peerId));
    const cells = Object.entries(profile?.cells ?? {}).filter(([, c]) => c.text && c.refs.length).slice(0, 4);
    const peerFacts = peer.rows.flatMap(r => (r.basis_facts ?? []).filter(f =>
      (f.subject_id === r.peer_id || f.subject_id === r.peer_product_id) && f.basis === "actual" && f.refs.length))
      .filter((f, i, all) => all.findIndex(x => x.fact_id === f.fact_id) === i).slice(0, 3);
    // Display labels often append English names in brackets or after a
    // space. Match those literal Chinese labels in reports, without
    // merging stored company identities or inferring relationship facts.
    const mentionNames = [...peer.aliases, peer.name, peer.name.split(/[（(]/)[0].trim(), peer.name.match(/^[\u3400-\u9fff]{2,}/)?.[0]]
      .filter((n): n is string => !!n);
    const latest = reports.filter(m => mentionNames.some(n => m.title.toLocaleLowerCase().includes(n.toLocaleLowerCase()))).slice(0, 2);
    const refs = (peer.refs ?? []).filter((r, i, all) => r.quote && !all.some((other, j) => j !== i
      && other.id === r.id && (other.quote?.length ?? 0) > r.quote!.length && other.quote?.includes(r.quote!))).slice(0, 2);
    // Search leads with nothing verified (news mentions alone do not count) become one compact line;
    // planned competitors keep their card so they can be confirmed at a glance.
    const verified = cells.length || refs.length || peerFacts.length || registryByPeer.has(peerKey(peer.name, peerId))
      || peer.rows.some(r => r.basis_facts?.length || r.peer_product_name);
    const empty = !selected && !verified && isLead(peer.name);
    return { peer, selected, peerId, cells, peerFacts, latest, refs, empty };
  };
  const views = orderedPeers.map(peerView);
  const cardViews = views.filter(v => !v.empty);
  const leadViews = views.filter(v => v.empty);

  const position = deep.data?.record?.analysis.position;
  const positionLabel = position ? deep.data?.labels?.position?.[position.verdict] : undefined;
  const confirmed = views.filter((v) => v.selected).length;
  const recent = reports.filter((m) => m.date && Date.now() - Date.parse(m.date) < 30 * 86400_000).length;
  const summaryOf = (v: (typeof views)[number]) => v.cells[0]?.[1].text || v.refs[0]?.quote || v.peerFacts[0]?.value
    || v.peer.rows.find((r) => r.peer_product_name)?.peer_product_name || (registryByPeer.get(peerKey(v.peer.name, v.peerId)) ?? [])[0]?.name || "";

  return (
    <div className="tab-frame landscape competition-view">
      <VerdictBand eyebrow={<>竞争对手{positionLabel && <span className={`rj-verdict small-verdict ${["leading", "ahead"].includes(position!.verdict) ? "up" : ["behind", "lagging"].includes(position!.verdict) ? "down" : ""}`}>{positionLabel}</span>}</>}
        meta={<button className="link small" disabled={saving || !detail.model_ready || !detail.project.activated || detail.jobs.some((j) => j.kind === "competition" && (j.status === "queued" || j.status === "running"))}
          title="新一轮最多24次搜索、21次公开页面读取、6次公开临床登记查询；每家公司最多3页，可能使用服务额度" onClick={research}>更新竞对资料 ↻</button>}
        title={position?.summary || "同一需求下的对手、所处阶段与最近动作"}
        figures={<>
          <Figure label="跟踪的对手" value={peers.size} unit="家" note={leadViews.length ? `另有 ${leadViews.length} 家待确认线索` : "含已确认与资料整理"} />
          <Figure label="已确认" value={confirmed} unit="家" note="你确认跟踪的" />
          <Figure label="近 30 天对手报道" value={recent} unit="条" note={`共收录 ${reports.length} 条`} />
          <Figure label="产品对照" value={supported.length} unit="项" note="有依据的产品关系" />
        </>} />
      <LandscapeStatus detail={detail} landscape={landscape} what="竞争格局" />
      <PositionJudgment view={deep.data ?? null} compact />
      {race}

      <details className="external-analysis-details" open={expandAll}><summary>竞对资料与跟踪管理 <span className="muted small">{peers.size} 家 · 定位、依据与最近报道</span></summary>
      <Block card title="竞对资料" label="竞争对手" note="一行一家；点开看定位、依据与最近报道">
        {cardViews.length > 0 && <ol className="rival-list">{cardViews.map((v) => {
          const { peer, selected, peerId, cells, peerFacts, latest, refs } = v;
          const open = expandAll || openPeer === peer.name;
          return <li key={peerKey(peer.name, peerId)} className={`rival-row${open ? " open" : ""}`}>
            <button className="rival-line" aria-expanded={open} onClick={() => setOpenPeer(open ? null : peer.name)}>
              <span className="rival-name"><h4>{peer.name}</h4>{selected ? <span className="cf-tag">已确认</span>
                : !peer.rows.length && isLead(peer.name) ? <span className="cf-tag muted" title="名字出自搜索摘要原文，尚未核实是否为竞对">搜索线索</span> : null}</span>
              <span className="rival-sum">{summaryOf(v) || (latest.length ? null : <span className="muted">{isLead(peer.name) ? "名字出自搜索摘要，尚无核实资料" : "暂无资料摘要"}</span>)}</span>
              <span className="rival-last">{latest[0] ? <><time>{latest[0].date}</time>{latest[0].title}</> : <span className="muted">近期无报道</span>}</span>
              <span className="rival-caret" aria-hidden="true">›</span>
            </button>
            {open && <div className="rival-detail">
              {cells.map(([label, cell]) => <div key={label}><p className="muted small">{label}</p>
                <p className="rival-pos">{cell.text}</p><EvidenceLinks refs={cell.refs} /></div>)}
              {!cells.length && refs.map(r => <p key={`${r.id}:${r.quote}`} className="rival-pos">{r.quote}<EvidenceLinks refs={[r]} /></p>)}
              {!cells.length && peerFacts.map(f => <div key={f.fact_id}><p className="rival-pos">{f.value}</p>
                <StoredEvidence projectId={detail.project.id} refs={f.refs} /></div>)}
              {peer.rows.filter(r => r.peer_product_name && r.peer_product_id).map(r => <p className="rival-pos" key={r.scope_key}>{r.peer_product_name}</p>)}
              {latest.map(m => <p className="rival-latest" key={m.id}><time>{m.date}</time>
                {m.url ? <a href={m.url} target="_blank" rel="noreferrer noopener">{m.title}</a> : m.title}</p>)}
              {peer.rows.map(r => <BilateralBasis key={r.scope_key} projectId={detail.project.id} row={r} />)}
              {(registryByPeer.get(peerKey(peer.name, peerId)) ?? []).slice(0, 2).map(p => <p className="rival-pos" key={p.product_id}>{p.name}
                <span className="muted small"> · {REGISTRY_LEVEL[p.level]}{p.indication_overlap === "same" ? " · 同一疾病类别" : ""}
                  {p.trials?.[0]?.phase ? ` · ${p.trials[0].phase}` : ""}</span></p>)}
              {!!peer.refs?.length && <details><summary className="muted small">出处</summary><EvidenceLinks refs={peer.refs} /></details>}
              <div className="peer-actions">{!selected && <button className="btn btn-sm" disabled={saving || !detail.project.config_version}
                aria-label={`确认 ${peer.name}`} onClick={() => select(peer.name, "include", peerId)}>确认</button>}
                <button className="link muted" disabled={saving || !detail.project.config_version}
                  aria-label={`移除 ${peer.name}`} onClick={() => select(peer.name, "exclude", peerId)}>移除</button></div>
            </div>}
          </li>;
        })}</ol>}
        {leadViews.length > 0 && <div className="rival-leads">
          <p className="muted small">待确认线索 {leadViews.length} 家 · 名字出自搜索摘要，尚无核实资料。确认后下次更新会补充资料。</p>
          <ul>{leadViews.map(({ peer, peerId, latest }) => <li key={peer.name}><span className="rival-lead-name">{peer.name}</span>
            <span className="muted small truncate" title={latest[0]?.title}>{latest[0] ? `${latest[0].date} ${latest[0].title}` : ""}</span>
            <button className="btn btn-sm" disabled={saving || !detail.project.config_version} aria-label={`确认 ${peer.name}`}
              onClick={() => select(peer.name, "include", peerId)}>确认</button>
            <button className="link muted" disabled={saving || !detail.project.config_version} aria-label={`移除 ${peer.name}`}
              onClick={() => select(peer.name, "exclude", peerId)}>移除</button></li>)}</ul>
        </div>}
        {!peers.size && <p className="muted small">还没有竞对，输入公司名称即可添加。</p>}
        <form className="peer-add" onSubmit={e => { e.preventDefault(); if (companyName(peerName)) void select(peerName.trim(), "include"); }}>
          <input aria-label="添加竞对公司" placeholder="添加竞对：输入公司名称" maxLength={200} value={peerName} onChange={e => setPeerName(e.target.value)} />
          <button className="btn btn-sm" disabled={saving || !companyName(peerName) || !detail.project.config_version}>添加</button>
        </form>
      </Block>
      </details>

      <Reference items={[
        { open: expandAll, key: "compare", title: "竞对对照", note: supported.length ? `${supported.length} 项产品关系` : undefined,
          body: supported.length ? <ProductComparison detail={detail} rows={supported} action={action} /> : null },
        { open: expandAll, key: "registry", title: "临床登记对照", body: detail.competition_research?.registry && detail.competition_research.registry.status !== 'not_applicable'
          && (registryRows(detail).length > 0 || detail.competition_research.registry.status === 'failed') ? <RegistryComparison detail={detail} /> : null },
        { open: expandAll, key: "financing", title: "同行融资", body: financing ?? null },
        { open: expandAll, key: "removed", title: "已移除", note: exclusions.length ? `${exclusions.length} 项` : undefined,
          body: exclusions.length ? <ul className="competition-candidates">{exclusions.map((r) => <li key={r.scope_key}><strong>{r.peer_name}</strong>
            <span className="muted small"> · {r.subject_product_name} / {r.peer_product_name}</span>
            {r.reason && <p className="muted small">{r.reason}</p>}
            {r.company_wide ? <button className="link" disabled={saving} onClick={() => select(r.peer_name, "include", r.peer_id)}>恢复</button> : action(r, "reset")}
          </li>)}</ul> : null },
        { open: expandAll, key: "legacy", title: "历史整理摘录",
          body: self && ownCells.length > 0 ? <div className="legacy-excerpts">{ownCells.map(dim => <div key={dim}><p className="muted small">摘录主题：{dim}</p>
            <p>{self.cells[dim].text} <EvidenceLinks refs={self.cells[dim].refs} /></p></div>)}</div> : null },
      ]} />
      <ExternalNews key={detail.project.id} title="竞品新闻" items={news} legacy={moves} loading={feed.loading}
        error={feed.error} failed={feed.data?.failed} onChange={feed.update} onRetry={feed.reload} />
    </div>
  );
}

// Pending source leads must stay separate even when paired with a legacy API.
export function competitionCandidates(detail: ProjectDetail) {
  const peerKey = competitionIdentity(detail);
  const comp = detail.landscape?.competition;
  const rows = [...(comp?.candidates ?? []),
    ...(comp?.companies.filter((c) => !c.is_self) ?? []),
    ...(detail.plan?.competitors ?? [])];
  const candidates = new Map<string, { name: string; refs: NonNullable<typeof comp>["summary_refs"]; peerId?: string }>();
  const excluded = new Set((detail.relationships?.exclusions ?? []).filter((r) => r.company_wide)
    .flatMap((r) => (r.peer_names ?? [r.peer_name]).map(n => peerKey(n, r.peer_id))));
  const ownNames = new Set([detail.project.name, detail.project.legal_name].filter(Boolean).map(n => peerKey(n!)));
  for (const row of rows) {
    const name = row.name.trim();
    const key = peerKey(name);
    if (!companyName(name) || excluded.has(key) || ownNames.has(key)) continue;
    const old = candidates.get(key);
    const refs = "refs" in row ? row.refs ?? [] : [];
    const namedRefs = refs.filter(r => (r.title + (r.quote ?? "")).includes(name));
    candidates.set(key, { name: old?.name ?? name, refs: [...(old?.refs ?? []), ...namedRefs].filter((r, i, all) =>
      all.findIndex((x) => x.id === r.id && x.quote === r.quote) === i) });
  }
  return [...candidates.values()];
}

// ------------------------------------------------------------------ 行业环境

export function ActivityPanel({ ind }: { ind: NonNullable<Landscape["industry"]> }) {
  const index = ind.heat_index;
  const signals = (index?.signals ?? []).filter(x => Number.isFinite(x.recent) && x.recent >= 0 && Number.isFinite(x.baseline) && x.baseline >= 0);
  const max = Math.max(1, ...signals.flatMap(x => [x.recent, x.baseline]));
  return <section className="activity-panel" aria-label="资讯活跃度">
    <div className="card-head"><h3>资讯活跃度</h3><span className="muted small">已收录报道</span></div>
    {index && signals.length ? <>
      <div className="activity-legend"><span><i className="recent" />近 {index.window_days} 天</span><span><i className="baseline" />此前同期均值</span></div>
      <ul className="activity-rows">{signals.map(x => <li key={x.key}>
        <span>{x.name}</span>
        <svg viewBox="0 0 240 24" role="img" aria-label={`${x.name}：近 ${index.window_days} 天 ${x.recent}，此前同期均值 ${x.baseline}`}>
          <rect className="activity-recent" x="0" y="2" height="8" width={240 * x.recent / max} rx="2" />
          <rect className="activity-baseline" x="0" y="14" height="6" width={240 * x.baseline / max} rx="2" />
        </svg>
        <span className="activity-values"><b>{x.recent.toLocaleString("zh-CN")}</b><small>{x.baseline.toLocaleString("zh-CN")}</small></span>
      </li>)}</ul>
      <details className="activity-method"><summary>统计口径</summary>
        <p>此前 {index.baseline_days} 天折算为每 {index.window_days} 天均值。仅统计已收录信息，不代表行业景气度。</p>
      </details>
    </> : <p className="muted small">暂无可对照的报道计数。</p>}
  </section>;
}

function IndustryFacts({ facts }: { facts: NonNullable<Landscape["industry"]>["facts"] }) {
  const kinds = [...new Set(facts.map(f => f.kind))];
  if (!kinds.length) return null;
  return <div className="industry-facts">{kinds.map(kind => <div key={kind}>
    <h4>{kind}</h4><ul>{facts.filter(f => f.kind === kind).map((f, i) => <li key={i}>{f.text} <EvidenceLinks refs={f.refs} /></li>)}</ul>
  </div>)}</div>;
}

export function IndustryView({ detail, compact = false, part = 'all' }: { detail: ProjectDetail; compact?: boolean; part?: 'all'|'industry'|'policy' }) {
  const landscape = detail.landscape;
  const ind = landscape?.industry;
  const categories: Category[] = part==='all'?['industry','policy']:[part];
  const feed = useExternalNews(detail.project.id, monitoringRevision(detail), categories);
  const news = feed.data?.items ?? [];
  const policyFacts = (ind?.facts ?? []).filter(f => /政策|监管|法规|医保|集采/.test(f.kind));
  const capitalFacts = (ind?.facts ?? []).filter(f => /融资|资本|并购|投资/.test(f.kind) && !policyFacts.includes(f));
  const marketFacts = (ind?.facts ?? []).filter(f => !policyFacts.includes(f) && !capitalFacts.includes(f));
  const anchor = `industry-${detail.project.id}`;

  const analysis = <>
      <LandscapeStatus detail={detail} landscape={landscape} what="行业环境" />
      {part==='all' && ind && <nav className="industry-nav" aria-label="行业章节"><a href={`#${anchor}-market`}>市场趋势</a>{ind && <a href={`#${anchor}-capital`}>资本动态</a>}<a href={`#${anchor}-policy`}>政策环境</a></nav>}
      {part!=='policy' && (ind ? (
        <>
          <section className="card card-pad industry-market" id={`${anchor}-market`}>
            <div className="industry-section-head"><span className="industry-number">01</span><div><span className="eyebrow">{ind.industry || detail.project.industry || "所在赛道"}</span><h3>市场趋势</h3></div></div>
            <div className="industry-editorial">
              <div className="industry-abstract"><span className="muted small">{ind.overview ? "行业摘要 · AI 整理" : "资料摘录"}</span>
                {ind.overview ? <><p>{ind.overview}</p><details className="chart-evidence"><summary>摘要依据</summary><EvidenceLinks refs={ind.overview_refs} /></details></>
                  : marketFacts.length ? <IndustryFacts facts={marketFacts} /> : <p>暂无市场趋势资料。</p>}
                {!ind.overview && !!ind.overview_refs?.length && <details className="activity-method"><summary>摘要来源</summary><EvidenceLinks refs={ind.overview_refs} /></details>}
              </div><ActivityPanel ind={ind} />
            </div>
            {ind.metrics.length > 0 && <div className="industry-metrics"><h4>市场指标</h4>
              <div className="table-scroll"><table className="table compact-table">
                <thead><tr><th scope="col">指标</th><th scope="col">期间</th><th scope="col" className="num">数值</th><th scope="col">口径 / 出处</th></tr></thead>
                <tbody>{ind.metrics.map((m, i) => <tr key={i}><th scope="row">{m.name}</th><td>{m.period || "—"}</td>
                  <td className="num">{Number.isFinite(m.value) ? `${m.value.toLocaleString("zh-CN")} ${m.unit}` : "—"}</td>
                  <td><span className={`cf-tag ${m.basis === "forecast" ? "metric-forecast" : ""}`}>{m.basis === "forecast" ? "预测 / 估计" : "来源报告"}</span><EvidenceLinks refs={m.refs} /></td></tr>)}</tbody>
              </table></div>
            </div>}
            {ind.overview && marketFacts.length > 0 && <details className="chart-evidence"><summary>市场资料摘录 · {marketFacts.length} 条</summary><IndustryFacts facts={marketFacts} /></details>}
          </section>
          <section className="card card-pad industry-capital" id={`${anchor}-capital`}>
            <div className="industry-section-head"><span className="industry-number">02</span><div><span className="eyebrow">CAPITAL</span><h3>资本动态</h3></div></div>
            {ind.funding.length ? <div className="table-scroll"><table className="table compact-table">
              <thead><tr><th scope="col">公司</th><th scope="col">轮次</th><th scope="col">金额</th><th scope="col">日期</th><th scope="col">出处</th></tr></thead>
              <tbody>{ind.funding.map((f, i) => <tr key={i}><th scope="row">{f.company}</th><td>{f.round || "—"}</td><td>{f.amount || "—"}</td><td>{f.date || "—"}</td><td><EvidenceLinks refs={f.refs} /></td></tr>)}</tbody>
            </table></div> : <p className="muted small">暂无赛道融资记录。</p>}
            <IndustryFacts facts={capitalFacts} />
          </section>
        </>
      ) : <p className="note industry-pending"><strong>行业摘要尚未整理。</strong>{detail.execution?.analysis === "stopped" ? "自动更新未运行，已有报道保留在下方。" : "已有报道保留在下方。"}</p>)}
      {part!=='industry' && <section className="card card-pad industry-policy" id={`${anchor}-policy`}>
        <div className="industry-section-head">{ind && <span className="industry-number">03</span>}<div>{ind && <span className="eyebrow">POLICY</span>}<h3>政策环境</h3></div></div>
        <IndustryFacts facts={policyFacts} />
        {!policyFacts.length && <p className="muted small">尚无已整理的政策摘录，可先阅读下方新闻原文。</p>}
      </section>}
      {part!=='policy' && ind && (ind.implications.length > 0 || ind.heat_reason) && <details className="card card-pad industry-interpretation"><summary>AI 解读 <span className="muted small">本公司与行业的联系</span></summary>
        {ind.heat_reason && <p>{ind.heat_reason} <EvidenceLinks refs={ind.heat_refs} /></p>}
        <ul className="bullets">{ind.implications.map((x, i) => <li key={i}>{x.text} <EvidenceLinks refs={x.refs} /></li>)}</ul>
      </details>}
    </>;
  return <div className="landscape industry-view">
    {compact ? <details className="external-analysis-details"><summary>{part==='policy'?'政策资料与分析依据':'行业资料与分析依据'} <span className="muted small">{part==='policy'?'已保存的政策摘录':part==='industry'?'市场指标与资本动态':'市场指标、资本动态与政策摘录'}</span></summary>{analysis}</details> : analysis}
    <ExternalNews key={`${detail.project.id}-${part}`} title={part==='all'?'行业与政策新闻':part==='policy'?'政策新闻':'行业新闻'} categories={categories} items={news} loading={feed.loading}
      error={feed.error} failed={feed.data?.failed} onChange={feed.update} onRetry={feed.reload} />
  </div>;
}

/** System-picked watch points: the next step of open storylines and the brief's "接下来关注", minus ones already watched. */
export function watchSuggestions(threads: { title: string; next_step: string; status: string }[],
  watch: { text: string }[], existing: { text: string; label: string }[]) {
  const have = existing.flatMap((w) => [w.text, w.label]).map((t) => t.toLowerCase());
  const known = (t: string) => have.some((h) => h && (h.includes(t.toLowerCase()) || t.toLowerCase().includes(h)));
  const out: string[] = [];
  for (const t of threads) if (t.status !== "done" && t.next_step) out.push(`${t.title}：${t.next_step}`);
  for (const w of watch) out.push(w.text);
  return [...new Set(out.map((t) => t.trim()).filter((t) => t.length >= 2 && !known(t)))].slice(0, 4);
}

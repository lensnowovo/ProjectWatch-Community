import { useEffect, useState } from "react";
import { api, type ProjectDetail, type MonitoringPlanView, type PlanChange, type PlanItem, type PlanModule, type PlanSection, type PlanTemplate } from "./api";
import { PlanModuleView } from "./planModules";
import { Figure, Reference, TabSkeleton, VerdictBand } from "./frame";
import { useToast } from "./motion";
import { MonitorLoop } from "./monitorLoop";
import { ErrorNote, relTime, useAsync } from "./ui";
import { workRevision, projectWork } from "./workState";
import { publicLink } from "./relationScope";
import { MonitorScope } from "./monitorScope";
import { CollectionSources } from "./CollectionSources";
import { TopicResearch } from "./TopicResearch";
import { collectionExecutionLabel } from "./externalCollection";

/** 监控方案: a complete plan grown from a fixed framework — four rings around the company, nine dimensions by
 * whom we watch, each with the default baseline, what is known for this project, and its focus points. */
const ACTION_LABEL: Record<PlanChange["action"], string> = { add: "新增", modify: "调整", reduce: "降频", retire: "关闭", restore: "恢复", meta: "概要", regroup: "归类" };
const ACTOR_LABEL: Record<PlanChange["actor"], string> = { system: "系统", human: "你", agent: "外部助手" };
const FIELD_LABEL: Record<string, string> = {
  text: "问题", cadence: "频率", tier: "层级", status: "状态", objects: "对象", queries: "检索词", reason: "纳入原因",
  current_state: "当前认识", trigger_rule: "触发", exit_rule: "退出", axis: "分类", sources: "来源",
  understanding: "项目理解", method: "采用方法", in_scope: "纳入范围", out_of_scope: "不进入", sections: "分类基础",
  scope_kind: "范围角色", relation_type: "关联类型", relation_target: "关注对象", relation_aliases: "对象别名",
};

const HOP_LABEL: Record<string, string> = { "0": "公司自身", "1": "直接相关", "2": "二阶相关" };
type Latest = NonNullable<MonitoringPlanView["latest"]>[string];

/** The project's plan read top to bottom: what we watch, what is new, where it is thin, what needs a decision. */
export function PlanView({ projectId, company = "", detail }: { projectId: string; company?: string; detail: ProjectDetail }) {
  const { data, error, loading, reload } = useAsync<MonitoringPlanView>(() => api.monitoringPlan(projectId), [projectId, workRevision(detail), detail.collection_report?.observed_at]);
  const [showSummary, setShowSummary] = useState(false);
  if (loading && !data) return <TabSkeleton label="读取监控方案" />;
  if (!data) return <ErrorNote error={error} />;
  const live = data.items.filter((i) => i.status !== "retired");
  const relations = live.filter((i) => i.scope_kind === "relation");
  const remaining = live.filter((i) => i.scope_kind !== 'relation' && i.scope_kind !== 'question');
  const closed = data.items.filter((i) => i.status === "retired");
  const open = data.changes.filter((c) => !c.reverted_at);
  const meta = data.meta ?? {};
  const proposalsFor = new Set((data.proposals ?? []).map((c) => c.watch_id));
  const needs = meta.review === "pending" || (data.proposals?.length ?? 0) > 0;
  const lead = (meta.understanding || "").split(/(?<=[。；])/)[0];
  const firstVersion = Math.min(...live.map((i) => Date.parse(i.created_at)).filter(Number.isFinite)) + 3600_000;
  const added = live.filter((i) => Date.parse(i.created_at) > firstVersion && Date.now() - Date.parse(i.created_at) < 7 * 86400_000).length;
  const allChannels = Object.values(data.coverage ?? {}).flat();
  const channels = { on: allChannels.filter((c) => c.state === "on" || c.state === "error").length,
    off: allChannels.filter((c) => c.state === "off").length };
  const work = projectWork(detail);
  const itemDetail = (item: PlanItem) => <ItemDetail item={item} latest={data.latest?.[item.id]} module={data.modules[item.id]}
    moduleLabel={data.module_labels[item.view] ?? ""} labels={data.labels} dims={data.framework} onChanged={reload} />;
  return (
    <div className="plan-view outline tab-frame monitor-scope">
      <VerdictBand
        eyebrow={<>监控方案{data.state && data.state.phase !== "empty" && <span className={`plan-phase phase-${data.state.phase}`}
          title={data.state.phase === "stable" ? "连续几轮没有新增或建议；有重大消息或新资料时会自动重新修订" : "每轮更新或新资料后自动修订"}>{data.state.label}</span>}</>}
        meta={(meta.understanding !== lead || meta.method || meta.in_scope?.length || meta.out_of_scope?.length)
          ? <button className="link small" onClick={() => setShowSummary((v) => !v)}>{showSummary ? "收起方案概要" : "方案概要"}</button> : undefined}
        title={lead || `${company || "这家公司"}的监控方案`}
        figures={<>
          <Figure label="常规采集" value={channels.on} unit="个来源" note={channels.off ? `${channels.off} 个未开启` : "每轮自动"} />
          <Figure label={relations.length ? "长期关注" : "专项问题"} value={relations.length || live.length} unit="个"
            note={relations.length ? `${live.filter((i) => i.scope_kind === "question").length} 个研究分支` : `核心 ${live.filter((i) => i.tier === "core").length} 个`} />
          <Figure label="本周新增" value={added} unit="个"  />
          <Figure label="近 7 日有收录" value={data.week?.fresh ?? 0} unit="个"  />
        </>}>
        {showSummary && <dl className="po-summary">
          {meta.understanding && <div><dt>项目理解</dt><dd>{meta.understanding}</dd></div>}
          {meta.method && <div><dt>采用方法</dt><dd>{meta.method}</dd></div>}
          {!!meta.in_scope?.length && <div><dt>纳入范围</dt><dd>{meta.in_scope.join("；")}</dd></div>}
          {!!meta.out_of_scope?.length && <div><dt>不进入</dt><dd>{meta.out_of_scope.join("；")}</dd></div>}
        </dl>}
      </VerdictBand>

      <nav className="sd-jumps" aria-label="监控范围页内导航"><a href="#scope-runtime">运行概览</a><a href="#scope-relations">长期关联 · {relations.length}</a><a href="#scope-research">临时研究 · {data.items.filter(i=>i.scope_kind==='question').length}</a><a href="#scope-review">方案审阅 · {(data.proposals?.length??0)+Number(meta.review==='pending')}</a></nav>
      <section id="scope-runtime" className="sd-runtime">
        <header className="sd-section-head"><div><span className="eyebrow">运行概览</span><h2>监控循环</h2></div><span className="sd-current" role="status">{work.moving&&<i className="sd-activity-ring" aria-hidden="true"/>}{work.label}</span></header>
        <MonitorLoop detail={detail} plan={data} inline />
      </section>

      {!!error && <ErrorNote error={error} />}
      <MonitorScope key={projectId} plan={data} company={company || detail.project.name}
        renderState={(item) => <StateSwitch item={item} onChanged={reload} />} renderDetail={itemDetail} />

      <section id="scope-review" className="sd-section" aria-label="方案审阅">
        <header className="sd-section-head"><div><span className="eyebrow">人工决定</span><h2>需要你决定的范围变化</h2></div><span className="muted">{(data.proposals?.length??0)+Number(meta.review==='pending')} 项</span></header>
        {meta.review === "pending" && <details className="sd-review"><summary><strong>首版方案 · 一次整体审阅</strong><span>待你审阅</span></summary><ReviewBanner projectId={projectId} count={live.length} onChanged={reload} /></details>}
        {(data.proposals?.length ?? 0) > 0 && <details className="sd-review"><summary><strong>待你确认的建议</strong><span>{data.proposals.length} 项</span></summary><Proposals proposals={data.proposals} labels={data.labels} onChanged={reload} /></details>}
        {!needs && <p className="muted small">当前没有待审阅的范围变化。</p>}
      </section>

      <details className="relation-legacy" open={!relations.length}>
      <summary hidden={!relations.length}>其他关注点 · {remaining.length} 个（保留原配置）</summary>
      <ScopeTable projectId={projectId} items={remaining} framework={data.framework} rings={data.rings ?? []} latest={data.latest ?? {}}
        modules={data.modules} moduleLabels={data.module_labels} labels={data.labels} proposalsFor={proposalsFor}
        firstVersion={firstVersion} templates={data.templates} chosen={meta.templates ?? []} onChanged={reload} />
      </details>

      {data.health && <details className="plan-health small"><summary>运行详情</summary><p>
        运行情况：{data.health.items} 个关注点中，{data.health.ran_14d} 个近两周检索过，{data.health.hit_30d} 个近 30 天有命中
        {data.health.never_hit > 0 && `，${data.health.never_hit} 个至今没有命中`}
        {data.health.system_added > 0 && `；系统新增 ${data.health.system_added} 个（你撤销 ${data.health.system_reverted} 个）`}
        {(data.health.proposals.accepted + data.health.proposals.rejected) > 0
          && `；建议已接受 ${data.health.proposals.accepted}、已拒绝 ${data.health.proposals.rejected}`}。
      </p></details>}

      <Reference items={[
        { key: "source-choices", title: "来源选择", body: <CollectionSources key={projectId} projectId={projectId} /> },
        { key: "routine", title: "常规采集", note: `${channels.on} 个来源每轮自动${channels.off ? ` · ${channels.off} 个未开启` : ""}`,
          body: <RoutineChannels framework={data.framework} coverage={data.coverage ?? {}} /> },
        { key: "changes", title: "方案变更", note: open.length ? `${open.length} 项 · 可撤销` : undefined,
          body: open.length ? <ChangeLog changes={open.slice(0, 30)} labels={data.labels} onChanged={reload} /> : null },
        { key: "closed", title: "已关闭的关注点", note: closed.length ? `${closed.length} 个 · 判断历史保留` : undefined,
          body: closed.length ? <ul className="plan-closed-list">{closed.map((item) => <li key={item.id}><span>{item.text}</span>
            <RowAction item={item} fields={{ status: "active" }} label="恢复" note="恢复监控" onChanged={reload} /></li>)}</ul> : null },
        { key: "templates", title: "行业模板", body: <TemplatePicker projectId={projectId} templates={data.templates} chosen={meta.templates ?? []} onChanged={reload} /> },
      ]} />
    </div>
  );
}

const PAGE = 12;
const stripBecause = (text: string) => text.replace(/^因为[：:，,\s]*/, "");

/** 监控范围 as one table: why each point is watched, which object and relation, the question, recent news and its state.
 * A row opens its full detail and actions beneath it. */
function ScopeTable({ projectId, items, framework, rings, latest, modules, moduleLabels, labels, proposalsFor, reveal, firstVersion, templates, chosen, onChanged }: {
  projectId: string; items: PlanItem[]; framework: PlanSection[]; rings: { key: string; label: string }[]; latest: Record<string, Latest>;
  modules: Record<string, PlanModule>; moduleLabels: Record<string, string>; labels: MonitoringPlanView["labels"];
  proposalsFor: Set<string>; reveal?: {id: string; n: number} | null; firstVersion: number; templates: PlanTemplate[]; chosen: string[]; onChanged: () => void;
}) {
  const [group, setGroup] = useState("all");
  const [openId, setOpenId] = useState<string | null>(null);
  const [adding, setAdding] = useState<null | { text: string; view: string; cadence: string }>(null);
  const [more, setMore] = useState(false);
  useEffect(()=>{
    const item = reveal && items.find(i=>i.id===reveal.id);
    if (!item) return;
    setGroup(item.section); setOpenId(item.id); setMore(true);
    const frame=requestAnimationFrame(()=>document.getElementById(`scope-row-${item.id}`)?.scrollIntoView({block:'nearest'}));
    return ()=>cancelAnimationFrame(frame);
  },[reveal]);
  const order = new Map(framework.map((f, i) => [f.key, i]));
  const ringLabel = new Map(rings.map((r) => [r.key, r.label]));
  const rank = (i: PlanItem) => ({ core: 0, background: 1, topic: 2 }[i.tier as "core"] ?? 3);
  const sorted = [...items].sort((a, b) => (order.get(a.section) ?? 99) - (order.get(b.section) ?? 99)
    || rank(a) - rank(b) || a.created_at.localeCompare(b.created_at));
  const groups = framework.filter((f) => items.some((i) => i.section === f.key));
  const visible = group === "all" ? sorted : sorted.filter((i) => i.section === group);
  const shown = more ? visible : visible.slice(0, PAGE);
  const byId = new Map(items.map((i) => [i.id, i]));
  const addTo = group === "all" ? groups[0]?.key ?? framework[0]?.key ?? "" : group;
  const suggestions = templates.filter((t) => chosen.includes(t.key)).flatMap((t) => t.focus).filter((f) => f.section === addTo);
  return (
    <section className="scope" aria-label="跟踪对象">
      <div className="scope-head">
        <h3>跟踪对象</h3>
        <span className="muted small">{items.length} 个关注点 · 点一行看依据与操作</span>
        <button className="btn btn-sm" onClick={() => setAdding(adding ? null : { text: "", view: "evidence", cadence: "medium" })}>{adding ? "取消添加" : "＋ 添加关注点"}</button>
      </div>
      <div className="scope-groups" role="tablist" aria-label="按方向筛选">
        <button role="tab" aria-selected={group === "all"} className={group === "all" ? "on" : ""} onClick={() => { setGroup("all"); setOpenId(null); }}>全部<span>{items.length}</span></button>
        {groups.map((g) => <button key={g.key} role="tab" aria-selected={group === g.key} className={group === g.key ? "on" : ""} onClick={() => { setGroup(g.key); setOpenId(null); }}>
          {g.label}<span>{items.filter((i) => i.section === g.key).length}</span></button>)}
      </div>
      {adding && <div className="scope-add">
        <AddItem projectId={projectId} axis={addTo} preset={adding} onAdded={() => { setAdding(null); onChanged(); }} onCancel={() => setAdding(null)} />
        <span className="muted small">加到「{framework.find((f) => f.key === addTo)?.label ?? "未分类"}」{suggestions.length > 0 && <>；模板建议：
          {suggestions.slice(0, 3).map((f) => <button key={f.text} className="link small" onClick={() => setAdding(f)}>{f.text}</button>)}</>}</span>
      </div>}
      {visible.length === 0 ? <p className="muted small">这一类还没有专项关注点，只靠常规采集覆盖。</p> :
      <div className="table-wrap scope-wrap"><table className="scope-table">
        <colgroup><col className="col-why" /><col className="col-obj" /><col className="col-q" /><col className="col-news" /><col className="col-state" /></colgroup>
        <thead><tr><th>方向 / 纳入理由</th><th>关系对象</th><th>关注事项</th><th>最新</th><th>状态</th></tr></thead>
        <tbody>{shown.flatMap((item) => {
          const l = latest[item.id];
          const open = openId === item.id;
          const parent = item.parent_id ? byId.get(item.parent_id) : undefined;
          const added = Date.parse(item.created_at);
          const isNew = added > firstVersion && Date.now() - added < 7 * 86400_000;
          const section = framework.find((f) => f.key === item.section);
          const rows = [<tr key={item.id} id={`scope-row-${item.id}`} className={`scope-row${open ? " open" : ""}${item.status === "reduced" ? " reduced" : ""}${l?.fresh ? " fresh" : ""}`}
            aria-expanded={open} onClick={() => setOpenId(open ? null : item.id)}>
            <td><div className="sc-group"><strong>{section?.label ?? "未分类"}</strong>
              <span className="muted small">{ringLabel.get(section?.ring ?? "") ?? ""}</span>
              {isNew && <span className="po-tag new">新增</span>}
              {item.plan_origin === "system" && <span className="po-tag">系统加的</span>}
              {item.status === "trial" && <span className="po-tag">试运行</span>}
              {item.tier === "topic" && <span className="po-tag">临时专题</span>}
              {proposalsFor.has(item.id) && <span className="po-tag warn">有建议</span>}</div>
              <p className="sc-reason">{item.reason ? stripBecause(item.reason) : <span className="muted">未写纳入理由</span>}</p></td>
            <td>{item.objects.length ? <strong className="sc-target">{item.objects.slice(0, 3).join("、")}{item.objects.length > 3 && ` 等 ${item.objects.length} 个`}</strong>
              : <span className="muted small">公司自身</span>}
              <span className="sc-relation">↳ {parent ? `由「${parent.label || parent.text.slice(0, 10)}」延伸 · ${HOP_LABEL[item.hop ?? "1"] ?? ""}` : HOP_LABEL[item.hop ?? "0"] ?? "直接相关"}</span></td>
            <td><p className="sc-q">{item.text}</p><span className="muted small">{labels.origin[item.plan_origin] ?? ""} · {labels.tier[item.tier] ?? ""}</span></td>
            <td>{l && l.kind !== "none" ? <><strong className="sc-count">{l.count || 1}</strong><span className="muted small"> 条</span>{l.date && <span className="sc-date">{l.date}</span>}
              {l.fresh && <span className="sc-fresh">本周新</span>}</> : <span className="muted small">{l?.date ? "暂无新信息" : "未检索"}</span>}</td>
            <td onClick={(e) => e.stopPropagation()}><StateSwitch item={item} onChanged={onChanged} /></td>
          </tr>];
          if (open) rows.push(<tr key={`${item.id}-detail`} className="scope-detail-row"><td colSpan={5}>
            <ItemDetail item={item} latest={l} module={modules[item.id]} moduleLabel={moduleLabels[item.view] ?? ""} labels={labels} dims={framework} onChanged={onChanged} />
          </td></tr>);
          return rows;
        })}</tbody>
      </table></div>}
      {visible.length > PAGE && <button className="link small" onClick={() => setMore((v) => !v)}>{more ? "收起" : `显示其余 ${visible.length - PAGE} 个`}</button>}
    </section>
  );
}

/** 跟踪中 ↔ 降频: the one state worth switching from the table; closing stays in the row detail. */
function StateSwitch({ item, onChanged }: { item: PlanItem; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const on = item.status !== "reduced";
  if (item.status === "trial") return <RowAction item={item} fields={{ status: "active" }} label="确认保留" note="确认系统建议" primary onChanged={onChanged} />;
  return <button role="switch" aria-checked={on} disabled={busy} className={`sc-switch${on ? " on" : ""}`} onClick={async () => {
    setBusy(true);
    try { await api.updatePlanItem(item.id, { status: on ? "reduced" : "active", note: on ? "降频观察" : "恢复频率" }); toast(on ? "已降频" : "已恢复频率"); onChanged(); }
    catch (e) { toast(e instanceof Error ? e.message : "未保存，请重试", "error"); }
    finally { setBusy(false); }
  }}><span aria-hidden="true" />{on ? "跟踪中" : "已降频"}</button>;
}

/** Standing channels per dimension, folded under 参考与依据. */
export function RoutineChannels({ framework, coverage }: { framework: PlanSection[]; coverage: NonNullable<MonitoringPlanView["coverage"]> }) {
  const rows = framework.filter((f) => (coverage[f.key] ?? []).length);
  if (!rows.length) return <p className="muted small">暂无专门来源，靠专项关注点覆盖。</p>;
  return <dl className="routine-list">{rows.map((f) => <div key={f.key}><dt>{f.label}</dt><dd>{coverage[f.key].map((c) =>
    <span key={c.key} className={`po-channel ${c.state}`} title={c.execution
      ? `${c.job_status === 'running' || c.job_status === 'queued' ? '本轮' : '最近一轮'} · ${collectionExecutionLabel(c.execution, c.active)}${c.count == null ? '' : ` · ${c.window} ${c.count} ${c.unit}`}`
      : c.state === "off" ? "需要先在设置中配置对应服务" : c.state === "error" ? "上一轮检索失败" : `${c.window} ${c.count} ${c.unit}`}>
      {c.label}{c.execution ? `（${collectionExecutionLabel(c.execution, c.active)}）` : c.state === "off" ? "（未开启）" : c.state === "pending" ? "（待首轮）" : c.state === "error" ? "（上轮失败）" : ` ${c.count}`}</span>)}</dd></div>)}</dl>;
}

/** A focus point's full detail: why, latest, its module, how it is searched, and every action. */
function ItemDetail({ item, latest, module, moduleLabel, labels, dims, onChanged }: {
  item: PlanItem; latest?: Latest; module?: PlanModule; moduleLabel: string; labels: MonitoringPlanView["labels"]; dims: PlanSection[]; onChanged: () => void;
}) {
  return <div className="po-detail">
    {item.reason && <p className="po-why"><b>因为</b>{stripBecause(item.reason)}</p>}
    {item.scope_kind !== "relation" && <><p className={`po-latest ${latest?.kind ?? "none"}`}><b>{latest?.kind === "note" ? "最新认识" : "最新"}</b>{latest?.text ?? "尚未检索"}
      {latest?.date && <time>{latest.kind === "none" ? `检索于 ${latest.date}` : latest.date}</time>}</p>
    <div className="plan-module"><span className="plan-module-label">{moduleLabel || "最新状态"}</span>
      <PlanModuleView watchId={item.id} module={module} onChanged={onChanged} /></div></>}
    <dl>
      {item.scope_kind === "relation" && item.sources && <div><dt>纳入依据</dt><dd>{item.sources.split(/\s+/).filter(Boolean).map((source, n) =>
        publicLink(source) ? <a key={`${source}-${n}`} href={source} target="_blank" rel="noreferrer" style={{ marginRight: 14 }}>来源 {n + 1} ↗</a> : <span key={n}>{source}</span>)}</dd></div>}
      {item.objects.length > 0 && <div><dt>盯哪些对象</dt><dd>{item.objects.join("、")}</dd></div>}
      {item.trigger_rule && <div><dt>出现什么就提醒</dt><dd>{item.trigger_rule}</dd></div>}
      {item.exit_rule && <div><dt>何时关闭或降频</dt><dd>{item.exit_rule}</dd></div>}
      <div><dt>怎么查</dt><dd>{item.queries.join("；") || "—"} · {labels.cadence[item.status === "reduced" ? "low" : item.cadence]}
        {item.last_run_at ? ` · 上次 ${relTime(item.last_run_at)}` : " · 尚未运行"}</dd></div>
      <div><dt>来源</dt><dd>{labels.origin[item.plan_origin] ?? "—"} · {labels.tier[item.tier]}</dd></div>
    </dl>
    {item.visibility === 'public' && item.tier === 'topic' && item.scope_kind !== 'relation'
      && !!item.queries.length && !!item.exit_rule && <TopicResearch key={`${item.id}:${item.lifecycle_version}`} item={item} />}
    <div className="plan-actions">
      {item.scope_kind !== "question" && item.tier !== "core" && <RowAction item={item} fields={{ tier: "core" }} label="升为核心" note="升为核心" onChanged={onChanged} />}
      {item.tier === "core" && <RowAction item={item} fields={{ tier: "background" }} label="降为背景" note="降为背景" onChanged={onChanged} />}
      {item.status === "retired" ? <RowAction item={item} fields={{ status: "active" }} label="恢复" note="恢复条目" onChanged={onChanged} /> :
        <RowAction item={item} fields={{ status: "retired" }} label="关闭" note="关闭条目" onChanged={onChanged} />}
      <MoveTo item={item} dims={dims} onChanged={onChanged} />
    </div>
  </div>;
}

/** 移到: put a focus point under the dimension of the object it is really about. */
function MoveTo({ item, dims, onChanged }: { item: PlanItem; dims: PlanSection[]; onChanged: () => void }) {
  const toast = useToast();
  return <label className="po-move small muted">移到
    <select value={item.section} onChange={async (e) => {
      try { await api.updatePlanItem(item.id, { axis: e.target.value, note: "移到" }); toast("已移动"); onChanged(); }
      catch (err) { toast(err instanceof Error ? err.message : "未保存，请重试", "error"); }
    }}>{dims.map((d) => <option key={d.key} value={d.key}>{d.label}</option>)}</select></label>;
}

function RowAction({ item, fields, label, note, primary, onChanged }: {
  item: PlanItem; fields: Partial<PlanItem>; label: string; note: string; primary?: boolean; onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  return <button className={`btn btn-sm${primary ? " btn-primary" : " btn-ghost"}`} disabled={busy} onClick={async () => {
    setBusy(true);
    try { await api.updatePlanItem(item.id, { ...fields, note }); toast(`已${label}`); onChanged(); }
    catch (e) { toast(e instanceof Error ? e.message : "未保存，请重试", "error"); }
    finally { setBusy(false); }
  }}>{label}</button>;
}

function describe(change: PlanChange, labels: MonitoringPlanView["labels"]): string {
  if (change.action === "add") return "";
  const keys = Object.keys(change.after);
  return keys.map((k) => {
    const map = (labels as Record<string, Record<string, string>>)[k];
    const show = (v: unknown) => {
      const text = Array.isArray(v) ? v.join("、") : map?.[String(v)] ?? String(v ?? "");
      return text.trim() ? (text.length > 40 ? `${text.slice(0, 40)}…` : text) : "—";
    };
    return `${FIELD_LABEL[k] ?? k}：${show(change.before[k as keyof PlanItem])} → ${show(change.after[k as keyof PlanItem])}`;
  }).join("；");
}

function ChangeLog({ changes, labels, onChanged }: { changes: PlanChange[]; labels: MonitoringPlanView["labels"]; onChanged: () => void }) {
  const toast = useToast();
  const revert = async (c: PlanChange) => {
    try { await api.revertPlanChange(c.id); toast("已撤销"); onChanged(); }
    catch (e) { toast(e instanceof Error ? e.message : "撤销失败", "error"); }
  };
  return (
    <div className="plan-changes">
      <ol>{changes.map((c) => <li key={c.id}>
        <span className={`plan-action action-${c.action}`}>{ACTION_LABEL[c.action]}</span>
        <div>
          <strong>{c.item || (c.after.text as string) || "条目"}</strong>
          <span className="muted small"> · {ACTOR_LABEL[c.actor]} · {relTime(c.created_at)}{c.reason ? ` · ${c.reason}` : ""}</span>
          {describe(c, labels) && <p className="muted small">{describe(c, labels)}</p>}
          {!!c.evidence?.length && <p className="plan-evidence small">依据：{c.evidence.map((e) => e.title || e.text).filter(Boolean).join("；")}</p>}
        </div>
        {c.action !== "regroup" && <button className="link small" onClick={() => revert(c)}>撤销</button>}
      </li>)}</ol>
    </div>
  );
}

function AddItem({ projectId, axis, preset, onAdded, onCancel }: {
  projectId: string; axis: string; preset: { text: string; view: string; cadence: string }; onAdded: () => void; onCancel: () => void;
}) {
  const [text, setText] = useState(preset.text);
  const [reason, setReason] = useState("");
  const [tier, setTier] = useState("core");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const toast = useToast();
  const add = async () => {
    setBusy(true); setError(null);
    try {
      if (/[{｛]/.test(text)) throw new Error("请先把｛｝里的内容换成本项目的具体对象");
      await api.addPlanItem(projectId, { text: text.trim(), reason: reason.trim(), tier, axis, view: preset.view,
        cadence: tier === "core" ? preset.cadence : "low" });
      setText(""); setReason(""); toast("已加入监控方案，下次更新时开始检索"); onAdded();
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  return (
    <form className="plan-add" onSubmit={(e) => { e.preventDefault(); if (text.trim().length >= 2) void add(); }}>
      <input value={text} onChange={(e) => setText(e.target.value)} placeholder="添加一个要持续回答的问题" aria-label="新问题" maxLength={200} />
      <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="为什么值得盯（可选）" aria-label="纳入原因" maxLength={300} />
      <select value={tier} onChange={(e) => setTier(e.target.value)} aria-label="层级">
        <option value="core">核心</option><option value="background">背景</option><option value="topic">临时专题</option>
      </select>
      <button className="btn btn-sm" disabled={busy || text.trim().length < 2}>添加</button>
      <button type="button" className="link small" onClick={onCancel}>取消</button>
      <ErrorNote error={error} />
    </form>
  );
}

/** Industry templates this plan grows from; choosing them fills section baselines (project text is kept). */
function TemplatePicker({ projectId, templates, chosen, onChanged }: {
  projectId: string; templates: PlanTemplate[]; chosen: string[]; onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [picked, setPicked] = useState<string[]>(chosen);
  const toast = useToast();
  const label = chosen.map((k) => templates.find((t) => t.key === k)?.label ?? k).join(" + ");
  const apply = async () => {
    try { await api.applyPlanTemplates(projectId, picked); toast("已套用行业模板"); setOpen(false); onChanged(); }
    catch (e) { toast(e instanceof Error ? e.message : "未保存", "error"); }
  };
  return <div className="plan-templates">
    <span className="small">行业模板：{label || <span className="muted">未选择</span>}</span>
    <button className="link small" onClick={() => { setPicked(chosen); setOpen((v) => !v); }}>{open ? "收起" : "调整"}</button>
    {open && <div className="plan-template-pick">
      {templates.map((t) => <label key={t.key} className="check check-inline"><input type="checkbox" checked={picked.includes(t.key)}
        onChange={(e) => setPicked(e.target.checked ? [...picked, t.key] : picked.filter((k) => k !== t.key))} />{t.label}</label>)}
      <button className="btn btn-sm" disabled={!picked.length} onClick={apply}>套用</button>
      <p className="muted small">模板提供每一类的默认监控内容和关注点建议；本项目已写的内容不会被覆盖。</p>
    </div>}
  </div>;
}

/** System suggestions that touch items a person added or reviewed: nothing changes until accepted. */
function Proposals({ proposals, labels, onChanged }: { proposals: PlanChange[]; labels: MonitoringPlanView["labels"]; onChanged: () => void }) {
  const toast = useToast();
  const decide = async (c: PlanChange, accept: boolean) => {
    try { await (accept ? api.acceptPlanChange(c.id) : api.rejectPlanChange(c.id)); toast(accept ? "已接受并生效" : "已拒绝，方案不变"); onChanged(); }
    catch (e) { toast(e instanceof Error ? e.message : "未保存", "error"); }
  };
  return <section className="plan-proposals">
    <h3>待你确认的建议 <span className="muted small">{proposals.length} 项 · 系统根据新信息提出，接受前方案不变</span></h3>
    <ol>{proposals.map((c) => <li key={c.id}>
      <div>
        <strong>{ACTION_LABEL[c.action]}「{c.item || "条目"}」</strong>
        {describe(c, labels) && <span className="muted small"> · {describe(c, labels)}</span>}
        <p>{c.reason}</p>
        {!!c.evidence?.length && <p className="plan-evidence small">依据：{c.evidence.map((e) => e.title || e.text).filter(Boolean).join("；")}</p>}
      </div>
      <div className="plan-actions">
        <button className="btn btn-sm btn-primary" onClick={() => decide(c, true)}>接受</button>
        <button className="btn btn-sm btn-ghost" onClick={() => decide(c, false)}>拒绝</button>
      </div>
    </li>)}</ol>
  </section>;
}

/** The system wrote the first plan from the name and first round; the owner confirms it once. */
function ReviewBanner({ projectId, count, onChanged }: { projectId: string; count: number; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  return <section className="plan-review">
    <div>
      <strong>首版方案由系统根据公司名称和首轮资料生成，待你审阅</strong>
      <p className="muted small">已按此方案运行。你可以逐条调整、关闭或添加；确认后，这 {count} 个关注点视为你审阅过的条目，系统只能对它们提出建议，不再自行关闭或降频。</p>
    </div>
    <button className="btn btn-sm btn-primary" disabled={busy} onClick={async () => {
      setBusy(true);
      try { await api.confirmPlanReview(projectId); toast("已确认首版方案"); onChanged(); }
      catch (e) { toast(e instanceof Error ? e.message : "未保存", "error"); }
      finally { setBusy(false); }
    }}>确认方案</button>
  </section>;
}

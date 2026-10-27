import { useState } from 'react';
import { Link } from 'react-router-dom';
import { api, type MonitoringPlanView, type PlanItem, type ProjectDetail } from './api';
import { ErrorNote, useAsync } from './ui';
import { Skeleton } from './frame';
import { publicLink } from './relationScope';
import { workRevision } from './workState';
import { ChainJudgment } from './judgment';
import { businessIntro } from './companyDossier';
import './scopeReading.css';
import './externalNews.css';

const BASIS: Record<string,string> = {summary:'标题 / 摘要匹配',full_body:'正文匹配',partial_body:'部分正文匹配',search_summary:'搜索摘要匹配'};
const FALLBACK = {chain:['customer','partners'],science:['science']};
export function scopeItems(plan:MonitoringPlanView, projectId:string, scope:'chain'|'science') {
  const sections = scope==='chain' ? plan.framework.filter(s=>s.ring==='chain').map(s=>s.key) : ['science'];
  const keys = new Set(sections.length ? sections : FALLBACK[scope]);
  return plan.items.filter(i=>i.project_id===projectId && i.status!=='retired' && keys.has(i.section));
}
export function scopeRelations(plan:MonitoringPlanView, projectId:string, scope:'chain'|'science') {
  return scopeItems(plan,projectId,scope).filter(i=>i.scope_kind==='relation');
}

/** Only explicit saved roles can enter the supply/customer lanes. Descendants are not direct ties. */
export function chainLane(item:PlanItem):'supply'|'demand'|'other' {
  if(item.parent_id || (item.hop && item.hop!=='1')) return 'other';
  return item.relation_type==='supplier'?'supply':item.relation_type==='customer'?'demand':'other';
}
type Report = NonNullable<MonitoringPlanView['relation_updates']>[string]['items'][number];
type MatchedReport = Report & {targets:string[]};
export function scopeReports(plan:MonitoringPlanView, relations:PlanItem[]) {
  const reports = new Map<string,MatchedReport>();
  for(const relation of relations) for(const report of plan.relation_updates?.[relation.id]?.items??[]) {
    if(!report.event_id) continue;
    const target=relation.relation_target||relation.label;
    const prior=reports.get(report.event_id);
    if(prior) { if(!prior.targets.includes(target)) prior.targets.push(target); }
    else reports.set(report.event_id,{...report,targets:[target]});
  }
  const stamp=(date:string)=>{const n=Date.parse(date);return Number.isFinite(n)?n:-Infinity;};
  return [...reports.values()].sort((a,b)=>stamp(b.date)-stamp(a.date));
}

/** Group already ordered matches without substituting collection dates for missing dates. */
export function scopeReportGroups(reports:MatchedReport[]) {
  const groups=new Map<string,MatchedReport[]>();
  for(const report of reports) {
    const date=Date.parse(report.date??'');
    const label=Number.isFinite(date)?new Date(date).toLocaleDateString('zh-CN',{year:'numeric',month:'long'}):'日期未知';
    groups.set(label,[...(groups.get(label)??[]),report]);
  }
  return [...groups.entries()];
}

function ScopeReportCard({report:r}:{report:MatchedReport}) {
  const url=publicLink(r.url);
  return <article className="item scope-report">
    <div className="item-body">
      <div className="item-meta"><span className="muted">{r.source||'来源未标注'}</span><span className="cat">{BASIS[r.basis]||'关联匹配'}</span><time className="muted item-date" dateTime={Number.isFinite(Date.parse(r.date))?r.date:undefined}>{r.date?.slice(0,10)||'日期未知'}</time>
      </div>
      {url?<a className="item-title" href={url} target="_blank" rel="noreferrer noopener">{r.title}</a>:<span className="item-title">{r.title}</span>}
      <div className="item-reading-detail">
        <p className="item-summary scope-report-target">关联对象：{r.targets.join('、')}</p>
        <p className="item-impact scope-report-evidence"><span>匹配依据</span>{r.excerpt||'暂无匹配原文。'}</p>
      </div>
    </div>
  </article>;
}

export function ScopeReports({plan,relations,title}: {plan:MonitoringPlanView;relations:PlanItem[];title:string}) {
  const [limit,setLimit]=useState(12);
  const reports=scopeReports(plan,relations);
  return <section className="scope-reports external-news" aria-label={title}>
    <header className="external-news-head"><div><h3>{title}</h3></div><span className="muted small">{reports.length} 条匹配报道</span></header>
    {!reports.length && <p className="muted small">暂无匹配报道。</p>}
    {reports.length>0 && <div className="items items-flat">{reports.slice(0,limit).map(r=><ScopeReportCard key={r.event_id} report={r}/>)}</div>}
    {reports.length>limit && <button className="btn btn-ghost btn-sm news-older" onClick={()=>setLimit(n=>n+20)}>查看更多已读取报道（共 {reports.length} 条）</button>}
  </section>;
}

export function ChainReading({plan,detail}: {plan:MonitoringPlanView;detail:ProjectDetail}) {
  const relations=scopeRelations(plan,detail.project.id,'chain');
  const items=scopeItems(plan,detail.project.id,'chain');
  const reports=scopeReports(plan,relations);
  const groups=[{key:'supply',title:'供应端',note:'已标注的供应商',items:relations.filter(i=>chainLane(i)==='supply')},{key:'demand',title:'客户端',note:'已标注的客户',items:relations.filter(i=>chainLane(i)==='demand')}];
  const other=relations.filter(i=>chainLane(i)==='other');
  const intro=businessIntro(detail);
  return <div className="scope-reading">
    <header className="scope-reading-head"><div><span className="eyebrow">纵向关联</span><h2>产业链动态</h2><p>沿已有供应、合作与客户关系，看变化如何影响公司。</p></div><Link className="link small" to={`/projects/${detail.project.id}?tab=plan`}>管理监控范围 →</Link></header>
    <dl className="scope-metrics" aria-label="已保存的产业链记录概览">
      <div><dt>已纳入关联</dt><dd>{relations.length}<span>个对象</span></dd></div>
      <div><dt>其中试关注</dt><dd>{relations.filter(i=>i.status==='trial').length}<span>个对象</span></dd></div>
      <div><dt>最近匹配报道</dt><dd>{reports.length}<span>条 · 去重后</span></dd></div>
      <div><dt>关注问题与旧记录</dt><dd>{items.length-relations.length}<span>条 · 不计入关联</span></dd></div>
    </dl>
    {detail.capabilities?.insight_projections===true && <SavedChainJudgment key={detail.project.id} detail={detail}/>}
    <section className="chain-atlas" aria-label="已纳入的产业链关联">
      <header className="chain-atlas-head"><h3>关系地图</h3><span>已关注的关联对象</span></header>
      <div className="chain-atlas-grid">
        <ChainGroup key={`${detail.project.id}-supply`} title={groups[0].title} note={groups[0].note} items={groups[0].items}/>
        <section className="chain-company-lane">
          <header className="chain-lane-heading"><div className="chain-lane-head"><h3>关注的公司</h3></div><p className="chain-lane-note">已保存的公司信息</p></header>
          <div className={`chain-atlas-core${groups[0].items.length?' has-supply':''}${groups[1].items.length?' has-demand':''}`}>
          <h3>{detail.project.short_name||detail.project.name}</h3>
          <span className="chain-core-label">已保存的公司简介</span>
          <p>{intro||'暂无公司简介。'}</p>
          <Link className="link small" to={`/projects/${detail.project.id}`}>查看公司概览 →</Link>
          </div>
        </section>
        <ChainGroup key={`${detail.project.id}-demand`} title={groups[1].title} note={groups[1].note} items={groups[1].items}/>
      </div>
      {!!other.length && <div className="chain-atlas-other"><ChainGroup key={`${detail.project.id}-other`} title="其他关联 / 角色待确认" note="合作、资本及延伸关联" items={other}/></div>}
      {!!relations.length && <details className="chain-counts chart-evidence" aria-label="关联报道分布"><summary>匹配报道分布 · 共 {reports.length} 条去重报道</summary>
        <p>只统计本次读取的匹配报道，不代表供应集中度、交易额或重要性；同一报道可涉及两侧。</p>
        <ul>{[...groups,{key:'other',title:'其他关联',items:other}].map(g=><li key={g.key}><span>{g.title}</span><b>{scopeReports(plan,g.items).length} 条</b></li>)}</ul>
      </details>}
    </section>
    <ScopeTopics items={scopeItems(plan,detail.project.id,'chain')}/>
    <ScopeReports key={detail.project.id} plan={plan} relations={relations} title="产业链关联报道"/>
  </div>;
}
function SavedChainJudgment({detail}:{detail:ProjectDetail}) {
  const {data,error,loading,reload}=useAsync(()=>api.insight(detail.project.id),[detail.project.id,workRevision(detail)]);
  if(error)return <div><ErrorNote error={error}/><button className="link small" onClick={reload}>重新读取产业链判断</button></div>;
  if(loading&&!data)return <div role="status" aria-label="读取已保存产业链判断"><Skeleton lines={2}/></div>;
  return data?.record?.analysis.chain?<ChainJudgment view={data}/>:<p className="scope-quiet">暂无产业链判断。</p>;
}
export function ScopeTopics({items}: {items:PlanItem[]}) {
  const topics=items.filter(i=>i.scope_kind!=='relation');
  if(!topics.length)return null;
  return <details className="scope-topics chart-evidence" open={items.every(i=>i.scope_kind!=='relation')}><summary>已有关注问题与旧版记录 · {topics.length} 条</summary>
    <p className="muted small">这些是已保存的关注点，尚不能作为具体公司之间的关联；不自动补造上下游对象。</p>
    {topics.map(i=><div key={i.id}><p>{i.text||i.label}</p>{i.reason && <p className="muted small">{i.reason}</p>}</div>)}
  </details>;
}
function ChainGroup({title,note,items}: {title:string;note?:string;items:PlanItem[]}) {
  const [all,setAll]=useState(false);
  const shown=all?items:items.slice(0,3);
  return <section className="chain-lane"><header className="chain-lane-heading"><div className="chain-lane-head"><h3>{title}</h3><span>{items.length} 个</span></div>
    {note && <p className="chain-lane-note">{note}</p>}
    </header>
    <div className={`chain-lane-list${items.length?' has-records':''}`}>
    {!items.length && <p className="muted small chain-lane-empty">尚未纳入关联</p>}
    {shown.map(i=><article className="chain-node" key={i.id}>
      <div className="chain-node-meta"><span>{i.relation_type==='supplier'?'供应商':i.relation_type==='customer'?'客户':'角色待确认'}{i.parent_id || (i.hop && i.hop!=='1')?' · 延伸层级待核对':''}</span><span className="scope-state">{i.status==='trial'?'试关注':'已纳入'}</span></div>
      <h4>{i.relation_target||i.label}</h4>
      <p className="chain-node-state">{i.current_state||'尚未记录当前状态'}</p>
      <details className="chart-evidence"><summary>关注原因与依据</summary><p>{i.reason||'尚未记录纳入原因'}</p><p className="muted small">纳入依据：{i.sources||'尚未记录'}</p>{i.updated_at && <p className="muted small">记录更新 {i.updated_at.slice(0,10)}</p>}</details>
    </article>)}</div>
    {items.length>3 && <button className="link small chain-lane-more" aria-expanded={all} onClick={()=>setAll(!all)}>{all?'收起':`显示 ${shown.length} / 共 ${items.length} 个 · 展开全部`}</button>}
  </section>;
}

/** Reads saved monitoring records only; a project-keyed result never leaks old rows during navigation. */
export function ScopeReading({detail,scope='chain'}: {detail:ProjectDetail;scope?:'chain'|'science'}) {
  const id=detail.project.id;
  const result=useAsync(async()=>({id,plan:await api.monitoringPlan(id)}),[id,workRevision(detail)]);
  const plan=result.data?.id===id?result.data.plan:null;
  return <><ErrorNote error={result.error}/>{result.error && <button className="link small" onClick={result.reload}>重新读取关联</button>}
    {!plan && result.loading && <Skeleton lines={4}/>}
    {plan && (scope==='chain'?<ChainReading plan={plan} detail={detail}/>:<><ScopeTopics items={scopeItems(plan,id,'science')}/><ScopeReports key={id} plan={plan} relations={scopeRelations(plan,id,'science')} title="技术监控关联报道"/></>)}</>;
}

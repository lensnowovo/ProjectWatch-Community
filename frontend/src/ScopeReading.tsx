import { useState } from 'react';
import { Link } from 'react-router-dom';
import { api, type ChainStage, type MonitoringPlanView, type PlanItem, type ProjectDetail } from './api';
import { ErrorNote, useAsync } from './ui';
import { Skeleton } from './frame';
import { publicLink } from './relationScope';
import { workRevision } from './workState';
import { ChainJudgment } from './judgment';
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
  // A known role decides the side; an unverified link level stays visible on the card as 延伸层级待核对.
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
  const projections=detail.capabilities?.insight_projections===true;
  const insight=useAsync(()=>projections?api.insight(detail.project.id):Promise.resolve(null),[detail.project.id,workRevision(detail),projections]);
  const outline=insight.data?.record?.analysis.chain?.outline;
  return <div className="scope-reading">
    <header className="scope-reading-head"><h2>产业链动态</h2><Link className="link small" to={`/projects/${detail.project.id}?tab=plan`}>管理监控范围 →</Link></header>
    <dl className="scope-metrics" aria-label="已保存的产业链记录概览">
      <div><dt>已纳入关联</dt><dd>{relations.length}<span>个对象</span></dd></div>
      <div><dt>其中试关注</dt><dd>{relations.filter(i=>i.status==='trial').length}<span>个对象</span></dd></div>
      <div><dt>最近匹配报道</dt><dd>{reports.length}<span>条 · 去重后</span></dd></div>
      <div><dt>监控方向</dt><dd>{items.filter(i=>!i.scope_kind).length}<span>项</span></dd></div>
      <div><dt>临时研究问题</dt><dd>{items.filter(i=>i.scope_kind==='question').length}<span>个未关闭</span></dd></div>
    </dl>
    {projections && <SavedChainJudgment key={detail.project.id} insight={insight}/>}
    <section className="chain-brief" aria-label="产业链解读">
      <header className="chain-brief-heading"><h3>产业链解读</h3></header>
      <ChainNarrative title="供应与生产" items={groups[0].items} stages={outline?.upstream} side="上游"/>
      <ChainNarrative title="客户与应用" items={groups[1].items} stages={outline?.downstream} side="下游"/>
      {!!other.length && <ChainNarrative title="合作与其他关联" items={other}/>}
    </section>
    <ScopeTopics items={scopeItems(plan,detail.project.id,'chain')}/>
    <ScopeReports key={detail.project.id} plan={plan} relations={relations} title="产业链关联报道"/>
  </div>;
}
function SavedChainJudgment({insight}:{insight:{data?:Awaited<ReturnType<typeof api.insight>>|null;error?:unknown;loading:boolean;reload:()=>void}}) {
  const {data,error,loading,reload}=insight;
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
function ChainNarrative({title,items,stages,side}: {title:string;items:PlanItem[];stages?:ChainStage[];side?:string}) {
  const stageNames=[...new Set((stages??[]).map(s=>s.stage.trim()).filter(Boolean))];
  const states=items.filter(i=>i.current_state?.trim()).slice(0,2);
  const sentence=(text:string)=>/[。！？.!?]$/.test(text.trim())?text.trim():text.trim()+'。';
  return <section className="chain-brief-section">
    <header className="chain-section-title">{side && <span>{side}</span>}<h4>{title}</h4></header>
    <div className="chain-brief-copy">
      {stageNames.length>0 && <p>{side}环节包括{stageNames.join('、')}。</p>}
      {states.length>0 && <p>{states.map(i=>(i.relation_target||i.label)+'：'+sentence(i.current_state!)).join('')}</p>}
      {!stageNames.length && !states.length && <p className="muted">{items.length?'已关注'+items.map(i=>i.relation_target||i.label).join('、')+'，暂无进展摘要。':'暂无相关材料。'}</p>}
      {(!!stages?.length || !!items.length) && <details className="chain-brief-evidence chart-evidence">
        <summary>对象与依据</summary>
        {!!stages?.length && <div className="chain-brief-basis"><p className="muted small">产业环节按行业常识整理；下列名称来自已收录材料，不等同于已确认的交易关系。</p>
          <ul>{stages.map((s,index)=><li key={s.stage+'-'+index}><strong>{s.stage}</strong>{s.players.length>0 && <span>：{s.players.join('、')}</span>}</li>)}</ul>
        </div>}
        {items.map(i=><div className="chain-brief-basis" key={i.id}>
          <h5>{i.relation_target||i.label} <span className="muted small">{i.status==='trial'?'试关注':'已关注'}{i.parent_id || (i.hop && i.hop!=='1')?' · 延伸关联':''}</span></h5>
          {i.current_state && <p>{i.current_state}</p>}
          {i.reason && <p>{i.reason}</p>}
          <p className="muted small">纳入依据：{i.sources||'暂无来源记录'}{i.updated_at?' · 更新 '+i.updated_at.slice(0,10):''}</p>
        </div>)}
      </details>}
    </div>
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

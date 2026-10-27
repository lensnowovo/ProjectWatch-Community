import {useState, type ReactNode} from 'react';
import type {MonitoringPlanView, PlanItem} from './api';
import {branchState, publicLink} from './relationScope';
import {REACH_DIRECTIONS} from './monitorReachModel';
import './monitorScope.css';

/** Preserve provenance, including closed ancestors. Broken/cyclic chains never fabricate a company link. */
export function scopePath(item:PlanItem, items:PlanItem[]) {
  const path:PlanItem[]=[]; const seen=new Set<string>(); let current:PlanItem|undefined=item;
  while(current) {
    if(seen.has(current.id)||items.filter(i=>i.id===current!.id).length!==1)return {path,complete:false};
    seen.add(current.id);path.unshift(current);
    if(!current.parent_id)return {path,complete:true};
    current=items.find(i=>i.id===current!.parent_id);
  }
  return {path,complete:false};
}
export function scopeDirection(item:PlanItem, plan:Pick<MonitoringPlanView,'items'|'framework'>) {
  const trail=scopePath(item,plan.items);
  if(!trail.complete)return 'other';
  const ring=plan.framework.find(f=>f.key===trail.path[0].section)?.ring;
  return REACH_DIRECTIONS.some(r=>r.id===ring)?ring!:'other';
}
export function closedResearch(item:PlanItem, exploration?:{state:string}) {
  return item.status==='retired'||exploration?.state==='stopped';
}
const title=(item:PlanItem)=>item.relation_target||item.label||item.text;

export function MonitorScope({plan,company,renderDetail,renderState}:{plan:MonitoringPlanView;company:string;
 renderDetail:(item:PlanItem)=>ReactNode;renderState:(item:PlanItem)=>ReactNode}) {
 const [filter,setFilter]=useState('all'),[research,setResearch]=useState('open');
 const [limit,setLimit]=useState(12),[questionLimit,setQuestionLimit]=useState(12);
 const relations=plan.items.filter(i=>i.scope_kind==='relation'&&i.status!=='retired');
 const questions=plan.items.filter(i=>i.scope_kind==='question');
 const directions=[...REACH_DIRECTIONS,{id:'other',label:'其他 / 来源待核对'}].filter(d=>d.id!=='other'||relations.some(i=>scopeDirection(i,plan)==='other'));
 const visible=relations.filter(i=>filter==='all'||scopeDirection(i,plan)===filter);
 const isClosed=(i:PlanItem)=>closedResearch(i,plan.explorations?.[i.id]);
 const openCount=questions.filter(i=>!isClosed(i)).length;
 const shown=questions.filter(i=>research==='all'||(research==='closed'?isClosed(i):!isClosed(i)));
 const branches=(id:string)=>questions.filter(q=>scopePath(q,plan.items).path.some(p=>p.id===id));
 return <>
  <section id="scope-relations" className="sd-section">
   <header className="sd-section-head"><div><span className="eyebrow">长期主干</span><h2>关联监控</h2></div><span className="muted">{relations.length} 个对象</span></header>
   <div className="sd-directions" aria-label="关联方向筛选">{directions.map(d=><button key={d.id} data-direction={d.id} aria-pressed={filter===d.id} onClick={()=>{setFilter(filter===d.id?'all':d.id);setLimit(12);}}><span>{d.label}</span><strong>{relations.filter(i=>scopeDirection(i,plan)===d.id).length} <small>个对象</small></strong></button>)}</div>
   <div className="sd-list-caption"><span>{filter==='all'?'全部方向':directions.find(d=>d.id===filter)?.label} · {visible.length} 个对象</span><button className="link" disabled={filter==='all'} onClick={()=>{setFilter('all');setLimit(12);}}>查看全部</button></div>
   <div className="sd-relations">{visible.slice(0,limit).map(item=>{
    const trail=scopePath(item,plan.items),news=plan.relation_updates?.[item.id];
    return <details key={item.id} className="sd-object" data-direction={scopeDirection(item,plan)}>
     <summary><div><span className="sd-kicker">{directions.find(d=>d.id===scopeDirection(item,plan))?.label}</span><h3>{title(item)}</h3><p>{item.current_state||news?.items[0]?.title||'尚无已保存的变化记录'}</p></div><div className="sd-object-end"><span>{item.status==='trial'?'试关注':item.status==='reduced'?'已降频':'持续关注'}</span>{!!branches(item.id).length&&<small>{branches(item.id).length} 个临时研究</small>}<span className="sd-disclose">关联路径与依据 <i aria-hidden="true">＋</i></span></div></summary>
     <div className="sd-object-detail"><p className="sd-path">{trail.complete?company:'来源链待核对'} → {trail.path.map(title).join(' → ')}</p>
      {!!item.relation_aliases?.length&&<p>对象别名：{item.relation_aliases.join('、')}</p>}
      <div className="scope-manage">{renderState(item)}</div>
      {!!news?.items.length&&<details className="scope-evidence"><summary>最近匹配报道 · {news.count ?? news.items.length} 条</summary>{news.items.map(n=><article key={n.event_id}><a href={publicLink(n.url)} target="_blank" rel="noreferrer noopener">{n.title}</a><p className="muted small">{n.source} · {n.date} · {({summary:'标题 / 摘要匹配',full_body:'正文匹配',partial_body:'部分正文匹配',search_summary:'搜索摘要匹配'})[n.basis]}</p><p>{n.excerpt}</p></article>)}</details>}
      {renderDetail(item)}
     </div>
    </details>;
   })}{!visible.length&&<p className="scope-empty">此方向尚未纳入关联对象，原有关注点和常规采集仍保留。</p>}</div>
   {visible.length>limit&&<button className="link small" onClick={()=>setLimit(n=>n+12)}>查看更多对象（另 {visible.length-limit} 个）</button>}
  </section>
  <section id="scope-research" className="sd-section"><header className="sd-section-head"><div><span className="eyebrow">临时分支</span><h2>临时研究问题</h2></div><span className="muted">{questions.length} 个问题</span></header>
   <div className="sd-tabs" role="group" aria-label="研究问题筛选">{[['open',`进行与待证据 · ${openCount}`],['closed',`已停止或关闭 · ${questions.length-openCount}`],['all',`全部 · ${questions.length}`]].map(([key,label])=><button key={key} aria-pressed={research===key} onClick={()=>{setResearch(key);setQuestionLimit(12);}}>{label}</button>)}</div>
   <div className="sd-questions">{shown.slice(0,questionLimit).map(item=>{
    const parent=plan.items.find(p=>p.id===item.parent_id),state=plan.explorations?.[item.id];
    return <details key={item.id}><summary><div><span className="sd-kicker">{parent?`来自 ${title(parent)}`:'来源对象尚未关联'}</span><h3>{item.text||item.label}</h3><p>{plan.latest?.[item.id]?.text||item.current_state||'尚无已保存的研究结论'}</p></div><span className="sd-question-state">{branchState(item,state)}<i aria-hidden="true">＋</i></span></summary><div className="sd-question-detail"><p><strong>结束或继续的条件</strong></p><p>{item.exit_rule||'尚未记录退出条件，不据此认定已完成。'}</p>{state?.label&&<p className="muted">{state.label}</p>}{renderDetail(item)}</div></details>;
   })}{!shown.length&&<p className="scope-empty">当前分类没有临时研究问题。</p>}</div>
   {shown.length>questionLimit&&<button className="link small" onClick={()=>setQuestionLimit(n=>n+12)}>查看更多问题（另 {shown.length-questionLimit} 个）</button>}
  </section>
 </>;
}

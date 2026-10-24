import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import type { ProjectDetail } from './api';
import { DOSSIER_GROUPS, dossierRail, dossierSnapshot, emptyObserver, observeDossier, type Arrival } from './dossierMotionState';
import './dossierMotion.css';

type Receipt = Arrival & {serial:number};
export type DossierMotionState = {project:string; fresh:boolean; receipts:Receipt[]; recent:Receipt[]};
const blank=():DossierMotionState=>({project:'',fresh:false,receipts:[],recent:[]});

/** Lives above the tab panel. No requests, writes, progress timers or browser storage. */
export function useDossierMotion(detail:ProjectDetail|null,shown:boolean):DossierMotionState {
  const observer=useRef(emptyObserver());
  const serial=useRef(0);
  const latest=useRef(detail); latest.current=detail;
  const resumeAfter=useRef<ProjectDetail|null>(null);
  const [state,setState]=useState<DossierMotionState>(blank);
  useEffect(()=>{
    const visibility=()=>{
      observer.current=emptyObserver();resumeAfter.current=latest.current;
      setState(blank());
    };
    document.addEventListener('visibilitychange',visibility);
    return ()=>document.removeEventListener('visibilitychange',visibility);
  },[]);
  useEffect(()=>{
    if(document.hidden || !detail || detail.status_stale){observer.current=emptyObserver();setState(s=>s.project?blank():s);return;}
    // Returning from a hidden tab waits for a fresh response, then quietly baselines.
    if(resumeAfter.current===detail) return;
    resumeAfter.current=null;
    const next=observeDossier(observer.current,dossierSnapshot(detail));
    const changedProject=observer.current.snapshot?.project!==detail.project.id;
    observer.current=next;
    if(!shown){setState(s=>s.receipts.length||s.recent.length?blank():s);return;}
    if(changedProject){setState({project:detail.project.id,fresh:true,receipts:[],recent:[]});return;}
    if(!next.arrivals.length){setState(s=>s.project===detail.project.id&&s.fresh?s:{project:detail.project.id,fresh:true,receipts:[],recent:[]});return;}
    const incoming=next.arrivals.map(a=>({...a,serial:++serial.current}));
    setState(s=>{
      const merge=(prior:Receipt[])=>[...incoming,...prior.filter(p=>!incoming.some(i=>i.group===p.group))].slice(0,7);
      return {project:detail.project.id,fresh:true,receipts:merge(s.receipts),recent:merge(s.recent)};
    });
  },[detail,shown]);
  const newest=state.receipts[0]?.serial;
  useEffect(()=>{
    if(newest===undefined) return;
    // Only the visual receipt expires. Stored content and task state never do.
    const timer=setTimeout(()=>setState(s=>({...s,receipts:[]})),4800);
    return ()=>clearTimeout(timer);
  },[newest]);
  return state.project===detail?.project.id?state:blank();
}

export function DossierRail({detail,state}:{detail:ProjectDetail;state:DossierMotionState}){
  const observed=dossierRail(detail);
  const fresh=state.project===detail.project.id&&state.fresh;
  const work=fresh||detail.status_stale?observed:{...observed,flowing:false,waiting:false,state:'unavailable',label:'等待最新任务状态',stage:''};
  const receipts=state.project===detail.project.id && !detail.status_stale ? state.receipts:[];
  const recent=state.project===detail.project.id ? state.recent:[];
  const active=work.flowing||work.waiting||work.state==='stopping';
  const visible=active||work.pending||recent.length>0;
  if(!visible) return null;
  const pauseInFlight=detail.project.status==='paused'&&active;
  return <aside className={`dossier-rail dr-${work.state}`} data-flowing={work.flowing} aria-label="档案更新状态">
    <header><span className="dr-dot" aria-hidden="true"/>档案更新</header>
    <div className="dr-state" role="status" aria-live="polite" aria-atomic="true">{work.label}</div>
    {work.stage && <p className="dr-stage">{work.stage}</p>}
    <div className="dr-track" aria-hidden="true"><i className="dr-beam"/><i className="dr-glow"/></div>
    {pauseInFlight && <p className="dr-note">自动监控已暂停，在途任务尚未结束。</p>}
    {work.alongside && <p className="dr-warning">另有{work.alongside.label}</p>}
    {receipts.length>0 && <div key={receipts[0].serial} className="dr-receipt" role="status" aria-live="polite" aria-atomic="true">
      <strong>{receipts.length===1?`${DOSSIER_GROUPS[receipts[0].group].label}已${receipts[0].kind==='added'?'补充':'更新'}`:`${receipts.length} 类资料已更新`}</strong>
      <span>已保存到项目</span>
    </div>}
    {recent.length>0 && <div className="dr-saved"><small>本页查看期间已保存</small><ul>{recent.map(r=><li key={r.group}><Link to={`?tab=${DOSSIER_GROUPS[r.group].tab}${DOSSIER_GROUPS[r.group].tab==='overview'?`#dossier-${r.group}`:''}`} onClick={e=>{if(!e.ctrlKey&&!e.metaKey&&!e.shiftKey&&DOSSIER_GROUPS[r.group].tab==='overview')document.getElementById(`dossier-${r.group}`)?.scrollIntoView({block:'start'});}}>{DOSSIER_GROUPS[r.group].label}<span>{r.kind==='added'?'已补充':'已更新'} ↗</span></Link></li>)}</ul></div>}
    {!recent.length && <p className="dr-note">有资料保存后，会在这里显示。</p>}
    <a className="dr-task-link" href={`/tasks?project=${encodeURIComponent(detail.project.id)}`}>查看任务记录 →</a>
  </aside>;
}

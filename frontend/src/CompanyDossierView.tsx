import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import type { ProjectDetail } from './api';
import { businessIntro, dossierEntries, milestonesFor, safeWebsite, type DossierEntry, type DossierGroup } from './companyDossier';
import { locatorLabel, useOpenSource } from './ui';
import './companyDossier.css';

/** Centered, keyboard-native source reader. Original text and qualifiers remain intact. */
export function DossierSource({ entry, onClose }: {entry:DossierEntry|null; onClose:()=>void}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const openSource = useOpenSource();
  useEffect(()=>{ if(entry && !dialog.current?.open) dialog.current?.showModal(); },[entry]);
  if (!entry) return null;
  return <dialog ref={dialog} className="cd-source" aria-labelledby={titleId} onClose={onClose} onClick={e=>{if(e.target===dialog.current)dialog.current?.close();}}>
    <header><span>{entry.fact ? '公开来源' : '公司资料'}</span><button className="icon-btn" aria-label="关闭档案来源" autoFocus onClick={()=>dialog.current?.close()}>×</button></header>
    <h2 id={titleId}>{entry.label}</h2><p className="cd-source-text">{entry.text}</p>
    {entry.date && <p className="muted small">{entry.dateLabel} {entry.date}</p>}
    {(entry.fact?.refs ?? []).map((r,i)=><div className="cd-source-item" key={i}>
      {safeWebsite(r.url) ? <a href={safeWebsite(r.url)!} target="_blank" rel="noreferrer noopener">{r.source || r.title || '公开来源'} ↗</a> : <span>{r.source || r.title || '来源地址不可用'}</span>}
      {r.published_at && <small>来源日期 {r.published_at.slice(0,10)}</small>}{r.quote && <blockquote>{r.quote}</blockquote>}
    </div>)}
    {entry.refs.map((r,i)=><div className="cd-source-item" key={i}>{r.url ? safeWebsite(r.url) ? <a href={safeWebsite(r.url)!} target="_blank" rel="noreferrer noopener">{r.source_name} ↗</a> : <span>{r.source_name} · 地址不可用</span> : <button className="link" onClick={()=>{onClose();openSource(r);}}>{r.source_name} · {locatorLabel(r.locator)}</button>}</div>)}
    {!entry.refs.length && !entry.fact?.refs?.length && <p className="muted small">这条记录尚未附来源。</p>}
  </dialog>;
}

function EntryList({entries, onOpen, limit=4, sectionTitle}: {entries:DossierEntry[];onOpen:(entry:DossierEntry)=>void;limit?:number;sectionTitle?:string}) {
  const [all,setAll] = useState(false);
  return <><ul className="cd-reading-list">{(all?entries:entries.slice(0,limit)).map((e,i,shown)=><li key={e.id}>
    {e.label!==sectionTitle && (i===0 || e.label!==shown[i-1].label) && <h3>{e.label}</h3>}<p>{e.text}</p>
    <div className="cd-citation"><span>{e.fact ? '公开资料 · 未经公司确认' : '公司资料'}{e.date ? ` · ${e.dateLabel} ${e.date}` : ''}</span><button className="link" onClick={()=>onOpen(e)} aria-label={`查看依据：${e.label}`}>查看依据 ↗</button></div>
  </li>)}</ul>
    {entries.length>limit && <button className="link small cd-more" onClick={()=>setAll(!all)} aria-expanded={all}>{all?'收起':`展开另外 ${entries.length-limit} 条`}</button>}</>;
}

const CHAPTERS = [
  ['intro','公司简介'], ['business','业务与产品'], ['customers','客户与应用'], ['team','核心团队'],
  ['capital','融资与股东'], ['history','发展历程'], ['registry','工商信息'],
] as const;

function Chapter({id, title, note, children}: {id:typeof CHAPTERS[number][0];title:string;note?:string;children:ReactNode}) {
  return <section className="cd-chapter" id={`dossier-${id}`} aria-labelledby={`cd-${id}-title`}>
    <div className="cd-heading"><h2 id={`cd-${id}-title`}><span>{String(CHAPTERS.findIndex(c=>c[0]===id)+1).padStart(2,'0')}</span>{title}</h2>{note && <small>{note}</small>}</div>
    <div className="cd-chapter-body">{children}</div>
  </section>;
}

export function DossierOverview({detail, sources, registry, children, arrivals=[]}: {detail:ProjectDetail;sources:ReactNode;registry:ReactNode;children?:ReactNode;arrivals?:string[]}) {
  const entries=dossierEntries(detail);
  const group=(g:DossierGroup)=>entries.filter(e=>e.group===g);
  const [source,setSource]=useState<DossierEntry|null>(null);
  const milestones=milestonesFor(detail);
  const dated=milestones.filter(e=>e.date);
  const undated=milestones.filter(e=>!e.date);
  const [moreDates,setMoreDates]=useState(false);
  const visibleDates=moreDates?dated:dated.slice(-4);
  const summary=detail.profile?.summary?.trim();
  const intro=businessIntro(detail);
  const highlights=(detail.profile?.highlights ?? []).filter(h=>h.text).map((h,i):DossierEntry=>({id:`highlight-${i}`,label:'资料要点',group:'other',text:h.text,refs:h.refs ?? [],date:'',dateLabel:''}));
  useEffect(()=>{setSource(null);setMoreDates(false);},[detail.project.id]);
  return <article className="cd-overview cd-document" data-arrivals={arrivals.join(' ')} id="dossier-profile" aria-label="公司概览">
    <div className="cd-page-line"><span>公司概览</span><div>{sources}</div></div>
    <nav className="cd-index" aria-label="公司概览章节">{CHAPTERS.map(([id,title])=><a key={id} href={`#dossier-${id}`}>{title}</a>)}</nav>
    <Chapter id="intro" title="公司简介" note="公司档案">
      {summary || intro ? <p className="cd-introduction">{summary || intro}</p> : <p className="cd-empty">尚未取得公司介绍。补充公司资料后将在这里展示。</p>}
      <dl className="cd-identity">{[['公司名称',detail.project.legal_name || detail.project.name],['行业 / 赛道',detail.project.industry],['成立时间',detail.project.founded],['所在地',detail.project.location]].map(([label,value])=><div key={label}><dt>{label}</dt><dd>{value || '尚未取得'}</dd></div>)}</dl>
    </Chapter>
    {CHAPTERS.slice(1,5).map(([id,title])=>{
      const rows=group(id as DossierGroup);
      const empty=id==='business'?'尚未取得产品与业务资料。':id==='customers'?'尚未整理出带来源的客户与应用资料。':`尚未取得${title}资料。`;
      return <Chapter key={`${detail.project.id}-${id}`} id={id} title={title} note={arrivals.includes(id)?'资料已更新':undefined}>
        {rows.length ? <EntryList entries={rows} onOpen={setSource} sectionTitle={title}/> : <p className="cd-empty">{empty}</p>}
      </Chapter>;
    })}
    <Chapter id="history" title="发展历程" note="按事项日期 · 计划与已完成分别标注">
      {!milestones.length && <p className="cd-empty">尚未取得发展历程资料。</p>}
      {dated.length>0 && <ol className="cd-history-list">{visibleDates.map(e=><li key={e.id}><div><time>{e.date}</time><small>{e.state==='planned'?'计划':e.state==='completed'?'已完成':'公开 / 材料记录'}</small></div><div><h3>{e.label}</h3><p>{e.text}</p><button className="link small" onClick={()=>setSource(e)}>查看依据 ↗</button></div></li>)}</ol>}
      {dated.length>4 && <button className="link small" onClick={()=>setMoreDates(!moreDates)} aria-expanded={moreDates}>{moreDates?'收起':`查看全部 ${dated.length} 个节点`}</button>}
      {undated.length>0 && <details className="cd-undated"><summary>{undated.length} 条记录日期待补充</summary><EntryList key={detail.project.id} entries={undated} onOpen={setSource}/></details>}
    </Chapter>
    <Chapter id="registry" title="工商信息">
      <div className="cd-registry-open" role="region" aria-label="工商档案">{registry || <p className="cd-empty">尚未取得工商登记信息。</p>}</div>
      {group('registry').length>0 && <EntryList key={detail.project.id} entries={group('registry')} onOpen={setSource}/>}
    </Chapter>
    {(group('other').length>0 || highlights.length>0) && <details className="cd-supplement"><summary>补充资料与研究要点</summary><EntryList key={detail.project.id} entries={[...group('other'),...highlights]} onOpen={setSource}/></details>}
    {children}
    <DossierSource entry={source} onClose={()=>setSource(null)}/>
  </article>;
}

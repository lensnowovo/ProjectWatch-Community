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

function EntryList({entries, onOpen, limit=3}: {entries:DossierEntry[];onOpen:(entry:DossierEntry)=>void;limit?:number}) {
  const [all,setAll] = useState(false);
  return <><ul className="cd-entry-list">{(all?entries:entries.slice(0,limit)).map(e=><li key={e.id}><button onClick={()=>onOpen(e)}><strong>{e.label}</strong><span>{e.text}</span><small>{e.date ? `${e.dateLabel} ${e.date}` : e.fact ? '公开资料' : '公司资料'} · 查看依据 ↗</small></button></li>)}</ul>
    {entries.length>limit && <button className="link small cd-more" onClick={()=>setAll(!all)} aria-expanded={all}>{all?'收起':`展开另外 ${entries.length-limit} 条`}</button>}</>;
}

export function DossierOverview({detail, sources, registry, children, arrivals=[]}: {detail:ProjectDetail;sources:ReactNode;registry:ReactNode;children?:ReactNode;arrivals?:string[]}) {
  const entries=dossierEntries(detail);
  const group=(g:DossierGroup)=>entries.filter(e=>e.group===g);
  const [source,setSource]=useState<DossierEntry|null>(null);
  const [all,setAll]=useState(false);
  const [expanded,setExpanded]=useState<string|null>(null);
  const rows=group('business');
  const milestones=milestonesFor(detail);
  const dated=milestones.filter(e=>e.date);
  const undated=milestones.filter(e=>!e.date);
  const [moreDates,setMoreDates]=useState(false);
  const visibleDates=moreDates?dated:dated.slice(-4);
  const summary=detail.profile?.summary?.trim();
  const intro=businessIntro(detail);
  // Highlights are sourced content, not new computed claims. Use one existing point, not a new model call.
  const highlight=detail.profile?.highlights?.find(h=>h.text && h.refs?.length);
  const otherHighlights=(detail.profile?.highlights ?? []).filter(h=>h!==highlight && h.text).map((h,i):DossierEntry=>({id:`highlight-${i}`,label:'资料要点',group:'other',text:h.text,refs:h.refs ?? [],date:'',dateLabel:''}));
  useEffect(()=>{setSource(null);setExpanded(null);setAll(false);setMoreDates(false);},[detail.project.id]);
  return <div className="cd-overview" data-arrivals={arrivals.join(' ')} id="dossier-profile">
    <div className="cd-page-line"><span>公司档案</span><div>{sources}</div></div>
    {summary && summary!==intro && <details className="cd-abstract"><summary>完整公司介绍</summary><p>{summary}</p></details>}
    <section className="cd-card cd-products" id="dossier-business" aria-labelledby="cd-business-title"><div className="cd-heading"><h2 id="cd-business-title"><span>01</span>业务与产品</h2><small>{arrivals.includes('business')?'资料已更新':'按已有资料展示'}</small></div>
      {highlight && <button className="cd-highlight" onClick={()=>setSource({id:'highlight',label:'业务要点',group:'business',text:highlight.text,refs:highlight.refs,date:'',dateLabel:''})}>{highlight.text}<small>查看依据 ↗</small></button>}
      {otherHighlights.length>0 && <details className="cd-extra-points"><summary>其他资料要点 · {otherHighlights.length}</summary><EntryList entries={otherHighlights} onOpen={setSource}/></details>}
      <div className="cd-business-layout"><div className="cd-table-wrap">
        {rows.length ? <table className="cd-product-table"><thead><tr><th scope="col">产品 / 事项</th><th scope="col">业务与进展</th><th scope="col">日期 / 依据</th></tr></thead><tbody>{(all?rows:rows.slice(0,5)).map(e=><tr key={e.id}><th scope="row"><button className="cd-row-toggle" aria-expanded={expanded===e.id} onClick={()=>setExpanded(expanded===e.id?null:e.id)}>{e.label}<span>{expanded===e.id?'−':'＋'}</span></button></th><td><p className={expanded===e.id?'':'cd-clamp'}>{e.text}</p></td><td><small>{e.dateLabel}</small><time>{e.date || '待补充'}</time><button className="link small" onClick={()=>setSource(e)}>查看来源 ↗</button></td></tr>)}</tbody></table> : <p className="cd-empty">尚未取得产品与业务资料。</p>}
        {rows.length>5 && <button className="link small cd-more" onClick={()=>setAll(!all)} aria-expanded={all}>{all?'收起':`展开另外 ${rows.length-5} 条`}</button>}
      </div><aside className="cd-market"><h3>客户与应用</h3>{group('customers').length ? <EntryList entries={group('customers')} onOpen={setSource} limit={2}/> : <p className="cd-empty">尚未整理出带来源的客户与应用资料。</p>}</aside></div>
    </section>
    <div className="cd-two-col">{([{group:'team',title:'核心团队',num:'02'},{group:'capital',title:'融资与股东',num:'03'}] as const).map(c=><section className="cd-card" id={`dossier-${c.group}`} key={c.group}><div className="cd-heading"><h2><span>{c.num}</span>{c.title}</h2>{arrivals.includes(c.group)&&<small className="cd-arrival-note">资料已更新</small>}</div>{group(c.group).length ? <EntryList entries={group(c.group)} onOpen={setSource}/> : <p className="cd-empty">尚未取得{c.title}资料。</p>}</section>)}</div>
    {group('other').length>0 && <section className="cd-card"><div className="cd-heading"><h2>其他经营资料</h2></div><EntryList entries={group('other')} onOpen={setSource}/></section>}
    {milestones.length>0 && <section className="cd-history" id="dossier-history"><div className="cd-heading"><h2><span>04</span>发展历程</h2><small>按事项日期 · 状态逐条标注</small></div>
      {dated.length>0 && <ol>{visibleDates.map(e=><li key={e.id} className={`cd-node-${e.state}`}><time>{e.date}</time><span className="cd-node-label">{e.state==='planned'?'计划':e.state==='completed'?'已完成':'公开 / 材料记录'}</span><button onClick={()=>setSource(e)}><strong>{e.label}</strong><span className="cd-clamp">{e.text}</span><small>查看依据 ↗</small></button></li>)}</ol>}
      {dated.length>4 && <button className="link small" onClick={()=>setMoreDates(!moreDates)} aria-expanded={moreDates}>{moreDates?'收起':`查看全部 ${dated.length} 个节点`}</button>}
      {undated.length>0 && <details className="cd-undated"><summary>{undated.length} 条记录日期待补充</summary><EntryList entries={undated} onOpen={setSource}/></details>}
    </section>}
    <details className="cd-registry" aria-label="工商档案"><summary>工商档案 <span>查看登记信息 ⌄</span></summary>{registry}{group('registry').length>0 && <EntryList entries={group('registry')} onOpen={setSource}/>}</details>
    {children}
    <DossierSource entry={source} onClose={()=>setSource(null)}/>
  </div>;
}

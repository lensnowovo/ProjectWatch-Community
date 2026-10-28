import { useState } from 'react';
import { api, type Category, type Item, type Landscape } from './api';
import { ItemList } from './items';
import { seenAt, useAsync } from './ui';
import { Skeleton } from './frame';
import './externalNews.css';

const LABEL: Partial<Record<Category,string>> = {competitor:'竞品',industry:'行业',policy:'政策'};
export type LegacyReport = Landscape['moves'][number];

/** Category queries cannot be crowded out by other categories. All requests read stored events only. */
export async function readNews(projectId:string, categories:Category[]) {
  const results=await Promise.allSettled(categories.map(category=>api.feed({project_id:projectId,category,days:180,limit:200})));
  if(results.every(r=>r.status==='rejected')) throw new Error('新闻列表暂时无法读取');
  const items=results.flatMap((r,i)=>r.status==='fulfilled'?r.value.filter(x=>x.project_id===projectId && x.category===categories[i]):[]);
  return {projectId,scope:categories.join('|'),items,failed:categories.filter((_,i)=>results[i].status==='rejected')};
}

export function useExternalNews(projectId:string, revision:string, categories:Category[]) {
  const scope=categories.join('|');
  const result=useAsync(()=>readNews(projectId,categories),[projectId,revision,scope]);
  const data=result.data?.projectId===projectId && result.data.scope===scope?result.data:null;
  return {...result,data,update:(changed:Item)=>result.setData(old=>old?.projectId===projectId?{...old,items:old.items.map(i=>i.id===changed.id?changed:i)}:old)};
}

/** Identity deduplication only; different articles with the same headline remain distinct. */
export function unmatchedReports(items:Item[], reports:LegacyReport[]) {
  const ids=new Set(items.flatMap(i=>[i.id,...(i.sources??[]).map(s=>s.id)]));
  const urls=new Set(items.flatMap(i=>[i.url,...(i.sources??[]).map(s=>s.url)]).filter(Boolean));
  const seenIds=new Set<string>(),seenUrls=new Set<string>();
  return reports.filter(r=>{
    if((r.id && (ids.has(r.id)||seenIds.has(r.id))) || (r.url && (urls.has(r.url)||seenUrls.has(r.url)))) return false;
    if(r.id)seenIds.add(r.id);if(r.url)seenUrls.add(r.url);return true;
  });
}

const newsTime = (item:Item) => {const n=Date.parse(seenAt(item)??'');return Number.isFinite(n)?n:-Infinity;};
export function newsGroups(items:Item[]) {
  const groups=new Map<string,Item[]>();
  for(const item of [...new Map(items.filter(i=>i.status!=='dismissed').map(i=>[i.id,i])).values()].sort((a,b)=>newsTime(b)-newsTime(a))){
    const stamp=seenAt(item),time=Date.parse(stamp??'');
    const label=Number.isFinite(time)?new Date(time).toLocaleDateString('zh-CN',{year:'numeric',month:'long'}):'日期未知';
    groups.set(label,[...(groups.get(label)??[]),item]);
  }
  return [...groups.entries()];
}

/** Same event rows as 公司进展, with a bounded, explicitly expandable news stream. */
export function ExternalNews({title,items,loading,error,failed=[],legacy=[],categories=[],onChange,onRetry}: {
  title:string;items:Item[];loading:boolean;error?:unknown;failed?:Category[];legacy?:LegacyReport[];
  categories?:Category[];onChange:(item:Item)=>void;onRetry:()=>void;
}) {
  const [category,setCategory]=useState('all');
  const [major,setMajor]=useState(false);
  const [limit,setLimit]=useState(12);
  const [legacyLimit,setLegacyLimit]=useState(8);
  const visible=items.filter(i=>i.status!=='dismissed');
  const filtered=visible.filter(i=>(category==='all'||i.category===category)&&(!major||i.importance==='major'));
  const ordered=newsGroups(filtered).flatMap(([,rows])=>rows);
  const reports=unmatchedReports(items,legacy).filter(r=>!major||r.importance==='major').sort((a,b)=>(b.date||'').localeCompare(a.date||''));
  return <section className="external-news" aria-label={title}>
    <header className="external-news-head"><div><h3>{title}</h3><p>近 180 天</p></div><span className="muted small">{visible.length} 条已读取</span></header>
    <div className="filters external-news-filters">
      {categories.length>1 && <div className="external-news-tabs" role="group" aria-label="新闻分类">{[['all','全部'],...categories.map(c=>[c,LABEL[c]??c])].map(([key,label])=><button key={key} className={category===key?'on':''} aria-pressed={category===key} onClick={()=>{setCategory(key);setLimit(12);}}>{label}</button>)}</div>}
      <label className="check check-inline"><input type="checkbox" checked={major} onChange={e=>{setMajor(e.target.checked);setLimit(12);}}/>只看重大</label>
    </div>
    {(!!error||failed.length>0) && <p className="external-news-error" role="status">{error?'新闻读取失败，已读取的内容保留。':`${failed.map(c=>LABEL[c]??c).join('、')}新闻读取失败，其余已读取内容保留。`} <button className="link small" onClick={onRetry}>重新读取</button></p>}
    {loading && !items.length && <Skeleton lines={4}/>}
    {!loading && !error && !failed.length && !ordered.length && <p className="muted small">{visible.length?'当前筛选下没有新闻。':'暂无收录新闻。'}</p>}
    <details className="chart-evidence"><summary>来源范围</summary><p>近 180 天，每类最多读取 200 条，按日期倒序。</p></details>
    {ordered.length>0 && <ItemList items={ordered.slice(0,limit)} grouped={false} onChange={onChange}/>}
    {ordered.length>limit && <button className="btn btn-ghost btn-sm news-older" onClick={()=>setLimit(n=>n+20)}>再看 {Math.min(20,ordered.length-limit)} 条（当前筛选共 {ordered.length} 条）</button>}
    {reports.length>0 && <div className="external-news-legacy"><h4>历史整理中的相关报道</h4><details className="chart-evidence"><summary>来源说明</summary><p>未匹配到新闻事件的旧记录，保留原始标题；归属尚未确认。</p></details>
      {reports.slice(0,legacyLimit).map((r,i)=><article className="item" key={`${r.id}-${i}`}><div className="item-body"><div className="item-meta"><span>旧版整理记录</span><span>{r.source||'来源未记录'}</span><time>{r.date||'日期未知'}</time></div>{r.url?<a className="item-title" href={r.url} target="_blank" rel="noreferrer noopener">{r.title}</a>:<span className="item-title">{r.title}</span>}</div></article>)}
      {reports.length>legacyLimit && <button className="link small" onClick={()=>setLegacyLimit(n=>n+20)}>查看更多历史报道（共 {reports.length} 条）</button>}
    </div>}
  </section>;
}

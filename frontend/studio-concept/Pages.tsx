import {useState} from 'react';
import {analyses,analysisAnswers,events,type PageProps,type Event,type Document} from '../prototype/data';
import {projectProfiles} from '../prototype/projectProfiles';
import {LineChart} from '../prototype/ui';
import {CompactNewsRow} from '../prototype/InformationPages';
import {InternalFinance,InternalRecords} from '../prototype/InternalModules';
import {AnalysisVisual,TechnologyMap} from '../prototype/AnalysisVisuals';

export function Stats({project}:Pick<PageProps,'project'>){return <div className="studio-stats">{project.stats.map(s=><div key={s.label}><span>{s.label}</span><strong>{s.value}</strong><small>{s.delta}</small></div>)}</div>}
export function NewsList({rows,openEvent}: {rows:Event[];openEvent:PageProps['openEvent']}){return <div className="studio-news-list">{rows.map(e=><CompactNewsRow key={e.id} event={e} openEvent={openEvent} namespace="studio" compact/>)}{!rows.length&&<p className="studio-empty">没有符合筛选的动态</p>}</div>}
export function Overview(props:PageProps & {openDocument:(d:Document)=>void}){
 const {project,navigate,openEvent,openDocument}=props,p=projectProfiles[project.id];
 const rows=events.filter(e=>e.projectId===project.id).slice(0,3);
 return <><section className="studio-profile"><div><p className="studio-kicker">公司概览 <span>COMPANY PROFILE</span></p><h2>{project.description}</h2><p className="studio-copy">{p.summary}</p><button className="studio-source" onClick={()=>openDocument(project.docs[1])}>项目介绍 · {project.docs[1].date} ↗</button></div><dl>{[['成立',p.registry.founded],['所在地',p.registry.location],['融资阶段',project.stage],['行业 / 赛道',p.track]].map(([k,v])=><div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl></section>
 <Stats project={project}/><section className="studio-overview-grid"><div><div className="studio-section-title"><h2>业务与产品</h2><button onClick={()=>navigate(`/project/${project.id}/internal`)}>经营情况 ↗</button></div><table className="studio-products"><thead><tr><th>产品 / 平台</th><th>业务方向</th><th>当前阶段</th></tr></thead><tbody>{p.products.map(row=><tr key={row.name}><th>{row.name}</th><td>{row.use}</td><td><span className="studio-chip">{row.stage}</span></td></tr>)}</tbody></table><details className="studio-registry"><summary>工商档案</summary><dl>{Object.entries({'工商全称':p.registry.name,'注册资本':p.registry.capital,'登记状态':p.registry.status}).map(([k,v])=><div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl></details></div><aside className="studio-reading"><p className="studio-kicker">当前关注</p><h3>{project.thesis}</h3><button className="studio-link" onClick={()=>navigate(`/project/${project.id}/analysis`)}>查看分析与依据 ↗</button></aside></section>
 <div className="studio-section-title"><h2>最近变化</h2><button onClick={()=>navigate(`/project/${project.id}/news`)}>公司动态 ↗</button></div><NewsList rows={rows} openEvent={openEvent}/></>;
}
export function Operating({project}:PageProps){
 const [mode,setMode]=useState('current');
 return <><div className="studio-section-title"><h2>经营情况</h2><span>2026 年第三季度 · 内部材料</span></div><div className="studio-subnav" role="group" aria-label="经营模块">{[['current','经营概览'],['business','业务与研发'],['finance','财务三表'],['events','重大事项']].map(([id,label])=><button key={id} aria-pressed={mode===id} onClick={()=>setMode(id)}>{label}</button>)}</div>{mode==='current'?<><Stats project={project}/><div className="studio-chart-sheet"><LineChart project={project}/></div><InternalRecords projectId={project.id} mode="business"/></>:mode==='finance'?<InternalFinance projectId={project.id}/>:<InternalRecords projectId={project.id} mode={mode==='business'?'business':'events'}/>}</>;
}
export function CompanyNews({project,openEvent}:PageProps){
 const [filter,setFilter]=useState('all'),[query,setQuery]=useState('');
 const all=events.filter(e=>e.projectId===project.id&&(e.category==='company'||e.category==='clinical'&&e.subject===project.name));
 const rows=all.filter(e=>(filter==='all'||(filter==='focus'?e.importance==='重点':e.category===filter))&&`${e.title}${e.source}`.includes(query));
 return <><div className="studio-section-title"><h2>公司动态</h2><span>{all.length} 条 · 截至 10 月 15 日</span></div><div className="studio-toolbar"><div className="studio-subnav" role="group" aria-label="公司动态分类">{[['all','全部'],['company','公司公告'],['clinical','专利与注册'],['focus','重点']].map(([id,label])=><button key={id} aria-pressed={filter===id} onClick={()=>setFilter(id)}>{label}</button>)}</div><input aria-label="搜索公司动态" placeholder="搜索动态" value={query} onChange={e=>setQuery(e.target.value)}/></div><NewsList rows={rows} openEvent={openEvent}/></>;
}
export function Competition(props:PageProps){
 const {project,navigate,openEvent}=props,analysis=analyses.find(a=>a.id===`${project.id}-position`)!;
 return <><div className="studio-answer-lead"><p className="studio-kicker">竞争位置</p><h2>{analysisAnswers[analysis.id].headline}</h2><div className="studio-answer-meta"><span>{project.peers.length} 家对照公司</span><span>{project.id==='chengyao'?'同靶点 · 不同技术路线':'相同应用 · 相邻产品路线'}</span><button onClick={()=>navigate(`/project/${project.id}/scope`)}>为什么关注这些公司 ↗</button></div></div><AnalysisVisual {...props} analysis={analysis} questionType="position"/><div className="studio-section-title"><h2>同行进展</h2><button onClick={()=>navigate(`/project/${project.id}/analysis?id=${analysis.id}`)}>完整分析 ↗</button></div><NewsList rows={events.filter(e=>e.projectId===project.id&&e.category==='peers')} openEvent={openEvent}/></>;
}
export function Industry(props:PageProps & {initialCategory?:string}){
 const {project,openEvent,navigate}=props;
 const [category,setCategory]=useState(props.initialCategory&&['industry','policy','papers','clinical'].includes(props.initialCategory)?props.initialCategory:'industry');
 const a=analyses.find(a=>a.id===`${project.id}-environment`)!;
 return <><div className="studio-answer-lead"><p className="studio-kicker">行业环境</p><h2>{a.conclusion}</h2><div className="studio-wind" aria-label="行业风向：温和向好，模拟判断">{['承压','偏弱','平稳','温和向好','快速扩张'].map((s,i)=><span className={i===3?'active':''} key={s}><i/>{s}</span>)}</div></div><div className="studio-subnav" role="group" aria-label="行业分类">{[['industry','行业与产业链'],['policy','政策'],['papers','论文'],['clinical','专利与注册'],['technology','技术路线']].map(([id,label])=><button key={id} aria-pressed={category===id} onClick={()=>setCategory(id)}>{label}</button>)}</div>{category==='technology'?<div className="studio-chart-sheet"><TechnologyMap project={project} openEvent={openEvent}/><button className="studio-link" onClick={()=>navigate(`/project/${project.id}/analysis?id=${project.id}-technology`)}>技术路线分析 ↗</button></div>:<NewsList rows={events.filter(e=>e.projectId===project.id&&e.category===category)} openEvent={openEvent}/>}</>;
}

import {useEffect,useState} from 'react';
import {projects,events,scopes,type Document} from '../prototype/data';
import {projectProfiles} from '../prototype/projectProfiles';
import {Ambient,Icon,Modal} from '../prototype/ui';
import {DocumentPreview} from '../prototype/InformationPages';
import {AnalysisPage,ScopePage} from '../prototype/ResearchPages';
import {Overview,Operating,CompanyNews,Competition,Industry,NewsList} from './Pages';
import {tabs,readRoute,normalizePath} from './navigation';

export default function App(){
 const [hash,setHash]=useState(location.hash),[theme,setTheme]=useState(()=>localStorage.getItem('pw-studio-theme')||'light'),[menu,setMenu]=useState(false);
 const [dialog,setDialog]=useState(''),[eventId,setEventId]=useState(''),[doc,setDoc]=useState<Document>(),[toast,setToast]=useState(''),[files,setFiles]=useState<string[]>([]);
 useEffect(()=>{const change=()=>{setHash(location.hash);setMenu(false);setDoc(undefined);setEventId('');window.scrollTo(0,0)};window.addEventListener('hashchange',change);return()=>window.removeEventListener('hashchange',change)},[]);
 useEffect(()=>{document.documentElement.dataset.theme=theme;localStorage.setItem('pw-studio-theme',theme)},[theme]);
 useEffect(()=>{if(!toast)return;const id=setTimeout(()=>setToast(''),3500);return()=>clearTimeout(id)},[toast]);
 const route=readRoute(hash),project=projects.find(p=>p.id===route.projectId)!,profile=projectProfiles[project.id];
 const navigate=(path:string)=>{if(path.includes('/agent')||new URLSearchParams(path.split('?')[1]||'').get('ask')==='1'){setDialog('agent');return;}location.hash=normalizePath(path)};
 const props={project,navigate,openEvent:setEventId,notify:setToast};
 const event=events.find(e=>e.id===eventId),scope=event&&scopes.find(s=>s.id===event.scopeId);
 const globalMode=hash.includes('/daily')?'daily':hash.includes('/report')?'report':'portfolio';
 const upload=(incoming:FileList|null)=>{if(incoming)setFiles(Array.from(incoming).map(f=>f.name))};
 return <div className="studio-app"><a className="skip-link" href="#studio-main" onClick={e=>{e.preventDefault();document.getElementById('studio-main')?.focus()}}>跳到正文</a>
 <button className="studio-mobile-menu" aria-label="展开导航" onClick={()=>setMenu(!menu)}><Icon name="menu"/></button>
 <aside className={`studio-sidebar ${menu?'is-open':''}`}><button className="studio-brand" onClick={()=>navigate('/portfolio')}><span className="studio-brand-orbit">◎</span><span>ProjectWatch<small>项目监控</small></span></button><button className="studio-add" onClick={()=>setDialog('projects')}>＋ 添加项目</button><div className="studio-demo">模拟数据 · 设计对照版</div><nav aria-label="工作台">{[['/daily','今日动态'],['/portfolio','项目组合'],['/report','周报']].map(([path,label])=><button key={path} className={route.portfolio&&hash.includes(path)?'active':''} onClick={()=>navigate(path)}>{label}</button>)}</nav><p className="studio-nav-caption">关注的公司</p><nav aria-label="项目列表">{projects.map(p=><button key={p.id} className={!route.portfolio&&p.id===project.id?'active':''} onClick={()=>navigate(`/project/${p.id}/overview`)}>{p.name}<i className="studio-status" title="模拟监控正常"/></button>)}</nav><div className="studio-sidebar-bottom"><button onClick={()=>setDialog('settings')}><Icon name="settings" size={15}/>设置</button><div className="studio-theme" role="group" aria-label="颜色主题">{[['light','浅色'],['dark','深色']].map(([id,label])=><button aria-pressed={theme===id} key={id} onClick={()=>setTheme(id)}>{label}</button>)}</div></div></aside>
 <main id="studio-main" className="studio-main" tabIndex={-1}>
 {route.portfolio?<><header className="studio-global-header"><Ambient variant="neutral"/><p className="studio-kicker">PROJECTWATCH / 工作台</p><h1>{globalMode==='daily'?'今日动态':globalMode==='report'?'本周回顾':'项目组合'}</h1><p>{globalMode==='portfolio'?'两家公司，两种成长路径。':'2026.10.09 — 2026.10.15'}</p></header>{globalMode==='portfolio'?<div className="studio-project-grid">{projects.map(p=><button key={p.id} onClick={()=>navigate(`/project/${p.id}/overview`)}><span className="studio-kicker">{p.sector} · {p.stage}</span><h2>{p.name}</h2><p>{p.description}</p><div><strong>{p.stats[0].value}</strong><span>{p.stats[0].label}</span><span className="studio-project-arrow">查看项目 ↗</span></div></button>)}</div>:globalMode==='daily'?<NewsList rows={events.slice().sort((a,b)=>b.date.localeCompare(a.date))} openEvent={setEventId}/>:<div className="studio-project-grid">{projects.map(p=><section className="studio-report" key={p.id}><p className="studio-kicker">{p.name}</p><h2>{p.signal}</h2><p>{p.thesis}</p><button className="studio-link" onClick={()=>navigate(`/project/${p.id}/analysis`)}>查看研究分析 ↗</button></section>)}</div>}</>:<>
 <header className="studio-project-header"><Ambient variant="neutral"/><div className="studio-header-top"><button onClick={()=>navigate('/portfolio')}>← 项目组合</button><div><button className="studio-upload" onClick={()=>{setFiles([]);setDialog('upload')}}>＋ 上传资料</button><span>模拟数据</span></div></div><p className="studio-kicker">{project.sector} · {project.stage} · {project.city}</p><h1>{project.name}</h1><p className="studio-fullname">{profile.registry.name}</p><div className="studio-header-bottom"><span><i className="studio-status"/> 监控中 <span className="studio-muted">· 模拟状态</span></span><span>更新于 {project.updated}</span></div></header>
 <nav className="studio-tabs" aria-label="项目页签">{tabs.map(([id,label])=><button key={id} aria-current={route.tab===id?'page':undefined} onClick={()=>navigate(`/project/${project.id}/${id}`)}>{label}{id==='documents'&&<small>{project.docs.length}</small>}</button>)}</nav>
 <div className="studio-page" key={`${project.id}-${route.tab}`}>
 {route.tab==='overview'&&<Overview {...props} openDocument={setDoc}/>}
 {route.tab==='internal'&&<Operating {...props}/>}
 {route.tab==='news'&&<CompanyNews {...props}/>}
 {route.tab==='competition'&&<Competition {...props}/>}
 {route.tab==='industry'&&<Industry {...props} initialCategory={route.params.get('category')||undefined}/>}
 {route.tab==='analysis'&&<AnalysisPage {...props} selectedId={route.params.get('id')||undefined}/>}
 {route.tab==='scope'&&<ScopePage {...props} selectedId={route.params.get('id')||undefined}/>}
 {route.tab==='documents'&&<><div className="studio-section-title"><h2>资料库</h2><span>{project.docs.length} 份资料</span></div><div className="studio-document-list">{project.docs.map(d=><button key={d.id} onClick={()=>setDoc(d)}><Icon name="file"/><span><strong>{d.title}</strong><small>{d.kind} · {d.pages} 页</small></span><time>{d.date}</time><span>↗</span></button>)}</div></>}
 </div></>}
 <footer className="studio-footer"><span>ProjectWatch Studio</span><span>虚构公司与模拟数据 · 仅作设计演示</span></footer>
 </main>
 {event&&<Modal title={event.title} wide onClose={()=>setEventId('')}><div className="source-meta"><span>{event.source}</span><span>{event.date}</span><span>{event.kind}</span></div><p className="source-lead">{event.summary}</p><div className="evidence-grid"><div><h3>内容摘录</h3><p className="source-excerpt">{event.excerpt}</p><small className="muted">虚构来源，非真实报道</small></div><div><h3>与项目的关联</h3><p>{scope?.why||event.relation}</p><button className="studio-link" onClick={()=>{setEventId('');navigate(`/project/${event.projectId}/scope?id=${event.scopeId}`)}}>查看监控路径 ↗</button><button className="studio-link" onClick={()=>{setEventId('');navigate(`/project/${event.projectId}/analysis?id=${event.analysisId}`)}}>相关分析 ↗</button></div></div></Modal>}
 {doc&&<DocumentPreview document={doc} project={project} navigate={navigate} onClose={()=>setDoc(undefined)}/>}
 {dialog&&<Modal title={dialog==='projects'?'选择示例项目':dialog==='upload'?'添加资料':dialog==='settings'?'设置':'继续研究'} onClose={()=>setDialog('')}>
 {dialog==='projects'&&<><p className="muted">此版本提供两组虚构数据，不创建真实项目。</p><div className="demo-choices">{projects.map(p=><button key={p.id} onClick={()=>{setDialog('');navigate(`/project/${p.id}/overview`)}}><span><strong>{p.name}</strong><small>{p.sector}</small></span><span>↗</span></button>)}</div></>}
 {dialog==='upload'&&<><p className="studio-copy">产品流程：资料加入后，分析并更新项目信息。</p><label className="studio-drop" onDragOver={e=>e.preventDefault()} onDrop={e=>{e.preventDefault();upload(e.dataTransfer.files)}}><Icon name="upload" size={26}/><strong>拖入文件，或点击选择</strong><small>此演示只显示文件名，不读取或上传</small><input type="file" multiple aria-label="选择演示文件" onChange={e=>upload(e.target.files)}/></label>{files.length>0&&<ul>{files.map((f,i)=><li key={`${f}-${i}`}>{f}</li>)}</ul>}<div className="studio-modal-actions"><button className="studio-upload" onClick={()=>setDialog('')}>完成</button></div></>}
 {dialog==='settings'&&<><p>设计对照版未连接模型、搜索或真实项目。</p><div className="studio-subnav">{[['light','浅色'],['dark','深色']].map(([id,label])=><button key={id} aria-pressed={theme===id} onClick={()=>setTheme(id)}>{label}</button>)}</div></>}
 {dialog==='agent'&&<p>此版本展示分析结果与依据，尚未接入对话执行器。可在分析页记录你的判断。</p>}
 </Modal>}
 {toast&&<div role="status" className="toast">{toast}</div>}
 </div>;
}

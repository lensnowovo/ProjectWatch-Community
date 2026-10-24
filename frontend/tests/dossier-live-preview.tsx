// Synthetic state transitions, explicit buttons only. Production components; no network.
import React, {useState} from 'react';
import {createRoot} from 'react-dom/client';
import {BrowserRouter,MemoryRouter,Routes,Route} from 'react-router-dom';
import '../src/styles.css';
import './company-dossier-integration.css';
import ProjectHeader from '../src/ProjectHeader';
import {CompanyOverview} from '../src/overview';
import {useDossierMotion} from '../src/DossierMotion';
import {ToastProvider} from '../src/motion';
import {api,type ProjectDetail} from '../src/api';
import ProjectPage from '../src/pages/Project';
for(const key of Object.keys(api)) (api as any)[key]=async()=>{if(key==='questions')return {questions:[]};throw new Error('No live API in synthetic preview');};
const base=():ProjectDetail=>({project:{id:'synthetic-motion',name:'澄序医疗',description:'神经工程与康复研究设备。公司与下列材料均为合成样本。',industry:'医疗器械',status:'active',activated:true},profile:null,brief:null,plan:null,metrics:[],counts:{},jobs:[],documents:[],model_ready:false,pending_documents:0,public_profile:{facts:[],sources:0,updated_at:''},execution:{analysis:'running',parsing:'running'},dossier_saved:{version:1,relations:{count:0,revision:'empty'},judgment:{count:0,revision:'empty'}}} as unknown as ProjectDetail);
const fact=(kind:string,label:string,text:string)=>({kind,label,text,date:'2026-10',refs:[{url:'https://example.invalid/source',source:'合成资料',title:'测试来源',quote:text,published_at:'2026-10-01'}]});
const actualPage=new URLSearchParams(location.search).get('page')==='1';
let stored=base();
let reads=0;
api.project=async()=>{reads++;document.documentElement.dataset.projectReads=String(reads);return structuredClone(stored);};
function App(){
 const [detail,setDetail]=useState(()=>{const d=base();if(actualPage)d.jobs=[{id:'synthetic-job',kind:'external',status:'running',stage:'读取公开资料',error:'',created_at:'2026-10-22',finished_at:null,runtime:{state:'running',label:'正在读取',phase:'read',progress_at:null,heartbeat_at:null,progress_age_seconds:null,yield_count:0,recovery_attempts:0}}];return d;}); const [shown,setShown]=useState(true);const motion=useDossierMotion(detail,shown);
 stored=detail;
 const status=(state:string)=>setDetail(d=>({...d,status_stale:state==='unavailable',jobs:[{id:'synthetic-job',kind:'external',status:['failed','partial','needs_confirmation'].includes(state)?'failed':state==='done'?'done':state==='queued'?'queued':'running',created_at:'2026-10-22',finished_at:null,stage:'阅读公开产品资料',error:'',runtime:{state,label:({partial:'部分来源未覆盖',needs_confirmation:'结果不明，等待确认',done:'已完成',waiting_response:'等待模型响应'} as any)[state]||state,phase:'read',progress_at:null,heartbeat_at:null,progress_age_seconds:null,yield_count:0,recovery_attempts:0}}]}));
 const product=()=>setDetail(d=>({...d,public_profile:{...d.public_profile!,facts:[...d.public_profile!.facts,fact('product',`采集系统 ${d.public_profile!.facts.length+1}`,'已取得科研用途资料；尚未确认医疗器械获批。') as any]}}));
 return <ToastProvider><div className="integration-tools"><strong>隔离验证 · 合成数据 · 不连接当前实例</strong><button onClick={()=>status('running')}>运行</button><button onClick={product}>保存产品</button><button onClick={()=>setDetail(d=>({...d,public_profile:{...d.public_profile!,facts:[...d.public_profile!.facts,fact('person','合成团队','神经工程研发背景，履历为合成样本。') as any]}}))}>保存团队</button><button onClick={()=>setDetail(d=>({...d,dossier_saved:{version:1,relations:{count:2,revision:'saved-relations'},judgment:{count:1,revision:'saved-judgment'}}}))}>保存关联与判断</button>
 {['waiting_response','partial','needs_confirmation','queued','paused','stalled','unavailable','done'].map(s=><button key={s} onClick={()=>status(s)}>{s}</button>)}<button onClick={()=>setDetail(d=>({...d}))}>重复轮询</button><button onClick={()=>setDetail(d=>({...d,project:{...d.project,id:d.project.id==='synthetic-motion'?'synthetic-other':'synthetic-motion'}}))}>换项目</button><button onClick={()=>setShown(!shown)}>切换页签</button><button onClick={()=>document.documentElement.setAttribute('data-theme',document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark')}>深浅色</button><button onClick={()=>setDetail(base())}>清空测试</button></div>
 {actualPage?<Routes><Route path="/projects/:id" element={<ProjectPage onChanged={()=>{}}/>}/></Routes>:<main className="integration-page"><ProjectHeader detail={detail} actions={null} status={null} goto={()=>{}}/><nav className="integration-tabs"><strong>项目总览</strong><span>内部信息</span><span>外部信息</span><span>监控范围</span></nav>{shown?<CompanyOverview detail={detail} onReload={()=>{}} dossierMotion={motion}/>:<p>其他页签（观察器保持挂载）</p>}</main>}</ToastProvider>;
}
createRoot(document.getElementById('root')!).render(actualPage?<MemoryRouter initialEntries={['/projects/synthetic-motion?tab=overview']}><App/></MemoryRouter>:<BrowserRouter><App/></BrowserRouter>);

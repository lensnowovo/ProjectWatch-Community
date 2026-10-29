// Synthetic fixtures only. Every API is replaced, and fetch is prohibited.
import React, {useState} from 'react';
import {createRoot} from 'react-dom/client';
import {MemoryRouter, Routes, Route} from 'react-router-dom';
import ProjectPage from '../src/pages/Project';
import {api, type Item, type ProjectDetail} from '../src/api';
import {ToastProvider} from '../src/motion';
import '../src/styles.css';

window.fetch = async () => {throw new Error('Network prohibited in synthetic preview');};
for (const key of Object.keys(api)) (api as any)[key] = async () => {throw new Error('Unmocked API: ' + key);};
let tick=0, scenario='mixed', clinical=false;
const ago=(days:number)=>new Date(Date.now()-days*86400_000).toISOString().slice(0,10);
const row=(id:string,title:string,date:string|null,importance:Item['importance'],pending=false):Item=>({
  id,project_id:'synthetic-news',project_name:'合成科技',category:'company',title,url:'https://example.invalid/'+id,
  source:'合成公告',snippet:'',published_at:date,collected_at:new Date().toISOString(),importance,
  importance_status:pending?'pending':'assessed',importance_reason:pending?'':'合成材料描述了这一变化及其影响；仅用于测试评级展示。',
  summary:'这是隔离测试数据，不代表任何真实公司的情况。',impact:'',status:'read',extra:{},source_count:1,
});
const data=()=>scenario==='undated' ? [row('unknown','日期未确认的旧报道',null,'routine',true)] : [
  row('major','合成公司完成核心产品正式交付',ago(2),'major'),
  row('notable','合成公司披露产品试用进展',ago(5),'notable'),
  row('routine','合成公司一般活动报道',ago(8),'routine'),
  row('pending','已收录但尚未完成评级的报道',ago(1),'routine',true),
  row('old','去年完成的产品测试，今年才收录',ago(400),'major'),
  row('unknown','日期未知的报道',null,'routine',true),
  row('future','计划下个月举行的活动',ago(-30),'notable'),
];
const detail=():ProjectDetail=>({project:{id:'synthetic-news',name:'合成科技',legal_name:'合成科技',status:'active',activated:true,execution_mode:'api'},
  profile:null,brief:null,plan:clinical?{is_biomed:true,trial_sponsors:[]}:null,metrics:[],counts:{company:data().length},jobs:[],documents:[],model_ready:false,pending_documents:0,
  public_profile:{facts:[],sources:0,updated_at:''},execution:{analysis:'stopped',parsing:'stopped'},
  collection_report:{observed_at:String(tick)},observations:{items:[]},event_states:{items:[]},capabilities:{},
} as unknown as ProjectDetail);
api.project=async()=>{document.documentElement.dataset.reads=String(Number(document.documentElement.dataset.reads||0)+1);return detail();};
api.monitoringPlan=async()=>({framework:[],items:[],changes:[],proposals:[],relation_updates:{}} as any);
api.discoveredSources=async()=>({items:[],total:0,next_offset:null} as any);
api.monitor=async()=>({watchpoints:[],watch_labels:{},threads:[]} as any);
api.feed=async(params)=>params?.category==='trial'?[]:data();
api.commitments=async()=>({items:[]} as any);
api.signals=async()=>({cadence:{hours:24}} as any);

function App(){const [refresh,setRefresh]=useState(0); return <ToastProvider>
  <div style={{padding:'16px 32px',position:'sticky',top:0,zIndex:100,background:'var(--paper)'}}>
    <strong>隔离预览 · 全部为合成数据</strong>{' '}
    <button className="btn btn-sm" onClick={()=>{tick++;document.dispatchEvent(new Event('visibilitychange'));}}>刷新状态</button>{' '}
    <button className="btn btn-sm" onClick={()=>{scenario=scenario==='mixed'?'undated':'mixed';setRefresh(n=>n+1);}}>切换日期未知样本</button>
    {' '}<button className="btn btn-sm" onClick={()=>{clinical=!clinical;setRefresh(n=>n+1);}}>项目类型：{clinical?'医疗类':'通用科技'}</button>
  </div>
  <MemoryRouter key={refresh} initialEntries={['/projects/synthetic-news?tab=external']}>
    <Routes><Route path="/projects/:id" element={<ProjectPage onChanged={()=>{}}/>}/></Routes>
  </MemoryRouter></ToastProvider>;}
createRoot(document.getElementById('root')!).render(<App/>);

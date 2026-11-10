import React, {useState} from 'react';
import {createRoot} from 'react-dom/client';
import {CategoryStrip, TechnologyNavigation} from '../src/pages/Project';
import {api, type ProjectDetail} from '../src/api';
import {PageSummary} from '../src/pageSummary';
import {type Tab} from '../src/overview';
import '../src/styles.css';
import '../src/scopeReading.css';
window.fetch=async()=>{throw Error('Network prohibited');};
api.monitoringPlan=async()=>({framework:[],items:[],changes:[],proposals:[],relation_updates:{}} as any);
function App(){const [page,go]=useState<Tab>('tech-progress');const [medical,setMedical]=useState(false);
const detail={project:{id:'synthetic'},counts:{company:12,paper:3,patent:2},plan:{is_biomed:medical},jobs:[],page_summaries:{technology:{text:'合成公司的新一代控制系统已完成实验室验证，改进重点是响应速度与能耗。下一阶段将进入客户现场测试，验证持续运行表现。',refs:[],date:'2026-11-09'}}} as unknown as ProjectDetail;
return <main className="page-wide" style={{padding:'48px 24px'}}><button className="btn" onClick={()=>{setMedical(!medical);go('tech-progress')}}>合成项目：{medical?'医疗企业':'科技企业'}</button><h1>外部信息</h1><CategoryStrip detail={detail} page={page} go={go}/><TechnologyNavigation detail={detail} page={page} go={go}/><h2>{page==='tech-progress'?'研发进展':page==='papers'?'论文':page==='patents'?'专利与注册':'临床进展'}</h2><PageSummary detail={detail} subject={page==='papers'?'papers':page==='patents'?'patents':page==='trials'?'trials':'technology'}/></main>}
createRoot(document.getElementById('root')!).render(<App/>);

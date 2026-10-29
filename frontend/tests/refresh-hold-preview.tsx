// Every API is replaced; no real task, provider, or database is reachable.
import React from 'react';
import {createRoot} from 'react-dom/client';
import {MemoryRouter} from 'react-router-dom';
import {api,type TaskJob} from '../src/api';
import {FailureRow} from '../src/pages/Tasks';
import '../src/styles.css';

window.fetch=async()=>{throw Error('Network prohibited in synthetic preview');};
for(const key of Object.keys(api)) (api as any)[key]=async()=>{throw Error('Unmocked API: '+key);};
const views=['business','details'].map((kind,i)=>({job_id:'prior-'+i,kind,requests:{model:1},
  version:'synthetic-version',can_recover:true,reason:'',mode:'resume',job_status:'failed',
  requires_duplicate_cost_consent:true,notice:'只恢复此任务中明确确认的一条模型请求；其他待核请求保留，普通新更新仍受阻。'}));
api.recoveryProject=async()=>({items:views} as any);
api.recoveryPreview=async(_pid,id)=>views.find(v=>v.job_id===id) as any;
let approvals=0;
api.recover=async(_pid,id)=>{approvals++;document.querySelector('#audit')!.textContent=`合成确认次数：${approvals} · ${id}`;return {job_id:id,status:'queued'} as any;};
const job={id:'blocked',project_id:'synthetic',project_name:'合成公司',label:'收集公开信息',kind:'external',status:'failed',
  error:'request_outcome_unknown',finished_at:new Date().toISOString(),dismissed:false,calls:0,unknown:0,
  runtime:{state:'blocked',label:'本次未执行，先前请求结果待核',blocking_jobs:['prior-0','prior-1']},
  advice:{action:'none',why:'本次未执行，先前请求结果待核。本任务没有发送新的外部请求，已有结果保留。',
  todo:'查看先前请求，核对服务商记录后按对应任务恢复；不必反复点击立即更新。'}} as TaskJob;
createRoot(document.getElementById('root')!).render(<MemoryRouter><main style={{padding:'32px',maxWidth:1100,margin:'auto'}}>
  <h1>隔离预览 · 请求恢复</h1><p id="audit">合成确认次数：0</p>
  <section className="card card-pad"><ul className="fix-list"><FailureRow j={job} onChanged={()=>{}}/></ul></section>
</main></MemoryRouter>);

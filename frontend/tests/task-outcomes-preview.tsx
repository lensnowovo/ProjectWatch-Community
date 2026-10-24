// Synthetic UI interaction harness. Every API used here is replaced; no live instance.
import React from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import TasksPage from '../src/pages/Tasks';
import { api, type TaskJob } from '../src/api';
import '../src/styles.css';

const job = {id:'synthetic-partial',project_id:'synthetic',project_name:'合成技术',kind:'commercial',
  label:'专业来源',status:'done',error:'',created_at:new Date().toISOString(),finished_at:new Date().toISOString(),
  runtime:{state:'partial',label:'部分来源已更新，其余暂不可用'},calls:1,unknown:0,dismissed:false} as TaskJob;
api.tasks = async () => ({jobs:[{...job}],documents:[],worker:'running',model_ready:true,
  summary:{running:0,queued:0,done_24h:0,partial_24h:1,failed_24h:0}});
api.dismissTask = async () => { job.dismissed=true; };

createRoot(document.getElementById('root')!).render(<BrowserRouter>
  <main style={{maxWidth:1100,margin:'24px auto',padding:24}}>
    <p>隔离验收 · 合成数据 · 收起提醒仅修改本页内存</p>
    <TasksPage projects={[{id:'synthetic',name:'合成技术'}]} />
  </main>
</BrowserRouter>);

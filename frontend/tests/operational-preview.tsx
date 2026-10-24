// Synthetic visual fixtures only. No live project, settings, model or search.
import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { MotionConfig } from 'motion/react';
import { MonitorLoop } from '../src/monitorLoop';
import { Progress } from '../src/pages/Project';
import { FailureRow } from '../src/pages/Tasks';
import { UploadProgress } from '../src/upload';
import { QuestionAnalysis } from '../src/questionAnalysis';
import { WorkIndicator, useVisiblePoll } from '../src/Operational';
import { useAsync } from '../src/ui';
import { api } from '../src/api';
import '../src/styles.css';

// All data dependencies are synthetic; opening this preview never reaches an API.
api.signals = async () => ({cadence:{hours:24}});

const states = ['running','mixed','waiting_response','queued','partial','dismissed','failed','needs_confirmation','stalled','recovering','paused','idle','agent'];
const labels = {running:'正在执行',mixed:'执行中 + 另一类未完成',waiting_response:'等待模型响应',queued:'排队中',partial:'部分完成',dismissed:'已收起提醒的未完成任务',failed:'未完成',needs_confirmation:'请求结果待确认',stalled:'长时间无进展',recovering:'等待恢复',paused:'已暂停',idle:'监控待命',agent:'Agent 按需更新'};
const phases = {collect:'检索公司新闻',read:'阅读报道正文',judge:'更新跟踪问题',revise:'修订监控方案',none:'阶段未记录'};
const probe = {calls:0,active:0,max:0};
function PollProbe() {
  const {data,reload} = useAsync(async()=>{
    probe.calls++; probe.active++; probe.max=Math.max(probe.max,probe.active);
    await new Promise(resolve=>setTimeout(resolve,100));
    probe.active--; return probe.calls;
  },[]);
  useVisiblePoll(reload,1000);
  return <div><button onClick={()=>{reload();reload();}} className="btn">重复刷新测试</button><output data-testid="poll-probe">{JSON.stringify({...probe,data})}</output></div>;
}
function Preview() {
  const [state,setState]=useState('running');
  const [phase,setPhase]=useState('judge');
  const [dark,setDark]=useState(false);
  const [tick,setTick]=useState(0);
  const [completed,setCompleted]=useState('2026-10-18T00:00:00Z');
  const ph = phase==='none'?null:phase;
  const own = state==='mixed'?'running':state==='dismissed'?'failed':state;
  const failedLike = ['failed','needs_confirmation'].includes(own);
  const job = {id:'synthetic-job',project_id:'synthetic',project_name:'合成技术',label:'补全外部信息',dismissed:state==='dismissed',kind:'external_followup',status:failedLike?'failed':own==='partial'?'done':own==='queued'||own==='paused'?'queued':'running',created_at:'2026-10-19T00:00:00Z',finished_at:failedLike||own==='partial'?'2026-10-19T00:05:00Z':null,
    stage:phases[phase],error:'',runtime:{state:own,label:own==='partial'?'已有结果保留，部分来源暂不可用':labels[own],phase:ph,progress_at:new Date(Date.parse('2026-10-19T00:00:00Z')+tick*1000).toISOString()}};
  const older = {id:'synthetic-old',kind:'landscape',status:'failed',created_at:'2026-10-18T22:00:00Z',finished_at:'2026-10-18T22:10:00Z',
    stage:'阅读报道正文',error:'合成：解析超时',runtime:{state:'failed',label:'未完成',phase:'read',progress_at:null}};
  const detail = {project:{id:'synthetic',name:'合成技术',status:state==='paused'?'paused':'active',activated:true,execution_mode:state==='agent'?'agent':'api'},
    jobs:['idle','agent'].includes(state)?[]:state==='mixed'?[job,older]:[job],documents:[],counts:{company:12,industry:8},execution:{analysis:'running',parsing:'running'},observations:{items:[]},event_states:{items:[]},
    monitoring_progress:{state:state==='idle'?'observed':'running',completed_at:completed,round:4}};
  return <main className="page-workspace" style={{maxWidth:1140,margin:'24px auto',padding:24}}>
    <p className="muted small">隔离验收 · 所有数据均为合成 · 不连接实际实例</p>
    <h1>合成技术</h1>
    <div className="filters">
      <label>运行状态 <select aria-label="运行状态" value={state} onChange={e=>setState(e.target.value)}>{states.map(s=><option key={s} value={s}>{labels[s]}</option>)}</select></label>
      <label>执行阶段 <select aria-label="执行阶段" value={phase} onChange={e=>setPhase(e.target.value)}>{Object.keys(phases).map(p=><option key={p} value={p}>{phases[p]}</option>)}</select></label>
      <button className="btn" onClick={()=>setTick(t=>t+1)}>模拟一次进展</button>
      <button className="btn" onClick={()=>{setState('idle');setCompleted(new Date().toISOString());}}>模拟本轮完成</button>
      <button className="btn" onClick={()=>{setDark(!dark); document.documentElement.dataset.theme=dark?'light':'dark';}}>切换深浅色</button>
    </div>
    <Progress detail={detail} onReload={()=>{}} />
    <MonitorLoop detail={detail} plan={{coverage:{company:[{state:'on'},{state:'on'}]},items:[],week:{fresh:2},state:{revisions:3},health:{items:4,hit_30d:2}}} />
    {(failedLike||own==='partial') && <section className="card card-pad" style={{marginTop:24}}><h3>任务原因（合成空错误回执）</h3><ul className="fix-list"><FailureRow j={job} onChanged={()=>{}} /></ul></section>}
    <section className="card card-pad" style={{marginTop:24}}><h3>资料处理</h3>
      <UploadProgress stale={state==='recovering'} document={{steps:[{state:'done',label:'接收'},{state:state==='running'?'running':'waiting',label:'识别文字',note:state==='running'?'正在读取正文':'等待后台开始'}]}} />
    </section>
    <div className="op-local"><WorkIndicator state={state} label={`综合判断 · ${labels[state]}`} /><span className="muted small">当前显示上次结果</span></div>
    <QuestionAnalysis detail={{...detail,jobs:[]}} view={{record:null,generation_available:true,job:null,model_ready:true}} error={null} goto={()=>{}} />
    <details><summary>只读轮询验收</summary><PollProbe /></details>
  </main>;
}
createRoot(document.getElementById('root')!).render(<BrowserRouter><MotionConfig reducedMotion="user"><Preview /></MotionConfig></BrowserRouter>);

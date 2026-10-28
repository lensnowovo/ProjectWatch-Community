import assert from 'node:assert/strict';
import { createRequire, Module } from 'node:module';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
const require = createRequire(new URL('../package.json', import.meta.url));
const { buildSync } = require('esbuild');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
function load(path) {
  const entry = fileURLToPath(new URL(path, import.meta.url));
  const mod = new Module(entry); mod.paths = require.resolve.paths('react');
  mod._compile(buildSync({entryPoints:[entry], bundle:true, write:false, platform:'node', format:'cjs', packages:'external', loader:{'.css':'empty'}}).outputFiles[0].text, entry);
  return mod.exports;
}
const { projectWork, unresolvedJobs, isMoving } = load('../src/workState.ts');

test('external job label does not pretend every refresh is the first round', () => {
  const { JOB_LABEL } = load('../src/workState.ts');
  assert.equal(JOB_LABEL.external, '收集公开信息');
});
test('completed insight refreshes every judgment even without a new collection timestamp', () => {
  const { insightRevision } = load('../src/workState.ts');
  const before = {project:{last_external_at:'2026-10-19T01:00:00Z'}, jobs:[{id:'i',kind:'insight',status:'running'}]};
  const after = {...before, jobs:[{...before.jobs[0],status:'done',finished_at:'2026-10-19T02:00:00Z'}]};
  assert.notEqual(insightRevision(before), insightRevision(after));
  assert.equal(insightRevision(after), insightRevision({...after, jobs:[...after.jobs,{id:'x',kind:'commercial',status:'running'}]}));
});
const { MonitorLoop, loopTransition, loopOutcome } = load('../src/monitorLoop.tsx');
const { WorkIndicator } = load('../src/Operational.tsx');
const { UploadProgress } = load('../src/upload.tsx');
const { FailureRow } = load('../src/pages/Tasks.tsx');
const { MemoryRouter } = require('react-router-dom');
const base = () => ({ project:{id:'synthetic',activated:true,status:'active'}, jobs:[], documents:[],counts:{},execution:{analysis:'running',parsing:'running'} });
const job = (state='running', phase='collect') => ({id:'job',kind:'external_followup',status:state==='queued'?'queued':state==='failed'?'failed':'running',created_at:'2026-10-19T00:00:00Z',stage:'实际执行步骤',runtime:{state,label:state,phase}});
const render = (component, props) => renderToStaticMarkup(React.createElement(component, props));

test('an unresolved historical receipt survives unrelated later success without claiming success', () => {
  const held = {...job('done'), id:'old',status:'done',finished_at:'2026-10-19T01:00:00Z',
    runtime:{state:'needs_confirmation',label:'模型服务出错，等待确认恢复'}};
  const newer = {...held,id:'new',finished_at:'2026-10-19T02:00:00Z',runtime:{state:'done',label:'已完成'}};
  assert.deepEqual(unresolvedJobs([held,newer]).map(j=>j.id),['old']);
  const { FinishedRow } = load('../src/pages/Tasks.tsx');
  const html = render(MemoryRouter,{children:React.createElement(FinishedRow,{j:held})});
  assert.match(html,/仍有请求待确认/);
  assert.doesNotMatch(html,/tag-ok/);
});

for (const phase of ['collect','read','judge','revise']) test(`actual ${phase} phase overrides job kind`, () => {
  const detail = {...base(),jobs:[job('running',phase)]};
  assert.equal(projectWork(detail).phase,phase);
  const html=render(MonitorLoop,{detail});
  assert.equal((html.match(/aria-current="step"/g)||[]).length,1);
});
for (const state of ['queued','paused','blocked','stalled','recovering','needs_confirmation','failed']) test(`${state} has no moving work indicator or active step`, () => {
  const html=render(MonitorLoop,{detail:{...base(),jobs:[job(state)]}});
  assert.doesNotMatch(html,/aria-current="step"|ml-halo|op-moving/);
  assert.equal(isMoving(state),false);
});
test('unknown phases are not guessed from the job type',()=>{
  const detail={...base(),jobs:[job('running',null)]};
  assert.equal(projectWork(detail).phase,null);
  assert.doesNotMatch(render(MonitorLoop,{detail}),/aria-current="step"/);
});
test('queued document does not steal a running search stage',()=>{
  assert.equal(projectWork({...base(),jobs:[job()],documents:[{status:'queued'}]}).phase,'collect');
});
test('waiting response is distinct from running and recovery does not animate',()=>{
  assert.match(render(WorkIndicator,{state:'waiting_response',label:'等待模型响应'}),/op-waiting_response op-moving/);
  assert.doesNotMatch(render(WorkIndicator,{state:'recovering',label:'等待恢复'}),/op-moving/);
});
test('unrelated success never hides failure; same-kind later success does',()=>{
  const failed={...job('failed'),finished_at:'2026-10-19T01:00:00Z'};
  const done={...failed,id:'two',kind:'brief',status:'done',finished_at:'2026-10-19T02:00:00Z',runtime:{state:'done'}};
  assert.equal(unresolvedJobs([done,failed]).length,1);
  assert.equal(unresolvedJobs([{...done,kind:failed.kind},failed]).length,0);
  assert.equal(unresolvedJobs([{...failed,dismissed:true}]).length,0);
});
test('paused monitoring does not pretend an in-flight request was cancelled',()=>{
  const detail={...base(),project:{...base().project,status:'paused'},jobs:[job()]};
  assert.equal(projectWork(detail).moving,true);
  assert.match(render(MonitorLoop,{detail}),/在途任务尚未结束/);
});
test('idle and Agent modes are quiet and enabled sources are not called running',()=>{
  const html=render(MonitorLoop,{detail:base(),plan:{coverage:{company:[{state:'on'}]}}});
  assert.match(html,/已启用 1 个来源/); assert.doesNotMatch(html,/来源在跑|op-moving|ml-halo/);
  assert.equal(projectWork({...base(),project:{...base().project,execution_mode:'agent'}}).state,'agent');
});
test('upload received is not analysis complete and stale progress stops animating',()=>{
  assert.match(render(UploadProgress,{stale:false}),/已接收.*等待处理状态/);
  const document={steps:[{label:'接收',state:'done'},{label:'解析',state:'running',note:'识别文字'}]};
  assert.match(render(UploadProgress,{document,stale:false}),/解析.*识别文字/);
  assert.doesNotMatch(render(UploadProgress,{document,stale:true}),/op-moving|已纳入项目/);
  assert.match(render(UploadProgress,{document:{steps:[{state:'done'}]},stale:false}),/已纳入项目/);
});
test('failed status reads retain content but never claim work is still running',()=>{
  const detail={...base(),status_stale:true,jobs:[job()]};
  assert.equal(projectWork(detail).moving,false);
  const html=render(MonitorLoop,{detail});
  assert.match(html,/状态暂时无法读取/);
  assert.doesNotMatch(html,/op-moving|aria-current="step"/);
});
test('an unresolved failure of another kind never hides executing work',()=>{
  const failed={...job('failed','read'),id:'old',kind:'landscape',error:'解析超时',finished_at:'2026-10-19T01:00:00Z'};
  const detail={...base(),jobs:[{...job('running','collect'),created_at:'2026-10-19T02:00:00Z'},failed]};
  const work=projectWork(detail);
  assert.equal(work.moving,true); assert.equal(work.phase,'collect');
  assert.equal(work.alongside?.job.id,'old'); assert.equal(work.additional,0);
  const html=render(MonitorLoop,{detail});
  assert.equal((html.match(/aria-current="step"/g)||[]).length,1);
  assert.match(html,/has-other-stop/); assert.match(html,/另有任务未完成/);
  assert.doesNotMatch(html,/ml-stage stop stop-failed|停在这里/);
  assert.match(html,/另有 整理竞争与行业/); assert.match(html,/停在「解析」/);
});
test('stop reason is anchored to the reported phase and unknown places stay unknown',()=>{
  const html=render(MonitorLoop,{detail:{...base(),jobs:[{...job('failed','judge'),error:''}]}});
  assert.match(html,/停在「判断」/); assert.match(html,/任务未完成，已有结果保留/);
  const unknown=render(MonitorLoop,{detail:{...base(),jobs:[job('failed',null)]}});
  assert.doesNotMatch(unknown,/停止位置未记录|停止原因未记录|ml-stage stop/);
});

test('partial completion is neither a failure nor a generic rerun prompt',()=>{
  const partial={...job(),status:'done',kind:'commercial',error:'',runtime:{state:'partial',label:'部分来源已更新，其余暂不可用',phase:null}};
  const html=renderToStaticMarkup(React.createElement(MemoryRouter,null,React.createElement(FailureRow,{j:{...partial,project_id:'synthetic',project_name:'合成公司',label:'专业来源'},onChanged(){}})));
  assert.match(html,/部分完成/);assert.match(html,/部分来源已更新/);
  assert.doesNotMatch(html,/>失败<|重新运行|停止原因未记录/);
  const loop=render(MonitorLoop,{detail:{...base(),jobs:[partial]}});
  assert.match(loop,/部分来源已更新/);
  assert.doesNotMatch(loop,/停止位置未记录|停止原因未记录|aria-current="step"/);
});

test('dismissing a reminder does not complete a chain or relight it',()=>{
  const dismissed={...job('needs_confirmation',null),status:'failed',dismissed:true,error:''};
  const detail={...base(),jobs:[dismissed]};
  assert.equal(unresolvedJobs([dismissed]).length,0);
  assert.equal(projectWork(detail).state,'needs_confirmation');
  const html=render(MonitorLoop,{detail});
  assert.match(html,/提醒已收起.*仍未完成/);
  assert.match(html,/结果不明/);
  assert.doesNotMatch(html,/监控待命|ml-halo|停止原因未记录/);
  const running={...job('running','judge'),id:'live',kind:'brief'};
  assert.equal(projectWork({...detail,jobs:[running,dismissed]}).job.id,'live');
});

test('later success clears ignored failure and source gaps stay visible without a job',()=>{
  const failed={...job('failed'),dismissed:true,finished_at:'2026-10-19T01:00:00Z'};
  const done={...failed,id:'later',status:'done',runtime:{state:'done'},finished_at:'2026-10-19T02:00:00Z'};
  assert.equal(projectWork({...base(),jobs:[done,failed]}).state,'idle');
  const detail={...base(),monitoring_progress:{state:'partial',source_failures:3,pending_items:0}};
  assert.equal(projectWork(detail).state,'partial');
  assert.match(render(MonitorLoop,{detail}),/部分来源未覆盖/);
  assert.equal(projectWork({...detail,project:{...detail.project,status:'paused'}}).state,'paused');
});
test('stale reads do not mark a stop place or light the loop',()=>{
  const html=render(MonitorLoop,{detail:{...base(),status_stale:true,jobs:[job('failed','read')]}});
  assert.doesNotMatch(html,/ml-stage stop|aria-current="step"/);
});
test('stopping work is not called in progress',()=>{
  const detail={...base(),execution:{analysis:'stopping',parsing:'running'},jobs:[{...job('running','read'),runtime:undefined,stage:'阅读报道正文'}]};
  // Without a server runtime, the phase is unknown; the label still must not claim progress.
  assert.doesNotMatch(render(MonitorLoop,{detail}),/进行中/);
});
test('the loop names the real round and calls cadence a check interval, not a round length',()=>{
  const html=render(MonitorLoop,{detail:{...base(),monitoring_progress:{completed_at:null,round:3}}});
  assert.match(html,/第 3 轮/); assert.doesNotMatch(html,/小时一轮/);
  assert.doesNotMatch(render(MonitorLoop,{detail:base()}),/第 \d+ 轮/);
});

const snap = (phase, extra={}) => ({project:'one',job:'a',phase,state:'running',round:4,...extra});
test('lights cross only adjacent observed phases in the same job and round',()=>{
  assert.deepEqual(loopTransition(snap('collect'),snap('read')),{from:0,to:1});
  for(const next of [snap('judge'),snap('collect'),snap('read',{job:'b'}),snap('read',{project:'two'}),snap('read',{round:5})])
    assert.equal(loopTransition(snap('collect'),next),null);
});
test('backtracking is not a new round; return light requires round advance and a completed prior job',()=>{
  assert.equal(loopTransition(snap('read'),snap('collect')),null);
  assert.equal(loopTransition(snap('revise'),snap('collect')),null);
  assert.equal(loopTransition(snap('revise'),snap('collect',{job:'b',round:5})),null);
  assert.deepEqual(loopTransition(snap('revise'),snap('collect',{job:'b',round:5}),true),{from:3,to:0});
  assert.equal(loopTransition(snap('revise'),snap('collect',{job:'b',round:6}),true),null);
});
test('waiting, stale, stopping and resumed reads do not earn a transition',()=>{
  for(const state of ['waiting_response','unavailable','stopping','failed','queued']) {
    assert.equal(loopTransition(snap('collect'),snap('read',{state})),null);
    assert.equal(loopTransition(snap('collect',{state}),snap('read')),null);
  }
});
test('a completed timestamp alone never certifies a full round',()=>{
  for(const state of ['partial','running','failed','unobserved','paused',undefined]) {
    const detail={...base(),monitoring_progress:{state,completed_at:'2026-01-01T00:00:00Z'}};
    assert.equal(loopOutcome(detail).completedAt,null);
    assert.doesNotMatch(render(MonitorLoop,{detail}),/最近一轮处理已收口|本轮处理已收口/);
  }
  assert.equal(loopOutcome({...base(),status_stale:true,monitoring_progress:{state:'observed',completed_at:'2026-01-01T00:00:00Z'}}).completedAt,null);
});
test('completion has an explicit read-only result link and no invented collection count',()=>{
  const detail={...base(),monitoring_progress:{state:'observed',completed_at:'2026-01-01T00:00:00Z'}};
  const html=render(MonitorLoop,{detail});
  assert.match(html,/最近一轮处理已收口/); assert.match(html,/\/projects\/synthetic\?tab=external/);
  assert.doesNotMatch(html,/本轮新增|ml-completed-fresh/);
  assert.equal(loopOutcome({...detail,monitoring_progress:{state:'observed',completed_at:'invalid'}}).completedAt,null);
  assert.equal(loopOutcome({...detail,monitoring_progress:{state:'observed',completed_at:'2999-01-01'}}).completedAt,null);
});
test('revision outputs are separate from monitoring completion and weekly cumulative figures',()=>{
  const plan={state:{revisions:9,last:{at:'2026-01-01',added:2,noted:3}},week:{fresh:90}};
  assert.equal(loopOutcome(base(),plan).completedAt,null);
  const html=render(MonitorLoop,{detail:base(),plan});
  assert.match(html,/最近一次方案修订：新增 2 个关注点，更新 3 条认识/);
  assert.doesNotMatch(html,/该轮新增|本轮新增 90/);
});
test('active work is named at the top, not duplicated in the loop footer',()=>{
  const html=render(MonitorLoop,{detail:{...base(),jobs:[job('running','revise')]}});
  assert.match(html,/进行中/); assert.doesNotMatch(html,/补全外部信息|ml-orbit-repeat/);
  assert.match(html,/步骤说明与明细/);
});

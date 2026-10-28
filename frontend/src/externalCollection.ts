import type { CollectionReport, ProjectDetail } from './api';
import { jobWorkState } from './workState';

/** Saved per-source execution; these labels never claim current live activity. */
export function collectionExecutionLabel(execution: string, active = false) {
  if (execution === 'running' && active) return '正在收集';
  return ({success: '检索完成', empty: '已检索，暂无结果', partial: '部分完成',
    failed: '检索失败', unknown: '结果待确认', budget_deferred: '预算用尽，已延后',
    not_due: '未到更新时间', not_configured: '未配置', not_integrated: '未接入',
    needs_scope: '查询范围待确定', disabled: '暂不采集', not_applicable: '不适用',
    pending: '尚未执行', running: '尚未收口', not_in_round: '本轮未纳入',
    unavailable: '回执无法核验', scope_resolved: '已转入具体查询'} as Record<string, string>)[execution] ?? '状态待核对';
}

export function collectionChannelExecution(snapshot: CollectionReport, key: string) {
  if (snapshot.view_state === 'unavailable') return 'unavailable';
  if (snapshot.view_state === 'awaiting_plan') return 'pending';
  return snapshot.channels[key]?.execution ?? 'not_in_round';
}

/** Project-level status, never proof that a particular source is being queried. */
export function externalCollection(detail: ProjectDetail, channels?: string[]) {
  const jobs = (detail.jobs ?? []).filter(j => j.kind === 'external' || j.kind === 'external_followup');
  const states = jobs.filter(j => j.status === 'running' || j.status === 'queued').map(j => ({job:j,state:jobWorkState(detail,j)}));
  if (detail.status_stale) return {label:'状态待更新',description:'暂时无法确认采集状态。'};
  if (detail.execution?.health?.restart_required) return {label:'采集受阻',description:'后台需要恢复；已有资料保留。'};
  if (jobs.some(j => j.runtime?.state === 'needs_confirmation')) return {label:'结果待确认',description:'有请求结果尚未确认，不会自动重发。'};
  if (detail.project.status === 'paused') return {label:'监控已暂停',description:'监控已暂停；不会因查看本栏目而启动采集。'};
  const live = detail.collection_report;
  if (channels?.length && live?.schema_version === 1) {
    const outcomes = channels.map(key => collectionChannelExecution(live, key));
    const execution = outcomes.includes('unknown') ? 'unknown' : outcomes.includes('running') ? 'running'
      : outcomes.every(value => value === outcomes[0]) ? outcomes[0] : 'partial';
    const task = states.find(s => s.job.id === live.job_id);
    const active = live.active && !!task && ['running','waiting_response'].includes(task.state);
    const label = collectionExecutionLabel(execution, active);
    return {label, description:`${live.job_status === 'done' ? '最近一轮' : '本轮'}：${label}。本栏目目前暂无收录。`};
  }
  const moving = states.find(s => s.state === 'running' || s.state === 'waiting_response');
  if (moving) return moving.job.runtime?.phase === 'collect'
    ? {label:'正在收集',description:'项目公开信息正在收集；不代表正在查询本栏目所有来源，也不保证一定收录到结果。'}
    : {label:'研究进行中',description:'项目外部研究正在推进；当前无法确认是否正在采集本栏目。'};
  if (states.some(s => s.state === 'stopping')) return {label:'正在结束',description:'本轮外部研究正在结束，尚未收录的内容不代表不存在。'};
  if (states.some(s => ['blocked','stalled','recovering','offline','waiting_restart'].includes(s.state))) return {label:'采集待恢复',description:'外部任务尚未恢复运行，已有资料保留。'};
  if (states.some(s => s.state === 'queued')) return {label:'等待收集',description:'外部任务正在排队，尚未开始采集。'};
  const snapshot = detail.plan?.collection_report;
  if (channels?.length && snapshot?.schema_version === 1 && snapshot.job_id === detail.plan?.round_id) {
    const outcomes = channels.map(key => detail.plan?.last_report?.[key]?.execution ?? 'not_in_round');
    const execution = outcomes.includes('unknown') ? 'unknown'
      : outcomes.every(value => value === outcomes[0]) ? outcomes[0] : 'partial';
    return {label:collectionExecutionLabel(execution), description:`最近一轮：${collectionExecutionLabel(execution)}。本栏目目前暂无收录。`};
  }
  return {label:'暂无收录',description:'目前没有收录到本栏目信息，不代表没有相关进展，也不代表已完成全面检索。'};
}

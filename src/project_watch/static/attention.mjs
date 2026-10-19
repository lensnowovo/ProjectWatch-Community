// Conservative first pass: source-marked current news, not investment-risk scoring.
export function selectAttention(items) {
  const seen=new Set();
  return [...items].filter(i=>i.kind==='news'&&i.freshness==='current'&&i.title&&/^https?:\/\//.test(i.url||''))
    .sort((a,b)=>(b.time||'').localeCompare(a.time||''))
    .filter(i=>{const key=i.project_id+'|'+i.url;if(seen.has(key))return false;seen.add(key);return true;})
    .slice(0,5).map(i=>({...i,reason:'来源标记为近期消息 · 按时间排列，尚未核实'}));
}
export function monitorCopy(plans=[],error=false){
  if(error)return '暂时无法读取监控状态。';
  const enabled=plans.filter(p=>p.enabled);
  if(!enabled.length)return plans.length?'已暂停公开动态检查，已有消息仍可阅读。':'尚未开启公开动态。';
  if(enabled.some(p=>['unreachable','rate_limited'].includes(p.last_run_status)))return '部分来源最近检查未成功，消息可能不完整。';
  if(enabled.every(p=>p.last_run_status==='no_results'))return '截至各来源上次成功检查，未发现新增相关消息。';
  if(enabled.some(p=>p.last_run_status==='not_run'))return '已开启，部分来源尚未完成首次检查。';
  return '已开启定期检查；公开消息不代表已核实事实。';
}

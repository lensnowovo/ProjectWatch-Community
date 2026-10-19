// Read-only presentation helpers. No claims are confirmed and no jobs are started.
export function externalNavigation(state) {
  const live=!state.externalError&&state.external?.project_id===state.current?.id;
  const groups=live?state.external.categories||[]:[];
  return [['company','公司',['company']],['industry','行业',['sector','industry']],['competitors','竞品',['competitors']],['policy','政策',['policy']]].map(([key,title,keys])=>{
    const parts=keys.map(k=>groups.find(g=>g.key===k));
    const plans=parts.flatMap(g=>g?.plans||[]);
    const items=parts.flatMap(g=>g?.items||[]).filter(i=>i.project_id===state.current?.id);
    const states=parts.map(g=>g?.coverage||'unavailable');
    const configured=states.filter(s=>!['not_configured','unavailable'].includes(s));
    const coverage=states.includes('failed')?'failed':states.includes('pending')?'pending':
      configured.length&&states.some(s=>['not_configured','unavailable'].includes(s))?'partial':
      states.includes('checked')&&states.includes('paused')?'partial':
      states.every(s=>s==='paused')?'paused':states.every(s=>s==='not_configured')?'not_configured':
      states.every(s=>s==='checked')?'checked':'unavailable';
    const status=({failed:'部分检查失败',pending:'正在检查',partial:'部分范围已覆盖',paused:'已暂停',not_configured:'尚未开启',checked:items.length?`${items.length} 条消息`:'本轮未命中',unavailable:'状态暂不可用'})[coverage];
    const successes=plans.map(p=>p.last_success_at).filter(Boolean).map(value=>new Date(typeof value==='number'&&value<1e12?value*1000:value)).filter(d=>Number.isFinite(d.getTime())).sort((a,b)=>b-a);
    return {key,title,keys,plans,items,coverage,status,lastSuccess:successes[0]?.toISOString()||null};
  });
}

export function compactStoryText(saved,fallback='') {
  const relationship=saved?.analysis?.relationship;
  return relationship?.text?{label:'AI 关联 · 待核实',text:relationship.text}:{label:'公开线索',text:fallback||'具体内容见原文，项目影响尚未核实。'};
}

export function materialSelection(sources,projectId,{query='',topic=''}={}) {
  const term=query.trim().toLocaleLowerCase();
  return sources.filter(source=>source.project_id===projectId&&(!topic||source.topic===topic)&&(!term||`${source.reference} ${source.topic||''}`.toLocaleLowerCase().includes(term)))
    .sort((a,b)=>(b.created_at||'').localeCompare(a.created_at||'')||String(a.id).localeCompare(String(b.id)));
}

export function internalReceiptSummary(state) {
  const sources=(state.sources||[]).filter(s=>s.project_id===state.current?.id),result={total:sources.length,readable:0,processing:0,failed:0,ocr:0};
  for(const source of sources){
    const job=(state.jobs||[]).filter(j=>j.project_id===state.current.id&&j.source_id===source.id).sort((a,b)=>(b.created_at||'').localeCompare(a.created_at||''))[0];
    if(['completed','succeeded'].includes(job?.status))result.readable++;
    else if(['queued','running','processing'].includes(job?.status))result.processing++;
    else if(job?.status==='failed')result.failed++;
    else if(job?.status==='needs_ocr')result.ocr++;
  }
  result.copy=result.failed?'部分资料解析未完成，已有内容仍可阅读。':result.processing?'资料正在处理，完成后会更新这里。':result.ocr?'部分页面还需要文字识别，已提取的正文可先阅读。':result.total===1&&result.readable?'第一份资料已可阅读；收到后续材料后，可查看新旧内容对照。':result.readable?'已收到的资料可阅读；下方分别保留材料补充与已核对变化。':result.total?'资料已收到，阅读状态尚待更新。':'收到公司材料后，拖入即可开始。';
  return result;
}

export function trackDisclosure(node,expanded,key) {
  node.dataset.topic=key;node.open=expanded.has(key);
  node.addEventListener('toggle',()=>{if(!node.isConnected)return;if(node.open)expanded.add(key);else expanded.delete(key);});
}

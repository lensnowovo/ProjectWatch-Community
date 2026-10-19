// Generated company reading is separate from confirmed facts and human notes.
export function compiledReading(reading, projectId, sources) {
  if (!projectId || reading?.project_id !== projectId || reading.compiled?.project_id !== projectId) return [];
  const live = new Map(sources.filter(s=>s.project_id===projectId).map(s=>[s.id,s]));
  const groups = new Map();
  const batches=[...(reading.compiled.batches||[])].sort((a,b)=>(live.get(b.source_id)?.created_at||'').localeCompare(live.get(a.source_id)?.created_at||'')||b.id.localeCompare(a.id));
  for (const batch of batches) {
    const source = live.get(batch.source_id);
    if (!source || source.content_sha256 !== batch.source_version) continue;
    for (const item of batch.items) {
      if (item.origin!=='ai_synthesis' || item.review_status!=='unreviewed' || !item.source_refs?.length ||
          !item.source_refs.every(r=>r.source_id===source.id && r.version===source.content_sha256 && r.locator && r.quote)) continue;
      if (!groups.has(item.topic)) groups.set(item.topic,{topic:item.topic,title:item.title,items:[]});
      const items=groups.get(item.topic).items;
      const prior=items.find(i=>i.text===item.text && i.as_of===item.as_of);
      if(prior){
        for(const ref of item.source_refs)if(!prior.source_refs.some(r=>r.source_id===ref.source_id&&r.locator===ref.locator&&r.quote===ref.quote))prior.source_refs.push({...ref});
      }else items.push({...item,source_refs:item.source_refs.map(r=>({...r})),recorded_at:batch.recorded_at,batch_id:batch.id});
    }
  }
  const order=['business','stage','products','customers','team','operating','financial','milestones','risks'];
  return [...groups.values()].sort((a,b)=>order.indexOf(a.topic)-order.indexOf(b.topic));
}

export function readingStatusCopy(background) {
  if(!background)return '';
  const labels={model_missing:'尚未连接 AI，原件可以照常阅读。',consent_required:'开启自动整理后，新材料会补充到公司情况。',paused:'自动整理已暂停，已有内容仍可阅读。',budget_reached:'本次整理额度已用完，已有内容仍可阅读。',model_limit:'AI 调用额度已用完，请在设置中检查。',unavailable:'后台整理暂未启动，原件仍可阅读。'};
  if(labels[background.reason])return labels[background.reason];
  const counts=background.counts||{};
  if(counts.dispatched||counts.running||counts.queued)return '正在整理授权材料，完成后会更新在这里。';
  if(counts.uncertain||counts.failed)return '部分内容未能完成整理，未自动重复调用。原件仍可阅读。';
  return '自动整理已开启；新材料会在后台处理。';
}

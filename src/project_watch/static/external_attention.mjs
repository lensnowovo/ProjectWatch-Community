// Presentation only. An investment-related topic is not verified project impact.
export function attentionFor(item){
  const a=item?.attention;
  return a?.version==='headline-topic-v1'&&a.basis==='headline_topic_only'&&a.level==='read_first'&&a.event_verified===false&&a.impact===null?a:null;
}
export function newsMode(dataset,projectId){return dataset.focusProject===projectId&&dataset.newsMode==='all'?'all':'priority';}
export function readingNews(data,projectId,mode='priority'){
  if(data?.project_id!==projectId)return [];
  const seen=new Set();
  const items=(data.categories||[]).flatMap(g=>g.items||[]).filter(i=>{
    if(i.project_id!==projectId||!i.id||seen.has(i.id))return false;
    seen.add(i.id);return true;
  });
  return items.sort((a,b)=>(mode==='all'?0:Number(!!attentionFor(b))-Number(!!attentionFor(a)))||String(b.published_at||'').localeCompare(String(a.published_at||''))||a.id.localeCompare(b.id));
}

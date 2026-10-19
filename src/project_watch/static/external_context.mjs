export function visibleInternalContext(item,state) {
  const context=item.internal_context;
  if(state.externalError||context?.project_id!==state.current?.id||context.status!=='shared_term_only'||context.relationship_verified!==false||context.impact!==null||context.term!==item.match_term)return [];
  const live=new Map((state.sources||[]).filter(s=>s.project_id===state.current.id).map(s=>[s.id,s]));
  return (context.materials||[]).filter(ref=>ref.project_id===state.current.id&&ref.version&&live.get(ref.source_id)?.content_sha256===ref.version&&ref.quote&&ref.locator&&ref.fragment_id);
}
const el=(tag,cls,text)=>{const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=text;return node;};
export function appendInternalContext(article,item,state,{openSource,expanded=new Set()}) {
  const refs=visibleInternalContext(item,state);if(!refs.length)return;
  const context=item.internal_context,details=el('details','external-materials');
  details.dataset.topic=`external-materials-${item.id}`;details.open=expanded.has(details.dataset.topic);
  details.addEventListener('toggle',()=>{if(details.open)expanded.add(details.dataset.topic);else expanded.delete(details.dataset.topic);});
  details.append(el('summary','',`相关内部材料 · ${refs.length} 份`));
  details.append(el('p','reading-note',`原件中也提到“${context.term}”`));
  for(const ref of refs){
    const section=el('section','external-material');
    section.append(el('h4','',ref.reference),el('p','reading-note',`${new Date(ref.received_at).toLocaleDateString('zh-CN')} 收到 · 材料所述期间未核实`),el('blockquote','',ref.quote));
    const link=el('button','text-button',`${ref.locator} · 查看原件 ↗`);link.type='button';link.onclick=()=>openSource(state.sources.find(s=>s.id===ref.source_id),ref.locator);section.append(link);
    if(ref.excerpt_clipped)section.append(el('p','reading-note','此处为局部摘录，完整上下文见原件。'));
    details.append(section);
  }
  if(context.total_materials>refs.length)details.append(el('p','reading-note',`另有 ${context.total_materials-refs.length} 份匹配材料，此处展示最近收到的材料。`));
  details.append(el('p','reading-note',context.boundary));article.append(details);
}

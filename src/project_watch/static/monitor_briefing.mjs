// A read-only presentation of source-bound changes, separate from AI opinions.
export function visibleBriefing(state) {
  if(state.briefingError||state.briefing?.project_id!==state.current?.id)return [];
  const sources=new Map(state.sources.map(s=>[s.id,s]));
  const observations=new Map(state.observations.map(o=>[o.id,o]));
  return state.briefing.items.filter(item=>item.project_id===state.current.id&&item.evidence_refs.length&&item.evidence_refs.every(ref=>{
    const source=sources.get(ref.source_id),o=observations.get(ref.observation_id);
    return source?.project_id===state.current.id&&source.content_sha256===ref.source_sha256&&o?.project_id===state.current.id&&o.status==='confirmed'&&o.version===ref.observation_version&&o.source_id===source.id&&o.source_locator===ref.locator;
  }));
}

const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
export function appendBriefing(root,state,{openSource,expanded=new Set()}) {
  const section=el('section','monitor-briefing');section.dataset.readingAnchor='monitor-briefing';
  section.append(el('h2','','值得关注的变化'));
  const items=visibleBriefing(state);
  if(!items.length){
    section.append(el('p','reading-note',state.briefingError?'变化内容暂未读取成功。':'暂没有可比较的已核对数据，或已采用的跟踪更新。可在内部情况继续阅读材料。'));
  }
  for(const item of items.slice(0,3))section.append(renderItem(item));
  if(items.length>3){const rest=el('details','reading-more');rest.dataset.topic='briefing-more';rest.open=expanded.has('briefing-more');rest.append(el('summary','',`其余 ${items.length-3} 条变化`));for(const item of items.slice(3))rest.append(renderItem(item));section.append(rest);}
  root.append(section);

  function renderItem(item){
    const article=el('article','briefing-item');article.dataset.readingAnchor=item.id;
    article.append(el('p','section-label',item.change_kind==='financial'?'内部 · 数值比较':'内部 · 跟踪更新'),el('h3','',item.headline));
    const c=item.comparison;
    if(c){
      const values=el('dl','briefing-values');
      for(const [label,value] of [[c.previous_period,c.previous],[c.period,c.current]]){const cell=el('div');cell.append(el('dt','',`${label} 年`),el('dd','',`${value} ${c.unit}`));values.append(cell);}
      article.append(values,el('p','reading-note',`${c.scope} · 实际数 · 已核对数据的派生计算`));
    }else article.append(el('p','',item.statement));
    article.append(el('p','',item.interpretation));
    const detail=el('details','reading-more');detail.dataset.topic=`briefing-${item.id}`;detail.open=expanded.has(detail.dataset.topic);detail.append(el('summary','','依据与边界'),el('p','',item.why_important));
    for(const unknown of item.unknowns)detail.append(el('p','reading-note',unknown));
    const seen=new Set();
    for(const ref of item.evidence_refs){
      const key=ref.source_id+ref.locator;if(seen.has(key))continue;seen.add(key);
      const source=state.sources.find(s=>s.id===ref.source_id);
      const button=el('button','text-button',`${ref.reference} · ${ref.locator} ↗`);button.type='button';button.onclick=()=>openSource(source,ref.locator);detail.append(button);
    }
    detail.append(el('p','reading-note',`记录于 ${new Date(item.discovered_at).toLocaleDateString('zh-CN')}，不是事项发生日期。`));
    article.append(detail);return article;
  }
}

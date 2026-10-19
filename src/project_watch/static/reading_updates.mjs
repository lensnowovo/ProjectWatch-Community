export function visibleUpdates(state) {
  const updates=state.reading?.updates;
  if(state.readingError||updates?.project_id!==state.current?.id)return [];
  const live=new Map(state.sources.filter(s=>s.project_id===state.current.id).map(s=>[s.id,s]));
  return updates.items.filter(update=>{
    if(update.project_id!==state.current.id||update.status!=='unreviewed'||update.business_change_verified!==false)return false;
    for(const source of [update.current_source,update.previous_source].filter(Boolean))if(live.get(source.id)?.content_sha256!==source.version)return false;
    return [...update.current,...update.previous].every(item=>item.review_status==='unreviewed'&&item.source_refs?.length&&item.source_refs.every(ref=>live.get(ref.source_id)?.content_sha256===ref.version&&ref.quote&&ref.locator));
  });
}
const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
const date=t=>new Date(t).toLocaleDateString('zh-CN');
export function appendReadingUpdates(root,state,{openSource,expanded}) {
  const updates=visibleUpdates(state);if(!updates.length)return;
  const section=el('section','material-updates');section.dataset.readingAnchor='material-updates';
  section.append(el('h3','','新材料中的补充'),el('p','reading-note','AI 据材料整理，尚未核对。接收顺序不代表事情发生的先后。'));
  const render=update=>{
    const article=el('article','material-update');article.dataset.readingAnchor=update.id;
    article.append(el('p','section-label',`${update.title} · ${date(update.received_at)} 收到`));
    const comparison=el('details','reading-more material-compare');comparison.dataset.topic=update.id;comparison.open=expanded.has(update.id);
    comparison.append(el('summary','',update.current[0]?.headline||'查看材料补充'));
    const columns=el('div','material-compare-columns');
    columns.append(side('后收到的材料',update.current_source,update.current,update.current_total));
    columns.append(side('先收到的材料',update.previous_source,update.previous,update.previous_total));
    comparison.append(columns,el('p','reading-note',update.interpretation));article.append(comparison);return article;
  };
  for(const update of updates.slice(0,3))section.append(render(update));
  if(updates.length>3){const more=el('details','reading-more');more.dataset.topic='more-material-updates';more.open=expanded.has(more.dataset.topic);more.append(el('summary','',`其他 ${updates.length-3} 组材料对照`));for(const update of updates.slice(3))more.append(render(update));section.append(more);}
  if(state.reading.updates.truncated)section.append(el('p','reading-note','这里展示最近 20 组；完整材料仍可在资料库阅读。'));
  root.append(section);
  function side(label,source,items,total){
    const box=el('section');box.append(el('h4','',label));
    if(!source){box.append(el('p','reading-note','此前整理内容中未找到同主题描述。'));return box;}
    box.append(el('p','reading-note',`${source.reference} · ${date(source.received_at)} 收到`));
    for(const item of items){box.append(el('p','material-statement',item.text));if(item.as_of)box.append(el('p','reading-note',`材料所述期间：${item.as_of}`));
      for(const ref of item.source_refs){const button=el('button','text-button',`${ref.locator} · 查看原文 ↗`);button.type='button';button.onclick=()=>openSource(state.sources.find(s=>s.id===ref.source_id),ref.locator);box.append(button);}
    }
    if(total>items.length)box.append(el('p','reading-note',`另有 ${total-items.length} 条描述，请打开原件查看。`));
    return box;
  }
}

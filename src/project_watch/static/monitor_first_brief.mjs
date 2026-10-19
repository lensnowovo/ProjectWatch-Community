// The first reading view joins sources and monitoring questions, not invented impacts.
import {analysisFor,appendExternalAnalysis,evidenceCoverage} from './external_analysis.mjs';
import {trackDisclosure} from './page_polish.mjs';
const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
const action=(text,fn)=>{const b=el('button','text-button',text);b.type='button';b.onclick=fn;return b;};

export function firstBriefItems(state){
  const data=state.external,project=state.current?.id,brief=data?.briefing;
  if(state.externalError||data?.project_id!==project||brief?.project_id!==project||brief.rule_version!=='external-first-brief-v1')return [];
  const news=new Map(data.categories.flatMap(g=>g.items).map(i=>[i.id,i]));
  const sources=new Map((state.sources||[]).filter(s=>s.project_id===project).map(s=>[s.id,s]));
  return brief.items.filter(i=>{
    const story=news.get(i.id);
    return story?.project_id===project&&i.project_id===project&&story.title===i.statement&&story.summary===i.summary&&story.url===i.url&&story.category===i.category&&Date.parse(story.published_at)===Date.parse(i.published_at)&&i.impact===null&&i.relationship?.verified===false&&i.origin==='source_bound_projection'&&i.content_scope==='headline_and_feed_summary_only'&&(i.evidence_refs||[]).every(r=>sources.get(r.source_id)?.content_sha256===r.version);
  });
}

export function briefCoverageText(c){
  return {failed:'部分来源检查失败',pending:'正在检查',paused:'已暂停',not_configured:'尚未覆盖',checked:c.item_count?`${c.item_count} 条匹配消息`:'本轮未命中近期消息'}[c.state]||'暂未取得检查状态';
}

// The same exact report can be collected under both sector and peer scopes.
// Deduplication must not hide its newer body-backed analysis. Validate each saved
// analysis against its OWN live signal; never graft its packet onto another one.
export function briefAnalysisStory(item,state){
  const matches=(state.external?.categories||[]).flatMap(g=>g.items).filter(s=>s.project_id===item.project_id&&s.url===item.url&&s.title===item.statement&&s.summary===item.summary&&Date.parse(s.published_at)===Date.parse(item.published_at));
  return matches.map(story=>({story,saved:analysisFor(story,state)})).sort((a,b)=>(b.saved?.recorded_at||0)-(a.saved?.recorded_at||0))[0]?.story;
}

// A reversible reading preference, never a claim of verified relevance or impact.
export function partitionFirstBrief(state){
  const priorityFor=item=>{
    const story=briefAnalysisStory(item,state),analysis=story?analysisFor(story,state)?.analysis:null,assessment=analysis?.relationship?.assessment;
    const a=item.attention,topic=a?.version==='headline-topic-v1'&&a.basis==='headline_topic_only'&&a.event_verified===false&&a.impact===null&&a.level==='read_first';
    const supported=assessment==='potentially_relevant'&&analysis.implications?.some(i=>i.hypothesis&&i.condition&&i.evidence?.some(e=>e.ref_id?.startsWith('material:')));
    return assessment!=='not_relevant'&&(topic||supported);
  };
  const items=firstBriefItems(state),byId=new Map(items.map(i=>[i.id,i])),used=new Set(),groups=[];
  const recent=(a,b)=>Date.parse(b.published_at)-Date.parse(a.published_at)||a.id.localeCompare(b.id);
  const bodyBacked=item=>{const story=briefAnalysisStory(item,state);return story&&analysisFor(story,state)?.packet.content_scope==='article_excerpts';};
  for(const group of state.external?.briefing?.report_groups||[]){
    const ids=group.member_ids;
    if(group.version!=='report-groups-v1'||group.event_verified!==false||group.status!=='candidate'||!Array.isArray(ids)||ids.length<2||new Set(ids).size!==ids.length||ids.some(id=>!byId.has(id)||used.has(id)))continue;
    const reports=ids.map(id=>byId.get(id)).sort((a,b)=>Number(priorityFor(b))-Number(priorityFor(a))||Number(bodyBacked(b))-Number(bodyBacked(a))||recent(a,b));
    groups.push({...reports[0],reports});ids.forEach(id=>used.add(id));
  }
  for(const item of items)if(!used.has(item.id))groups.push({...item,reports:[item]});
  const primary=groups.filter(g=>g.reports.some(priorityFor)).sort(recent),secondary=groups.filter(g=>!g.reports.some(priorityFor)).sort(recent);
  // The cap is on groups, not individual URLs. Overflow is never discarded.
  secondary.push(...primary.splice(5));secondary.sort(recent);
  return {primary,secondary};
}

export function renderFirstBrief(root,state,{openSource,expanded=new Set(),selectCategory}){
  const brief=state.external.briefing,items=firstBriefItems(state);
  const {primary,secondary}=partitionFirstBrief(state);
  const head=el('header','first-brief-heading');
  head.append(el('h3','','近期重点'),el('p','reading-note',`近 ${brief?.window_days||30} 天 · ${items.length} 条公开线索 · 项目影响尚未核实`));root.append(head);
  if(!items.length)root.append(el('p','reading-empty','暂未取得可展示的近期消息。检查结果见下方覆盖范围，不能据此判断项目没有变化。'));
  if(items.length&&!primary.length)head.append(el('p','reading-note','本轮没有筛出重点消息，其余线索保留在下方。'));
  const secondaryPanel=el('details','first-brief-secondary');
  secondaryPanel.dataset.topic=`brief-secondary-${state.current.id}`;secondaryPanel.open=expanded.has(secondaryPanel.dataset.topic);
  secondaryPanel.ontoggle=()=>{if(!secondaryPanel.isConnected)return;if(secondaryPanel.open)expanded.add(secondaryPanel.dataset.topic);else expanded.delete(secondaryPanel.dataset.topic);};
  secondaryPanel.append(el('summary','',`其他线索 · ${secondary.length} 组`),el('p','reading-note','包括其余重点主题和暂未建立具体关联的消息。分类列表保留全部报道。'));
  const primaryIds=new Set(primary.map(i=>i.id));
  for(const group of [...primary,...secondary]){
    const destination=primaryIds.has(group.id)?root:secondaryPanel;
    let additional;
    if(group.reports.length>1){
      destination.append(el('p','reading-note',`疑似同一事项 · ${group.reports.length} 篇报道 · 尚未确认，逐篇保留原文及判断`));
      additional=el('details','first-brief-evidence');additional.dataset.topic=`report-group-${group.id}`;additional.open=expanded.has(additional.dataset.topic);
      additional.ontoggle=()=>{if(!additional.isConnected)return;if(additional.open)expanded.add(additional.dataset.topic);else expanded.delete(additional.dataset.topic);};
      additional.append(el('summary','',`其他报道与表述 · ${group.reports.length-1} 篇`));
    }
    for(const [index,item] of group.reports.entries()){
    const article=el('article','first-brief-story');article.dataset.readingAnchor=`brief-${item.id}`;
    article.append(el('p','section-label',`${item.category_label} · 发布于 ${new Date(item.published_at).toLocaleDateString('zh-CN')}${item.attention?.level==='read_first'?` · ${item.attention.label}`:''}`),el('h3','',item.statement));
    const story=briefAnalysisStory(item,state),saved=story?analysisFor(story,state):null;
    if(saved)appendExternalAnalysis(article,story,state,{openSource,expanded});
    else {
      const connection=el('p','story-connection');connection.append(el('span','story-attribution','跟踪线索'),document.createTextNode(item.relationship.text));article.append(connection);
      const more=el('details','story-analysis');trackDisclosure(more,expanded,`story-summary-${item.id}`);more.append(el('summary','','消息摘要与跟踪问题'));
      if(item.summary)more.append(el('p','first-brief-summary',item.summary));
      if(item.question)more.append(el('h4','','跟踪的问题'),el('p','',item.question));
      more.append(el('p','reading-note',item.unknowns.slice(1).join(' ')));article.append(more);
    }
    const footer=el('div','first-brief-footer');
    try{const u=new URL(item.url);if(['http:','https:'].includes(u.protocol)){const link=el('a','text-button',`${u.hostname} · 阅读原文 ↗`);link.href=u.href;link.target='_blank';link.rel='noopener noreferrer';footer.append(link);}}catch{}
    footer.append(el('span','reading-note',evidenceCoverage(saved?.packet)));
    article.append(footer);
    const details=el('details','first-brief-evidence');details.dataset.topic=`first-brief-${item.id}`;details.open=expanded.has(details.dataset.topic);
    details.ontoggle=()=>{if(!details.isConnected)return;if(details.open)expanded.add(details.dataset.topic);else expanded.delete(details.dataset.topic);};
    details.append(el('summary','','BP 依据与核对边界'));
    if(!item.evidence_refs.length)details.append(el('p','reading-note','尚无有效的 BP 引用支持此条关联。'));
    for(const ref of item.evidence_refs){
      details.append(el('blockquote','',ref.quote));
      const source=state.sources.find(s=>s.id===ref.source_id);
      if(source)details.append(action(`${ref.reference} · ${ref.locator} ↗`,()=>openSource(source,ref.locator)));
    }
    for(const text of item.unknowns)details.append(el('p','reading-note',text));
    // Keep citations directly with the expandable report, rather than a second
    // full-width control competing with every headline.
    (article.querySelector('.story-analysis')||article).append(details);(index?additional:destination).append(article);
    }
    if(additional)destination.append(additional);
  }
  if(secondary.length)root.append(secondaryPanel);
  const coverage=el('details','first-brief-coverage');coverage.open=!items.length;coverage.append(el('summary','','覆盖范围与检查结果'));
  if(brief?.deferred_market_count)coverage.append(el('p','reading-note',`另有 ${brief.deferred_market_count} 条行情类消息，保留在分类列表中。`));
  for(const c of brief?.coverage||[]){const row=el('div','first-brief-coverage-row');row.append(action(c.title,()=>selectCategory(c.key)),el('span','reading-note',briefCoverageText(c)));if(c.last_success_at)row.append(el('small','reading-note',`最近成功：${new Date(c.last_success_at).toLocaleString('zh-CN')}`));coverage.append(row);}
  coverage.append(el('p','reading-note','疑似同一事项仅作阅读归组，不合并事实。日期、阶段或数字不一致时保持分开。未命中、暂停和检查失败是不同状态；当前来源不代表完整覆盖。'));root.append(coverage);
}

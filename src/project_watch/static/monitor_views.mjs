// Saved excerpts are readable with attribution, never implicitly confirmed.
import {monitorCopy} from './attention.mjs';
import {appendMonitorProfile} from './monitor_profile.mjs';
import {compiledReading, readingStatusCopy} from './compiled_reading.mjs';
import {openReadingConsent} from './reading_consent.mjs';
import {captureReadingPosition} from './reading_position.mjs';
import {renderExternalFocus} from './external_focus.mjs';
import {appendBriefing} from './monitor_briefing.mjs';
import {appendReadingUpdates,visibleUpdates} from './reading_updates.mjs';
import {readingNews,attentionFor} from './external_attention.mjs';
import {analysisFor,analysisStatusCopy} from './external_analysis.mjs';
import {openExternalAnalysisConsent} from './external_analysis_consent.mjs';
import {openMonitorAutopilot} from './monitor_autopilot.mjs';
import {appendAnalysisRecovery} from './analysis_recovery.mjs';
export function companyReading(state) {
  const confirmed = state.observations.filter(o => o.status === 'confirmed');
  const facts = confirmed.filter(o => ['公司与团队','经营情况','产品与技术'].includes(o.subject) && !o.number);
  const sources = [...state.sources].sort((a,b) => b.created_at.localeCompare(a.created_at));
  const batches = new Map();
  for (const source of sources) {
    const day = source.created_at.slice(0,10);
    if (!batches.has(day)) batches.set(day, {kind:'materials', time:source.created_at, sources:[]});
    batches.get(day).sources.push(source);
  }
  const changes = [...batches.values(), ...state.signals.map(s => ({kind:'news',time:s.published_at||s.first_seen_at,title:s.title,url:s.url,discovered:!s.published_at}))]
    .sort((a,b) => (b.time||'').localeCompare(a.time||''));
  const liveSources=new Set(state.sources.map(s=>s.id));
  const liveObservations=new Map(state.observations.map(o=>[o.id,o]));
  const reading = state.reading && state.current && state.reading.project_id === state.current.id && state.reading.sections.every(section=>section.items.every(item=>{
    const observation=liveObservations.get(item.id);
    return observation && observation.status!=='rejected' && observation.statement===item.text && item.source_refs.every(ref=>liveSources.has(ref.source_id));
  })) ? state.reading : null;
  return {facts, sources, changes, materialUpdates:changes.filter(i=>i.kind==='materials'),externalUpdates:changes.filter(i=>i.kind==='news'), reading, pending:state.observations.filter(o=>o.status==='candidate').length};
}

const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
const action=(text,fn)=>{const n=el('button','text-button',text);n.type='button';n.onclick=fn;return n;};
const date=t=>t?new Date(t).toLocaleDateString('zh-CN'):'日期未记录';

export function renderCompanyReading(state, {navigate,openSource,request,onProfileSaved,onReadingChanged,openSettings}) {
  const data=companyReading(state), overview=document.getElementById('company-overview'), internal=document.getElementById('company-internal'), changes=document.getElementById('company-changes');
  if(!overview||!internal||!state.current)return;
  const backgroundCopy=readingStatusCopy(data.reading?.background);
  const previousStatus=internal.querySelector('.reading-background-copy');if(previousStatus)previousStatus.textContent=backgroundCopy;
  const contentData={...data,reading:data.reading?{...data.reading,background:null}:null};
  const key=JSON.stringify([state.current.id,contentData,state.profile,state.profileError?.message,state.readingError?.message,state.monitoringError?.message,state.monitoringPlans,state.external,state.externalError?.message,state.briefing,state.briefingError?.message,state.externalAnalysis,state.externalAnalysisError?.message]);
  if(overview.dataset.readingKey===key)return;
  const restorePosition=overview.dataset.projectId===state.current.id?captureReadingPosition([overview,internal,changes]):()=>{};
  overview.dataset.projectId=state.current.id;
  overview.dataset.readingKey=key;
  const expanded=new Set([overview,internal,changes].flatMap(root=>[...root.querySelectorAll('details[open]')]).map(n=>n.dataset.topic));
  const go=tab=>navigate(state.current.id,tab);
  overview.replaceChildren();internal.replaceChildren();changes.replaceChildren();
  const intro=el('section','company-intro');
  intro.dataset.readingAnchor='company-intro';
  const compiled=compiledReading(data.reading,state.current.id,state.sources);
  const business=compiled.find(g=>g.topic==='business')?.items[0];
  const savedSummary=data.reading?.summary;
  const summary=savedSummary?.review_status==='confirmed'?savedSummary:(business||savedSummary);
  const introHead=el('header','reading-heading');introHead.append(el('span','section-label','公司概况'));
  introHead.append(action('查看业务 →',()=>go('business')));
  intro.append(introHead,el('h2','','公司近况'));
  const manualBusiness=state.profile?.project_id===state.current.id&&state.profile.fields.business.origin==='human'?state.profile.fields.business:null;
  if(manualBusiness){
    intro.append(el('p','reading-note','手动保留 · 非正式事实'),el('p','company-description company-summary',manualBusiness.text||'已清空业务介绍。'));
  }else if(summary){
    intro.append(el('p','reading-note',summary.review_status==='confirmed'?'已核对内容':summary.origin==='ai_synthesis'?'AI 据材料整理 · 尚未核对':'据已整理资料 · 尚未核对'));
    const copy=el('p','company-description company-summary',summary.text);intro.append(copy);
    const refs=el('div','reading-sources');appendRefs(refs,summary.source_refs);intro.append(refs);
  }else if(data.facts.length){
    intro.append(el('p','company-description',data.facts.map(f=>f.statement).join(' ')));
  }else{
    intro.append(el('p','company-description',data.sources.length?'资料已收到，暂未整理出业务介绍。可以先看原件。':'添加公司介绍或 BP，开始了解这家公司。'));
    intro.append(action(data.sources.length?'阅读公司资料 →':'添加第一份资料 →',()=>go('materials')));
    if(!state.teamMode&&!data.sources.length)intro.append(action('上传 BP，开始监控 →',()=>openMonitorAutopilot({projectId:state.current.id,sources:state.sources,request,onChanged:onReadingChanged,openSettings})));
  }
  overview.append(intro);
  const internalHead=el('header','reading-heading');internalHead.append(el('h2','','材料中的公司情况'));
  if(!state.teamMode)internalHead.append(action('自动整理',()=>openReadingConsent({projectId:state.current.id,sources:state.sources,request,onChanged:onReadingChanged,openSettings})));
  const background=el('details','reading-background');background.dataset.topic='reading-background';background.open=expanded.has(background.dataset.topic);
  background.append(el('summary','','整理状态与范围'),el('p','reading-note reading-background-copy',backgroundCopy));internal.append(internalHead,background);
  appendReadingUpdates(internal,state,{openSource,expanded});
  if(visibleUpdates(state).length){
    const update=visibleUpdates(state)[0];
    const note=el('section','overview-material-update');note.dataset.readingAnchor='overview-material-update';
    note.append(el('p','section-label',`新材料 · ${update.title}`),el('p','',update.current[0].text),el('p','reading-note','AI 据材料整理，尚未核对。'),action('查看材料对照 →',()=>go('internal')));overview.append(note);
  }
  if(state.profile?.project_id===state.current.id){appendMonitorProfile(overview,state.profile,{request,onSaved:onProfileSaved,openSource,sources:state.sources,expanded});overview.querySelector('.profile-details').open=expanded.has('profile');}
  if(state.profileError)overview.append(el('p','reading-note','项目关注暂未更新，请稍后重试。'));
  if(state.readingError)for(const box of [overview,internal])box.append(el('p','reading-note','本次内容更新未成功，已有内容仍可阅读。'));
  if(compiled.length){
    const report=el('div','compiled-report');
    report.append(el('p','reading-note','以下按材料整理，原文引用可展开核对。不同材料的描述分别保留，不代表正式事实。'));
    for(const group of compiled){
      const block=el('section','compiled-topic');block.dataset.topic=`compiled-${group.topic}`;block.append(el('h3','',group.title));
      for(const item of group.items.slice(0,3))appendCompiled(block,item);
      if(group.items.length>3){const more=el('details','reading-more');more.dataset.topic=`more-${group.topic}`;more.open=expanded.has(more.dataset.topic);more.append(el('summary','',`查看其余 ${group.items.length-3} 条材料描述`));for(const item of group.items.slice(3))appendCompiled(more,item);block.append(more);}
      report.append(block);
    }internal.append(report);
  }
  if(data.reading?.sections.length){
    const report=el('div','reading-excerpts');
    for(const section of data.reading.sections){
      const block=el('section','reading-topic');block.append(el('h3','',section.title));
      if(section.title==='财务表现'){block.classList.add('financial-excerpts');block.querySelector('h3').textContent='财务相关描述';block.append(el('p','reading-note','公司材料所述的预算、预测与规划，不计入已实现业绩。'));}
      for(const item of section.items.slice(0,2))appendExcerpt(block,item);
      if(section.items.length>2){const rest=el('details','reading-more');rest.dataset.topic=section.title;rest.open=expanded.has(section.title);rest.append(el('summary','',`另外 ${section.items.length-2} 条`));for(const item of section.items.slice(2))appendExcerpt(rest,item);block.append(rest);}
      report.append(block);
    }
    if(compiled.length){const old=el('details','reading-more');old.dataset.topic='saved-excerpts';old.open=expanded.has(old.dataset.topic);old.append(el('summary','','查看已有摘录与核对记录'),report);internal.append(old);}else internal.append(report);
  }
  if(!compiled.length&&!data.reading?.sections.length){
    internal.append(el('p','reading-empty',data.sources.length?'还没有整理好的公司情况。可先阅读原件，或设置自动整理。':'加入 BP、业务介绍或经营财务材料后，在这里阅读公司情况。'));
  }
  const library=el('details','reading-more material-history');library.dataset.topic='material-history';library.open=expanded.has(library.dataset.topic);
  library.append(el('summary','',`资料与接收记录 · ${data.sources.length} 份`),action('打开资料库 →',()=>go('materials')));
  const receipts=el('div','company-timeline');renderChanges(receipts,data.materialUpdates);library.append(receipts);internal.append(library);
  const main=el('section','company-section overview-news'),mainHead=el('header','reading-heading');
  if(!state.teamMode)appendBriefing(overview,state,{openSource,expanded});
  mainHead.append(el('h3','','近期公开消息'),action('查看外部动态 →',()=>go('external')));main.append(mainHead);
  main.append(el('p','reading-note',monitorCopy(state.monitoringPlans,!!state.monitoringError)));
  const list=el('div','company-timeline');
  const publicItems=state.teamMode?data.externalUpdates:readingNews(state.external,state.current.id).map(i=>({...i,kind:'news',time:i.published_at}));
  if(publicItems.some(attentionFor))main.append(el('p','reading-note','先看经营相关主题；分类不代表事项已核实。'));
  renderChanges(list,publicItems.slice(0,3),state.externalError?'公开消息暂未读取成功，请稍后重试。':'尚无可展示的近期公开消息。');main.append(list);overview.append(main);
  const heading=el('header','reading-heading');heading.append(el('h2','','外部动态'));changes.append(heading);
  if(state.teamMode)changes.append(el('p','reading-note',monitorCopy(state.monitoringPlans,!!state.monitoringError)));
  if(!state.teamMode){
    heading.append(action('从 BP 开始监控',()=>openMonitorAutopilot({projectId:state.current.id,sources:state.sources,request,onChanged:onReadingChanged,openSettings})));
    const analysisOptions=el('details','external-analysis-options');analysisOptions.dataset.topic='analysis-options';analysisOptions.open=expanded.has(analysisOptions.dataset.topic);analysisOptions.append(el('summary','','AI 解读'));
    analysisOptions.append(el('p','reading-note external-analysis-status',analysisStatusCopy(state.externalAnalysis,state.externalAnalysisError)),action('解读范围与用量',()=>openExternalAnalysisConsent({projectId:state.current.id,sources:state.sources,external:state.external,request,onChanged:onReadingChanged,openSettings})));
    const status=el('details','external-runtime');status.dataset.topic='external-runtime';status.open=expanded.has(status.dataset.topic);status.append(el('summary','','监控状态与设置'),analysisOptions);changes.append(status);
  }
  if(!state.teamMode){
    appendAnalysisRecovery(changes.querySelector('.external-runtime'),state,{request,onChanged:onReadingChanged});
    const recovery=changes.querySelector('.analysis-recovery');if(recovery)recovery.open=expanded.has('analysis-recovery');
    renderExternalFocus(changes,state,{request,onChanged:onReadingChanged,openSource,expanded});
    changes.append(changes.querySelector('.external-runtime'));
  }
  else {const timeline=el('div','company-timeline change-page');renderChanges(timeline,data.externalUpdates,'当前没有可展示的公开消息。来源范围和检查结果见下方设置。');changes.append(timeline);}
  restorePosition();

  function appendRefs(box,refs){
    const seen=new Set();
    for(const ref of refs){const source=state.sources.find(s=>s.id===ref.source_id);const k=ref.source_id+ref.locator;if(!source||seen.has(k))continue;seen.add(k);box.append(action(`${ref.reference} · ${ref.locator} ↗`,()=>openSource(source,ref.locator)));}
  }
  function appendExcerpt(box,item){
    const row=el('article','reading-excerpt');
    row.dataset.readingAnchor=`excerpt-${item.id}`;
    row.append(el('p','',item.text));
    const meta=el('div','excerpt-meta');meta.append(el('span','',item.review_status==='confirmed'?'已核对':'资料整理 · 尚未核对'));
    if(item.as_of)meta.append(el('span','',`期间 ${item.as_of}`));
    if(item.number!==null)meta.append(el('span','',`${item.number} ${item.unit||''} · ${item.scope||'范围未注明'} · ${{actual:'实际',forecast:'预测',budget:'预算',unknown:'口径未注明'}[item.basis]}`));
    if(item.has_conflict)meta.append(el('span','reading-conflict','相同口径存在不同数值'));
    row.append(meta);const refs=el('div','reading-sources');appendRefs(refs,item.source_refs);row.append(refs);box.append(row);
  }

  function appendCompiled(box,item){
    const row=el('article','compiled-item');row.append(el('h4','',item.headline),el('p','compiled-copy',item.text));
    row.dataset.readingAnchor=`compiled-${item.batch_id}-${item.headline}-${item.text}`;
    const meta=el('p','reading-note',`AI 整理 · 尚未核对${item.as_of?` · 材料所述期间 ${item.as_of}`:''}`);row.append(meta);
    const refs=el('div','reading-sources');appendRefs(refs,item.source_refs);row.append(refs);
    const quote=el('details','reading-more');quote.dataset.topic=`quote-${item.batch_id}-${item.headline}`;quote.open=expanded.has(quote.dataset.topic);quote.append(el('summary','','展开原文依据'));
    for(const ref of item.source_refs){const q=el('blockquote','compiled-quote',ref.quote);quote.append(q);}
    quote.append(el('p','reading-note',`整理于 ${date(item.recorded_at)}，不是事项发生日期。`));row.append(quote);box.append(row);
  }

  function renderChanges(box,items,empty='还没有收到资料。'){
    if(!items.length){box.append(el('p','reading-empty',empty));return;}
    for(const item of items){
      const row=el('article','company-change'),meta=el('div','change-date');meta.append(el('time','',date(item.time)),el('span','',item.kind==='materials'?'收到材料':'公开消息'));
      row.dataset.readingAnchor=`${item.kind}-${item.id||item.url||item.time}`;
      const content=el('div');
      content.append(el('h4','',item.kind==='materials'?`收到 ${item.sources.length} 份资料`:item.title));
      if(item.kind==='materials'){
        content.append(el('p','',item.sources.slice(0,3).map(s=>s.reference).join('、')+(item.sources.length>3?'等':'')));
        content.append(action('查看资料 →',()=>go('materials')));
      }else {
        const attention=attentionFor(item);if(attention)content.append(el('p','',`${attention.label} · ${attention.reason}`));
        content.append(el('p','reading-note',`公开报道，尚未核实${item.discovered?' · 显示发现日期':''}`));
        const analysis=analysisFor(item,state);
        if(analysis){content.append(el('p','overview-analysis',`AI 关联解读：${analysis.analysis.relationship.text}`),action('查看解读与依据 →',()=>{[...changes.querySelectorAll('button[data-category]')].find(b=>b.dataset.category===item.category)?.click();go('external');}));}
        try{const url=new URL(item.url);if(['http:','https:'].includes(url.protocol)){const link=el('a','text-button','阅读原文 ↗');link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';content.append(link);}}catch{}
      }
      row.append(meta,content);box.append(row);
    }
  }
}

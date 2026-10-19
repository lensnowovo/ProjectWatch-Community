// Saved interpretation is attributed, scoped and separate from formal facts.
const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
import {compactStoryText,trackDisclosure} from './page_polish.mjs';

export function evidenceCoverage(packet){
  if(packet?.rule_version==='external-analysis-v2'&&packet.content_scope==='article_excerpts')return packet.article?.status==='partial_body'?'依据：部分正文与选定背景':'依据：已读取正文片段与选定背景';
  return packet?.article?.status==='unavailable'?'正文暂不可用 · 依据标题与摘要':'依据：标题与订阅摘要，非全文';
}

export function analysisFor(item,state){
  const data=state.externalAnalysis,projectId=state.current?.id;
  if(state.externalAnalysisError||state.externalError||data?.project_id!==projectId||item.project_id!==projectId)return null;
  const sources=new Map((state.sources||[]).filter(s=>s.project_id===projectId).map(s=>[s.id,s]));
  return [...(data.items||[])].sort((a,b)=>b.recorded_at-a.recorded_at).find(record=>{
    const p=record.packet,a=record.analysis;
    if(p?.project_id!==projectId||p.signal_id!==item.id||a?.project_id!==projectId||a.signal_id!==item.id||
       p.term!==item.match_term||p.category!==item.category||p.source_url!==item.url||
       a.origin!=='ai_synthesis'||a.review_status!=='unreviewed'||a.relationship_verified!==false||
       a.policy_applicability_verified!==false||!['headline_and_feed_summary_only','article_excerpts'].includes(a.content_scope)||
       a.support_status!=='quotes_located_not_semantic_verification')return false;
    const refs=p.references||[],body=p.rule_version==='external-analysis-v2'&&p.content_scope==='article_excerpts';
    if(body&&(a.rule_version!==p.rule_version||a.content_scope!==p.content_scope||!p.article?.snapshot_digest||!['article_body','partial_body'].includes(p.article.status)))return false;
    return Number.isFinite(Date.parse(p.published_at))&&Date.parse(p.published_at)===Date.parse(item.published_at)&&
      refs.some(r=>r.ref_id==='news:title'&&r.text===item.title.trim().slice(0,1000))&&
      (body||(refs.find(r=>r.ref_id==='news:summary')?.text||'')===(item.summary||'').trim().slice(0,4000))&&
      refs.filter(r=>r.kind==='external').every(r=>r.ref_id==='news:title'||r.ref_id==='news:summary'&&r.text===(item.summary||'').trim().slice(0,4000)||body&&r.ref_id.startsWith('article:')&&r.source_version===p.article.snapshot_digest)&&
      refs.filter(r=>r.kind==='internal').every(r=>sources.get(r.source_id)?.content_sha256===r.source_version&&r.text&&r.locator);
  })||null;
}

export function analysisStatusCopy(data,error){
  if(error)return 'AI 解读暂未加载，公开消息仍可阅读。';
  if(!data)return '';
  const p=data.processing||{},counts=p.counts||{};
  const labels={model_missing:'连接 AI 后可整理公开消息，并结合选定背景解读。',consent_required:'可设置 AI 解读，先核对发送范围和用量。',paused:'AI 解读已暂停，已保存内容仍可阅读。',budget_reached:'本次解读额度已用完。',model_limit:'AI 调用额度已用完，可到设置中调整。',scope_changed:'背景材料或跟踪范围已变化，请重新核对解读设置。',news_paused:'公开跟踪已暂停，暂不解读新消息。'};
  let text=labels[p.reason]||(!data.automatic_analysis_available?'后台解读未启动，已有内容仍可阅读。':
    counts.running||counts.dispatched?'正在解读授权范围内的消息。':'AI 解读已开启，仅处理授权范围内的消息。');
  if(counts.uncertain||counts.failed)text+=' 部分解读未完成，未自动重试已发送的请求。';
  if(data.stale_count)text+=' 依据已变化的旧解读已收起。';
  return text;
}

export function externalConsentPayload(draft,viewed,confirmed,requestKey){
  if(!confirmed)throw Error('请确认上述发送范围。');
  if(!viewed||viewed.draft!==JSON.stringify(draft)||!/^[a-f0-9]{64}$/.test(viewed.disclosure_digest))throw Error('选择已变化，请重新查看发送范围。');
  return {...draft,consent:true,request_key:requestKey,disclosure_digest:viewed.disclosure_digest};
}

export function appendExternalAnalysis(article,item,state,{openSource,expanded=new Set()}){
  const saved=analysisFor(item,state);if(!saved)return;
  const summary=compactStoryText(saved);
  const connection=el('p','story-connection');connection.append(el('span','story-attribution',summary.label),document.createTextNode(summary.text));article.append(connection);
  const disclosure=el('details','story-analysis');trackDisclosure(disclosure,expanded,`story-analysis-${item.id}`);
  disclosure.append(el('summary','','解读与依据'));
  const {analysis:a,packet:p}=saved,report=el('section','external-analysis-report');
  report.setAttribute('aria-label','AI 关联解读');
  const head=el('header','reading-heading');head.append(el('h4','','消息解读'),el('span','reading-note','AI 整理 · 尚未核对'));report.append(head);
  for(const statement of a.source_statements)report.append(el('p','analysis-source-statement',`来源提及：${statement.text}`));
  report.append(el('h5','',({unclear:'与项目的关系尚不明确',not_relevant:'AI 暂未发现直接关联',potentially_relevant:'可能与项目相关 · 待核对'}[a.relationship.assessment]||'与项目的关系')));
  report.append(el('p','analysis-relation',a.relationship.text));
  if(a.implications.length){
    const effects=el('div','analysis-effects');
    for(const effect of a.implications){const row=el('section');row.append(el('h5','','可能影响'),el('p','',effect.hypothesis),el('p','reading-note',`前提：${effect.condition}`));effects.append(row);}report.append(effects);
  }
  const unknowns=el('div','analysis-unknowns');unknowns.append(el('h5','','还不确定'));const list=el('ul');for(const text of a.unknowns)list.append(el('li','',text));unknowns.append(list);report.append(unknowns);
  const detail=el('details','analysis-evidence');detail.dataset.topic=`external-analysis-${saved.id}`;detail.open=expanded.has(detail.dataset.topic);
  detail.addEventListener('toggle',()=>{if(detail.open)expanded.add(detail.dataset.topic);else expanded.delete(detail.dataset.topic);});
  detail.append(el('summary','','查看消息陈述与引用依据'));
  const citations=[...a.source_statements,a.relationship,...a.implications].flatMap(s=>s.evidence),seen=new Set();
  for(const cite of citations){
    const key=cite.ref_id+'|'+cite.quote;if(seen.has(key))continue;seen.add(key);
    const ref=p.references.find(r=>r.ref_id===cite.ref_id);if(!ref)continue;
    const group=el('div','analysis-citation');group.append(el('p','reading-note',ref.kind==='external'?(ref.ref_id.startsWith('article:')?`公开正文 · ${ref.locator}`:'公开消息 · 标题或摘要'):'项目材料'),el('blockquote','',cite.quote));
    if(ref.ref_id.startsWith('article:')){const context=el('details');context.append(el('summary','','查看当时读取的段落'),el('p','',ref.text),el('small','reading-note',`保存于 ${new Date(p.article.fetched_at*1000).toLocaleString('zh-CN')} · 版本 ${p.article.snapshot_digest.slice(0,12)}`));group.append(context);}
    if(ref.kind==='internal'){
      const source=state.sources.find(s=>s.id===ref.source_id),link=el('button','text-button',`${source.reference} · ${ref.locator} ↗`);
      link.type='button';link.onclick=()=>openSource(source,ref.locator);group.append(link);
    }else{try{const url=new URL(p.source_url);if(['https:','http:'].includes(url.protocol)){const link=el('a','text-button','阅读公开原文 ↗');link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';group.append(link);}}catch{}}
    detail.append(group);
  }
  detail.append(el('p','reading-note',`整理于 ${new Date(saved.recorded_at*1000).toLocaleString('zh-CN')}。${evidenceCoverage(p)}。来源陈述和项目影响尚未核实。`));
  report.append(detail);disclosure.append(report);article.append(disclosure);
}

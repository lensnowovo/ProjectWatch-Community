// Public tracking is separate from private profile text; preview never starts a run.
import {appendInternalContext} from './external_context.mjs';
import {attentionFor,readingNews,newsMode} from './external_attention.mjs';
import {analysisFor,appendExternalAnalysis} from './external_analysis.mjs';
import {renderFirstBrief} from './monitor_first_brief.mjs';
import {externalNavigation,compactStoryText} from './page_polish.mjs';
const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
const action=(text,fn)=>{const b=el('button','text-button',text);b.type='button';b.onclick=fn;return b;};
export const categories={company:'公司',sector:'赛道',competitors:'竞品',policy:'政策',industry:'行业'};
export function coverageText(category){
  return ({not_configured:'尚未添加关注词',paused:'已暂停自动检查',failed:'部分来源检查失败，已有消息仍可阅读',pending:'等待检查',checked:'已检查；以下仅展示近 30 天的匹配消息'})[category.coverage]||'检查状态暂不可用';
}
export function focusPayload(preview,selection,confirmed){
  if(!confirmed||!preview?.eligible)throw Error('请先核对公开查询范围。');
  for(const key of ['category','provider','term'])if(preview[key]!==selection[key].trim())throw Error('关注词已变化，请重新预览。');
  return {category:preview.category,provider:preview.provider,term:preview.term,consent_version:preview.consent_version};
}

export function renderExternalFocus(root,state,{request,onChanged,openSource,expanded}){
  if(state.external?.project_id!==state.current.id){root.append(el('p','reading-note',state.externalError?'外部动态暂未加载，请稍后再试。':'正在读取公开关注…'));return;}
  const data=state.external;
  const navigation=externalNavigation(state);
  const strategy=data.monitoring_strategy;
  const chosen=root.dataset.focusProject===state.current.id?root.dataset.focusCategory:'brief';
  const scopeOpen=root.dataset.focusProject===state.current.id&&root.dataset.scopeOpen==='true';
  let mode=newsMode(root.dataset,state.current.id);
  root.dataset.focusProject=state.current.id;
  root.dataset.newsMode=mode;
  root.dataset.scopeOpen=String(scopeOpen);
  const tabs=el('nav','external-categories');tabs.setAttribute('aria-label','公开关注分类');
  const body=el('section','external-content');
  const briefButton=action('重点',()=>select('brief'));briefButton.dataset.category='brief';tabs.append(briefButton);
  for(const group of navigation){const button=action(group.title,()=>select(group.key));button.dataset.category=group.key;button.append(el('small','',group.status));button.title=group.lastSuccess?`${group.status} · 最近成功检查 ${new Date(group.lastSuccess).toLocaleString('zh-CN')}`:group.status;tabs.append(button);}
  root.append(tabs,body);select(categories[chosen]?chosen:'brief');
  function select(key){
    if(key==='sector')key='industry';
    root.dataset.focusCategory=key;
    if(key!=='brief'&&![...tabs.children].some(b=>b.dataset.category===key)){const b=action(categories[key],()=>select(key));b.dataset.category=key;tabs.append(b);}
    for(const button of tabs.children)button.setAttribute('aria-pressed',String(button.dataset.category===key));
    if(key==='brief'){body.replaceChildren();renderFirstBrief(body,state,{openSource,expanded,selectCategory:select});return;}
    const group=navigation.find(g=>g.key===key);body.replaceChildren();if(!group)return;
    const head=el('header','reading-heading');head.append(el('h3','',`${group.title}动态`),action('添加关注',()=>openFocusEditor({projectId:state.current.id,projectName:state.current.name,category:key,request,onChanged})));
    body.append(head,el('p','reading-note',`${group.status}${group.lastSuccess?` · 最近成功检查 ${new Date(group.lastSuccess).toLocaleString('zh-CN')}`:''}`));
    if(group.plans.length){
      const scopes=el('details','external-scopes');scopes.open=root.dataset.scopeOpen==='true';scopes.ontoggle=()=>{if(scopes.isConnected)root.dataset.scopeOpen=String(scopes.open);};scopes.append(el('summary','',`${group.plans.length} 个关注词 · 查看范围`));
      for(const p of group.plans){
        const row=el('div','external-scope');row.append(el('strong','',p.term));
        const scope=strategy?.scopes?.find(s=>s.plan_id===p.id);
        row.append(el('span','reading-note',`${{gdelt:'GDELT',brave:'Brave News',bing:'Bing 新闻订阅 · 试用来源',rss:'订阅源'}[p.provider]||'公开来源'} · ${p.enabled?'每天检查':'已暂停'} · ${p.last_success_at?`上次成功 ${new Date(p.last_success_at).toLocaleString('zh-CN')}`:'尚未成功检查'}`));
        if((p.source_checks||[]).some(s=>s.provider==='gdelt'&&s.state!=='succeeded')&&(p.source_checks||[]).some(s=>s.provider==='bing'&&s.state==='succeeded'))row.append(el('span','reading-note','原来源暂不可用，本次已从 Bing 补充。'));
        const followup=(p.source_checks||[]).find(s=>s.request_variant==='recent');
        if(followup)row.append(el('span','reading-note',followup.state==='succeeded'?'首轮未命中近期消息，已用相同公开词按时间补查。':'首轮未命中近期消息；补查未完成，本轮覆盖不完整。'));
        const toggle=action(p.enabled?'暂停':'继续',async()=>{toggle.disabled=true;try{await request(`/api/projects/${state.current.id}/monitoring/plans/${p.id}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!p.enabled})});await onChanged();}catch(err){row.append(el('p','error-inline',err.message));toggle.disabled=false;}});row.append(toggle);
        if(scope){
          const context=el('div','external-scope-context');
          context.append(el('p','external-summary',scope.question),el('p','reading-note',scope.rationale));
          const refs=scope.source_refs||[];
          if(refs.length)context.append(el('p','reading-note',`依据：${[...new Set(refs.map(r=>`${r.reference} · ${r.locator}`))].join('；')}`));
          row.append(context);
        }
        scopes.append(row);
      }body.append(scopes);
    }
    if(!group.items.length){body.append(el('p','reading-empty',group.coverage==='not_configured'?`添加一个${group.title==='公司'?'公司名称':group.title==='竞品'?'竞品名称':'公开关键词'}，跟踪相关消息。`:'目前没有可展示的近期匹配消息。这不代表项目没有变化。'));return;}
    const order=el('nav','external-order');order.setAttribute('aria-label','消息顺序');
    for(const [value,label] of [['priority','经营相关优先'],['all','全部 · 按时间']]){
      const button=action(label,()=>{
        const open=new Set([...body.querySelectorAll('details[open]')].map(n=>n.dataset.topic));
        mode=value;root.dataset.newsMode=mode;select(key);
        for(const detail of body.querySelectorAll('details[data-topic]'))detail.open=open.has(detail.dataset.topic);
        body.querySelector(`button[data-news-mode="${value}"]`)?.focus({preventScroll:true});
      });button.dataset.newsMode=value;button.setAttribute('aria-pressed',String(mode===value));order.append(button);
    }
    body.append(order,el('p','reading-note','按标题主题整理；主体、事项进展及项目影响仍以原文核对为准。'));
    const ordered=readingNews({project_id:data.project_id,categories:[group]},state.current.id,mode);
    let generalHeading=false;
    for(const item of ordered){
      const attention=attentionFor(item);
      if(mode==='priority'&&!attention&&!generalHeading){body.append(el('h4','external-other-heading','其他近期消息'));generalHeading=true;}
      const article=el('article','external-story');article.dataset.readingAnchor=`external-${item.id}`;
      article.append(el('p','reading-note',`${new Date(item.published_at).toLocaleDateString('zh-CN')} · ${attention?.label||'公开报道'}`),el('h3','',item.title));
      const analysis=analysisFor(item,state);
      const raw=el('details','external-original');
      raw.dataset.topic=`external-original-${item.id}`;raw.open=expanded.has(raw.dataset.topic);raw.ontoggle=()=>{if(!raw.isConnected)return;if(raw.open)expanded.add(raw.dataset.topic);else expanded.delete(raw.dataset.topic);};raw.append(el('summary','','消息摘要与匹配依据'));
      if(item.summary)raw.append(el('p','external-summary',item.summary));
      if(attention)raw.append(el('p','external-reading-reason',attention.reason));
      raw.append(el('p','external-relevance',item.relevance));
      try{const url=new URL(item.url);if(['http:','https:'].includes(url.protocol)){const link=el('a','text-button',`${url.hostname} · 阅读原文 ↗`);link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';article.append(link);}}catch{}
      appendExternalAnalysis(article,item,state,{openSource,expanded});
      if(!analysis){const summary=compactStoryText(null,item.summary||item.relevance),copy=el('p','story-connection');copy.append(el('span','story-attribution',summary.label),document.createTextNode(summary.text));article.append(copy);}
      (article.querySelector('.story-analysis')||article).append(raw);
      appendInternalContext(raw,item,state,{openSource,expanded});
      body.append(article);
    }
  }
}

export function openFocusEditor({projectId,projectName,category,request,onChanged}){
  const dialog=el('dialog','profile-editor external-editor'),form=el('form'),body=el('div','profile-edit-body');
  const title=el('h2','','添加公开关注');title.id='external-focus-title';dialog.setAttribute('aria-labelledby',title.id);
  const error=el('p','error-inline');error.role='alert';
  const controls=el('div','dialog-actions'),close=action('取消',()=>dialog.close()),next=el('button','primary-button','查看查询范围');next.type='submit';controls.append(close,next);
  const field=(title,input)=>{const label=el('label','profile-input');label.append(el('span','',title),input);body.append(label);return input;};
  const type=field('关注方向',el('select'));
  for(const [value,label] of Object.entries(categories)){const o=el('option','',label);o.value=value;type.append(o);}type.value=category;
  const term=field('公开关注词',el('input'));term.maxLength=120;term.required=true;term.minLength=2;term.value=category==='company'?projectName:'';term.placeholder=category==='competitors'?'输入竞品名称':'输入公司名称或公开议题';
  const provider=field('信息来源',el('select'));for(const [value,label] of [['gdelt','GDELT · 无需密钥，覆盖有限'],['brave','Brave News · 需自行配置额度']]){const o=el('option','',label);o.value=value;provider.append(o);}
  const disclosure=el('p','reading-note');body.append(disclosure);
  let preview=null,busy=false;
  const invalidate=()=>{preview=null;disclosure.textContent='';next.textContent='查看查询范围';};for(const input of [type,term,provider])input.addEventListener('input',invalidate);
  form.append(title,body,error,controls);dialog.append(form);document.body.append(dialog);
  dialog.addEventListener('close',()=>dialog.remove(),{once:true});dialog.addEventListener('cancel',e=>{if(busy)e.preventDefault();});dialog.showModal();term.focus();
  form.onsubmit=async event=>{
    event.preventDefault();if(busy)return;busy=true;next.disabled=true;close.disabled=true;error.textContent='';
    for(const input of [type,term,provider])input.disabled=true;
    try{
      const selection={category:type.value,term:term.value.trim(),provider:provider.value};
      const path=`/api/projects/${projectId}/monitoring/focus`;
      if(!preview){preview=await request(`${path}?${new URLSearchParams(selection)}`);if(!dialog.isConnected)return;term.value=preview.term;disclosure.textContent=preview.disclosure;if(!preview.eligible){preview=null;throw Error('该来源尚未配置或额度不足，请到设置中检查。');}next.textContent='确认并开始跟踪';}
      else{await request(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(focusPayload(preview,selection,true))});await onChanged();dialog.close();}
    }catch(err){error.textContent=err.message;}
    finally{busy=false;next.disabled=false;close.disabled=false;for(const input of [type,term,provider])input.disabled=false;}
  };
}

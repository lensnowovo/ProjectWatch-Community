import {externalConsentPayload,analysisStatusCopy} from './external_analysis.mjs';
const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
const action=(text,fn)=>{const n=el('button','text-button',text);n.type='button';n.onclick=fn;return n;};
const check=text=>{const wrap=el('label','consent-check'),input=el('input');input.type='checkbox';wrap.append(input,el('span','',text));return {wrap,input};};
const field=(text,type,value='')=>{const wrap=el('label','profile-input'),input=el(type==='select'?'select':'input');if(type!=='select')input.type=type;input.value=value;wrap.append(el('span','',text),input);return {wrap,input};};

export async function openExternalAnalysisConsent({projectId,sources,external,request,onChanged,openSettings}){
  const dialog=el('dialog','profile-editor reading-consent external-analysis-consent'),form=el('form'),body=el('div','profile-edit-body');
  const title=el('h2','','AI 解读设置');title.id='external-analysis-title';dialog.setAttribute('aria-labelledby',title.id);
  const error=el('p','error-inline');error.role='alert';const footer=el('div','dialog-actions');
  const close=action('关闭',()=>dialog.close());footer.append(close);form.append(title,body,error,footer);dialog.append(form);document.body.append(dialog);
  let busy=false,viewed=null,requestKey='',lastSignature='';
  form.onsubmit=e=>e.preventDefault();dialog.addEventListener('close',()=>dialog.remove(),{once:true});dialog.addEventListener('cancel',e=>{if(busy)e.preventDefault();});dialog.showModal();
  body.append(el('p','reading-note','正在读取设置…'));
  const route=`/api/workspace/projects/${projectId}/external-analysis`;
  let status;try{status=await request(route);}catch(err){body.replaceChildren();error.textContent=err.message;return;}
  if(!dialog.isConnected)return;body.replaceChildren();
  body.append(el('p','reading-note',analysisStatusCopy(status)),action('模型与服务设置 ↗',()=>{dialog.close();openSettings();}));
  const token=field('本机管理口令','password');token.input.autocomplete='off';body.append(token.wrap);
  async function run(fn){
    if(busy)return;error.textContent='';if(!token.input.value){error.textContent='请填写本机管理口令。';token.input.focus();return;}
    busy=true;let focusAfter=null;const controls=[...form.querySelectorAll('input,select,button')].map(n=>[n,n.disabled]);controls.forEach(([n])=>n.disabled=true);
    try{focusAfter=await fn();}catch(err){error.textContent=err.message;}finally{busy=false;controls.forEach(([n,disabled])=>n.disabled=disabled);if(dialog.isConnected)focusAfter?.focus();}
  }
  const send=(path,method,payload)=>request(path,{method,headers:{'Content-Type':'application/json','X-Settings-Token':token.input.value},body:JSON.stringify(payload)});
  const grants=status.authorizations||[];
  if(grants.length){
    const history=el('details','consent-existing');history.open=true;history.append(el('summary','','已开启的范围与用量'));
    for(const grant of grants){
      const row=el('div','consent-grant');row.append(el('strong','',({active:'已开启',paused:'已暂停',revoked:'已撤销'})[grant.status]),el('p','reading-note',`${grant.recipient.base_url} · ${grant.recipient.model}\n${Object.keys(grant.scope.plan_scopes).length} 个公开关注范围 · ${grant.scope.material_refs.length} 段背景 · 已用 ${grant.requests_used} / ${grant.limits.max_requests} 次`));
      if(grant.limits.max_cost_cny!==null)row.append(el('p','reading-note',`预算 ¥${grant.limits.max_cost_cny} · 已预留 ¥${grant.reserved_cny}`));
      for(const [next,label] of (grant.status==='active'?[['paused','暂停'],['revoked','撤销']]:grant.status==='paused'?[['active','继续解读'],['revoked','撤销']]:[])){
        row.append(action(label,()=>run(async()=>{await send(`${route}/authorization/${grant.id}`,'PATCH',{status:next});await onChanged();dialog.close();})));
      }history.append(row);
    }body.append(history);
  }
  if(!status.automatic_analysis_available){body.append(el('p','reading-note','后台解读尚未启动，暂不能开启新范围。'));return;}
  if(status.processing?.reason==='model_missing')return;
  const groups=external?.project_id===projectId?external.categories:[];
  const plans=[...new Map(groups.flatMap(g=>g.plans.map(p=>({...p,label:`${g.title} · ${p.term}`}))).map(p=>[p.id,p])).values()];
  if(!plans.length){body.append(el('p','reading-note','先在外部动态中添加公开关注，再开启解读。'));return;}
  body.append(el('h3','','公开消息范围'));
  const choices=plans.map(plan=>{const c=check(plan.label+(plan.enabled?'':' · 已暂停'));c.input.checked=plan.enabled;body.append(c.wrap);return {...c,id:plan.id};});
  const future=check('也解读这些关注范围以后收到的新消息');body.append(future.wrap);
  const articles=check('读取公开新闻正文后解读；读不到时使用标题和摘要');articles.input.checked=true;body.append(articles.wrap);
  const selections=[],context=el('details','consent-existing');context.append(el('summary','','附带项目背景（可选）'),el('p','reading-note','只发送你选中的片段，不发送整个项目、以后新增的文件或人的判断。最多选择 3 段。'));
  const documents=sources.filter(s=>s.project_id===projectId&&s.kind==='document');
  const source=field('选择背景材料','select');source.input.append(el('option','','请选择材料'));
  for(const doc of documents){const o=el('option','',doc.reference);o.value=doc.id;source.input.append(o);}context.append(source.wrap);
  const fragment=field('选择正文片段','select');fragment.input.disabled=true;context.append(fragment.wrap);
  const excerpt=el('p','consent-background-preview'),selected=el('div');let fragments=[];
  const add=action('加入这段背景',()=>{
    error.textContent='';const f=fragments.find(f=>String(f.ordinal)===fragment.input.value);
    if(!f||f.text.trim().length<4){error.textContent='请选择有正文的片段。';return;}
    if(selections.length>=3){error.textContent='最多附带 3 段背景。';return;}
    if(selections.some(s=>s.source_id===source.input.value&&s.ordinal===f.ordinal))return;
    selections.push({source_id:source.input.value,ordinal:f.ordinal,offset:0,length:Math.min(3000,f.text.length),label:documents.find(s=>s.id===source.input.value).reference+' · '+f.locator});
    renderSelected();invalidate();
  });add.disabled=true;context.append(excerpt,add,selected);body.append(context);
  function renderSelected(){selected.replaceChildren();for(const [index,s] of selections.entries()){const row=el('div','consent-selected');row.append(el('span','',s.label),action('移除',()=>{selections.splice(index,1);renderSelected();invalidate();}));selected.append(row);}}
  source.input.onchange=async()=>{
    fragments=[];fragment.input.replaceChildren();fragment.input.disabled=true;add.disabled=true;excerpt.textContent='';
    if(!documents.some(s=>s.id===source.input.value))return;
    const id=source.input.value;
    try{const data=await request(`/api/projects/${projectId}/sources/${id}/fragments`);if(!dialog.isConnected||source.input.value!==id)return;
      fragments=data.filter(f=>f.source_id===id&&f.project_id===projectId&&f.text?.trim().length>=4);
      for(const f of fragments){const o=el('option','',`${f.locator} · ${f.text.slice(0,45)}`);o.value=String(f.ordinal);fragment.input.append(o);}
      fragment.input.disabled=!fragments.length;add.disabled=!fragments.length;showFragment();
    }catch(err){error.textContent=err.message;}
  };
  function showFragment(){const f=fragments.find(f=>String(f.ordinal)===fragment.input.value);excerpt.textContent=f?f.text.slice(0,3000)+(f.text.length>3000?'\n（仅选取本段前 3000 字）':''):'此材料尚无可用正文。';}
  fragment.input.onchange=showFragment;
  const quota=field('本次调用上限','number','20');quota.input.min='1';quota.input.max='1000';quota.input.step='1';body.append(quota.wrap);
  const money=el('details','consent-existing');money.append(el('summary','','金额上限（可选）'));
  const prices=[field('预算（元）','number'),field('输入价格（元 / 百万 tokens）','number'),field('输出价格（元 / 百万 tokens）','number')];
  for(const p of prices){p.input.min='0.000001';p.input.step='any';money.append(p.wrap);}body.append(money);
  const preview=el('section','analysis-disclosure');body.append(preview);
  const confirm=check('我同意将以上消息与所选背景发送给所示服务。');confirm.wrap.hidden=true;body.append(confirm.wrap);
  const next=el('button','primary-button','查看发送范围');next.type='submit';footer.prepend(next);
  function invalidate(){viewed=null;confirm.input.checked=false;confirm.wrap.hidden=true;preview.replaceChildren();next.textContent='查看发送范围';}
  for(const c of [...choices,future,articles,quota,...prices])c.input.addEventListener('input',invalidate);
  function draft(){
    const count=Number(quota.input.value);if(!Number.isInteger(count)||count<1||count>1000)throw Error('调用上限为 1–1000 次。');
    const value={plan_ids:choices.filter(c=>c.input.checked).map(c=>c.id),selections:selections.map(({label,...s})=>s),include_future_news:future.input.checked,read_articles:articles.input.checked,max_requests:count};
    if(!value.plan_ids.length)throw Error('请至少选择一个公开关注范围。');
    if(prices.some(p=>p.input.value)){if(prices.some(p=>!p.input.value||!Number.isFinite(Number(p.input.value))||Number(p.input.value)<=0))throw Error('请填写完整、正数的预算与输入输出价格。');[value.max_cost_cny,value.input_cny_per_million,value.output_cny_per_million]=prices.map(p=>p.input.value);}
    return value;
  }
  form.onsubmit=e=>{e.preventDefault();run(async()=>{
    const value=draft();
    if(!viewed){
      const result=await send(route+'/preview','POST',value);if(!dialog.isConnected)return;
      viewed={...result,draft:JSON.stringify(value)};preview.replaceChildren(el('h3','','将发送给'),el('p','consent-recipient',`${result.recipient.base_url}\n${result.recipient.model}`),el('p','reading-note',`${value.include_future_news?'当前及未来':'当前已收到的'}选定范围消息。${result.source_note} 每次最多输出 ${result.limits.max_output_tokens} tokens；最多 ${value.max_requests} 次调用。`));
      for(const ref of result.scope.material_refs)preview.append(el('p','reading-note',`背景 · ${ref.locator}`),el('blockquote','',ref.text));
      if(!result.scope.material_refs.length)preview.append(el('p','reading-note','不附带私有材料；项目关联不足时会明确说明。'));
      preview.append(el('p','reading-note','不会自动确认正式事实。暂停阻止新请求，已发送的请求无法撤回；结果不明时不自动重复计费调用。'));
      confirm.wrap.hidden=false;next.textContent='确认并开启解读';return confirm.input;
    }else{
      const signature=JSON.stringify(value);if(signature!==lastSignature){requestKey=crypto.randomUUID();lastSignature=signature;}
      const payload=externalConsentPayload(value,viewed,confirm.input.checked,requestKey);
      await send(route+'/authorization','POST',payload);await onChanged();dialog.close();
    }
  });};
}

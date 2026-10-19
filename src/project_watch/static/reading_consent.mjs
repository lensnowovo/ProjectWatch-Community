// Local explicit egress consent. No credentials or private scope in browser storage.
const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
const button=(text,fn)=>{const n=el('button','text-button',text);n.type='button';n.onclick=fn;return n;};

export function consentPayload(status, {hashes, includeFuture, maxRequests, consent, requestKey}) {
  const count=Number(maxRequests);
  if(!status.model || !status.automatic_reading_available)throw Error('请先连接 AI 并启动后台整理。');
  if(!status.rule_version || !Number.isInteger(status.model.max_output_tokens) || status.model.max_output_tokens<1)throw Error('请重新打开自动整理窗口，核对最新用量上限。');
  if(consent!==true)throw Error('请确认材料发送范围。');
  if(!includeFuture&&!hashes.length)throw Error('请选择要整理的材料。');
  if(!Number.isInteger(count)||count<1||count>1000)throw Error('调用上限为 1–1000 次。');
  return {consent:true,config_id:status.model.config_id,request_key:requestKey,
    rule_version:status.rule_version,max_output_tokens:status.model.max_output_tokens,
    source_hashes:[...new Set(hashes)].sort(),include_future:includeFuture===true,max_requests:count};
}

export async function openReadingConsent({projectId,sources,request,onChanged,openSettings}) {
  const dialog=el('dialog','profile-editor reading-consent'),form=el('form'),body=el('div','profile-edit-body');
  const title=el('h2','','自动整理材料');title.id='reading-consent-title';dialog.setAttribute('aria-labelledby',title.id);
  const error=el('p','error-inline');error.role='alert';
  const footer=el('div','dialog-actions'),close=button('关闭',()=>dialog.close());footer.append(close);
  form.append(title,body,error,footer);dialog.append(form);document.body.append(dialog);
  form.onsubmit=event=>event.preventDefault();
  let busy=false,status=null,lastSignature='',requestKey='';
  dialog.addEventListener('close',()=>dialog.remove(),{once:true});
  dialog.addEventListener('cancel',e=>{if(busy)e.preventDefault();});dialog.showModal();
  body.append(el('p','reading-note','正在读取设置…'));
  const route=`/api/workspace/projects/${projectId}/reading-authorization`;
  const field=(label,type,value='')=>{const wrap=el('label','profile-input');wrap.append(el('span','',label));const input=el('input');input.type=type;input.value=value;wrap.append(input);return {wrap,input};};
  const check=(label)=>{const wrap=el('label','consent-check'),input=el('input');input.type='checkbox';wrap.append(input,el('span','',label));return {wrap,input};};
  try{status=await request(route);}catch(err){body.replaceChildren();error.textContent=err.message;return;}
  if(!dialog.isConnected)return;
  body.replaceChildren();
  body.append(el('p','reading-note','仅生成带出处的公司情况，不确认正式事实，也不替换你手动保留的内容。'));
  if(!status.model){body.append(el('p','reading-note','尚未连接 AI。请先在设置中选择接收服务和模型。'),button('打开设置',()=>{dialog.close();openSettings();}));}
  else {
    body.append(el('h3','','材料接收方'),el('p','consent-recipient',`${status.model.recipient.base_url}\n${status.model.recipient.model}`));
    body.append(el('p','reading-note',`发送文件名称、解析后的正文和位置标记。长文件分段发送，每段最多输出 ${status.model.max_output_tokens} tokens；调用可能产生费用。`));
  }
  const token=field('本机管理口令','password');token.input.autocomplete='off';
  if(status.model||status.authorizations.length)body.append(token.wrap);
  async function change(grant,next){
    if(busy)return;
    if(!token.input.value){error.textContent='请填写本机管理口令。';token.input.focus();return;}
    busy=true;close.disabled=true;error.textContent='';
    try{await request(`${route}/${grant.id}`,{method:'PATCH',headers:{'Content-Type':'application/json','X-Settings-Token':token.input.value},body:JSON.stringify({status:next})});await onChanged();dialog.close();}
    catch(err){error.textContent=err.message;}
    finally{busy=false;close.disabled=false;}
  }
  if(status.authorizations.length){
    const existing=el('details','consent-existing');existing.append(el('summary','','已有授权与用量'));existing.open=true;
    for(const grant of status.authorizations){
      const row=el('div','consent-grant');const matching=grant.config_id===status.model?.config_id&&grant.rule_version===status.rule_version;
      row.append(el('strong','',({active:'已开启',paused:'已暂停',revoked:'已撤销'})[grant.status]),el('p','reading-note',`${grant.recipient.base_url} · ${grant.model}\n${grant.include_future?'本项目现有及以后加入的全部材料':`选定的 ${grant.source_hashes.length} 个材料版本`} · 已调用 ${grant.requests_used} / ${grant.limits.max_requests} 次`));
      if(grant.limits.max_cost_cny!==null)row.append(el('p','reading-note',`预算上限 ¥${grant.limits.max_cost_cny} · 预留 ¥${grant.reserved_cny}`));
      if(!matching&&grant.status!=='revoked')row.append(el('p','reading-note','接收方或整理方式已变化，需要重新确认后才能继续。'));
      if(grant.status==='active')row.append(button('暂停',()=>change(grant,'paused')));
      if(grant.status==='paused'&&matching)row.append(button('继续整理',()=>change(grant,'active')));
      if(grant.status!=='revoked')row.append(button('撤销授权',()=>change(grant,'revoked')));
      existing.append(row);
    }body.append(existing);
  }
  if(!status.model||!status.automatic_reading_available){if(status.model)body.append(el('p','reading-note','后台整理尚未启动，暂不能开启新授权。'));return;}
  body.append(el('h3','','本次整理范围'));
  const documents=sources.filter(s=>s.project_id===projectId&&s.kind==='document'),choices=[];
  const files=el('div','consent-files');
  for(const source of documents){const c=check(source.reference);choices.push({input:c.input,hash:source.content_sha256});files.append(c.wrap);}
  if(!documents.length)files.append(el('p','reading-note','还没有材料。可以先授权后续加入的文件。'));
  body.append(files);
  const all=button('选择当前全部材料',()=>{for(const c of choices)c.input.checked=true;});body.append(all);
  const future=check('包括本项目现有及以后加入的全部材料');body.append(future.wrap);
  future.input.onchange=()=>{for(const c of choices)c.input.disabled=future.input.checked;all.disabled=future.input.checked;};
  const quota=field('本次调用上限 · 长文件可能使用多次调用','number','20');quota.input.min='1';quota.input.max='1000';quota.input.step='1';body.append(quota.wrap);
  const money=el('details','consent-existing');money.append(el('summary','','另设金额上限（可选）'));
  const prices=[field('预算上限（元）','number'),field('输入价格（元 / 百万 tokens）','number'),field('输出价格（元 / 百万 tokens）','number')];
  for(const p of prices){p.input.min='0.000001';p.input.step='any';money.append(p.wrap);}body.append(money);
  body.append(el('p','reading-note','额度用完即停止；暂停后不再发送新请求，已发送的请求无法撤回。金额按你填写的价格保守估算，以服务商账单为准。'));
  const confirm=check('我同意将上述范围的材料发送给所示服务，用于本项目内容整理。');body.append(confirm.wrap);
  const save=el('button','primary-button','确认并开启');save.type='submit';footer.prepend(save);
  form.onsubmit=async event=>{
    event.preventDefault();if(busy)return;error.textContent='';let payload;
    try{
      if(!token.input.value)throw Error('请填写本机管理口令。');
      payload=consentPayload(status,{hashes:choices.filter(c=>c.input.checked).map(c=>c.hash),includeFuture:future.input.checked,maxRequests:quota.input.value,consent:confirm.input.checked,requestKey:'pending'});
      if(prices.some(p=>p.input.value)){if(prices.some(p=>!p.input.value||!Number.isFinite(Number(p.input.value))||Number(p.input.value)<=0))throw Error('请填写完整的预算和输入、输出价格，且均大于零。');
        [payload.max_cost_cny,payload.input_cny_per_million,payload.output_cny_per_million]=prices.map(p=>p.input.value);}
      const signature=JSON.stringify(payload);if(signature!==lastSignature){requestKey=crypto.randomUUID();lastSignature=signature;}payload.request_key=requestKey;
    }catch(err){error.textContent=err.message;return;}
    busy=true;save.disabled=true;close.disabled=true;
    try{await request(route,{method:'POST',headers:{'Content-Type':'application/json','X-Settings-Token':token.input.value},body:JSON.stringify(payload)});await onChanged();dialog.close();}
    catch(err){error.textContent=err.message;}
    finally{busy=false;save.disabled=false;close.disabled=false;}
  };
}

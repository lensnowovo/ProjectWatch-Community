// An unfinished response is not silently retried or presented as a conclusion.
const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
export function failureCopy(code){
  const evidence={citation_mismatch:'回复中的引文与原文不一致',unsupported_number:'回复中的数字缺少对应引用',evidence_scope:'项目关联缺少两侧的引用依据',unsupported_impact:'影响判断缺少项目关联依据'};
  if(evidence[code])return evidence[code];
  return ({incomplete_response:'回复没有完整返回',invalid_json:'回复格式未能识别',output_schema:'回复缺少必要内容',evidence_validation:'引用或结论未通过证据检查',transport:'模型服务连接未完成',scope_changed:'背景或授权发生了变化',request_blocked:'请求未通过发送检查',executor_unavailable:'AI 服务未能完成运行',worker_interrupted:'服务中断，未取得完整结果',legacy_unknown:'旧记录未保存具体原因',unknown:'本次未取得可用结果'})[code]||'本次未取得可用结果';
}
export function retryPayload(viewed,confirmed){
  if(!confirmed)throw Error('请确认新增一次模型请求。');
  if(!viewed||!/^[a-f0-9]{64}$/.test(viewed.disclosure_digest))throw Error('请先核对本次发送内容。');
  return {consent:true,disclosure_digest:viewed.disclosure_digest};
}
export function appendAnalysisRecovery(root,state,{request,onChanged}){
  const data=state.externalAnalysis;
  if(state.externalAnalysisError||data?.project_id!==state.current?.id||!data.failures?.length)return;
  const detail=el('details','reading-more analysis-recovery');
  detail.dataset.topic='analysis-recovery';
  detail.append(el('summary','',`${data.failures.length} 条消息尚未完成解读`),el('p','reading-note','公开消息仍可阅读。已发送的请求不会自动重跑。'));
  for(const item of data.failures){
    const row=el('div','consent-grant');row.append(el('strong','',item.title),el('p','reading-note',failureCopy(item.code)));
    const retry=el('button','text-button','再试一次');retry.type='button';
    retry.onclick=()=>openAnalysisRetry({projectId:state.current.id,item,request,onChanged});
    row.append(retry);detail.append(row);
  }
  root.append(detail);
}
export function openAnalysisRetry({projectId,item,request,onChanged}){
  const dialog=el('dialog','profile-editor reading-consent'),form=el('form'),body=el('div','profile-edit-body');
  const title=el('h2','','重新解读这条消息');title.id='analysis-retry-title';dialog.setAttribute('aria-labelledby',title.id);
  body.append(el('p','',item.title),el('p','reading-note',failureCopy(item.code)),el('p','reading-note','沿用这条消息及原先选定的背景，不添加其他资料。新增请求可能产生费用，原记录与已用额度仍保留。'));
  const tokenLabel=el('label','profile-input'),token=el('input');token.type='password';token.autocomplete='off';tokenLabel.append(el('span','','本机管理口令'),token);body.append(tokenLabel);
  const preview=el('div','analysis-disclosure'),confirmLabel=el('label','consent-check'),confirm=el('input');confirm.type='checkbox';confirmLabel.append(confirm,el('span','','同意按上述范围新增一次请求'));confirmLabel.hidden=true;body.append(preview,confirmLabel);
  const error=el('p','error-inline');error.role='alert';const footer=el('div','dialog-actions'),next=el('button','primary-button','核对发送内容'),close=el('button','text-button','取消');close.type='button';next.type='submit';close.onclick=()=>dialog.close();footer.append(next,close);
  form.append(title,body,error,footer);dialog.append(form);document.body.append(dialog);
  const route=`/api/workspace/projects/${projectId}/external-analysis/jobs/${item.id}`;let busy=false,viewed=null;
  dialog.addEventListener('close',()=>{token.value='';dialog.remove();},{once:true});dialog.addEventListener('cancel',e=>{if(busy)e.preventDefault();});
  form.onsubmit=async e=>{
    e.preventDefault();if(busy)return;error.textContent='';
    if(!token.value){error.textContent='请填写本机管理口令。';token.focus();return;}
    busy=true;next.disabled=close.disabled=token.disabled=confirm.disabled=true;
    try{
      const options={method:'POST',headers:{'Content-Type':'application/json','X-Settings-Token':token.value}};
      if(!viewed){
        const value=await request(route+'/retry-preview',options);if(!dialog.isConnected)return;viewed=value;
        preview.replaceChildren(el('p','consent-recipient',`${value.recipient.model} · ${value.recipient.base_url}`),el('p','reading-note',`最多新增 1 次，计入原有 ${value.limits.max_requests} 次额度。`));
        const refs=el('details','reading-more');refs.append(el('summary','','本次发送的消息与背景'));
        for(const ref of value.references)refs.append(el('p','reading-note',ref.kind==='internal'?`项目背景 · ${ref.locator}`:'公开消息'),el('blockquote','',ref.text));
        preview.append(refs);confirmLabel.hidden=false;next.textContent='确认并重试';
      }else{
        await request(route+'/retry',{...options,body:JSON.stringify(retryPayload(viewed,confirm.checked))});
        await onChanged();dialog.close();
      }
    }catch(err){error.textContent=err.message;}finally{busy=false;next.disabled=close.disabled=token.disabled=confirm.disabled=false;}
  };
  dialog.showModal();token.focus();
}

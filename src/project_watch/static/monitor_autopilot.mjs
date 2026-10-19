import {waitForBp} from './bp_intake.mjs';
const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
const action=(text,fn)=>{const n=el('button','text-button',text);n.type='button';n.onclick=fn;return n;};
export function selectedPacket(projectId,source,choices){
  const excerpts=choices.filter(c=>c.selected).map((c,i)=>({id:`bp-${i+1}`,locator:c.locator,text:c.text.trim()}));
  if(!source||!excerpts.length||excerpts.length>80||excerpts.some(e=>!e.text||e.text.length>6000)||excerpts.reduce((n,e)=>n+e.text.length,0)>60000)throw Error('请选择公司、产品等必要片段；单段不超过 6000 字。');
  return {project_id:projectId,source_id:source.id,source_sha256:source.content_sha256,reference:source.reference,excerpts};
}
export const workflowCopy=state=>({queued:'准备理解 BP',running:'正在理解项目',dispatched:'正在理解项目',succeeded:'正在建立监控',activating:'正在建立监控',collecting:'正在查找公开动态',checked:'已完成检查，可查看外部动态',partial_coverage:'部分方向已检查，其余方向保持暂停',partial_failure:'部分来源暂不可用，已有内容保留',uncertain:'本次响应未能确认，未自动重复调用',failed:'本次未完成',activation_failed:'监控范围未能建立，已保留分析结果',strategy_changed:'检索方式已升级，请重新确认 BP',material_changed:'材料已换版，请重新确认',paused:'已暂停'})[state]||'状态待确认';

export async function openMonitorAutopilot({projectId,sources,request,onChanged,openSettings}){
  const dialog=el('dialog','profile-editor reading-consent'),form=el('form'),body=el('div','profile-edit-body'),error=el('p','error-inline'),footer=el('div','dialog-actions');
  const title=el('h2','','从 BP 开始监控');title.id='bp-monitor-title';dialog.setAttribute('aria-labelledby',title.id);error.role='alert';
  const closeButton=action('关闭',()=>dialog.close());footer.append(closeButton);form.append(title,body,error,footer);dialog.append(form);document.body.append(dialog);dialog.showModal();form.onsubmit=e=>e.preventDefault();
  let busy=false,view=null,choices=[],source=null,timer=null,selectionGeneration=0;
  dialog.addEventListener('close',()=>{clearInterval(timer);dialog.remove();},{once:true});
  const route=`/api/workspace/projects/${projectId}/monitor-autopilot`;
  const statusBox=el('div','consent-existing');body.append(statusBox);
  const tokenLabel=el('label','profile-input','本机管理口令'),token=el('input');token.type='password';token.autocomplete='off';token.setAttribute('aria-label','本机管理口令');tokenLabel.append(token);
  const send=(path,payload)=>request(path,{method:'POST',headers:{'Content-Type':'application/json','X-Settings-Token':token.value},body:JSON.stringify(payload)});
  async function run(fn){if(busy)return;busy=true;error.textContent='';const controls=[...form.querySelectorAll('input,select,textarea,button')].filter(n=>n!==closeButton).map(n=>[n,n.disabled]);controls.forEach(([n])=>n.disabled=true);try{await fn();}catch(e){error.textContent=e.message;}finally{busy=false;controls.forEach(([n,d])=>n.disabled=d);}}
  async function status(){
    const data=await request(route);if(!dialog.isConnected)return;
    statusBox.replaceChildren();
    for(const w of data.workflows||[]){const row=el('div','consent-grant');row.append(el('strong','',workflowCopy(w.state)),el('p','reading-note',`${sources.find(s=>s.id===w.source_id)?.reference||'BP'} · 已调用 ${w.requests_used} 次`));
      if(w.analysis?.enabled)row.append(el('p','reading-note',`消息解读 ${w.analysis.requests_used} / ${w.analysis.max_requests} 次${w.state==='paused'?' · 已暂停':''}`));
      if(!w.analysis?.read_articles&&!['paused','material_changed','strategy_changed'].includes(w.state))row.append(action(w.analysis?.enabled?'启用正文解读':'接通消息解读',()=>run(async()=>{view=await send(`${route}/${w.id}/analysis-preview`,{});showDisclosure(true);})));
      if(w.state==='activation_failed')row.append(action('重试建立监控（不重读 BP）',()=>run(async()=>{await send(`${route}/${w.id}/retry-activation`,{});await status();await onChanged();})));
      if(w.state!=='paused')row.append(action('暂停',()=>run(async()=>{await send(`${route}/${w.id}/pause`,{});await status();await onChanged();})));statusBox.append(row);}
    return data;
  }
  let initial;try{sources=await request(`/api/projects/${projectId}/sources`);initial=await status();}catch(e){error.textContent=e.message;return;}
  if(!dialog.isConnected)return;
  timer=setInterval(()=>{if(!busy)status().catch(()=>{});},3000);
  if(!initial?.worker_enabled){body.append(el('p','reading-note','后台处理尚未启动。'));return;}
  body.append(el('p','reading-note','选一份 BP，确认可外发的公司及产品介绍。监控方向由 AI 整理，无需填写关键词。'),tokenLabel,action('模型与服务设置',()=>{dialog.close();openSettings();}));
  const selectLabel=el('label','profile-input','项目 BP'),select=el('select');select.setAttribute('aria-label','项目 BP');Object.assign(select.style,{display:'block',width:'100%',minWidth:'0',padding:'10px 12px',marginTop:'8px',border:'1px solid #d8d8d4',borderRadius:'8px',background:'#fff',color:'inherit',font:'inherit'});select.append(new Option('选择已上传的 BP',''));
  for(const s of sources.filter(s=>s.kind==='document'))select.append(new Option(s.reference,s.id));selectLabel.append(select);body.append(selectLabel);
  const excerptBox=el('div','bp-monitor-excerpts'),disclosure=el('div','consent-existing');body.append(excerptBox,disclosure);
  const analysisLabel=el('label','consent-check'),analysisChoice=el('input');analysisChoice.type='checkbox';analysisChoice.onchange=()=>invalidate();analysisLabel.append(analysisChoice,el('span','','采集到消息后，自动解读与项目的关系（最多 20 次）'));disclosure.before(analysisLabel);
  const file=el('input');file.type='file';file.accept='.pdf,.pptx,.docx,.txt';file.hidden=true;
  const upload=action('上传一份新 BP',()=>file.click());selectLabel.before(upload,file);
  file.onchange=()=>run(async()=>{
    const chosen=file.files?.[0];if(!chosen)return;
    invalidate();choices=[];source=null;excerptBox.replaceChildren(el('p','reading-note','正在保存 BP…'));
    const data=new FormData();data.append('file',chosen);
    const result=await request(`/api/projects/${projectId}/files`,{method:'POST',body:data});file.value='';
    if(!dialog.isConnected)return;
    if(!sources.some(s=>s.id===result.source.id)){sources=[...sources,result.source];select.append(new Option(result.source.reference,result.source.id));}
    select.value=result.source.id;await onChanged();await loadSelected();
  });
  function invalidate(){view=null;disclosure.replaceChildren();}
  async function loadSelected(){invalidate();choices=[];excerptBox.replaceChildren();source=sources.find(s=>s.id===select.value);if(!source)return;
    const generation=++selectionGeneration;
    const fragments=await waitForBp({projectId,sourceId:source.id,request,cancelled:()=>!dialog.isConnected||generation!==selectionGeneration,
      onWaiting:()=>excerptBox.replaceChildren(el('p','reading-note','正在读取 BP，完成后会在这里显示可选内容。原件已保存在本机。'))});
    if(!fragments)return;excerptBox.replaceChildren();
    excerptBox.append(el('p','reading-note','只选必要页面。含财务明细、非公开客户或联系方式的页面不要勾选；也可在框内保留一段连续原文。'));
    const all=action('这是公开版 BP，选中全部片段',()=>{choices.forEach(c=>c.check.checked=true);invalidate();});excerptBox.append(all);
    for(const f of fragments){const detail=el('details','consent-existing'),summary=el('summary'),check=el('input'),text=el('textarea'),caption=el('span','',`${f.locator} · ${f.text.slice(0,40)}`);Object.assign(summary.style,{display:'flex',alignItems:'center',gap:'8px',minWidth:'0'});Object.assign(caption.style,{overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'});check.style.flexShrink='0';check.type='checkbox';check.setAttribute('aria-label',`发送 ${f.locator}`);text.value=f.text;text.rows=5;text.style.width='100%';text.style.boxSizing='border-box';text.setAttribute('aria-label',`${f.locator} 原文片段`);summary.append(check,caption);detail.append(summary,text);excerptBox.append(detail);check.onchange=invalidate;text.oninput=invalidate;choices.push({locator:f.locator,check,text});}
  }
  select.onchange=()=>run(loadSelected);
  footer.prepend(action('核对发送范围',()=>run(async()=>{
    if(!token.value)throw Error('请填写本机管理口令。');
    const packet=selectedPacket(projectId,source,choices.map(c=>({locator:c.locator,selected:c.check.checked,text:c.text.value})));
    view=await send(route+'/preview',{packet,automatic_analysis:analysisChoice.checked});showDisclosure(false);
  })));
  function showDisclosure(existing){
    disclosure.replaceChildren();
    disclosure.append(el('h3','','本次使用范围'),el('p','reading-note',`${view.recipient.base_url} · ${view.recipient.model}\n${view.bp_reused?'沿用已有 BP 工作流，不重复调用':`${view.packet.excerpts.length} 段内容 · BP 理解最多调用 1 次${existing?'（模型或理解规则已更新）':''}`}`),el('p','reading-note',view.notice));
    if(view.analysis){const detail=el('details','consent-existing');detail.append(el('summary','',`消息解读使用的 ${view.analysis.material_refs.length} 段 BP 原文`));for(const ref of view.analysis.material_refs)detail.append(el('p','reading-note',`${ref.locator}\n${ref.text}`));disclosure.append(el('p','reading-note',`${view.analysis.public_source_note} 解读最多 ${view.analysis.max_requests} 次，重启不重置；BP 换版后停止。仅使用下面的 BP 片段，不读取其他材料。原有摘要解读保留，本次正文解读单独计数。`),detail);}
    const label=el('label','consent-check'),consent=el('input');consent.type='checkbox';label.append(consent,el('span','','确认这些片段可外发至上述模型，其中公司及产品名称可用于公开检索。'));disclosure.append(label);
    disclosure.append(action(existing?'接通解读':'开始监控',()=>run(async()=>{if(!view||!consent.checked)throw Error('请先确认本次发送范围。');await send(route,{packet:view.packet,model_consent:true,public_consent:true,automatic_analysis:view.automatic_analysis,disclosure_digest:view.disclosure_digest});invalidate();await status();await onChanged();})));
    disclosure.scrollIntoView({block:'nearest',behavior:'smooth'});
  }
}

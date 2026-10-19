// Monitoring preferences stay local. Editing them never confirms a business fact.
const labels={business:'公司业务',stage:'发展阶段',products:'产品与服务',customers:'客户与市场',thesis:'我的判断'};
const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
const button=(text,fn)=>{const n=el('button','text-button',text);n.type='button';n.onclick=fn;return n;};

export function saveProfile(request,projectId,body){
  return request(`/api/workspace/projects/${projectId}/profile`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
}

export function profileEditPayload(profile, values, focus, restored=new Set(), restoreFocus=false){
  const fields={};
  for(const key of Object.keys(labels)){
    if(!restored.has(key)&&values[key]!==profile.fields[key].text)fields[key]=values[key];
  }
  const lines=focus.split('\n').map(t=>t.trim()).filter(Boolean);
  const focusChanged=JSON.stringify(lines)!==JSON.stringify(profile.focus.map(f=>f.text));
  return {expected_version:profile.version,fields,restore_fields:[...restored],restore_focus:restoreFocus,
    ...(focusChanged&&!restoreFocus?{focus:lines}:{})};
}

export function visibleProfileFocus(profile,sources){
  const live=new Map(sources.filter(s=>s.project_id===profile.project_id).map(s=>[s.id,s]));
  return profile.focus.filter(item=>item.origin!=='ai_synthesis'||item.review_status==='unreviewed'&&
    item.source_refs?.length&&item.source_refs.every(ref=>ref.locator&&live.get(ref.source_id)?.content_sha256===ref.version));
}

export function appendMonitorProfile(root,profile,{request,onSaved,openSource,sources,expanded=new Set()}){
  const section=el('section','monitor-profile');
  const header=el('header','reading-heading');header.append(el('h3','','项目关注'),button('调整关注',openEditor));section.append(header);
  const focusItems=visibleProfileFocus(profile,sources),list=el('ul','profile-focus');
  const focusRow=(item,index)=>{
    const row=el('li');row.dataset.readingAnchor=`profile-focus-${item.id||index}`;row.append(el('p','',item.text));
    if(item.origin==='ai_synthesis')row.append(el('small','reading-note','AI 据材料整理 · 尚未核对'));
    if(item.source_refs.length){
      const detail=el('details','profile-focus-evidence');detail.dataset.topic=row.dataset.readingAnchor;detail.open=expanded.has(detail.dataset.topic);
      detail.append(el('summary','','查看依据'));
      if(item.detail&&item.detail!==item.text)detail.append(el('p','',item.detail));
      for(const ref of item.source_refs){const source=sources.find(s=>s.id===ref.source_id&&s.project_id===profile.project_id&&s.content_sha256===ref.version);if(!source)continue;
        if(ref.quote)detail.append(el('blockquote','',ref.quote));
        detail.append(button(`${ref.reference} · ${ref.locator} ↗`,()=>openSource(source,ref.locator)));
      }row.append(detail);
    }return row;
  };
  for(const [index,item] of focusItems.slice(0,5).entries())list.append(focusRow(item,index));
  if(focusItems.length)section.append(list);
  else section.append(el('p','reading-note','还没有关注事项。可记下想持续跟踪的问题。'));
  if(focusItems.length>5){const rest=el('details');rest.dataset.topic='profile-focus-more';rest.open=expanded.has(rest.dataset.topic);rest.append(el('summary','',`另外 ${focusItems.length-5} 项`));const more=el('ul','profile-focus');for(const [index,item] of focusItems.slice(5).entries())more.append(focusRow(item,index+5));rest.append(more);section.append(rest);}
  const thesis=profile.fields.thesis;
  if(thesis.text){section.append(el('h4','','我的判断'),el('p','profile-thesis',thesis.text));}
  const details=el('details','profile-details');details.dataset.topic='profile';details.append(el('summary','','公司档案'));
  for(const key of Object.keys(labels).filter(k=>k!=='thesis')){
    const item=profile.fields[key],row=el('div','profile-field');row.append(el('strong','',labels[key]),el('p','',item.text||'尚未补充'));
    row.append(el('small','reading-note',item.origin==='human'?'手动保留 · 非正式事实':item.origin==='missing'?'':item.review_status==='confirmed'?'已核对材料':item.origin==='ai_synthesis'?'AI 据材料整理 · 尚未核对':'材料描述 · 尚未核对'));
    for(const ref of item.source_refs){const source=sources.find(s=>s.id===ref.source_id);if(source)row.append(button(`${ref.reference} · ${ref.locator} ↗`,()=>openSource(source,ref.locator)));}
    details.append(row);
  }
  if(profile.peers.length){const row=el('div','profile-field');row.append(el('strong','','已确认同行'),el('p','',profile.peers.map(p=>p.name).join('、')));details.append(row);}
  section.append(details);root.append(section);

  function openEditor(){
    const dialog=el('dialog','profile-editor'),form=el('form'),body=el('div','profile-edit-body'),inputs={},restored=new Set();let restoreFocus=false;
    dialog.setAttribute('aria-labelledby','profile-editor-title');
    const title=el('h2','','调整项目关注');title.id='profile-editor-title';
    form.append(title,el('p','reading-note','只保存在本项目。手动修改会保留；不会自动确认材料，也不会发送给外部服务。'));
    for(const [key,label] of Object.entries(labels)){
      const wrap=el('label','profile-input');wrap.append(el('span','',label));
      const input=el('textarea');input.value=profile.fields[key].text;input.maxLength=2000;input.rows=2;inputs[key]=input;wrap.append(input);body.append(wrap);
      input.oninput=()=>restored.delete(key);
      if(profile.fields[key].origin==='human'&&key!=='thesis')body.append(button('使用材料建议',()=>{input.value=profile.suggestions[key].text;restored.add(key);input.focus();}));
    }
    const focusLabel=el('label','profile-input');focusLabel.append(el('span','','关注事项 · 每行一项'));
    const focus=el('textarea');focus.rows=4;focus.value=profile.focus.map(f=>f.text).join('\n');focusLabel.append(focus);body.append(focusLabel);
    focus.oninput=()=>{restoreFocus=false;};
    if(profile.focus_is_manual)body.append(button('恢复材料与跟踪建议',()=>{focus.value=profile.suggested_focus.map(f=>f.text).join('\n');restoreFocus=true;focus.focus();}));
    form.append(body);
    const error=el('p','error-inline');error.setAttribute('role','alert');form.append(error);
    const footer=el('div','dialog-actions'),cancel=button('取消',()=>dialog.close()),save=el('button','primary-button','保存');save.type='submit';footer.append(cancel,save);form.append(footer);
    let saving=false;
    dialog.addEventListener('cancel',e=>{if(saving)e.preventDefault();});
    form.onsubmit=async e=>{
      e.preventDefault();const values=Object.fromEntries(Object.entries(inputs).map(([k,n])=>[k,n.value]));
      const body=profileEditPayload(profile,values,focus.value,restored,restoreFocus);
      if(body.focus?.length>20||body.focus?.some(t=>t.length>300)){error.textContent='关注事项最多 20 项，每项最多 300 字。';return;}
      saving=true;save.disabled=true;cancel.disabled=true;error.textContent='';
      try{const result=await saveProfile(request,profile.project_id,body);onSaved(result);dialog.close();}
      catch(err){error.textContent=err.status===409?'档案已被更新。请先复制当前修改，关闭后重新打开。':err.status===422?'请检查填写内容：每项最多 2000 字，关注事项每行最多 300 字。':err.message;}
      finally{saving=false;save.disabled=false;cancel.disabled=false;}
    };
    dialog.append(form);document.body.append(dialog);dialog.addEventListener('close',()=>dialog.remove(),{once:true});dialog.showModal();
  }
}

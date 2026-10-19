import { parseRoute, routeHash } from './navigation.mjs';
import { renderCompanyReading } from './monitor_views.mjs';
import {selectAttention} from './attention.mjs';
import {renderMonitorDashboard,splitFinancialReading} from './monitor_dashboard.mjs';

const $ = id => document.getElementById(id);
const el = (tag, cls, text) => { const n=document.createElement(tag); if(cls)n.className=cls; if(text!==undefined)n.textContent=text; return n; };
const button = (label, action, cls='secondary-button') => { const b=el('button',cls,label); b.type='button'; b.onclick=action; return b; };
const time = value => value ? new Date(value).toLocaleString('zh-CN',{dateStyle:'short',timeStyle:'short'}) : '时间未记录';
const statusName = s => ({queued:'排队中',running:'执行中',processing:'执行中',needs_ocr:'待识别',completed:'已完成',succeeded:'已完成',success:'已完成',failed:'失败',pending:'待执行',candidate:'待核对',proposed:'已提交候选',cancelled:'已取消'}[s] || s || '状态待核实');

export function createClientShell(bridge) {
  const {state,request,toast}=bridge;
  try {state.pinnedIds=new Set(JSON.parse(localStorage.getItem('pw-pinned-projects')||'[]'));}catch{state.pinnedIds=new Set();}
  let epoch=0, currentHash='', settingsReturn='#home', settingsTab='model', operator='', testedTicket=null;
  let query='', filter='open', offset=0, pendingItem=null;
  let showDemo=false;
  const positions=new Map(), panels=new Map();
  function prefs() {
    try { const p=JSON.parse(localStorage.getItem('pw-appearance')||'{}');
      document.body.classList.toggle('density-compact',p.compact===true);
      document.body.classList.toggle('text-large',p.large===true);
    } catch { /* Optional presentation preferences never block work. */ }
  }
  prefs();
  function heading(title,copy,action) {
    const h=el('header','screen-heading'), text=el('div'); text.append(el('h1','',title),el('p','',copy));h.append(text);if(action)h.append(action);return h;
  }
  function empty(box,title,copy,action) {
    const n=el('div','screen-empty');n.append(el('div','empty-symbol','◇'),el('strong','',title),el('p','',copy));if(action)n.append(action);box.append(n);
  }
  function markNavigation(route) {
    document.querySelectorAll('.primary-nav a,.settings-link').forEach(a=>{a.removeAttribute('aria-current');if(a.hash===`#${route}`)a.setAttribute('aria-current','page');});
  }
  function sync() {
    const project=state.screen==='project';
    $('client-screen').hidden=project || state.teamMode;
    $('project-screen').hidden=!project || !state.current;
    $('welcome').hidden=!state.teamMode || !!state.current;
    markNavigation(project||state.screen==='projects'?'home':state.screen);
    $('client-breadcrumb').textContent=project ? `项目 / ${state.current?.name||'载入中'}` : ({home:'项目监控',projects:'项目监控',tasks:'处理记录',settings:'设置'}[state.screen]||'项目监控');
    const select=$('project-switch');
    if(select.options.length!==state.projects.length || select.value!==state.current?.id) {
      select.replaceChildren(...state.projects.map(p=>{const o=el('option','',p.name);o.value=p.id;return o;}));
      select.value=state.current?.id||'';
    }
    $('project-add-file').hidden=!state.capabilities.propose;
    $('project-ai').hidden=!state.capabilities.propose;
    $('internal-intake').hidden=!state.capabilities.propose;
    const intakeCopy=$('drop-overlay').querySelector('small').textContent;
    $('internal-intake-note').textContent=intakeCopy.includes('合成')?'此环境仅接收合成测试资料。':state.teamMode?'原件保存在机构空间。内容整理在授权范围内进行。':'原件保存在本机。内容整理在授权范围内进行。';
    if(state.teamMode){document.querySelector('.primary-nav').hidden=true;$('client-screen').hidden=true;document.querySelector('.settings-link').hidden=true;}
  }
  function navigate(id,tab='overview') {
    if(state.teamMode){state.screen='project';state.view=tab;if(state.current?.id!==id)bridge.selectProject(id).catch(e=>toast(e.message));else{bridge.renderView();if(tab==='report')bridge.loadReport();}return;}
    location.hash=routeHash({route:'project',projectId:id,tab});
  }
  async function route() {
    const ticket=++epoch;
    if(parseRoute(location.hash).route==='settings'&&currentHash&&parseRoute(currentHash).route!=='settings')settingsReturn=currentHash;
    if(currentHash){positions.set(currentHash,window.scrollY);panels.set(currentHash,!$('reader').hidden&&state.selectedSource?{id:state.selectedSource,scroll:$('reader').scrollTop}:null);}
    currentHash=location.hash;
    const target=parseRoute(location.hash);
    if(state.teamMode) { if(target.route==='project')navigate(target.projectId,target.tab);sync();return; }
    state.screen=target.route;
    bridge.closePanel();
    for(const d of document.querySelectorAll('dialog[open]'))d.close();
    sync();
    if(target.route==='project') {
      try {
        if(!state.projects.some(p=>p.id===target.projectId)) {
          const p=await request(`/api/projects/${encodeURIComponent(target.projectId)}`);
          if(ticket!==epoch)return;state.projects.push(p);
        }
        state.view=target.tab;
        if(state.current?.id!==target.projectId) await bridge.selectProject(target.projectId);
        else {bridge.renderView(); if(target.tab==='report')await bridge.loadReport();}
        if(ticket!==epoch)return;sync();
        const savedPanel=panels.get(currentHash);
        if(savedPanel){const source=state.sources.find(s=>s.id===savedPanel.id);if(source){await bridge.openSource(source);if(ticket===epoch)$('reader').scrollTop=savedPanel.scroll;}}
        if(ticket===epoch&&pendingItem?.project_id===state.current?.id){const item=pendingItem;pendingItem=null;
          if(item.kind==='待核对'){const observation=state.observations.find(o=>`observation:${o.id}`===item.id);if(observation)bridge.openReview(observation);}
          else if(item.kind==='材料'){const source=state.sources.find(s=>s.id===item.source_id);if(source)await bridge.openSource(source);}
        }
      } catch(error) {if(ticket===epoch){state.screen='projects';sync();$('client-screen').replaceChildren(el('p','error-inline',error.message));} }
    } else {query='';offset=0;await showGlobal(target.route,ticket);}
    if(ticket===epoch)window.scrollTo(0,positions.get(currentHash)||0);
  }
  async function showGlobal(screen,ticket=++epoch) {
    const root=$('client-screen');
    if(screen==='settings'){await settings(root,ticket);return;}
    if(screen==='home'||screen==='projects'){await projectCards(root,ticket);return;}
    root.replaceChildren(heading({home:'工作台',projects:'项目',tasks:'任务'}[screen],
      {home:'',projects:'',tasks:'查看资料整理和消息检查的进度。'}[screen],
      screen==='tasks'?button('刷新',()=>showGlobal(screen)):button('＋ 添加项目',()=>bridge.newProject(),'primary-button')));
    const tools=el('div','screen-tools');
    if(screen!=='tasks'){
      const form=el('form'),input=el('input','search-input');input.type='search';input.placeholder=screen==='projects'?'搜索项目':'搜索项目或内容';input.setAttribute('aria-label',input.placeholder);input.maxLength=100;input.value=query;
      const submit=button('搜索',()=>{},'text-button');submit.type='submit';form.append(input,submit);form.onsubmit=e=>{e.preventDefault();query=input.value.trim();offset=0;showGlobal(screen);};tools.append(form);
    }
    if(screen==='home') {
      const seg=el('div','segmented');for(const [v,label] of [['open','未处理'],['done','已处理'],['ignored','已忽略'],['all','全部']]){
        const b=button(label,()=>{filter=v;offset=0;showGlobal(screen);});b.setAttribute('aria-pressed',String(filter===v));seg.append(b);
      }tools.append(seg);
    }
    root.append(tools);
    const list=el('div','list-surface');root.append(list);list.append(el('p','empty','正在读取…'));
    try {
      const endpoint={home:'events',projects:'projects',tasks:'tasks'}[screen];
      const params=new URLSearchParams({limit:'30',offset:String(offset)});if(screen!=='tasks')params.set('q',query);if(screen==='home')params.set('status',filter);
      const result=await request(`/api/workspace/${endpoint}?${params}`);
      if(ticket!==epoch)return;list.replaceChildren();
      if(!result.items.length)empty(list,screen==='home'?'当前清单已看完':screen==='projects'?'还没有匹配的项目':'暂无后台任务',screen==='home'?'新材料、待核对记录和监控线索会出现在这里。':screen==='projects'?'可以直接用项目名称开始，再逐步补充材料。':'添加材料或运行一次监控后，可以在这里查看结果。',screen==='projects'?button('添加项目',()=>bridge.newProject(),'primary-button'):null);
      if(screen==='projects' && result.items.length){
        const table=el('table','client-table');table.innerHTML='<thead><tr><th>项目</th><th>待核对</th><th>监控</th><th>最近材料</th></tr></thead>';
        const body=el('tbody');for(const p of result.items){
          const row=el('tr'),name=el('td');
          const pin=button(state.pinnedIds.has(p.id)?'★':'☆',()=>{if(state.pinnedIds.has(p.id))state.pinnedIds.delete(p.id);else state.pinnedIds.add(p.id);try{localStorage.setItem('pw-pinned-projects',JSON.stringify([...state.pinnedIds]));}catch{}bridge.renderProjects();showGlobal(screen);},'pin-project');pin.setAttribute('aria-label',`${state.pinnedIds.has(p.id)?'取消固定':'固定项目'} ${p.name}`);
          name.append(pin,button(p.name,()=>{if(!state.projects.some(v=>v.id===p.id))state.projects.push(p);navigate(p.id);},''),el('div','muted',`${p.source_count} 份材料`));
          row.append(name,el('td','',String(p.pending_count)),el('td','',p.active_monitors?`${p.active_monitors} 个来源开启`:'未开启'),el('td','muted',time(p.updated_at)));body.append(row);
        }table.append(body);list.append(table);
      } else for(const item of result.items){
        const row=el('article','inbox-row'),main=button('',async()=>{
          if(!state.projects.some(p=>p.id===item.project_id))state.projects.push({id:item.project_id,name:item.project_name});
          pendingItem=item;
          navigate(item.project_id,item.tab||(item.kind==='材料解析'?'materials':'overview'));
        },'row-main');
        main.append(el('strong','',item.title),el('small','',`${item.project_name} · ${time(item.time)}${item.error_code?' · '+item.error_code:''}`));
        row.append(el('span','row-kind',screen==='tasks'?`${item.kind} · ${statusName(item.status)}`:item.kind),main);
        const actions=el('div','row-actions');
        if(screen==='home')for(const [v,label] of (item.status==='open'?[['done','已处理'],['ignored','忽略']]:[['open','移回未处理']]))actions.append(button(label,async()=>{
          try{await request('/api/workspace/events/state',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:item.id,status:v})});await showGlobal(screen);toast(v==='done'?'已移入已处理；未改变事实审核状态':'清单已更新');}catch(e){toast(e.message);}
        },''));
        else actions.append(button('打开项目',()=>navigate(item.project_id,item.kind==='材料解析'?'materials':'overview'),''));
        row.append(actions);list.append(row);
      }
      const foot=el('div','page-foot');foot.append(el('span','',screen==='tasks'?'点击项目，可继续查看材料和结果。':`${offset+result.items.length} 项已显示`));
      const pages=el('div','row-actions');if(offset)pages.append(button('上一页',()=>{offset=Math.max(0,offset-30);showGlobal(screen);}));if(result.has_more)pages.append(button('下一页',()=>{offset+=30;showGlobal(screen);}));foot.append(pages);root.append(foot);
    }catch(e){if(ticket===epoch){list.replaceChildren(el('p','error-inline',e.message),button('重试',()=>showGlobal(screen)));}}
  }
  async function projectCards(root,ticket){
    root.replaceChildren(heading('关注的项目','了解公司近况，跟进每一次变化。',button('＋ 添加项目',()=>bridge.newProject(),'primary-button')));
    const tools=el('div','screen-tools'),form=el('form'),input=el('input','search-input');input.type='search';input.placeholder='搜索公司名称';input.setAttribute('aria-label','搜索公司名称');input.value=query;input.maxLength=100;
    const search=button('搜索',()=>{},'text-button');search.type='submit';form.append(input,search);form.onsubmit=e=>{e.preventDefault();query=input.value.trim();offset=0;showGlobal(state.screen);};tools.append(form);
    const demo=el('label','demo-filter'),check=el('input');check.type='checkbox';check.checked=showDemo;check.onchange=()=>{showDemo=check.checked;offset=0;showGlobal(state.screen);};demo.append(check,document.createTextNode('显示演示与测试项目'));tools.append(demo);root.append(tools);
    const attention=el('section','attention-section');root.append(attention);
    const list=el('section','project-card-grid');list.setAttribute('aria-label','关注的项目列表');list.append(el('p','reading-note','正在读取项目…'));root.append(list);
    root.append(attention); // Projects lead the page; news remains below the directory.
    request(`/api/workspace/attention?${new URLSearchParams({q:query,include_demo:String(showDemo)})}`).then(result=>{
      if(ticket!==epoch)return;
      const items=selectAttention(result.items);if(!items.length)return;
      attention.append(el('h2','','近期公开动态'));
      for(const item of items){const row=el('article','attention-row');row.append(el('span','attention-company',item.project_name));const read=button(item.title,()=>navigate(item.project_id,'changes'));read.append(el('small','',`${time(item.time)} · ${item.reason}`));row.append(read);attention.append(row);}
    }).catch(()=>{if(ticket===epoch)attention.append(el('p','reading-note','公开动态暂未加载，项目资料仍可阅读。'));});
    try{
      const params=new URLSearchParams({q:query,limit:'30',offset:String(offset),include_demo:String(showDemo)});
      const result=await request(`/api/workspace/projects?${params}`);if(ticket!==epoch)return;list.replaceChildren();
      if(!result.items.length)empty(list,'还没有匹配的项目',query?'换个名称试试。':'输入公司名称即可开始；已有演示项目可通过上方开关查看。',button('添加项目',()=>bridge.newProject(),'primary-button'));
      for(const p of result.items){
        const open=()=>{if(!state.projects.some(v=>v.id===p.id))state.projects.push(p);navigate(p.id);};
        const card=el('article','project-company-card'),head=el('header'),name=button(p.name,open,'company-card-title');
        const pin=button(state.pinnedIds.has(p.id)?'★':'☆',()=>{if(state.pinnedIds.has(p.id))state.pinnedIds.delete(p.id);else state.pinnedIds.add(p.id);try{localStorage.setItem('pw-pinned-projects',JSON.stringify([...state.pinnedIds]));}catch{}bridge.renderProjects();showGlobal(state.screen);},'company-pin');pin.setAttribute('aria-label',`${state.pinnedIds.has(p.id)?'取消固定':'固定项目'} ${p.name}`);head.append(name,pin);card.append(head);
        card.append(el('p','company-card-state',p.active_monitors?'正在关注公开动态':'公开动态尚未开启'));
        const update=el('div','company-card-update');update.append(el('span','section-label',p.reading_summary?'公司情况':'公司资料'),el('p','card-business-summary',p.reading_summary?.text|| (p.latest_material?'已有材料，可进入阅读':'添加公司介绍或 BP')),el('small','',p.reading_summary?(p.reading_summary.review_status==='confirmed'?'据已核对内容':'据资料整理 · 尚未核对'):'暂无已整理的业务介绍'));card.append(update);
        const counts=el('div','company-card-counts');counts.append(el('span','',`${p.source_count} 份资料`),el('span','',`${p.signal_count||0} 条公开消息`));if(p.pending_count)counts.append(el('span','',`有内容待核对`));card.append(counts);
        const foot=el('footer');foot.append(button('进入项目 →',open,'text-button'));card.append(foot);list.append(card);
      }
      const foot=el('div','page-foot');foot.append(el('span','',showDemo?'包含名称中标有“合成、演示、synthetic、demo”的项目。':'演示与测试项目已隐藏，资料未删除。'));
      const pages=el('div','row-actions');if(offset)pages.append(button('上一页',()=>{offset=Math.max(0,offset-30);showGlobal(state.screen);}));if(result.has_more)pages.append(button('下一页',()=>{offset+=30;showGlobal(state.screen);}));foot.append(pages);root.append(foot);
    }catch(e){if(ticket===epoch)list.replaceChildren(el('p','error-inline',e.message),button('重试',()=>showGlobal(state.screen)));}
  }
  function field(form,label,type='text',value='') {
    const l=el('label','',label),input=el('input');input.type=type;input.value=value;input.autocomplete='off';l.append(input);form.append(l);return input;
  }
  function value(box,label,text){const row=el('div','settings-value');row.append(el('span','',label),el('strong','',String(text??'未配置')));box.append(row);}
  async function settings(root,ticket) {
    root.replaceChildren(heading('设置','连接、运行与外观。不会自动改变已有项目的监控配置。',button('返回项目',()=>{location.hash=settingsReturn;})));
    const layout=el('div','settings-layout'),nav=el('nav','settings-nav'),body=el('section','settings-content');nav.setAttribute('aria-label','设置分组');
    for(const [id,label] of [['model','AI 服务'],['sources','监控来源'],['limits','运行与额度'],['privacy','数据与隐私'],['appearance','外观'],['advanced','高级']]){
      const b=button(label,()=>{settingsTab=id;testedTicket=null;showGlobal('settings');},'');if(settingsTab===id)b.setAttribute('aria-current','page');nav.append(b);
    }layout.append(nav,body);root.append(layout);
    body.append(el('h2','',({model:'AI 服务',sources:'监控来源',limits:'运行与额度',privacy:'数据与隐私',appearance:'外观',advanced:'运行信息'})[settingsTab]));
    if(settingsTab==='appearance'){
      const form=el('div','settings-form');
      for(const [key,label,cls] of [['compact','紧凑列表','density-compact'],['large','较大正文','text-large']]){
        const input=field(form,label,'checkbox');input.checked=document.body.classList.contains(cls);input.onchange=()=>{document.body.classList.toggle(cls,input.checked);try{localStorage.setItem('pw-appearance',JSON.stringify({compact:document.body.classList.contains('density-compact'),large:document.body.classList.contains('text-large')}));}catch{toast('浏览器未允许保存偏好');}};
      }body.append(el('p','','暖灰背景、石墨色操作按钮。设置只保存在这个浏览器。'),form);return;
    }
    if(settingsTab==='privacy'){
      body.append(el('p','','材料保存在当前服务的私有数据目录。拖入文件只进行本地解析，调用外部模型需另行确认。'));
      value(body,'API 密钥','仅当前服务进程；重启后需重新配置');value(body,'浏览器存储','外观偏好、当前与固定项目 ID，不保存密钥');value(body,'事实更新','候选经人工核对后进入正式记录');value(body,'备份','本界面未执行备份；需由部署管理员备份私有数据目录');
      body.append(button('选择项目导出简报',()=>{location.hash='#projects';}));return;
    }
    if(settingsTab==='sources'){
      body.append(el('p','','在项目的“外部动态”页设置要关注的消息和检查频率。'));
      value(body,'可用来源','公开 RSS / 可选新闻搜索接口');value(body,'搜索 API 凭据','当前由启动环境管理，不在浏览器显示');
      body.append(button('前往项目',()=>{location.hash='#projects';},'primary-button'));return;
    }
    try {
      const config=await request('/api/settings/status');if(ticket!==epoch)return;
      if(settingsTab==='limits'){
        value(body,'配置模型',config.model||'未启用');value(body,'当前进程剩余次数',config.remaining_runs??'—');value(body,'单次材料范围','一个原文片段，最多 1500 字符');
        body.append(el('p','','这是当前进程的调用限制，不是月度套餐余额。连接测试也消耗一次调用，服务重启后计数重新开始。'));return;
      }
      if(settingsTab==='advanced'){
        value(body,'执行器','DSH');value(body,'界面','中性色工作台 / 2026.10');value(body,'模型配置',config.enabled?'已启用':'未启用');value(body,'连接协议',config.protocol||'未配置');
        body.append(button('查看处理记录',()=>{location.hash='#tasks';}),button('刷新运行状态',()=>showGlobal('settings')));return;
      }
      body.append(el('p','','DSH 负责执行；模型由你选择。测试连接仅发送合成样例，不读取项目材料。'));
      value(body,'当前模型',config.model||'未启用');value(body,'接收地址',config.base_url||'未配置');
      if(!config.editable){body.append(el('p','settings-notice','当前启动方式未启用设置权限。请使用带管理口令的本机启动器；已有模型仍可继续使用。'));return;}
      if(!operator){
        const unlock=el('form','settings-form'),token=field(unlock,'本机管理口令','password');token.required=true;
        const submit=button('解锁设置',()=>{},'primary-button');submit.type='submit';unlock.append(submit);
        unlock.onsubmit=async e=>{e.preventDefault();submit.disabled=true;try{await request('/api/settings/model',{headers:{'X-Settings-Token':token.value}});operator=token.value;token.value='';showGlobal('settings');}catch(err){toast(err.message);}finally{submit.disabled=false;}};body.append(unlock);return;
      }
      const form=el('form','settings-form'),presetLabel=el('label','','服务预设'),preset=el('select');
      for(const [v,t] of [['custom','自定义'],['glm','GLM Coding Plan'],['deepseek','DeepSeek']]){const o=el('option','',t);o.value=v;preset.append(o);}presetLabel.append(preset);form.append(presetLabel);
      const url=field(form,'API 地址','url',config.base_url||''),protocolLabel=el('label','','接口协议'),protocol=el('select');
      for(const [v,t] of [['openai-completions','OpenAI Chat Completions'],['anthropic-messages','Anthropic Messages']]){const o=el('option','',t);o.value=v;protocol.append(o);}protocol.value=config.protocol||'openai-completions';protocolLabel.append(protocol);form.append(protocolLabel);
      const model=field(form,'模型 ID','text',config.model||''),key=field(form,'API Key（不回显，不落盘）','password'),runs=field(form,'本进程最多调用次数','number','20');runs.min='1';runs.max='100';[url,model,key,runs].forEach(i=>i.required=true);
      preset.onchange=()=>{if(preset.value==='glm'){url.value='https://open.bigmodel.cn/api/anthropic';protocol.value='anthropic-messages';model.value='glm-5.3';}if(preset.value==='deepseek'){url.value='https://api.deepseek.com/v1';protocol.value='openai-completions';model.value='deepseek-chat';}};
      const notice=el('p','assistant-note','测试会使用你的接口额度。启用后只影响后续调用。'),actions=el('div','settings-actions'),test=button('测试连接',()=>{},'primary-button'),activate=button('启用此配置',async()=>{
        activate.disabled=true;try{await request('/api/settings/model/activate',{method:'POST',headers:{'Content-Type':'application/json','X-Settings-Token':operator},body:JSON.stringify({ticket:testedTicket})});testedTicket=null;toast('新配置已启用，仅本次服务运行有效');showGlobal('settings');}catch(e){notice.textContent=e.message;}
      });test.type='submit';activate.disabled=true;actions.append(test,activate);form.append(notice,actions);
      let formRevision=0;
      form.oninput=()=>{formRevision++;testedTicket=null;activate.disabled=true;};
      form.onchange=()=>{formRevision++;testedTicket=null;activate.disabled=true;};
      form.onsubmit=async e=>{e.preventDefault();test.disabled=true;activate.disabled=true;notice.textContent='正在用合成样例测试连接…';
        const submittedRevision=formRevision;
        try{const r=await request('/api/settings/model/test',{method:'POST',headers:{'Content-Type':'application/json','X-Settings-Token':operator},body:JSON.stringify({base_url:url.value.trim(),protocol:protocol.value,model:model.value.trim(),key:key.value,max_runs:Number(runs.value),thinking_mode:protocol.value==='anthropic-messages'?'disabled':'provider_default'})});key.value='';if(submittedRevision!==formRevision){notice.textContent='配置已变化，请重新测试。';return;}testedTicket=r.ticket;notice.textContent=`${r.model} 测试通过。点击启用后生效。`;activate.disabled=false;}
        catch(err){key.value='';notice.textContent=err.message;}finally{test.disabled=false;}
      };body.append(form,button('锁定设置',()=>{operator='';testedTicket=null;showGlobal('settings');},'text-button'));
    }catch(e){if(ticket===epoch)body.append(el('p','error-inline',e.message),button('重试',()=>showGlobal('settings')));}
  }
  function arrangeProject() {
    const reviewGrid=el('div','review-compare'),original=el('section','review-original-pane'),decision=el('section','review-decision-pane');
    original.append(el('h3','','原文摘录'));const originalText=el('p');originalText.id='review-original';original.append(originalText);
    decision.append(el('h3','','建议记录'));
    for(const id of ['review-statement','review-metric','review-evidence','review-open-source','preview-reviewer','team-reviewer'])decision.append($(id));
    reviewGrid.append(original,decision);$('review-form').querySelector('.dialog-actions').before(reviewGrid);
    $('view-report').append(document.querySelector('.coverage-panel'),$('coverage-result'));
    const overview=el('div');overview.id='company-overview';$('view-overview').prepend(overview);
    const context=el('details','dashboard-context');context.id='dashboard-context';context.append(el('summary','','项目档案与更多记录'));
    for(const child of [...$('view-overview').children])context.append(child);
    const dashboard=el('div','monitor-dashboard');dashboard.id='monitor-dashboard';$('view-overview').append(dashboard,context);
    $('view-business').querySelector('.section-intro').after($('company-internal'));
    const companyFinance=el('div');companyFinance.id='company-finance';$('view-finance').append(companyFinance);
    for(const [cls,target] of [['activity-monitor','business'],['competition-monitor','business'],['financial-monitor','finance'],['metric-monitor','finance'],['watch-monitor','review']])$('view-'+target).append(document.querySelector('.'+cls));
    context.append(document.querySelector('.local-monitor'));
    const businessRecords=el('details','business-records');businessRecords.append(el('summary','','已核对经营记录与竞争位置'),document.querySelector('.activity-monitor'),document.querySelector('.competition-monitor'));$('view-business').append(businessRecords);
    const more=el('details','public-monitor-settings');more.append(el('summary','','公开动态与监控设置'),document.querySelector('.public-monitor'));$('view-changes').append(more);
    const finance=$('view-finance'),financeCharts=el('div');financeCharts.id='finance-charts';finance.querySelector('.section-intro').after(financeCharts);
    const financeRecords=el('details','finance-records');financeRecords.append(el('summary','','数值记录与财务核验'),finance.querySelector('.metric-monitor'),finance.querySelector('.financial-monitor'));finance.append(financeRecords);
    for(const note of document.querySelectorAll('#view-business .activity-note,#view-finance .metric-note,#view-finance .financial-note')){
      const detail=el('details','reading-method');detail.append(el('summary','','查看统计口径'));note.before(detail);detail.append(note);
    }
    const peerForm=$('competition-form'),peerDetails=el('details','reading-method');peerDetails.append(el('summary','','添加同行公司'));peerForm.before(peerDetails);peerDetails.append(peerForm);
  }
  function assistant() {
    if(!state.current)return;
    state.selectedSource=null;$('reader').hidden=false;$('reader-title').textContent='AI 助手';$('reader-meta').textContent=`当前项目 · ${state.current.name}`;
    $('reader-label').textContent='材料交流';$('reader-source-info').open=false;
    const body=$('reader-content');body.replaceChildren();
    const intro=el('div','assistant-intro');intro.append(el('p','assistant-note','选择一份材料，定位原文片段后点击“AI 提取”。发送前会展示模型、接收地址和准确范围。'));
    if(!state.sources.length)intro.append(button('添加资料',()=>$('header-upload').click(),'primary-button'));
    for(const source of state.sources.slice(0,30))intro.append(button(source.reference,()=>bridge.openSource(source)));
    body.append(intro);$('reader').scrollTop=0;
  }
  $('toggle-nav').onclick=()=>{const collapsed=document.body.classList.toggle('nav-collapsed');$('toggle-nav').setAttribute('aria-expanded',String(!collapsed));$('toggle-nav').setAttribute('aria-label',collapsed?'展开导航':'收起导航');};
  $('close-directory').onclick=()=>{if(!document.body.classList.contains('nav-collapsed'))$('toggle-nav').click();};
  const narrow=matchMedia('(max-width: 560px)');
  const fitDirectory=()=>{document.body.classList.toggle('nav-collapsed',narrow.matches);$('toggle-nav').setAttribute('aria-expanded',String(!narrow.matches));$('toggle-nav').setAttribute('aria-label',narrow.matches?'展开导航':'收起导航');};
  if(narrow.matches)fitDirectory();narrow.addEventListener('change',fitDirectory);
  $('project-switch').onchange=e=>navigate(e.target.value);
  $('project-add-file').onclick=()=>$('header-upload').click();
  $('internal-upload').onclick=()=>$('header-upload').click();
  $('project-ai').onclick=assistant;
  window.addEventListener('hashchange',()=>route().catch(e=>toast(e.message)));
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!document.querySelector('dialog[open]'))bridge.closePanel();});
  arrangeProject();
  const resize=el('div','reader-resize');resize.role='separator';resize.tabIndex=0;resize.setAttribute('aria-label','调整预览宽度');resize.setAttribute('aria-orientation','vertical');$('reader').prepend(resize);
  const width=value=>{const max=Math.max(360,Math.min(720,document.querySelector('.content-layout').clientWidth-580));const next=Math.max(360,Math.min(max,value));document.documentElement.style.setProperty('--reader-width',`${next}px`);resize.setAttribute('aria-valuemin','360');resize.setAttribute('aria-valuemax',String(max));resize.setAttribute('aria-valuenow',String(Math.round(next)));};
  document.documentElement.style.setProperty('--reader-width','440px');resize.setAttribute('aria-valuenow','440');resize.setAttribute('aria-valuemin','360');resize.setAttribute('aria-valuemax','720');
  resize.onpointerdown=e=>{resize.setPointerCapture(e.pointerId);const start=e.clientX,initial=$('reader').getBoundingClientRect().width;resize.onpointermove=p=>width(initial+start-p.clientX);resize.onpointerup=()=>{resize.onpointermove=null;};};
  resize.onkeydown=e=>{if(e.key==='ArrowLeft'||e.key==='ArrowRight'){e.preventDefault();width($('reader').getBoundingClientRect().width+(e.key==='ArrowLeft'?32:-32));}};
  function renderReading(){
    const onProfileSaved=profile=>{if(state.current?.id===profile.project_id){state.profile=profile;state.profileError=null;renderReading();}toast('已保存。');};
    renderCompanyReading(state,{navigate,openSource:bridge.openSource,request,onReadingChanged:bridge.refreshProject,openSettings:()=>{location.hash='#settings';},onProfileSaved});
    splitFinancialReading(state.current?.id);
    renderMonitorDashboard(state,{navigate,openSource:bridge.openSource,request,onProfileSaved});
  }
  return {sync,navigate,start:route,refreshProject:renderReading,tabChanged(){if(state.screen==='project'&&state.current){history.replaceState(null,'',routeHash({route:'project',projectId:state.current.id,tab:state.view}));currentHash=location.hash;}}};
}

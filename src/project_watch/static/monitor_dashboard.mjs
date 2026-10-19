// Presentation only: evidence stays version-bound; personal judgments are separate.
import {renderDashboardCharts,selectDashboardSeries} from './dashboard_charts.mjs';
import {appendReadingUpdates} from './reading_updates.mjs';
import {appendBriefing,visibleBriefing} from './monitor_briefing.mjs';
import {saveProfile} from './monitor_profile.mjs';
import {captureReadingPosition} from './reading_position.mjs';
import {internalReceiptSummary,trackDisclosure} from './page_polish.mjs';
const el=(tag,cls,text)=>{const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=text;return node;};
const button=(text,fn)=>{const node=el('button','text-button',text);node.type='button';node.onclick=fn;return node;};
const date=value=>value?new Date(value).toLocaleDateString('zh-CN'):'日期未记录';

export function dashboardSummary(state) {
  const id=state.current?.id;if(!id)return {sources:[],confirmed:[],receipts:[],profile:null};
  const sources=(state.sources||[]).filter(source=>source.project_id===id),live=new Map(sources.map(source=>[source.id,source]));
  const confirmed=(state.observations||[]).filter(record=>record.project_id===id&&record.status==='confirmed'&&live.has(record.source_id)&&(!record.source_sha256||record.source_sha256===live.get(record.source_id).content_sha256));
  const receipts=[...sources].sort((a,b)=>(b.created_at||'').localeCompare(a.created_at||''));
  const profile=!state.profileError&&state.profile?.project_id===id?state.profile:null;
  return {sources,confirmed,receipts,profile};
}
export function judgmentPatch(profile,text) {
  if(!profile?.project_id||!Number.isInteger(profile.version))throw new Error('项目档案尚未读取，请稍后重试。');
  return {expected_version:profile.version,fields:{thesis:text.trim()},restore_fields:[],restore_focus:false};
}
export function materialReadiness(source,jobs=[]) {
  const job=[...jobs].filter(job=>job.project_id===source.project_id&&job.source_id===source.id).sort((a,b)=>(b.created_at||'').localeCompare(a.created_at||''))[0];
  return ({completed:'可阅读',succeeded:'可阅读',failed:'解析未完成',needs_ocr:'等待文字识别',queued:'准备中',running:'正在解析',processing:'正在解析'})[job?.status]||'查看原件';
}

export function renderMonitorDashboard(state,{navigate,openSource,request,onProfileSaved}) {
  const root=document.getElementById('monitor-dashboard'),internal=document.getElementById('internal-changes');if(!root||!internal||!state.current)return;
  const summary=dashboardSummary(state),projectId=state.current.id;
  const key=JSON.stringify([projectId,state.current.name,state.teamMode,state.capabilities,state.jobs,document.getElementById('company-overview')?.dataset.readingKey,summary,state.metricSeries,state.metricError?.message,state.reading,state.readingError?.message,state.briefing,state.briefingError?.message]);
  if(root.dataset.contentKey===key)return;
  const restorePosition=root.dataset.projectId===projectId?captureReadingPosition([root,internal]):()=>{};
  const expanded=new Set([...internal.querySelectorAll('details[open]')].map(node=>node.dataset.topic));
  const intro=document.querySelector('#company-overview .company-intro')||(root.dataset.projectId===projectId?root.querySelector('.company-intro'):null);
  root.dataset.contentKey=key;root.dataset.projectId=projectId;root.replaceChildren();internal.replaceChildren();
  const go=tab=>navigate(projectId,tab),top=el('div','dashboard-top'),brief=el('section','dashboard-brief');
  brief.dataset.readingAnchor='dashboard-brief';
  // Reuse the existing attributed reader, never copy synthetic prototype content.
  if(intro)brief.append(intro);
  else brief.append(el('p','eyebrow','公司近况'),el('h2','',state.current.name),el('p','','添加公司介绍或 BP，开始了解这家公司。'),button('查看资料 →',()=>go('materials')));
  const stats=el('div','dashboard-metrics');
  for(const [value,label] of [[summary.sources.length,'份项目资料'],[summary.confirmed.length,'条已核对记录']]){const stat=el('div');stat.append(el('strong','',String(value)),el('span','',label));stats.append(stat);}brief.append(stats);
  const timeline=el('section','dashboard-timeline'),head=el('header');head.append(el('h2','','最近收到'),button('查看变化 →',()=>go('internal')));timeline.append(head);
  if(!summary.receipts.length)timeline.append(el('p','reading-note','还没有项目材料。'));
  const list=el('ul');for(const source of summary.receipts.slice(0,4)){const item=el('li');item.append(el('time','',date(source.created_at)),button(source.reference,()=>openSource(source)),el('p','reading-note','材料接收时间 · 不代表经营事项发生日期'));list.append(item);}timeline.append(list);top.append(brief,timeline);root.append(top);
  if(visibleBriefing(state).length){const section=el('section','dashboard-panel');appendBriefing(section,state,{openSource});root.append(section);}
  const charts=el('div');charts.id='dashboard-finance-charts';charts.dataset.readingAnchor='dashboard-finance';root.append(charts);renderDashboardCharts(charts,state,{openSource,navigate:go});
  const financeCharts=document.getElementById('finance-charts');if(financeCharts){financeCharts.replaceChildren();if(selectDashboardSeries(state).length)renderDashboardCharts(financeCharts,state,{openSource,navigate:go});else{const empty=el('section','finance-empty');empty.append(el('h3','','实际财务数据尚未建立'),el('p','','目前没有可绘图的已核对实际数据。公司材料中的规划和预测单独保留在下方。'));if(state.capabilities?.propose)empty.append(button('添加财务材料 →',()=>document.getElementById('header-upload').click()));financeCharts.append(empty);}}
  const note=el('section','dashboard-note'),noteHead=el('header'),thesis=summary.profile?.fields?.thesis;noteHead.append(el('h2','','我的判断'));
  note.dataset.readingAnchor='dashboard-judgment';
  if(summary.profile&&(!state.teamMode||state.capabilities?.propose))noteHead.append(button(thesis?.text?'修改':'写下判断',()=>editJudgment(summary.profile)));
  note.append(noteHead,el('p','',thesis?.text||'这里留给你对项目的看法。'),el('small','reading-note',state.teamMode?'团队部署暂未开放个人判断编辑。':state.profileError?'项目档案暂未读取成功，已有记录不会被修改。':'个人判断单独保留，不随新材料自动改写，也不作为已核对事实。'));root.append(note);
  const intake=internalReceiptSummary(state),progress=el('section','intake-summary');progress.dataset.readingAnchor='intake-summary';
  const progressHead=el('header');progressHead.append(el('h2','','资料与更新'),el('span','reading-note',`${intake.total} 份资料 · ${intake.readable} 份可阅读`));progress.append(progressHead,el('p','',intake.copy));
  const states=el('div','intake-states');for(const [count,label] of [[intake.processing,'份正在解析'],[intake.failed,'份解析未完成'],[intake.ocr,'份需要文字识别']])if(count)states.append(el('span','',`${count} ${label}`));progress.append(states);
  progress.append(button('查看公司情况 →',()=>go('business')),button(intake.failed?'查看未完成资料 →':'查看资料 →',()=>go('materials')));internal.append(progress);
  appendReadingUpdates(internal,state,{openSource,expanded});
  if(visibleBriefing(state).length)appendBriefing(internal,state,{openSource,expanded});
  else internal.append(el('p','reading-note confirmed-changes-empty',state.briefingError?'已核对变化暂未读取成功。':'尚无已核对的前后变化。材料中的描述保留在“业务”和“财务”中。'));
  const receipts=el('details','receipt-history');trackDisclosure(receipts,expanded,'receipt-history');receipts.append(el('summary','',`接收记录 · ${summary.receipts.length} 份`));
  if(!summary.receipts.length)receipts.append(el('p','reading-note','收到公司材料后，拖到上方即可。'));
  else {
    const table=el('table','dashboard-table');table.append(el('caption','','按接收时间排列；解析不等于事实确认'));
    const header=el('thead'),row=el('tr');for(const title of ['材料','收到时间','阅读状态'])row.append(el('th','',title));header.append(row);table.append(header);
    const body=el('tbody');for(const source of summary.receipts.slice(0,10)){const row=el('tr'),name=el('td');name.append(button(source.reference,()=>openSource(source)));row.append(name,el('td','',date(source.created_at)),el('td','',materialReadiness(source,state.jobs)));body.append(row);}table.append(body);receipts.append(table,button('全部资料 →',()=>go('materials')));
  }internal.append(receipts);restorePosition();
  function editJudgment(profile){
    const dialog=el('dialog','profile-editor'),form=el('form'),title=el('h2','','我的判断');title.id='judgment-editor-title';dialog.setAttribute('aria-labelledby',title.id);
    const label=el('label','profile-input'),input=el('textarea');label.append(el('span','','对项目的看法'),input);input.rows=7;input.maxLength=2000;input.value=profile.fields.thesis.text;
    const error=el('p','error-inline');error.setAttribute('role','alert');const actions=el('div','dialog-actions'),cancel=button('取消',()=>dialog.close()),save=el('button','primary-button','保存');save.type='submit';actions.append(cancel,save);
    form.append(title,el('p','reading-note','只保存在本项目，不发送给外部模型。'),label,error,actions);dialog.append(form);document.body.append(dialog);
    let saving=false;dialog.addEventListener('cancel',event=>{if(saving)event.preventDefault();});dialog.addEventListener('close',()=>dialog.remove());
    form.onsubmit=async event=>{event.preventDefault();if(saving)return;saving=true;save.disabled=true;cancel.disabled=true;
      try{const result=await saveProfile(request,profile.project_id,judgmentPatch(profile,input.value));if(state.current?.id===profile.project_id)onProfileSaved(result);dialog.close();}
      catch(err){error.textContent=err.status===409?'判断已在另一处修改。关闭后刷新项目，再保存。':err.message;}
      finally{saving=false;save.disabled=false;cancel.disabled=false;}
    };dialog.showModal();input.focus();
  }
}
export function splitFinancialReading(projectId) {
  const business=document.getElementById('company-internal'),finance=document.getElementById('company-finance');if(!business||!finance)return;
  const key=document.getElementById('company-overview')?.dataset.readingKey;
  if(finance.dataset.projectId!==projectId||finance.dataset.readingKey!==key){finance.replaceChildren();finance.dataset.projectId=projectId;finance.dataset.readingKey=key;}
  const nodes=[...business.querySelectorAll('.compiled-topic[data-topic="compiled-financial"],.reading-topic')].filter(node=>node.dataset.topic==='compiled-financial'||/财务/.test(node.querySelector('h3')?.textContent||''));
  if(nodes.length)finance.replaceChildren(...nodes);
  business.querySelector('.material-updates')?.remove();
}

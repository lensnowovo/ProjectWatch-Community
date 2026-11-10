import { useEffect, useState } from 'react';
import { api, type InsightRef, type ProjectDetail } from './api';
import { Refs } from './judgment';
import { relTime } from './ui';
import { jobWorkState } from './workState';
import './pageSummary.css';

export type SummarySubject = 'brief' | 'operations' | 'overview' | 'external' | 'trials' | 'papers' | 'patents' | 'competition' | 'chain' | 'industry' | 'policy' | 'technology' | 'ops' | 'business' | 'numbers' | 'fin' | 'risks';
type Summary = { details?: {text:string;refs:InsightRef[]}[]; text: string; refs: InsightRef[]; date?: string; historical?: boolean; editorial?:boolean };
const labels: Record<SummarySubject, string> = { brief:'整体研判', operations:'经营与资金', overview:'公司概况', external:'公司进展', trials:'临床进展', papers:'研究进展', patents:'知识产权', competition:'竞争格局', chain:'产业链关系', industry:'行业态势', policy:'政策影响', technology:'技术进展', ops:'经营概况', business:'产品与业务', numbers:'关键数据', fin:'财务表现', risks:'主要风险' };
/** Read only the current page's published artifact. Never synthesize prose in the UI. */
export function selectSummary(detail: ProjectDetail, subject: SummarySubject): Summary|null {
  const generated=detail.page_summaries?.[subject];
  const editorial=detail.editorial_summaries?.[subject];
  // A dated owner-reviewed copy remains available until an equally recent publication exists.
  if(editorial?.text && (!generated?.text || editorial.date >= generated.date))return editorial;
  return generated?.text ? generated : null;
}

export function PageSummary({detail,subject,fallback}: {detail:ProjectDetail;subject:SummarySubject;fallback?:Summary}) {
  const [evidence,setEvidence]=useState(false);
  const project=detail.project.id;
  useEffect(()=>{setEvidence(false);},[project,subject]);
  const summary=selectSummary(detail,subject) ?? fallback;
  const [submitting,setSubmitting]=useState(false);
  const [notice,setNotice]=useState('');
  const [requested,setRequested]=useState('');
  const kind:string=['ops','business','numbers','fin','risks'].includes(subject)?'internal_page_summary':'page_summary';
  const busy=detail.jobs?.find(j=>j.kind===kind && (!j.summary_subject || j.summary_subject===subject) && (j.status==='queued'||j.status==='running'));
  const last=detail.jobs?.find(j=>requested ? j.id===requested : j.kind===kind && (!j.summary_subject || j.summary_subject===subject));
  useEffect(()=>{setNotice('');setRequested('');},[project,subject]);
  async function generate(){
    setSubmitting(true);setNotice('');
    try {
      const job=await api.generateSummary(project,subject);
      setRequested(job.id);
      window.dispatchEvent(new Event('project-summary-requested'));
    } catch(e){setNotice(e instanceof Error?e.message:'摘要暂时无法生成，请稍后再试。');}
    finally{setSubmitting(false);}
  }
  const status = notice || (requested && !last && !busy ? '已提交生成请求。' :
    last?.status==='failed' ? '本次摘要未完成，可在任务中查看原因。' :
    last?.status==='done' && (!detail.page_summaries?.[subject] || (detail.page_summaries[subject].date < last.created_at)) ? '本次材料未形成新的摘要。' : '');
  const refs=[...new Map((summary?.refs||[]).map(r=>[`${r.kind}:${r.source_id||r.url||r.code}`,r])).values()];
  return <section className="page-reading" aria-label={labels[subject]}>
    <header><h2>{labels[subject]}</h2>{summary?.date&&<span>{summary.editorial?'整理于 ':''}{relTime(summary.date)}{summary.historical?' · 有新资料':''}</span>}<button className="btn small" disabled={submitting||!!busy}
      onClick={generate}>{submitting?'提交中…':busy?(busy.status==='queued'?'等待生成…':'生成中…'):summary?'更新摘要':'生成摘要'}</button></header>
    {status&&<small role="status">{status}</small>}
    {summary ? <><p>{summary.text}</p>
      {(!!refs.length || !!summary.details?.length)&&<div className="page-reading-actions">
        <button className="link small" aria-expanded={evidence} onClick={()=>setEvidence(v=>!v)}>{evidence?'收起依据':'查看依据 ↗'}</button>
      </div>}
      {evidence&&<div className="page-reading-evidence">
        {summary.details?.map((d,i)=><div className="page-reading-detail" key={'detail-'+i}><p>{d.text}</p><Refs refs={d.refs}/></div>)}
        {refs.map((r,i)=><div key={i}><Refs refs={[r]}/><span>{r.label}</span>{r.date&&<small>{r.date.slice(0,10)}</small>}</div>)}
      </div>}
    </>:<p className="muted">{summaryStatus(detail,subject)}</p>}
  </section>;
}

export function summaryStatus(detail: ProjectDetail, subject: SummarySubject): string {
  const internal = ['ops','business','numbers','fin','risks'].includes(subject);
  const active = (kind:string) => detail.jobs?.some(j => j.kind === kind && (!j.summary_subject || j.summary_subject===subject) && ['running','waiting_response','queued'].includes(jobWorkState(detail,j)));
  if (active(internal ? 'internal_page_summary' : 'page_summary')) return '正在整理各页摘要，完成后会自动显示。';
  if (internal) return active('internal') ? '正在整理内部资料，摘要完成后会自动显示。' : '本页还没有可展示的摘要。';
  if (active('collect')) return '公开信息收集中，材料筛选后将自动整理摘要。';
  if (active('analyze') || active('report')) return '正在分析公开材料，各页摘要完成后会自动显示。';
  const summaryKind:string='page_summary';
  const last=detail.jobs?.find(j=>j.kind===summaryKind && (!j.summary_subject || j.summary_subject===subject));
  if(last?.status==='failed') return '本轮摘要未完成，已有结果保留。';
  return '本页还没有可展示的摘要。';
}

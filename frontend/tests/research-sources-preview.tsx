// Synthetic only. No real instance, providers or research requests.
import React, {useState} from 'react';
import {createRoot} from 'react-dom/client';
import {api, type ResearchSource, type ResearchCategory, type ProjectDetail} from '../src/api';
import {ClinicalView, PapersView, PatentsView} from '../src/researchPages';
import {CategoryStrip} from '../src/pages/Project';
import '../src/styles.css';
window.fetch = async () => {throw new Error('Network prohibited in synthetic preview');};
for (const key of Object.keys(api)) (api as any)[key] = async () => {throw new Error('Unmocked API: '+key);};
let revision=1, count=81;
const calls: string[]=[];
(window as any).sourcePreview={calls,add:()=>{count++;revision++;},withdraw:()=>{count--;revision++;}};
api.researchSources=async(project,category,offset=0)=>{
  calls.push(`${category}:${offset}`);
  return {project_id:project,category,revision:String(revision),total:count,offset,next_offset:offset+40<count?offset+40:null,
    items:Array.from({length:Math.min(40,count-offset)},(_,n):ResearchSource=>({id:`${category}-${offset+n}`,project_id:project,category,
      title:`合成来源 ${offset+n+1} · Long research title: evidence of a public research programme and independent technical development`,
      url:'https://example.invalid/source',source:'合成公开来源',snippet:'这是一条合成来源摘要，仅用于检查排版与独立来源展示，不是模型结论。',
      published_at:n%3?new Date().toISOString().slice(0,10):null,collected_at:new Date().toISOString(),processing_status:'pending',source_type:'structured_record',read_scope:'partial',evidence_version_id:'synthetic-version',body_evidence_version_id:null,
      extra:category==='paper'?{openalex_id:`W${n}`,authors:['合成作者'],institutions:['合成研究院'],affiliations:['Independent Institute'],cited_by:n,paper_search:{query:'合成公司, Ltd.',mode:'affiliation'}}:
        category==='trial'?{nct_id:`NCT${String(n+offset).padStart(8,'0')}`,phase:'PHASE1',status:'RECRUITING',sponsor:'合成申办方',conditions:['合成适应症']}:
        {assignee_query:'合成公司',applicants:['独立机构'],applicant_quote:'申请人：独立机构'}}))};
};
api.monitoringPlan=async()=>({framework:[],items:[],changes:[],proposals:[],relation_updates:{}} as any);
const detail={project:{id:'synthetic',name:'合成公司',legal_name:'合成公司'},counts:{},research_source_counts:{paper:81,trial:81,patent:81},jobs:[],trials:[],plan:null} as unknown as ProjectDetail;
function Preview(){const [tab,setTab]=useState('papers');
  return <main style={{maxWidth:1500,margin:'32px auto',padding:'0 24px'}}><p className="muted">隔离合成数据 · 全部来源未形成分析事件</p>
    <CategoryStrip detail={detail} page={tab as any} go={p=>['papers','patents','trials'].includes(p)&&setTab(p)}/>
    <section key={tab}>{tab==='papers'?<PapersView detail={detail}/>:tab==='trials'?<ClinicalView detail={detail}/>:<PatentsView detail={detail}/>}</section>
  </main>;
}
createRoot(document.getElementById('root')!).render(<Preview/>);

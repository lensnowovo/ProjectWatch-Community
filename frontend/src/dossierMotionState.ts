import type { ProjectDetail } from './api';
import { dossierEntries, milestonesFor } from './companyDossier';
import { projectWork } from './workState';

export const DOSSIER_GROUPS = {
  business: {label:'业务与产品', tab:'overview'}, team: {label:'核心团队', tab:'overview'},
  capital: {label:'融资与股东', tab:'overview'}, history: {label:'发展历程', tab:'overview'},
  profile: {label:'公司介绍', tab:'overview'}, relations: {label:'关联关注', tab:'plan'},
  judgment: {label:'综合判断', tab:'brief'},
} as const;
export type ArrivalGroup = keyof typeof DOSSIER_GROUPS;
type Saved = {revision:string; count:number};
type Snapshot = {project:string; groups:Record<ArrivalGroup,Saved>};
export type Arrival = {group:ArrivalGroup; added:number; kind:'added'|'updated'};
export type DossierObserver = {snapshot:Snapshot|null; arrivals:Arrival[]};

/** Canonicalize content, never poll timestamps, list indexes or generated IDs. */
function canonical(value:unknown):string {
  if(Array.isArray(value)) return `[${value.map(canonical).sort().join(',')}]`;
  if(value && typeof value==='object') return `{${Object.entries(value).sort(([a],[b])=>a.localeCompare(b)).map(([k,v])=>`${JSON.stringify(k)}:${canonical(v)}`).join(',')}}`;
  return JSON.stringify(value) ?? 'null';
}
const saved=(values:unknown[]):Saved=>({revision:canonical(values),count:values.length});
const text=(value:string|null|undefined)=>value?.trim() || '';
export function dossierSnapshot(detail:ProjectDetail|null):Snapshot|null {
  if(!detail || detail.status_stale) return null;
  const entries=dossierEntries(detail);
  const content=(groups:string[])=>entries.filter(e=>groups.includes(e.group)).map(e=>({label:e.label,text:e.text,date:e.date,refs:e.fact?.refs ?? e.refs}));
  const intro=[text(detail.profile?.one_liner),text(detail.profile?.summary),text(detail.project.description)].filter(Boolean);
  return {project:detail.project.id,groups:{
    business:saved(content(['business','customers'])), team:saved(content(['team'])),
    capital:saved(content(['capital'])),
    history:saved(milestonesFor(detail).map(e=>({text:e.text,date:e.date,state:e.state,refs:e.fact?.refs ?? e.refs}))),
    profile:saved(intro), relations:detail.dossier_saved?.relations ?? saved([]),
    judgment:detail.dossier_saved?.judgment ?? saved([]),
  }};
}
export const emptyObserver=():DossierObserver=>({snapshot:null,arrivals:[]});
export function observeDossier(before:DossierObserver,next:Snapshot|null):DossierObserver {
  const arrivals:Arrival[]=[];
  if(next && before.snapshot?.project===next.project){
    for(const group of Object.keys(DOSSIER_GROUPS) as ArrivalGroup[]){
      const old=before.snapshot.groups[group],now=next.groups[group];
      // Removal is not a new result. Content remains visible independently of this receipt.
      if(now.count && now.count>=old.count && now.revision!==old.revision){
        arrivals.push({group,added:Math.max(0,now.count-old.count),kind:now.count>old.count?'added':'updated'});
      }
    }
  }
  return {snapshot:next,arrivals};
}
export function dossierRail(detail:ProjectDetail){
  const work=projectWork(detail);
  return {...work,flowing:work.state==='running',waiting:work.state==='waiting_response',
    pending:(detail.jobs ?? []).some(j=>j.status==='queued'||j.status==='running')};
}

import type { ProjectDetail, PublicFact, Ref } from './api';

export type DossierGroup = 'business' | 'customers' | 'team' | 'capital' | 'registry' | 'other';
export interface DossierEntry {
  id: string; label: string; text: string; group: DossierGroup;
  refs: Ref[]; fact?: PublicFact; date: string; dateLabel: string;
  state?: 'planned' | 'completed' | 'reported';
}
export function safeWebsite(value?: string | null): string | null {
  if (!value?.trim()) return null;
  try { const url = new URL(/^[a-z][a-z\d+.-]*:/i.test(value.trim()) ? value.trim() : `https://${value.trim()}`);
    return ['https:', 'http:'].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}
/** Accept exact source granularity, never substitute collection/publication time for event time. */
export function dossierDate(value?: string | null): string {
  const date = (value ?? '').trim();
  const match = /^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$/.exec(date);
  if (!match) return '';
  const [, y, m, d] = match;
  if (Number(y) < 1800 || Number(y) > 2200 || (m && (+m < 1 || +m > 12))) return '';
  if (d && (+d < 1 || +d > new Date(Date.UTC(+y, +m, 0)).getUTCDate())) return '';
  return date;
}
function sourceDate(fact: PublicFact) {
  return (fact.refs ?? []).map(r => dossierDate((r.published_at ?? '').slice(0, 10))).filter(Boolean).sort().slice(-1)[0] ?? '';
}
const CUSTOMER_LABEL = /客户|市场|订单|应用场景|应用领域/;
export function sectionGroup(title: string): DossierGroup {
  if (/团队|人员|管理|创始/.test(title)) return 'team';
  if (/融资|股权|股东/.test(title)) return 'capital';
  if (CUSTOMER_LABEL.test(title)) return 'customers';
  if (/业务|产品|技术/.test(title)) return 'business';
  return 'other';
}
const REGISTRY = /曾用名|注册资本|实缴资本|企业名称|公司名称|统一社会信用|工商登记|登记状态|注册地址|经营范围/;
export function dossierEntries(detail: ProjectDetail): DossierEntry[] {
  const internal = (detail.profile?.sections ?? []).flatMap((section, s) => (section.points ?? []).map((point, p) => ({
    id:`internal-${s}-${p}`, label:section.title, group:sectionGroup(section.title), text:point.text,
    refs:point.refs ?? [], date:dossierDate(point.date), dateLabel:'材料日期',
  })));
  const external = (detail.public_profile?.facts ?? []).filter(f => ['business','product','person','financing'].includes(f.kind)).map((fact, index): DossierEntry => ({
    id:`public-${index}`, label:fact.label || ({person:'团队资料',financing:'融资资料',product:'产品资料'} as Record<string,string>)[fact.kind] || '业务资料', text:fact.text,
    group:fact.kind === 'person' ? 'team' : fact.kind === 'financing' || /股权|股东/.test(fact.label) ? 'capital' : REGISTRY.test(fact.label) ? 'registry' : CUSTOMER_LABEL.test(fact.label) ? 'customers' : 'business',
    refs:[], fact, date:sourceDate(fact), dateLabel:'来源日期',
  })).sort((a,b)=>b.date.localeCompare(a.date));
  // Keep distinct records and their citations, including competing source statements.
  return [...internal,...external].filter(e=>e.text?.trim());
}
export function businessIntro(detail: ProjectDetail): string {
  const profile = detail.profile?.one_liner?.trim();
  if (profile) return profile;
  const description = detail.project.description?.trim();
  if (description && detail.project.entity_source === 'manual') return description;
  const explicit = (detail.public_profile?.facts ?? [])
    .filter(f => f.kind === 'business' && /^(公司介绍|公司简介|主营业务|业务概述|主要业务|业务)$/.test(f.label.trim()) && f.text?.trim())
    .sort((a,b)=>sourceDate(b).localeCompare(sourceDate(a)))[0];
  return explicit?.text || description || '';
}
export function stageLabel(value?: string | null): string {
  return /IPO|上市|科创板|创业板|北交所|港交所|招股|问询/i.test(value ?? '') ? '上市进程' : '融资阶段';
}
export function milestonesFor(detail: ProjectDetail): DossierEntry[] {
  return [
    ...(detail.profile?.milestones ?? []).map((m,i):DossierEntry=>({id:`milestone-internal-${i}`,label:'发展节点',text:m.text,group:'other',refs:m.refs ?? [],date:dossierDate(m.date),dateLabel:'事项日期',state:m.status === 'planned' ? 'planned' : ['done','completed'].includes(m.status ?? '') ? 'completed' : 'reported'})),
    ...(detail.public_profile?.facts ?? []).filter(f=>f.kind === 'milestone').map((fact,i):DossierEntry=>({id:`milestone-public-${i}`,label:fact.label || '公开记录',text:fact.text,group:'other',refs:[],fact,date:dossierDate(fact.date),dateLabel:'事项日期',state:'reported'})),
  ].filter(e=>e.text?.trim()).sort((a,b)=>a.date.localeCompare(b.date));
}
export function headerChanges(detail: ProjectDetail): DossierEntry[] {
  const today = new Date().toISOString().slice(0,10);
  // Snapshot-derived company records only. A source date is labelled as such, not "today's event".
  const milestones = milestonesFor(detail).filter(e=>e.date && e.state !== 'planned' && e.date <= today);
  const funding = dossierEntries(detail).filter(e=>e.group === 'capital' && e.date && e.date <= today);
  const seen = new Set<string>();
  return [...milestones,...funding].sort((a,b)=>b.date.localeCompare(a.date)).filter(e=>{
    if (seen.has(e.text)) return false; seen.add(e.text); return true;
  }).slice(0,2);
}

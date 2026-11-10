import type { ProjectDetail, ResearchSource } from './api';

const text = (v: unknown) => typeof v === 'string' ? v : '';
const strings = (v: unknown): string[] => Array.isArray(v) ? v.filter((s): s is string => typeof s === 'string') : [];
const norm = (s: string) => s.normalize('NFKC').replace(/\s/g, '').toLowerCase();
export function names(detail: ProjectDetail) {
  return [detail.project.name, detail.project.short_name, detail.project.legal_name, ...(detail.project.aliases || [])].filter((s): s is string => !!s).map(norm);
}
export function reviewed(row: ResearchSource) {
  return row.review === 'confirmed' && row.processing_status === 'organized' && row.extra?.research_relation !== 'unrelated';
}
/** Only the structured primary document carries ownership/date. Search snippets can contain citation tables. */
export function primaryPatent(row: ResearchSource) {
  const x = row.extra || {};
  if (x.provider_kind !== 'third_party_index' || !strings(x.applicants).length) return false;
  try {
    const u = new URL(row.url);
    const n = u.hostname === 'patents.google.com' ? u.pathname.match(/^\/patent\/([A-Z]{2}\d{4,14}[A-Z]\d{0,2})(?:\/|$)/)?.[1] : undefined;
    return !!n && n === x.patent_number;
  } catch { return false; }
}
export function relation(row: ResearchSource, detail: ProjectDetail): string {
  if (row.category === 'patent') {
    if (!primaryPatent(row)) return 'other';
    if (strings(row.extra.applicants).some(s => names(detail).includes(norm(s)))) return 'subject';
    return reviewed(row) && ['competitor','route','field'].includes(text(row.extra.research_relation)) ? text(row.extra.research_relation) : 'other';
  }
  if (!reviewed(row)) return 'other';
  if (row.category === 'trial') {
    const sponsors = [text(row.extra.sponsor), ...strings(row.extra.collaborators)];
    if (sponsors.some(s => s && names(detail).includes(norm(s)))) return 'subject';
    // Hospital-sponsored trials need explicit company participation evidence, not a model subject tag.
    return ['competitor','route','field'].includes(text(row.extra.research_relation)) ? text(row.extra.research_relation) : 'other';
  }
  return ['subject','competitor','route','field'].includes(text(row.extra.research_relation)) ? text(row.extra.research_relation) : 'other';
}
export function presentSource(row: ResearchSource): ResearchSource {
  if (row.category !== 'patent' || primaryPatent(row)) return row;
  const title = row.title.split(/\s+-\s+(?=[A-Z]{2}\d{6,}[A-Z]\d?\b)/)[0].trim();
  return {...row, title, published_at: null, extra: {...row.extra, research_relation: 'other'}};
}

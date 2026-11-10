import type { ProjectDetail } from './api';

/** Keep actual clinical records and explicit scope; ignore legacy company-name sponsor defaults. */
export function hasClinicalScope(detail: ProjectDetail): boolean {
  if ((detail.research_source_counts?.trial ?? detail.counts.trial ?? 0) > 0 || (detail.trials?.length ?? 0) > 0
    || detail.plan?.is_biomed === true || detail.plan?.trial_terms?.some(term => term.trim())) return true;
  const normalize = (value: string) => value.trim().toLocaleLowerCase();
  const own = new Set([detail.project?.name, detail.project?.short_name, detail.project?.legal_name].filter((v): v is string => !!v).map(normalize));
  return (detail.plan?.trial_sponsors ?? []).some(name => name.trim() &&
    (detail.plan?.is_biomed !== false || !own.has(normalize(name))));
}

import type { ProjectDetail } from './api';

/** Use the saved research scope and records, never a company-name keyword rule. */
export function hasClinicalScope(detail: ProjectDetail): boolean {
  return (detail.research_source_counts?.trial ?? detail.counts.trial ?? 0) > 0 || (detail.trials?.length ?? 0) > 0
    || detail.plan?.is_biomed === true
    || (detail.plan?.trial_sponsors ?? []).some(name => name.trim().length > 0);
}

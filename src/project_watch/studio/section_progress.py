"""Public, per-section state derived from the analysis job's own receipts."""
from project_watch.studio import section_analysis
from project_watch.studio.execution import JobExecution


def outcome_state(outcome):
    status = outcome.get('status')
    if status in ('approved', 'unchanged'):
        return {'approved': 'published', 'unchanged': 'retained'}[status]
    if 'request_outcome_unknown' in outcome.get('issues', ()):
        return 'unknown'
    blocking = outcome.get('blocking') or []
    if blocking and all(isinstance(i, dict) and i.get('code') in (
            'evidence_insufficient', 'section_without_supported_items') for i in blocking):
        return 'insufficient'
    return 'missing_draft' if status == 'missing' else 'review_blocked'


def states(db, job):
    if not job:
        return {}, {}
    from project_watch.studio.analyze_job import PUBLICATION, RESULT
    execution = JobExecution(None, job)
    publication = execution.read_checkpoint(PUBLICATION, db=db) or {}
    if not publication:
        from project_watch.studio.judgment_delivery import CURRENT
        publication = execution.read_checkpoint(CURRENT, db=db) or {}
    result = execution.read_checkpoint(RESULT, db=db) or {}
    waiting = job['status'] == 'queued' and job['retry_after']
    uncertain = db.execute("SELECT 1 FROM studio_job_calls WHERE job_id=? AND status='unknown' LIMIT 1",
                           (job['id'],)).fetchone()
    fallback = ('rate_limited' if waiting else 'pending' if job['status'] in ('queued', 'running')
                else 'unknown' if uncertain else 'no_material' if result.get('sections') == 'no_material'
                else 'waiting_model' if result.get('status') == 'waiting_for_model' else 'unfinished')
    sections, evidence_gaps = {}, {}
    for name in section_analysis.SECTIONS:
        outcome = publication.get('sections', {}).get(name)
        receipt = execution.read_checkpoint('analyze_section_outcome_v1:' + name, db=db)
        if not outcome:
            outcome = receipt and receipt['outcome']
        sections[name] = outcome_state(outcome) if outcome else fallback
        if receipt and outcome and outcome.get('status') == 'approved' and not receipt.get('published'):
            sections[name] = 'unfinished'
        if sections[name] == 'insufficient':
            for issue in outcome.get('blocking', ()):
                reason = str(issue.get('reason') or '').strip()
                if reason and any('\u4e00' <= c <= '\u9fff' for c in reason):
                    evidence_gaps[name] = reason
                    break
    return sections, evidence_gaps

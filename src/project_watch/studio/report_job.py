"""The rewritten pipeline's report stage: projections only, no collection.

Today's feed and the weekly digest are read-time projections of published
events and sections (studio/digest.py), so they need no generation here. This
stage records what the round produced for the progress view and refreshes the
project brief through the existing brief job when a model is configured.
"""
from contextlib import closing

from project_watch.studio.store import now

RESULT = 'report_result_v1'


def run(pipeline, execution):
    saved = execution.read_checkpoint(RESULT)
    if saved:
        return saved
    pid = execution.job['project_id']
    version = execution.job.get('input_version') or ''
    analyze_id = version[len('analyze:'):] if version.startswith('analyze:') else None
    with closing(pipeline.store._connect()) as db:
        started = db.execute('SELECT created_at FROM studio_jobs WHERE id=?', (analyze_id,)).fetchone() if analyze_id else None
        events = db.execute('SELECT COUNT(*) FROM studio_events WHERE project_id=? AND created_at>=?',
                            (pid, started[0] if started else '')).fetchone()[0]
    result = dict(status='complete', finished_at=now(), analyze_job_id=analyze_id, new_events=events)
    execution.checkpoint(RESULT, result)
    if pipeline.settings_store.load().model.ready:
        pipeline.store.enqueue(pid, 'brief', input_version='report:' + execution.job['id'])
    return result

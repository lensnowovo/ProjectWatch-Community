"""Budget admission for the explicit public scope/question worker stages."""
from contextlib import closing

from project_watch.studio import event_insight_work
from project_watch.studio.dsh_bootstrap import digest
from project_watch.studio.execution import JobExecution
from project_watch.studio.material_ownership import project_guard
from project_watch.studio.recovery import input_binding

INPUT = 'public_stage_input_v1'
put = event_insight_work._put


def inherit(db, target, owners):
    for owner in set(owners) - {None, target}:
        db.execute('INSERT OR IGNORE INTO studio_job_budget_links SELECT ?,job_id,call_key FROM studio_job_calls WHERE job_id=?',
                   (target, owner))
        db.execute('INSERT OR IGNORE INTO studio_job_budget_links SELECT ?,origin_job_id,call_key FROM studio_job_budget_links WHERE job_id=?',
                   (target, owner))


def enqueue(pipeline, db, pid, kind, work, *, origin=None, cap=4):
    version = kind + ':' + digest([origin, work])
    job = pipeline.store.enqueue(pid, kind, automatic=True, input_version=version, db=db)
    if job.get('input_version') != version or job['status'] not in ('queued', 'running'):
        return None
    reader = JobExecution(pipeline.store, job)
    if reader.read_checkpoint(INPUT, db=db):
        return job
    root, predecessor = job['id'], None
    if origin:
        ancestor = JobExecution(pipeline.store, {'id': origin})
        previous = ancestor.read_checkpoint(INPUT, db=db) or ancestor.read_checkpoint(event_insight_work.INPUT, db=db) or {}
        root = previous.get('round_root_job_id', origin)
        budget = JobExecution(pipeline.store, {'id': root}).read_checkpoint(event_insight_work.BUDGET, db=db)
        if budget and budget['cap'] != cap:
            raise ValueError('public stage round cap changed')
        predecessor = budget['tail_job_id'] if budget else origin
    payload = dict(work=work, cap=cap, origin_job_id=origin or job['id'], round_root_job_id=root,
                   predecessor_job_id=predecessor, settings_binding=input_binding(pipeline, pid, db),
                   budget_basis='shared_round_including_origin' if origin else 'explicit_periodic_scope_round')
    put(db, job['id'], INPUT, payload)
    inherit(db, job['id'], [origin, predecessor])
    put(db, root, event_insight_work.BUDGET, dict(cap=cap, tail_job_id=job['id']))
    return job


def prepare(pipeline, execution, manifest):
    """Import final predecessor usage, including calls made after child admission."""
    with closing(pipeline.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        execution._guard(db)
        project_guard(db, execution.job['project_id'], execution.job['id'])
        if input_binding(pipeline, execution.job['project_id'], db) != manifest['settings_binding']:
            raise ValueError('public stage settings or permissions changed')
        budget = JobExecution(pipeline.store, {'id': manifest['round_root_job_id']}).read_checkpoint(event_insight_work.BUDGET, db=db)
        if not budget or budget['cap'] != manifest['cap']:
            raise ValueError('public stage frozen round cap changed')
        predecessor = manifest.get('predecessor_job_id')
        if predecessor:
            row = db.execute('SELECT status FROM studio_jobs WHERE id=? AND project_id=?',
                             (predecessor, execution.job['project_id'])).fetchone()
            if not row or row[0] not in ('done', 'failed'):
                raise ValueError('public stage predecessor is not terminal')
            inherit(db, execution.job['id'], [predecessor])
    current = execution.budgets.get('model')
    execution.budgets.update(model=manifest['cap'] if current is None else min(current, manifest['cap']),
                             search=0, page=0, open_data=0, site=0)
    return manifest['work']

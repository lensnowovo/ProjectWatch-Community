"""A bootstrap followup shares its original round, never a fresh allowance."""
from contextlib import closing

from project_watch.studio import event_insight_work, material_handoff, public_stage_work
from project_watch.studio.execution import JobExecution
from project_watch.studio.material_ownership import project_guard
from project_watch.studio.recovery import input_binding

WORK = {'kind': 'collection_followup'}


def enqueue(pipeline, execution):
    state = execution.read_checkpoint(event_insight_work.ROUND)
    if not state:
        return pipeline.store.enqueue(execution.job['project_id'], 'external_followup', automatic=True,
                                      input_version=execution.job['id'], parent_job_id=execution.job['id'])  # Existing legacy protocol.
    with closing(pipeline.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        execution._guard(db)
        project_guard(db, execution.job['project_id'], execution.job['id'])
        return public_stage_work.enqueue(pipeline, db, execution.job['project_id'], 'external_followup', WORK,
                                         origin=execution.job['id'], cap=state['cap'], input_version=execution.job['id'])


def prepare(pipeline, execution):
    manifest = execution.read_checkpoint(public_stage_work.INPUT)
    if not manifest or manifest.get('work') != WORK:
        return None
    with closing(pipeline.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        execution._guard(db)
        pid = execution.job['project_id']
        project_guard(db, pid, execution.job['id'])
        if input_binding(pipeline, pid, db) != manifest['settings_binding']:
            raise ValueError('collection round settings or permissions changed')
        root = JobExecution(pipeline.store, {'id': manifest['round_root_job_id']})
        state = root.read_checkpoint(event_insight_work.ROUND, db=db)
        caps = root.read_checkpoint(material_handoff.CAPS, db=db)
        if not state or not caps or state['cap'] != manifest['cap']:
            raise ValueError('collection round original limits missing or changed')
        predecessor = manifest['predecessor_job_id']
        row = db.execute('SELECT status FROM studio_jobs WHERE project_id=? AND id=?', (pid, predecessor)).fetchone()
        if not row or row[0] not in ('done', 'failed'):
            raise ValueError('collection round predecessor is not terminal')
        public_stage_work.inherit(db, execution.job['id'], [manifest['origin_job_id'], predecessor])
    # These are the original cumulative allowances, not per-followup grants.
    for kind, maximum in {**caps, 'model': state['cap']}.items():
        current = execution.budgets.get(kind)
        if maximum is not None:
            execution.budgets[kind] = maximum if current is None else min(current, maximum)
    return state['cap']

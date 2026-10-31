"""Frozen independence for fresh public material research and its event analysis.

This grants no retry, takeover or legacy-extractor authority. Normal collection,
material review, evidence visibility/version checks and shared budgets still run.
"""
import json
import os
from contextlib import closing

from project_watch.studio.http import HttpError
from project_watch.studio.independent_collection import STAGE as COLLECTION
from project_watch.studio.independent_collection import _hash, dependencies

STAGE = 'independent_public_research_v1'
INSIGHT = 'event_insight_input_v1'
WORK_STAGES = {'insight': INSIGHT, 'scope': 'public_stage_input_v1', 'assessment': 'public_stage_input_v1',
               'external_followup': 'update_handoff_v1'}


def read(db, jid, stage=STAGE):
    row = db.execute('SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage=?', (jid, stage)).fetchone()
    return json.loads(row[0]) if row else None


def held_jobs(db, pid, jid):
    """Return only the exact terminal extractor jobs this public path excludes."""
    saved = read(db, jid)
    if saved is None:
        return set()
    project = db.execute('SELECT status FROM studio_projects WHERE project_id=?', (pid,)).fetchone()
    if not project or project[0] != 'active':
        raise HttpError('project_paused')
    if db.execute("SELECT 1 FROM studio_job_calls WHERE job_id=? AND status='unknown'", (jid,)).fetchone():
        raise HttpError('request_outcome_unknown')
    current = db.execute('SELECT project_id,kind FROM studio_jobs WHERE id=?', (jid,)).fetchone()
    root_id = saved.get('root_job_id')
    root = read(db, root_id) if root_id else None
    if (not current or current[0] != pid or saved.get('project_id') != pid or not root
            or root.get('root_job_id') != root_id or root.get('project_id') != pid
            or not root.get('holds') or root.get('holds') != saved.get('holds')):
        raise HttpError('public_research_admission_changed')
    if jid == root_id:
        if current[1] != 'external':
            raise HttpError('public_research_admission_changed')
        owner = read(db, jid, 'material_pipeline_v1')
        if owner and owner.get('owner') != 'materials':
            raise HttpError('public_research_requires_material_entry')
    else:
        stage = saved.get('input_stage', WORK_STAGES.get(current[1]))
        if stage == 'snapshot_analysis_input_v1' and current[1] != 'insight':
            raise HttpError('public_research_analysis_inputs_changed')
        if stage not in (*WORK_STAGES.values(), 'snapshot_analysis_input_v1'):
            raise HttpError('public_research_analysis_inputs_changed')
        payload = read(db, jid, stage) if stage else None
        if (not payload or payload.get('round_root_job_id') != root_id
                or _hash(payload) != saved.get('input_hash')):
            raise HttpError('public_research_analysis_inputs_changed')
    from project_watch.studio.recovery import RefreshBlocked

    try:
        live = dependencies(db, pid, exclude_job=jid)
    except RefreshBlocked as exc:
        raise HttpError('request_outcome_unknown') from exc
    if live != saved['holds']:
        raise HttpError('public_research_dependencies_changed')
    validate_legacy_owners(db, live)
    blocked = {row['job_id'] for row in live}
    # The independent path cannot inherit an uncertain call, even via a saved
    # recovery key or a shared-budget link. The old receipt stays with its owner.
    links = db.execute('SELECT origin_job_id FROM studio_job_budget_links WHERE job_id=?', (jid,))
    if (any(row[0] in blocked for row in links)
            or db.execute('SELECT 1 FROM studio_call_recoveries WHERE job_id=?', (jid,)).fetchone()
            or db.execute("SELECT 1 FROM studio_job_checkpoints WHERE job_id=? AND "
                          "(stage LIKE 'dsh_request:%' OR stage='dsh_model_continuation')", (jid,)).fetchone()):
        raise HttpError('public_research_cannot_recover_prior_calls')
    return blocked


def validate_legacy_owners(db, holds):
    material_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='studio_material_calls'").fetchone()
    for hold in holds:
        if ((material_table and db.execute('SELECT 1 FROM studio_material_calls WHERE job_id=?', (hold['job_id'],)).fetchone())
                or read(db, hold['job_id'], 'material_pipeline_v1')):
            raise HttpError('public_research_dependency_not_independent')


def admit(pipeline, execution):
    """Only a not-yet-started normal DSH external job receives this capability."""
    if (execution.job['kind'] != 'external' or not os.environ.get('PROJECT_WATCH_DSH_RUNTIME')
            or os.environ.get('PROJECT_WATCH_DSH_MONITOR') != '1'):
        return False
    with closing(pipeline.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        execution._guard(db)
        jid, pid = execution.job['id'], execution.job['project_id']
        if read(db, jid) is not None:
            held_jobs(db, pid, jid)
            return True
        stages = {r[0] for r in db.execute('SELECT stage FROM studio_job_checkpoints WHERE job_id=?', (jid,))}
        if stages - {COLLECTION, 'collection_request_v1'}:
            return False  # Old/frozen/recovery work retains its original entry.
        if (db.execute('SELECT 1 FROM studio_job_calls WHERE job_id=?', (jid,)).fetchone()
                or db.execute('SELECT 1 FROM studio_job_budget_links WHERE job_id=?', (jid,)).fetchone()):
            return False
        holds = dependencies(db, pid, exclude_job=jid)
        if not holds:
            return False
        # Independent legacy extractors have no public-material ownership to
        # transfer. A mixed or renamed owner must never use this exception.
        validate_legacy_owners(db, holds)
        value = dict(project_id=pid, root_job_id=jid, holds=holds)
        db.execute('INSERT INTO studio_job_checkpoints VALUES (?,?,?)', (jid, STAGE, json.dumps(value)))
        return True


def inherit_work(db, pid, origin, job, payload):
    """Only frozen public closeout work inherits the root's exclusion and budget."""
    if read(db, origin) is None:
        return
    held_jobs(db, pid, origin)
    root_id = read(db, origin)['root_job_id']
    if (job['kind'] not in WORK_STAGES or payload.get('round_root_job_id') != root_id
            or not payload.get('objects' if job['kind'] == 'insight' else 'work')
            or _hash(read(db, job['id'], WORK_STAGES[job['kind']])) != _hash(payload)):
        raise HttpError('public_research_analysis_inputs_changed')
    value = dict(project_id=pid, root_job_id=root_id, holds=read(db, root_id)['holds'], input_hash=_hash(payload))
    db.execute('INSERT INTO studio_job_checkpoints VALUES (?,?,?)', (job['id'], STAGE, json.dumps(value)))


def inherit_snapshot(db, pid, origin, job, payload):
    """Carry an existing independence proof, never create an unknown exception."""
    parent = read(db, origin)
    if parent is None:
        return
    held_jobs(db, pid, origin)
    stage = 'snapshot_analysis_input_v1'
    if (job['kind'] != 'insight' or payload.get('origin_job_id') != origin
            or payload.get('round_root_job_id') != parent['root_job_id']
            or _hash(read(db, job['id'], stage)) != _hash(payload)):
        raise HttpError('public_research_analysis_inputs_changed')
    row = db.execute('SELECT record FROM studio_insight_versions WHERE id=? AND project_id=? '
                     "AND artifact_key LIKE 'material-snapshot-v1:%'", (payload['snapshot_id'], pid)).fetchone()
    snapshot = json.loads(row[0]) if row else None
    prior = read(db, origin, stage)
    if (not snapshot or not snapshot.get('materials')
            or not (snapshot['batch_id'] == origin or (prior and prior['snapshot_id'] == snapshot['id']))):
        raise HttpError('public_research_analysis_inputs_changed')
    value = dict(project_id=pid, root_job_id=parent['root_job_id'], holds=parent['holds'],
                 input_hash=_hash(payload), input_stage=stage)
    db.execute('INSERT INTO studio_job_checkpoints VALUES (?,?,?)', (job['id'], STAGE, json.dumps(value)))

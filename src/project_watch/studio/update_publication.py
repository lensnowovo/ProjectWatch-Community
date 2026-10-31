"""Resume settled local projection without collecting or reviewing again."""
import json

from project_watch.studio import event_insight_work
from project_watch.studio.dsh_bootstrap import COVERAGE, digest


def validate_origin(origin, update, publication, *, db):
    from project_watch.studio.dsh_closeout import active_hold
    phases = (update or {}).get('phases', {})
    source = phases.get('sources', {})
    state = origin.read_checkpoint('dsh_state', db=db) or {}
    binding = origin.read_checkpoint('material_publication_v1', db=db)
    admission = origin.read_checkpoint('dsh_record_admission', db=db) or {}
    if (source.get('status') not in ('complete', 'partial')
            or not isinstance(source.get('output'), dict)
            or source.get('output_fingerprint') != digest(source['output'])
            or phases.get('events', {}).get('status') != 'blocked'
            or publication.get('publication_complete')
            or not isinstance(publication.get('before'), dict)
            or not isinstance(publication.get('publication_sources'), dict)
            or not state.get('records') or state.get('finished')
            or not binding or admission.get('binding') != binding
            or active_hold(origin.read_checkpoint('dsh_hold', db=db))):
        raise ValueError('settled reviewed publication required')


def carry(origin, job, publication, caps, *, db):
    """Copy only publisher inputs and exact review receipts, including chunks.

    Old task status, attempts, failed stage and call journal are never changed.
    The inherited call links still account for every original request.
    """
    names = {'dsh_state', 'dsh_binding', 'dsh_record_admission',
             'material_publication_v1', 'material_closeout_v1', 'bootstrap_collection_v1'}
    rows = list(db.execute('SELECT stage,data FROM studio_job_checkpoints WHERE job_id=?', (origin.job['id'],)))
    selected = {r['stage']: r['data'] for r in rows if r['stage'] in names
                or r['stage'].startswith(('dsh_record_review_', 'dsh_source_', 'dsh_body_'))}
    for name, raw in list(selected.items()):
        origin.read_checkpoint(name, db=db)  # Validate referenced chunk digest.
        value = json.loads(raw)
        if isinstance(value, dict) and value.get('_chunked_checkpoint') == 1:
            selected.update({r['stage']: r['data'] for r in rows if r['stage'].startswith(value['prefix'])})
    for name, raw in selected.items():
        db.execute('INSERT OR IGNORE INTO studio_job_checkpoints VALUES (?,?,?)', (job['id'], name, raw))
    event_insight_work._put(db, job['id'], event_insight_work.ROUND,
        {**publication, 'cap': caps['model'], 'collection_cap': caps['model'],
         'origin_job_id': job['id'], 'round_root_job_id': job['id']})


def publish(research):
    from project_watch.studio import dsh_record_review

    old = research.store.get_public_profile(research.project_id) or {}
    facts = [] if research.topic_projection else old.get('facts') or []
    binding = dsh_record_review.admission_plan(research, research.state['records'], facts)[4]
    if (binding != research.execution.read_checkpoint('material_publication_v1')
            or dsh_record_review.pending_admission_calls(research, research.state['records'], facts)):
        raise ValueError('saved publication inputs or review changed')
    # Never let this local repair silently dispatch a review if input changes.
    def no_new_review(_batch):
        raise ValueError('publication continuation requires saved review')
    research.record_reviewer = no_new_review
    return research.tool('pw_finish', {'gaps': {
        k: '本轮仅整理已采集来源，未确认全面覆盖' for k in COVERAGE}})

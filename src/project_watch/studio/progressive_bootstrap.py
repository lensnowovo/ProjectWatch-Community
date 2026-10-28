"""Host-owned bootstrap extraction, using the existing semantic publisher.

No second scheduler or model client. Legacy started/recovery jobs keep their
original path; material ownership is frozen before collection can dispatch.
"""
import hashlib
import json
from contextlib import closing

from project_watch.studio import (
    collection_dsh,
    dsh_closeout,
    dsh_extraction,
    dsh_record_review,
    evidence_tasks,
)
from project_watch.studio.dsh_bootstrap import COVERAGE
from project_watch.studio.http import HttpError
from project_watch.studio.material_ownership import project_guard

OWNER = 'material_pipeline_v1'
CLOSEOUT = 'material_closeout_v1'


def freeze(execution, entry):
    with closing(execution.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        execution._guard(db)
        saved = db.execute('SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage=?',
                           (execution.job['id'], OWNER)).fetchone()
        if saved:
            value = json.loads(saved[0])
            if value['entry'] != entry or value['policy'] != evidence_tasks.TYPED_POLICY:
                raise ValueError('frozen material pipeline mismatch')
            if value['owner'] == 'materials':
                project_guard(db, execution.job['project_id'], execution.job['id'])
            return value
        stages = {r[0] for r in db.execute('SELECT stage FROM studio_job_checkpoints WHERE job_id=?',
                                         (execution.job['id'],))}
        started = bool(stages - {'collection_request_v1', 'external_first_ready', 'external_followup_input'})
        started |= db.execute('SELECT 1 FROM studio_job_calls WHERE job_id=? LIMIT 1',
                              (execution.job['id'],)).fetchone() is not None
        value = dict(entry=entry, owner='materials' if entry == 'bootstrap' and not started else 'legacy',
                     policy=evidence_tasks.TYPED_POLICY,
                     reason='started_or_recovery' if started else 'monitor_integration_pending' if entry != 'bootstrap' else '')
        if value['owner'] == 'materials':
            project_guard(db, execution.job['project_id'], execution.job['id'])
        db.execute('INSERT INTO studio_job_checkpoints VALUES (?,?,?)',
                   (execution.job['id'], OWNER, json.dumps(value)))
        return value


def _map_records(research):
    """Restore exact references; use the same deterministic record validator."""
    with closing(research.store._connect()) as db:
        research.execution._guard(db)
        tasks = [dict(r) for r in db.execute('SELECT * FROM studio_evidence_tasks WHERE job_id=? AND policy=? ORDER BY rowid',
                                            (research.execution.job['id'], evidence_tasks.TYPED_POLICY))]
    issues = []
    # Rebuild from immutable candidates, not a partly projected state after a crash.
    research.state['records'] = []
    for task in tasks:
        research.guard()
        result = json.loads(task['result'])
        if task['status'] != 'complete':
            issues.append(dict(task_id=task['id'], code=result.get('code', 'material_pending')))
            continue
        sid = 'C' + hashlib.sha256(task['evidence_version_id'].encode()).hexdigest()[:18]
        record = research.store.evidence.get(research.project_id, task['evidence_version_id'], include_internal=False)
        for index, candidate in enumerate(result['observations']):
            ref = result['refs'][candidate['refs'][0]]
            part = record['parts'][ref['part_index']]['text']
            quote = part[candidate['start']:candidate['end']]
            offset = sum(len(p['text']) for p in record['parts'][:ref['part_index']]) + candidate['start']
            # Refresh the saved source window before passing the restored quote
            # to the old validator. No request or transcription is involved.
            collection_dsh.source(research, sid, offset=offset)
            args = {k: v for k, v in candidate.items() if k not in ('subject', 'refs', 'start', 'end', 'event_at')}
            try:
                research.pw_record({**args, 'source_id': sid, 'quote': quote})
            except ValueError:
                issues.append(dict(task_id=task['id'], candidate=index, code='record_contract_rejected'))
                continue
            row = research.state['records'][-1]
            row.update(subject=candidate['subject'], date=candidate['event_at'] or '')
            # Keep the chosen location even when the same text occurs twice.
            row['refs'][0].update(part_index=ref['part_index'], start=candidate['start'], end=candidate['end'], quote=quote)
    research.execution.checkpoint_chunked('material_mapping_v2', {'issues': issues, 'tasks': len(tasks)})
    research.state['errors'] = [e for e in research.state['errors'] if e.get('tool') != 'material_extract']
    if issues:
        research.state['errors'].append({'tool': 'material_extract', 'code': 'material_candidates_partial'})
    research.save()


def _review_calls(research):
    old = research.store.get_public_profile(research.project_id) or {}
    return dsh_record_review.pending_admission_calls(research, research.state['records'], old.get('facts') or [])


def _publication_binding(research):
    old = research.store.get_public_profile(research.project_id) or {}
    return dsh_record_review.admission_plan(research, research.state['records'], old.get('facts') or [])[4]


def _remaining(research):
    # Shared admission accounting includes inherited paid receipts. Bootstrap
    # calls finish directly, so legacy's eight model submission turns do not apply.
    return dsh_closeout.request_budget(research.execution)['model']['remaining']


def _pending(research):
    with closing(research.store._connect()) as db:
        research.execution._guard(db)
        project_guard(db, research.project_id, research.execution.job['id'])
        return [dict(r) for r in db.execute("SELECT * FROM studio_evidence_tasks WHERE job_id=? AND policy=? "
            "AND status='pending' ORDER BY rowid", (research.execution.job['id'], evidence_tasks.TYPED_POLICY))]


def _defer(research, *, candidate_task=None):
    execution = research.execution
    with closing(research.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        execution._guard(db)
        project_guard(db, research.project_id, execution.job['id'])
        if candidate_task:
            # Keep the entire proposal and its settled extraction receipt for
            # explicit transfer. It is neither rejected nor partly published.
            db.execute("UPDATE studio_evidence_tasks SET status='pending' WHERE id=? AND job_id=? AND status='complete'",
                       (candidate_task, execution.job['id']))
        pending = [dict(r) for r in db.execute("SELECT id,policy FROM studio_evidence_tasks WHERE job_id=? "
            "AND policy=? AND status='pending' ORDER BY rowid", (execution.job['id'], evidence_tasks.TYPED_POLICY))]
        for task in pending:
            attempts = db.execute('SELECT 1 FROM studio_job_checkpoints WHERE job_id=? AND stage IN (?,?)',
                (execution.job['id'], 'extract_attempt:' + task['id'], 'extract_result:' + task['id'])).fetchone()
            linked = db.execute('SELECT 1 FROM studio_material_calls WHERE task_id=?', (task['id'],)).fetchone()
            if not attempts and not linked:
                receipt = dict(task_id=task['id'], policy=task['policy'], dispatch='not_started', reason='publication_reserve')
                db.execute('INSERT OR IGNORE INTO studio_job_checkpoints VALUES (?,?,?)',
                           (execution.job['id'], 'material_deferred:' + task['id'], json.dumps(receipt)))
        manifest = {'reason': 'publication_reserve', 'pending_task_ids': [t['id'] for t in pending],
                    'continuation': 'material_ownership.take_over', 'requires_terminal_owner': True}
        db.execute('INSERT INTO studio_job_checkpoints VALUES (?,?,?)',
                   (execution.job['id'], CLOSEOUT, json.dumps(manifest)))
    execution.checkpoint(dsh_closeout.STAGE, {'phase': 'closeout'})
    _map_records(research)


def _finish_deferred(research):
    """No affordable admission: retain current projections and pending evidence."""
    with research.lock, research.store.write_scope(research.execution._guard):
        research.guard()
        result = {'status': 'partial', 'records': 0, 'retained_records': len(research.state['records']),
                  'executor': 'dsh', 'collection': collection_dsh.coverage(research),
                  'material_work': research.execution.read_checkpoint(CLOSEOUT),
                  'reason': 'publication_admission_deferred', 'source_errors': research.state['errors']}
        research.state['finished'] = result
        research.store.set_state('dsh-bootstrap:' + research.project_id, json.dumps(result, ensure_ascii=False))
        research.execution.checkpoint('dsh_coverage', {'partial': True, 'coverage': {}})
        research.store.touch(research.project_id, 'last_external_at')
        research.save()
        return result


def run(research, model, http, runtime_root, directory):
    # The parent never starts a legacy research turn or advertises pw_record.
    # Only host-restored candidates may reach its existing validator/publisher.
    research.tools = ('pw_status', 'pw_finish')
    def extract(source, material):
        source = {**source, 'project': {'legal_name': research.project['legal_name']}}
        return dsh_extraction.extract(research, source, model, http, runtime_root, directory, execution=material)

    _map_records(research)
    while not research.execution.read_checkpoint(CLOSEOUT):
        research.guard()
        pending = _pending(research)
        if not pending:
            break
        task = pending[0]
        remaining = _remaining(research)
        cached = research.execution.read_checkpoint('extract_result:' + task['id']) is not None
        # A new material can send at most two model requests. Reserve the exact
        # existing admission plan and at least one review for its first candidate.
        # A saved proposal needs no transport and remains locally consumable.
        if not cached and remaining is not None and remaining < 2 + max(1, _review_calls(research)):
            if remaining == 0 and not research.state['records'] and not research.execution.budget_used('model'):
                raise HttpError('request_budget')
            _defer(research)
            break
        evidence_tasks.run_pending(research.execution, extract, limit=1, policy=evidence_tasks.TYPED_POLICY)
        _map_records(research)
        remaining = _remaining(research)
        try:
            needed = _review_calls(research)
        except ValueError as exc:
            if str(exc) != 'review_plan_limit':
                raise
            needed = None  # Keep the whole candidate when the bounded plan is full.
        if needed is None or (remaining is not None and needed > remaining):
            _defer(research, candidate_task=task['id'])
            break
    remaining = _remaining(research)
    binding = _publication_binding(research)
    # If HTTP settled before the review checkpoint was saved, admission must
    # still replay that exact journalled request at a fully consumed budget.
    publishing = research.execution.read_checkpoint('material_publication_v1') == binding
    if remaining is not None and _review_calls(research) > remaining and not publishing:
        if not research.execution.read_checkpoint(CLOSEOUT):
            _defer(research)
        return _finish_deferred(research)
    research.execution.checkpoint('material_publication_v1', binding)
    # Batch semantic admission retains cross-record consistency checks. The
    # existing publisher commits news before profile/scope/analysis closeout.
    return research.tool('pw_finish', {'gaps': {k: '本轮仅整理已采集来源，未确认全面覆盖' for k in COVERAGE}})

"""Host-owned bootstrap extraction, using the existing semantic publisher.

No second scheduler or model client. Legacy started/recovery jobs keep their
original path; material ownership is frozen before collection can dispatch.
"""
import hashlib
import json
from contextlib import closing

from project_watch.studio import collection_dsh, dsh_extraction, evidence_tasks
from project_watch.studio.dsh_bootstrap import COVERAGE
from project_watch.studio.material_ownership import project_guard

OWNER = 'material_pipeline_v1'


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


def run(research, model, http, runtime_root, directory):
    # The parent never starts a legacy research turn or advertises pw_record.
    # Only host-restored candidates may reach its existing validator/publisher.
    research.tools = ('pw_status', 'pw_finish')
    def extract(source, material):
        source = {**source, 'project': {'legal_name': research.project['legal_name']}}
        return dsh_extraction.extract(research, source, model, http, runtime_root, directory, execution=material)

    while evidence_tasks.run_pending(research.execution, extract, policy=evidence_tasks.TYPED_POLICY):
        research.guard()
    _map_records(research)
    # Batch semantic admission retains cross-record consistency checks. The
    # existing publisher commits news before profile/scope/analysis closeout.
    return research.tool('pw_finish', {'gaps': {k: '本轮仅整理已采集来源，未确认全面覆盖' for k in COVERAGE}})

"""Explicit bounded collection handoff on an already admitted leased job.

No enqueue loop or uncertain-request recovery. The caller uses the existing
queue/claim API, then admits a single successor before its normal worker entry.
"""
import hashlib
import json
from contextlib import closing
from types import SimpleNamespace

from project_watch.studio import collection_dsh, collection_host, evidence_tasks
from project_watch.studio.collection_ledger import STAGE as ROUND
from project_watch.studio.collection_ledger import CollectionLedger
from project_watch.studio.collection_plan import make_plan
from project_watch.studio.execution import JobExecution
from project_watch.studio.material_ownership import project_guard, take_over
from project_watch.studio.store import now

STAGE = 'bootstrap_collection_resume_v1'
IMPORTS = 'collection_source_imports_v1'
SUCCESSOR = 'bootstrap_collection_successor_v1'


def admit(execution, origin_job_id):
    """Bind one terminal origin to this fresh job; never increase origin caps."""
    from project_watch.studio.bootstrap_collection import RESULT
    from project_watch.studio.bootstrap_collection import STAGE as COLLECT
    from project_watch.studio.progressive_bootstrap import OWNER

    collection_host.limit_budgets(execution, 'first')
    with closing(execution.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        execution._guard(db)
        pid, jid = execution.job['project_id'], execution.job['id']
        if execution.job['kind'] not in ('external', 'external_followup') or (
                execution.job['kind'] == 'external_followup' and execution.job['input_version'] != origin_job_id):
            raise ValueError('explicit successor worker input required')
        project_guard(db, pid, jid)
        old = db.execute('SELECT * FROM studio_jobs WHERE id=? AND project_id=?', (origin_job_id, pid)).fetchone()
        if not old or old['status'] not in ('done', 'failed') or origin_job_id == jid:
            raise ValueError('terminal collection owner in same project required')
        origin = JobExecution(execution.store, dict(old))
        existing = execution.read_checkpoint(STAGE, db=db)
        if existing:
            if existing['origin_job_id'] != origin_job_id:
                raise ValueError('collection continuation origin already frozen')
            return existing
        request = execution.read_checkpoint('collection_request_v1', db=db) or {}
        if request.get('origin') != 'owner' or request.get('mode') != 'refresh':
            raise ValueError('explicit owner-admitted collection job required')
        if db.execute('SELECT 1 FROM studio_job_calls WHERE job_id=?', (jid,)).fetchone() or db.execute(
                "SELECT 1 FROM studio_job_checkpoints WHERE job_id=? AND stage!='collection_request_v1'", (jid,)).fetchone():
            raise ValueError('fresh successor job required')
        owner = origin.read_checkpoint(OWNER, db=db) or {}
        state = origin.read_checkpoint(COLLECT, db=db) or {}
        materials = db.execute("SELECT id FROM studio_evidence_tasks WHERE job_id=? AND status='pending'", (origin_job_id,)).fetchall()
        if (owner.get('owner') != 'materials' or owner.get('entry') != 'bootstrap'
                or state.get('phase') != 'done' or not (state.get('pending') or materials)):
            raise ValueError('finished bounded collection with pending work required')
        claimed = origin.read_checkpoint(SUCCESSOR, db=db)
        if claimed and claimed['job_id'] != jid:
            raise ValueError('collection continuation already admitted')
        actions = state['actions']
        for pending in state['pending']:
            if pending.get('tool') not in ('pw_read', 'pw_search', 'pw_collect') or pending.get('reason') not in (
                    'collection_action_limit', 'page_budget_reserved', 'page_count_limit', 'model_budget_reserved_for_materials'):
                raise ValueError('explicit undispatched collection action required')
            key = hashlib.sha256(json.dumps([pending['tool'], pending['args']], ensure_ascii=False,
                                            sort_keys=True).encode()).hexdigest()[:40]
            if key in actions:
                raise ValueError('pending collection action was already dispatched')
        for key, action in actions.items():
            result_owner = action.get('origin_job_id', origin_job_id)
            reader = JobExecution(execution.store, {'id': result_owner})
            if action['status'] != 'complete' or reader.read_checkpoint(RESULT + key, db=db) is None:
                raise ValueError('completed collection action receipt required')
        reader = SimpleNamespace(job=dict(old), read_checkpoint=lambda stage: origin.read_checkpoint(stage, db=db))
        refs = [r for values in collection_dsh._refs(SimpleNamespace(execution=reader, project_id=pid)).values() for r in values]
        for ref in refs:
            if not db.execute('SELECT 1 FROM studio_evidence_versions e JOIN studio_items i ON i.project_id=e.project_id '
                    "WHERE e.project_id=? AND e.id=? AND e.visibility='public' AND i.id=?",
                    (pid, ref['evidence_version_id'], ref['item_id'])).fetchone():
                raise ValueError('continuation source outside public project')
        scope = origin.read_checkpoint(collection_host.SCOPE, db=db)
        if not scope:
            raise ValueError('frozen collection scope required')
        record = dict(origin_job_id=origin_job_id, budgets=dict(execution.budgets),
                      budget_basis='new_round_cap_includes_inherited_completed_requests',
                      pending=state['pending'], material_task_ids=[r[0] for r in materials], admitted_at=now())
        rows = [(jid, STAGE, record), (jid, IMPORTS, refs), (jid, collection_host.SCOPE, scope),
                (jid, OWNER, dict(entry='bootstrap', owner='materials', policy=evidence_tasks.TYPED_POLICY,
                                  reason='explicit_collection_continuation')),
                (origin_job_id, SUCCESSOR, {'job_id': jid})]
        db.executemany('INSERT INTO studio_job_checkpoints VALUES (?,?,?)',
                       [(job, stage, json.dumps(value)) for job, stage, value in rows])
        # Read-only source descriptors and cached text retain their immutable
        # evidence IDs. No source query or successful page is dispatched again.
        db.execute("INSERT INTO studio_job_checkpoints SELECT ?,stage,data FROM studio_job_checkpoints "
                   "WHERE job_id=? AND (stage LIKE 'dsh_source_%' OR stage LIKE 'dsh_body_%')", (jid, origin_job_id))
        db.execute("INSERT OR IGNORE INTO studio_job_budget_links SELECT ?,job_id,call_key FROM studio_job_calls "
                   "WHERE job_id=? AND status='completed'", (jid, origin_job_id))
        db.execute('INSERT OR IGNORE INTO studio_job_budget_links SELECT ?,origin_job_id,call_key '
                   'FROM studio_job_budget_links WHERE job_id=?', (jid, origin_job_id))
    return record


def prepare(research):
    """Import evidence, not old coverage success; dispatch only the new pass."""
    from project_watch.studio.bootstrap_collection import STAGE as COLLECT
    from project_watch.studio.dsh_bootstrap import digest

    execution = research.execution
    admission = execution.read_checkpoint(STAGE)
    with closing(research.store._connect()) as db:
        execution._guard(db)
        project_guard(db, research.project_id, execution.job['id'])
        old = db.execute('SELECT * FROM studio_jobs WHERE id=? AND project_id=?',
                         (admission['origin_job_id'], research.project_id)).fetchone()
        if not old or old['status'] not in ('done', 'failed'):
            raise ValueError('previous collection owner is not terminal')
        origin = JobExecution(research.store, dict(old))
        pending = [task for task in admission['material_task_ids'] if db.execute(
            "SELECT 1 FROM studio_evidence_tasks WHERE id=? AND job_id=? AND status='pending'",
            (task, origin.job['id'])).fetchone()]
    for kind, maximum in admission['budgets'].items():
        current = execution.budgets.get(kind)
        execution.budgets[kind] = maximum if current is None else min(current, maximum) if maximum is not None else current
    collection_host.guard_scope(research)
    collection_host.sources_for(research)
    for task_id in pending:
        take_over(execution, task_id)
    if execution.read_checkpoint(ROUND) is None:
        scope = execution.read_checkpoint(collection_host.SCOPE)
        CollectionLedger(execution, make_plan(round_id=execution.job['id'], project_id=research.project_id,
            binding=digest(scope), mode='first', candidates=[], at=now()))
    if execution.read_checkpoint(COLLECT) is None:
        actions = (origin.read_checkpoint(COLLECT) or {})['actions']
        inherited = {key: {**action, 'origin_job_id': action.get('origin_job_id', origin.job['id'])}
                     for key, action in actions.items()}
        execution.checkpoint_chunked(COLLECT, dict(actions=inherited, pending=[], finished=None, phase='prepare'))

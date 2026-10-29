"""Adopt deferred public materials at the normal collection entry, without enqueueing.

The admitted cycle first checks new sources with its own bounded allowance.
Before extraction, eligible backlog, references and consumed calls transfer in
one transaction. An exhausted predecessor is retained, never allowed to freeze
future source checks or obtain a fresh continuation budget.
"""
import json
from contextlib import closing
from types import SimpleNamespace

from project_watch.studio import collection_dsh, evidence_tasks
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError
from project_watch.studio.material_ownership import project_guard, take_over

STAGE = 'material_handoff_v1'
CAPS = 'material_round_caps_v1'
IMPORTS = 'collection_source_imports_v1'
CLAIM = 'material_handoff_claim_v1'


def _put(db, jid, stage, value):
    encoded = json.dumps(value, ensure_ascii=False)
    if len(encoded) > 1_000_000:
        raise ValueError('material handoff exceeds bounds')
    db.execute('INSERT INTO studio_job_checkpoints VALUES (?,?,?)', (jid, stage, encoded))


def _limit(execution, caps):
    for kind, maximum in caps.items():
        current = execution.budgets[kind]
        if maximum is not None:
            execution.budgets[kind] = maximum if current is None else min(current, maximum)


def restore_limits(execution, db):
    """Also used inside final journal admission, after cache lookup, before started."""
    saved = execution.read_checkpoint(STAGE, db=db)
    caps = saved['caps'] if saved else execution.read_checkpoint(CAPS, db=db)
    if caps is not None:
        _limit(execution, caps)


def prepare(research, scope):
    """Freeze this round's actual limits before collection, without taking backlog."""
    execution = research.execution
    owner = execution.read_checkpoint('material_pipeline_v1') or {}
    if owner.get('owner') != 'materials':
        return
    with closing(execution.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        execution._guard(db)
        if execution.read_checkpoint(CAPS, db=db) is None:
            caps = dict(execution.budgets)
            # An in-flight pre-upgrade round can prove a lower cap in its call
            # receipts. Reconstruction must not erase that original boundary.
            for kind, maximum in db.execute('SELECT kind,min(request_limit) FROM studio_job_calls '
                    'WHERE job_id=? AND request_limit IS NOT NULL GROUP BY kind', (execution.job['id'],)):
                caps[kind] = maximum if caps[kind] is None else min(caps[kind], maximum)
            _put(db, execution.job['id'], CAPS, caps)
        restore_limits(execution, db)


def _scope_matches(old, current):
    # Added watches are allowed; removing/changing the original scope is not.
    if any(old.get(k) != current.get(k) for k in ('project', 'settings_binding', 'source_decisions')):
        return False
    return all(row in current.get(key, []) for key in ('watches', 'source_queries') for row in old.get(key, []))


def _usable(db, task):
    return db.execute("""SELECT 1 FROM studio_evidence_versions e
        JOIN studio_evidence_heads h ON h.project_id=e.project_id
          AND h.source_key=e.source_key AND h.evidence_version_id=e.id
        LEFT JOIN studio_evidence_lifecycle l ON l.project_id=e.project_id AND l.evidence_version_id=e.id
        WHERE e.project_id=? AND e.id=? AND e.visibility='public' AND COALESCE(l.status,'')!='withdrawn'
        """, (task['project_id'], task['evidence_version_id'])).fetchone() is not None


def adopt(research):
    execution = research.execution
    scope = execution.read_checkpoint('collection_scope_v1')
    owner = execution.read_checkpoint('material_pipeline_v1') or {}
    request = execution.read_checkpoint('collection_request_v1') or {}
    if not scope or owner.get('owner') != 'materials' or owner.get('entry') not in ('bootstrap', 'monitor') or request.get('mode') == 'topic':
        return None
    from project_watch.studio.collection_host import guard_scope

    guard_scope(research)
    with closing(execution.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        execution._guard(db)
        pid, jid = execution.job['project_id'], execution.job['id']
        project_guard(db, pid, jid)
        project = db.execute('SELECT status FROM studio_projects WHERE project_id=?', (pid,)).fetchone()
        if not project or project[0] != 'active':
            raise HttpError('project_paused')
        saved = execution.read_checkpoint(STAGE, db=db)
        if saved is not None:
            _limit(execution, saved['caps'])
            return saved
        caps = execution.read_checkpoint(CAPS, db=db)
        if caps is None:
            caps = dict(execution.budgets)
            _put(db, jid, CAPS, caps)
        # Do not expand an already-running material phase on upgrade/reclaim.
        started = execution.read_checkpoint('material_mapping_v2', db=db) is not None
        receipt = dict(caps=caps, transfers=[], retained=[], reason='material_processing_already_started' if started else '')
        imports = execution.read_checkpoint(IMPORTS, db=db) or []
        candidates = [] if started else db.execute("""SELECT DISTINCT j.id,j.created_at FROM studio_jobs j
            JOIN studio_evidence_tasks t ON t.job_id=j.id
            WHERE j.project_id=? AND j.id!=? AND j.status IN ('done','failed')
              AND t.project_id=? AND t.policy=? AND t.status='pending'
            ORDER BY j.created_at,j.id""", (pid, jid, pid, evidence_tasks.TYPED_POLICY)).fetchall()
        for job in candidates:
            from project_watch.studio.acceptance_batch import guard_transfer

            try:
                guard_transfer(db, job['id'], jid)
            except HttpError as exc:
                if exc.code != 'acceptance_old_successor':
                    raise
                receipt['retained'].append(dict(origin=job['id'], reason='acceptance_old_successor'))
                continue
            origin = JobExecution(execution.store, {'id': job['id']})
            def read(stage, origin=origin):
                return origin.read_checkpoint(stage, db=db)
            old_owner, old_scope = read('material_pipeline_v1') or {}, read('collection_scope_v1') or {}
            closeout = read('material_closeout_v1') or {}
            if (old_owner.get('owner') != 'materials' or old_owner.get('entry') not in ('bootstrap', 'monitor')
                    or (read('collection_request_v1') or {}).get('mode') == 'topic'
                    or closeout.get('reason') != 'publication_reserve' or not _scope_matches(old_scope, scope)):
                receipt['retained'].append(dict(origin=job['id'], reason='scope_or_closeout_not_compatible'))
                continue
            if read(CLAIM):
                receipt['retained'].append(dict(origin=job['id'], reason='origin_already_claimed'))
                continue
            analysis = read('event_insight_round_v1') or {}
            analysis_root = analysis.get('round_root_job_id', job['id'])
            root_reader = JobExecution(execution.store, {'id': analysis_root})
            analysis_tail = root_reader.read_checkpoint('event_insight_budget_tail_v1', db=db)
            root_analysis = root_reader.read_checkpoint('event_insight_round_v1', db=db) or analysis
            # Unrouted work stays a durable responsibility at its original root.
            # It may share a material successor only while no analysis child has
            # claimed the tail; otherwise the existing serial owner goes first.
            if analysis and (analysis.get('phase') not in ('settled', 'waiting_inputs')
                    or analysis.get('insight_job_id') or root_analysis.get('insight_job_id')
                    or (analysis_tail and analysis_tail['tail_job_id'] not in (job['id'], jid))):
                receipt['retained'].append(dict(origin=job['id'], reason='analysis_owner_not_settled'))
                continue
            prior = read(STAGE) or {}
            origin_caps = prior.get('caps') or read(CAPS)
            if origin_caps is None:
                receipt['retained'].append(dict(origin=job['id'], reason='original_budget_not_bound'))
                continue
            # Include both jobs and their inherited receipts once, before any
            # ownership change. Never infer free requests from missing metadata.
            combined = dict(caps)
            for kind, maximum in origin_caps.items():
                if maximum is not None:
                    combined[kind] = maximum if combined[kind] is None else min(combined[kind], maximum)
            counts = dict(db.execute("""SELECT c.kind,count(*) FROM studio_job_calls c JOIN (
                SELECT job_id,call_key FROM studio_job_calls WHERE job_id IN (?,?)
                UNION SELECT origin_job_id,call_key FROM studio_job_budget_links WHERE job_id IN (?,?)
                ) b ON b.job_id=c.job_id AND b.call_key=c.call_key
                GROUP BY c.kind""", (jid, job['id'], jid, job['id'])))
            count = counts.get('model', 0)
            # Conservative room for a new two-turn extraction plus its review.
            # Completed proposal reuse may require only review, but this batch
            # does not invent a smaller allowance for unexamined proposals.
            if (combined['model'] is None or combined['model'] - count < 3
                    or any(maximum is not None and counts.get(kind, 0) > maximum for kind, maximum in combined.items())):
                receipt['retained'].append(dict(origin=job['id'], reason='original_budget_insufficient',
                                                model_used=count, model_cap=combined['model'], used=counts))
                continue
            tasks = [dict(t) for t in db.execute("SELECT * FROM studio_evidence_tasks WHERE job_id=? AND project_id=? "
                "AND policy=? AND status='pending' ORDER BY rowid", (job['id'], pid, evidence_tasks.TYPED_POLICY))]
            reader = SimpleNamespace(job={'id': job['id']}, read_checkpoint=read)
            old_refs = [ref for group in collection_dsh._refs(SimpleNamespace(execution=reader, project_id=pid)).values() for ref in group]
            transferred = []
            for task in tasks:
                refs = [r for r in old_refs if r['evidence_version_id'] == task['evidence_version_id']]
                page = read('material_page_sources_v1:' + task['id'])
                page_valid = page and page.get('evidence_version_id') == task['evidence_version_id'] and page.get('material_body_evidence_id') == task['evidence_version_id']
                items = [r.get('item_id') for r in refs] or ([r.get('item_id') for r in page.get('collection_matches', [])] if page_valid else [])
                allowed_items = items and all(db.execute("SELECT 1 FROM studio_items WHERE id=? AND project_id=? "
                    "AND relevant=1 AND status!='dismissed'", (item, pid)).fetchone() for item in items)
                if (task['id'] not in closeout.get('pending_task_ids', []) or not _usable(db, task) or not allowed_items):
                    receipt['retained'].append(dict(task_id=task['id'], reason='input_no_longer_eligible'))
                    continue
                db.execute('SAVEPOINT material_transfer')
                try:
                    take_over(execution, task['id'], db=db)
                except (ValueError, HttpError) as exc:
                    if str(exc) not in ('material dispatch provenance missing', 'material checkpoint conflict',
                                        'acceptance_old_successor'):
                        raise
                    db.execute('ROLLBACK TO material_transfer')
                    receipt['retained'].append(dict(task_id=task['id'], reason=(
                        'acceptance_old_successor' if str(exc) == 'acceptance_old_successor'
                        else 'material_provenance_not_reusable')))
                    continue
                finally:
                    db.execute('RELEASE material_transfer')
                transferred.append(task['id'])
                imports.extend(ref for ref in refs if ref not in imports)
            if transferred:
                caps.update(combined)
                _put(db, job['id'], CLAIM, dict(next_job_id=jid, tasks=transferred))
                db.execute('INSERT OR IGNORE INTO studio_job_budget_links SELECT ?,job_id,call_key '
                           'FROM studio_job_calls WHERE job_id=?', (jid, job['id']))
                db.execute('INSERT OR IGNORE INTO studio_job_budget_links SELECT ?,origin_job_id,call_key '
                           'FROM studio_job_budget_links WHERE job_id=?', (jid, job['id']))
                receipt['transfers'].append(dict(origin=job['id'], tasks=transferred))
                # Analysis may use its reserved tail, never a newly enlarged
                # whole-round cap. Persist before begin() can be replayed.
                current_analysis = execution.read_checkpoint('event_insight_round_v1', db=db)
                total_cap = analysis.get('cap', origin_caps['model'])
                if current_analysis:
                    current_analysis['cap'] = min(current_analysis['cap'], total_cap, root_analysis.get('cap', total_cap))
                    current_analysis['collection_cap'] = caps['model']
                    current_analysis['round_root_job_id'] = analysis_root
                    db.execute('UPDATE studio_job_checkpoints SET data=? WHERE job_id=? AND stage=?',
                               (json.dumps(current_analysis), jid, 'event_insight_round_v1'))
                    # Tightening is safe only before any analysis admission,
                    # checked above. Old unresolved work cannot later spend a
                    # separate copy of the original balance.
                    for aid in {job['id'], analysis_root}:
                        prior_analysis = JobExecution(execution.store, {'id': aid}).read_checkpoint('event_insight_round_v1', db=db)
                        if prior_analysis:
                            prior_analysis['cap'] = current_analysis['cap']
                            db.execute('UPDATE studio_job_checkpoints SET data=? WHERE job_id=? AND stage=?',
                                       (json.dumps(prior_analysis), aid, 'event_insight_round_v1'))
                    db.execute('INSERT INTO studio_job_checkpoints VALUES (?,?,?) ON CONFLICT(job_id,stage) DO UPDATE SET data=excluded.data',
                               (analysis_root, 'event_insight_budget_tail_v1', json.dumps(dict(cap=current_analysis['cap'], tail_job_id=jid))))
                break  # One exclusive predecessor per admitted cycle.
        _put(db, jid, STAGE, receipt)
        encoded = json.dumps(imports, ensure_ascii=False)
        if len(encoded) > 1_000_000:
            raise ValueError('material source imports exceed bounds')
        db.execute('INSERT INTO studio_job_checkpoints VALUES (?,?,?) ON CONFLICT(job_id,stage) DO UPDATE SET data=excluded.data',
                   (jid, IMPORTS, encoded))
    _limit(execution, caps)
    return receipt

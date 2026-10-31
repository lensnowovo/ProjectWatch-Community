"""Explicit terminal-work migration into the single update coordinator.

This is not a retry route or scheduler. Admission keeps the old root, calls and
outputs intact and creates exactly one declared descendant with the same cap.
"""
import hashlib
import json
from contextlib import closing
from types import SimpleNamespace

from project_watch.studio import collection_dsh, event_insight_work, update_run
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError
from project_watch.studio.store import now

STAGE = 'update_handoff_v1'
SUCCESSOR = 'update_handoff_successor_v1'


def admit(pipeline, origin_id, *, authorization, resume='sources', repair_content=False):
    from project_watch.studio import (
        acceptance_batch,
        evidence_tasks,
        public_research_admission,
        public_stage_work,
    )
    from project_watch.studio.material_ownership import project_guard
    from project_watch.studio.recovery import input_binding

    if not isinstance(authorization, str) or not 1 <= len(authorization) <= 200:
        raise ValueError('explicit update handoff authorization required')
    if resume not in ('sources', 'publication', 'analysis'):
        raise ValueError('invalid update continuation phase')
    store = pipeline.store
    with closing(store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT * FROM studio_jobs WHERE id=?', (origin_id,)).fetchone()
        if not old or old['status'] not in ('done', 'failed'):
            raise ValueError('terminal update required')
        origin = JobExecution(store, dict(old))
        existing = origin.read_checkpoint(SUCCESSOR, db=db)
        if existing:
            return dict(db.execute('SELECT * FROM studio_jobs WHERE id=?', (existing['job_id'],)).fetchone())
        pid = old['project_id']
        project = db.execute('SELECT status FROM studio_projects WHERE project_id=?', (pid,)).fetchone()
        if not project or project[0] != 'active':
            raise HttpError('project_paused')
        if db.execute("SELECT 1 FROM studio_jobs WHERE project_id=? AND status IN ('queued','running')", (pid,)).fetchone():
            raise ValueError('project work has not drained')
        owner = origin.read_checkpoint('material_pipeline_v1', db=db) or {}
        round_state = origin.read_checkpoint(event_insight_work.ROUND, db=db) or {}
        prior_update = origin.read_checkpoint(update_run.STATE, db=db)
        publication_complete = bool(round_state.get('publication_complete'))
        if resume == 'publication':
            from project_watch.studio.update_publication import validate_origin

            validate_origin(origin, prior_update, round_state, db=db)
        if not publication_complete and resume == 'sources':
            from project_watch.studio.dsh_bootstrap import digest as stage_digest

            # A terminal coordinator may have retained partial material work
            # before analysis failed. Its durable events boundary is sufficient
            # for *source/material* continuation, never a completed publication.
            phase = (prior_update or {}).get('phases', {}).get('events', {})
            output = phase.get('output')
            if (phase.get('status') in ('partial', 'complete') and isinstance(output, dict)
                    and phase.get('output_fingerprint') == stage_digest(output)
                    and isinstance(output.get('event_versions'), dict)
                    and type(output.get('publication_complete')) is bool):
                round_state = {**round_state, 'event_versions': output['event_versions']}
            else:
                raise ValueError('durable material stage receipt required')
        binding = input_binding(pipeline, pid, db)
        if (owner.get('owner') != 'materials' or (resume == 'analysis' and not publication_complete)
                or (origin.read_checkpoint('recovery_inputs', db=db) or {}).get('binding') != binding):
            raise ValueError('finished compatible material publication required')
        tail = origin.read_checkpoint(event_insight_work.BUDGET, db=db) or {}
        owners = {origin_id, tail.get('tail_job_id', origin_id), round_state.get('insight_job_id', origin_id)}
        owners.discard(None)
        calls, checkpoints = [], {}
        for jid in sorted(owners):
            job = db.execute('SELECT status,project_id FROM studio_jobs WHERE id=?', (jid,)).fetchone()
            if not job or job['status'] not in ('done', 'failed') or job['project_id'] != pid:
                raise ValueError('analysis owner has not drained')
            calls.extend(dict(r) for r in db.execute('SELECT c.* FROM studio_job_calls c JOIN ('
                'SELECT job_id,call_key FROM studio_job_calls WHERE job_id=? UNION '
                'SELECT origin_job_id,call_key FROM studio_job_budget_links WHERE job_id=?'
                ') b ON b.job_id=c.job_id AND b.call_key=c.call_key', (jid, jid)))
            checkpoints[jid] = [dict(r) for r in db.execute(
                'SELECT stage,data FROM studio_job_checkpoints WHERE job_id=? ORDER BY stage', (jid,))]
        unique = {(c['job_id'], c['call_key']): c for c in calls}
        if any(c['status'] not in ('completed', 'failed_read', 'not_sent') for c in unique.values()):
            raise HttpError('request_outcome_unknown')
        if any(c['kind'] == 'model' and (c['response_status'] or 0) >= 400 for c in unique.values()):
            raise HttpError('received_model_failure_requires_review')
        content_handoff = origin.read_checkpoint('update_content_review_handoff_v1', db=db) or {}
        repairs = []
        if repair_content:
            # Separate, explicit work on a received rejection. A settled
            # evidence-shape rejection may get a new full review, never an
            # automatic retry or a claim that its invalid old review passed.
            from project_watch.studio.event_insight import RoleExecution

            for jid in sorted(owners):
                reader = JobExecution(store, {'id': jid})
                hold = reader.read_checkpoint('dsh_hold', db=db)
                if hold not in ({'code': 'judgment_review_limit'}, {'code': 'judgment_review_invalid'}):
                    continue
                manifest = reader.read_checkpoint(event_insight_work.INPUT, db=db) or {}
                rejected = []
                for work in manifest.get('objects', []):
                    role = RoleExecution(reader, work['fingerprint'])
                    bindings = role.read_checkpoint('dsh_semantic_drafts', db=db) or []
                    reviews = [role.read_checkpoint('dsh_semantic_' + b[:40], db=db) for b in bindings]
                    draft = role.read_checkpoint('object_insight_draft', db=db)
                    content_rejected = (hold == {'code': 'judgment_review_limit'} and len(reviews) == 2
                        and all(r and not r.get('passed') and not r.get('invalid') and r.get('issues') for r in reviews))
                    evidence_rejected = (hold == {'code': 'judgment_review_invalid'} and 1 <= len(reviews) <= 2
                        and all(r and not r.get('passed') and r.get('binding') == b
                                and (r.get('invalid') == 'review_evidence_invalid'
                                     or (not r.get('invalid') and r.get('issues')))
                                for b, r in zip(bindings, reviews, strict=True))
                        and any(r.get('invalid') == 'review_evidence_invalid' for r in reviews))
                    path_rejected = (hold == {'code': 'judgment_review_invalid'} and bindings
                        and reviews[-1] is None and draft
                        and settled_path_response(db, reader, role, draft, bindings[-1]))
                    if not (content_rejected or evidence_rejected or path_rejected):
                        continue
                    if not draft or draft.get('fingerprint') != work['fingerprint']:
                        continue
                    rejected.append(dict(origin_job_id=jid, fingerprint=work['fingerprint'],
                        draft=draft, reviews=reviews, authorization=authorization,
                        full_new_review_required=True))
                if not rejected:
                    if hold == {'code': 'judgment_review_invalid'}:
                        continue  # Unrecognized protocol errors remain held.
                    raise ValueError('settled rejected review receipts required')
                raw = db.execute('SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage=?',
                                 (jid, 'dsh_hold')).fetchone()[0]
                content_handoff[jid] = hashlib.sha256(raw.encode()).hexdigest()
                repairs.extend(rejected)
        project_guard(db, pid, origin_id, content_handoff=content_handoff)
        caps = origin.read_checkpoint('material_round_caps_v1', db=db)
        if not caps or not isinstance(round_state.get('cap'), int):
            raise ValueError('original finite caps required')
        caps = {**caps, 'model': round_state['cap']}
        used = {kind: sum(c['kind'] == kind for c in unique.values()) for kind in caps}
        account = acceptance_batch._project(db, pid)
        if resume == 'analysis':
            inputs = [JobExecution(store, {'id': jid}).read_checkpoint(event_insight_work.INPUT, db=db)
                      for jid in owners]
            if not any(value and value.get('settings_binding') == binding for value in inputs):
                raise ValueError('analysis continuation requires fixed published work')
            if prior_update is None:
                # Legacy publication already handed its fixed input to a
                # terminal insight owner. Adapt that receipt, not its sources.
                result = (origin.read_checkpoint('dsh_state', db=db) or {}).get('finished')
                collection = origin.read_checkpoint('bootstrap_collection_v1', db=db)
                if not result or not collection:
                    raise ValueError('legacy publication receipts missing')
                outputs = dict(sources={'receipt_fingerprint': digest(collection), 'legacy_origin': origin_id},
                    events=dict(event_versions=round_state['event_versions'], result=result, publication_complete=True))
                prior_update = dict(finished_at=old['finished_at'], legacy=True, phases={
                    name: dict(output=value, output_fingerprint=digest(value), status='partial')
                    for name, value in outputs.items()})
            # The job must be terminal (checked above), not the whole update.
            # A failed analysis cannot set the coordinator's final timestamp.
            # Carry only validated, already completed publication boundaries.
            for name in ('sources', 'events'):
                phase = prior_update.get('phases', {}).get(name, {})
                output = phase.get('output')
                if (phase.get('status') not in ('complete', 'partial')
                        or not isinstance(output, dict)
                        or phase.get('output_fingerprint') != digest(output)):
                    raise ValueError('analysis continuation requires durable published work')
            published = prior_update['phases']['events']['output']
            if (published.get('publication_complete') is not True
                    or published.get('event_versions') != round_state.get('event_versions')):
                raise ValueError('analysis continuation requires matching publication')
        if account:
            from project_watch.studio import research_work_budget

            if research_work_budget.current(db, account['batch_id']):
                total, _ = research_work_budget.limits(db, account, json.loads(account['specification']),
                                                       json.loads(account['allocation']))
                caps['model'] = max(caps['model'], used['model'] + max(0, total['model'] -
                    acceptance_batch.usage(db, account['batch_id'], 'model')))
        # Admission does not send. Shared-work owners forecast the next actual
        # unit under the existing cost/unknown fences after obtaining a lease.
        account_snapshot = None
        if account:
            if not db.execute('SELECT 1 FROM studio_acceptance_jobs WHERE job_id=?', (origin_id,)).fetchone():
                raise HttpError('acceptance_unbound_successor')
            acceptance_batch._active(db, {**dict(account), 'state': 'open'})
            if db.execute('SELECT 1 FROM studio_acceptance_releases WHERE batch_id=?', (account['batch_id'],)).fetchone():
                raise HttpError('acceptance_continuation_released')
            if db.execute("SELECT 1 FROM studio_jobs j JOIN studio_acceptance_jobs a ON a.job_id=j.id "
                "JOIN studio_acceptance_projects p ON p.project_id=a.project_id WHERE p.batch_id=? "
                "AND j.status IN ('queued','running')", (account['batch_id'],)).fetchone():
                raise ValueError('acceptance work has not drained')
            if db.execute("SELECT 1 FROM studio_job_calls c JOIN studio_acceptance_jobs a ON a.job_id=c.job_id "
                "JOIN studio_acceptance_projects p ON p.project_id=a.project_id WHERE p.batch_id=? "
                "AND c.status IN ('started','unknown')", (account['batch_id'],)).fetchone():
                raise HttpError('request_outcome_unknown')
            costs = [dict(r) for r in db.execute('SELECT * FROM studio_research_costs WHERE batch_id=? ORDER BY job_id,call_key', (account['batch_id'],))]
            if any(c['settled'] == -1 for c in costs):
                raise HttpError('research_history_unpriced')
            account_snapshot = dict(batch_id=account['batch_id'], specification=account['specification'],
                deadline=account['deadline'], prior_state=account['state'], costs_sha256=digest(costs),
                usage={k: acceptance_batch.usage(db, account['batch_id'], k) for k in acceptance_batch.KINDS})
            # Explicit human-authorized handoff, same cumulative account and end.
            # Enqueue/descendant inheritance below remain in this transaction.
            db.execute("UPDATE studio_acceptance_batches SET state='open' WHERE id=?", (account['batch_id'],))
        for call in unique.values():
            body = call.pop('response_body')
            call['response_sha256'] = hashlib.sha256(body).hexdigest() if body is not None else None
        job = store.enqueue(pid, 'external_followup', automatic=True,
                            input_version='update-handoff:' + origin_id, parent_job_id=origin_id, db=db)
        if not job.get('id') or job['status'] != 'queued':
            raise ValueError('fresh handoff descendant required')
        reader = SimpleNamespace(job=dict(old), read_checkpoint=lambda stage: origin.read_checkpoint(stage, db=db))
        refs = [r for group in collection_dsh._refs(SimpleNamespace(execution=reader, project_id=pid, store=store)).values() for r in group]
        scope = origin.read_checkpoint('collection_scope_v1', db=db)
        if not scope:
            raise ValueError('original source scope required')
        public_root = (public_research_admission.read(db, origin_id) or {}).get('root_job_id', origin_id)
        # Carry immutable successful/failed query receipts as context. They are
        # not rerun or reclassified as work performed by the new owner.
        query_history = [entry for entry, _unit, _receipt in collection_dsh.historical_queries(
            SimpleNamespace(execution=reader, store=store, project_id=pid), db=db)]
        for jid in sorted(owners | {public_root}):
            previous = JobExecution(store, {'id': jid})
            stages = ['collection_round_v1'] + [r['stage'] for r in
                (previous.read_checkpoint('collection_supplements_v1', db=db) or [])]
            for name in stages:
                ledger = previous.read_checkpoint(name, db=db) or {}
                receipts = {r['unit_id']: r for r in ledger.get('receipts', [])}
                for unit in ledger.get('plan', {}).get('units', []):
                    receipt = receipts.get(unit['id'])
                    if receipt and unit['category'] in ('paper', 'patent', 'clinical'):
                        query_history.append(dict(origin_job_id=jid, unit_id=unit['id'],
                            adapter=unit['source'], query=unit['query'], outcome=receipt['outcome'],
                            result_count=receipt.get('result_count'), reason=receipt.get('reason', ''),
                            receipt_fingerprint=digest(receipt)))
        query_history = list({(entry['origin_job_id'], entry['unit_id']): entry for entry in query_history}.values())
        payload = dict(work={'kind': 'update_handoff'}, origin_job_id=origin_id,
            round_root_job_id=public_root, predecessor_job_id=None, cap=caps['model'], caps=caps,
            authorization=authorization, admitted_at=now(), settings_binding=binding,
            original_event_versions=round_state.get('event_versions', {}), original_scope=scope,
            original_publication_complete=publication_complete,
            analysis_owners=sorted(owners),
            professional_query_history=query_history,
            content_repairs=repairs,
            account=account_snapshot, carried_calls=list(unique.values()),
            checkpoint_fingerprints={jid: digest(rows) for jid, rows in checkpoints.items()},
            pending_task_ids=[r[0] for r in db.execute("SELECT id FROM studio_evidence_tasks WHERE job_id=? AND status='pending'", (origin_id,))])
        if resume in ('analysis', 'publication'):
            payload.update(resume_phase=resume, prior_update=prior_update)
        if resume == 'publication':
            payload['publication_collection_fingerprint'] = digest(collection_dsh.rounds(
                SimpleNamespace(execution=reader, project_id=pid, store=store)))
        put = event_insight_work._put
        put(db, job['id'], STAGE, payload)
        if content_handoff:
            put(db, job['id'], 'update_content_review_handoff_v1', content_handoff)
        put(db, origin_id, SUCCESSOR, dict(job_id=job['id'], input_fingerprint=digest(payload), authorization=authorization))
        entry = owner['entry'] if resume == 'publication' else 'monitor'
        put(db, job['id'], update_run.OWNER, dict(protocol=1, entry=entry, created_at=now(), owner_job_id=job['id'], origin_job_id=origin_id))
        put(db, job['id'], 'material_pipeline_v1', dict(entry=entry, owner='materials', policy=evidence_tasks.TYPED_POLICY, reason='explicit_update_handoff', mapping_version='subject-v1'))
        put(db, job['id'], 'collection_admission_v1', dict(enabled=True, mode='refresh', reason='explicit_update_handoff'))
        put(db, job['id'], 'collection_request_v1', dict(origin='owner', mode='refresh'))
        put(db, job['id'], 'recovery_inputs', dict(binding=binding))
        put(db, job['id'], 'collection_source_imports_v1', refs)
        if resume in ('analysis', 'publication'):
            put(db, job['id'], 'collection_scope_v1', scope)
            put(db, job['id'], 'material_round_caps_v1', caps)
        db.execute("INSERT INTO studio_job_checkpoints SELECT ?,stage,data FROM studio_job_checkpoints "
                   "WHERE job_id=? AND (stage LIKE 'dsh_source_%' OR stage LIKE 'dsh_body_%')", (job['id'], origin_id))
        if resume == 'publication':
            from project_watch.studio.update_publication import carry

            carry(origin, job, round_state, caps, db=db)
        public_stage_work.inherit(db, job['id'], owners)
        public_research_admission.inherit_work(db, pid, origin_id, job, payload)
        return job


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def settled_path_response(db, reader, role, draft, binding):
    """Recognize a settled field-shape rejection for explicit fresh review.

    This only permits explicitly authorized fresh work. It never records the
    old response as passed, skips its semantic review, or changes its hold.
    """
    from project_watch.studio import dsh_judgment_review as review

    progress = role.read_checkpoint('dsh_review_progress', db=db) or {}
    ctx = role.read_checkpoint('dsh_monitor_context', db=db)
    if (progress.get('binding') != binding or progress.get('status') != 'running'
            or progress.get('phase') != 'fields' or not ctx
            or draft.get('fingerprint') != ctx.get('fingerprint')):
        return False
    row = db.execute("SELECT response_body FROM studio_job_calls WHERE job_id=? AND kind='model' "
        "AND status='completed' AND response_status=200 ORDER BY created_at DESC LIMIT 1", (reader.job['id'],)).fetchone()
    if not row:
        return False
    try:
        batches = review.review_batches(review.review_input(draft['analysis'], ctx))
        index = progress.get('batch')
        if (type(index) is not int or not 1 <= index <= len(batches)
                or progress.get('total') != len(batches)):
            return False
        batch = batches[index - 1]
        if batch['phase'] != 'fields' or progress.get('fields') != len(batch['fields']):
            return False
        choice = json.loads(row[0])['choices'][0]
        message = choice['message']
        if choice.get('finish_reason') != 'stop' or message.get('tool_calls'):
            return False
        value = json.loads(message['content'])
        if (not isinstance(value, dict) or not isinstance(value.get('checks'), list)
                or not value['checks'] or any(not isinstance(r, dict) for r in value['checks'])):
            return False
        try:
            review.validate_review(value, batch)
        except ValueError as exc:
            # A received field/quote rejection is not an unknown request.
            # Permit explicit new work, never the old malformed result.
            return str(exc) in ('review_field_coverage_invalid', 'review_evidence_invalid')
        return True
    except (KeyError, IndexError, TypeError, ValueError):
        return False


def reuse_analysis(execution, manifest):
    """Carry a settled draft/review prefix only for the exact same fixed input.

    Old receipts stay immutable. The new owner may finish the pending review;
    it does not regenerate the draft or repeat already saved review batches.
    """
    from project_watch.studio.event_insight import RoleExecution

    handoff = execution.read_checkpoint(STAGE)
    if not handoff:
        return
    with closing(execution.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        execution._guard(db)
        if execution.read_checkpoint('update_analysis_reuse_v1', db=db) is not None:
            return
        carried = []
        for work in manifest['objects']:
            for repair in handoff.get('content_repairs', []):
                if repair['fingerprint'] == work['fingerprint']:
                    role = RoleExecution(execution, work['fingerprint'])
                    event_insight_work._put(db, execution.job['id'], role._stage('object_insight_repair_v1'), repair)
            for jid in handoff['analysis_owners']:
                origin = JobExecution(execution.store, {'id': jid})
                old_input = origin.read_checkpoint(event_insight_work.INPUT, db=db) or {}
                if (old_input.get('settings_binding') != manifest['settings_binding']
                        or not any(w['fingerprint'] == work['fingerprint'] for w in old_input.get('objects', []))):
                    continue
                role = RoleExecution(origin, work['fingerprint'])
                state = role.read_checkpoint('dsh_state', db=db) or {}
                draft = role.read_checkpoint('object_insight_draft', db=db)
                if (not draft or draft.get('fingerprint') != work['fingerprint']
                        or (state.get('finished') or {}).get('reason') != 'semantic_review_budget'):
                    continue
                rows = list(db.execute('SELECT stage,data FROM studio_job_checkpoints WHERE job_id=? AND stage LIKE ?',
                                       (jid, role.prefix + '%')))
                for row in list(rows):
                    # Validate and carry only the chunks referenced by this
                    # fixed-input role; unrelated model state is never copied.
                    origin.read_checkpoint(row['stage'], db=db)
                    value = json.loads(row['data'])
                    if isinstance(value, dict) and value.get('_chunked_checkpoint') == 1:
                        rows.extend(db.execute('SELECT stage,data FROM studio_job_checkpoints WHERE job_id=? AND stage LIKE ?',
                                               (jid, value['prefix'] + '%')))
                for row in rows:
                    db.execute('INSERT INTO studio_job_checkpoints VALUES (?,?,?)',
                               (execution.job['id'], row['stage'], row['data']))
                event_insight_work._put(db, execution.job['id'], role._stage('dsh_state'), {**state, 'finished': None})
                carried.append(dict(origin_job_id=jid, fingerprint=work['fingerprint'],
                                    draft_sha256=digest(draft), review_only=True))
                break
        event_insight_work._put(db, execution.job['id'], 'update_analysis_reuse_v1', dict(roles=carried))


def prepare(research):
    """Reuse saved source responses; only genuinely new discovered queries send."""
    from project_watch.studio import bootstrap_collection, collection_host, material_handoff
    from project_watch.studio.collection_ledger import STAGE as LEDGER
    from project_watch.studio.collection_ledger import CollectionLedger
    from project_watch.studio.collection_plan import make_plan
    from project_watch.studio.dsh_bootstrap import digest as input_digest
    from project_watch.studio.material_ownership import take_over

    execution = research.execution
    handoff = execution.read_checkpoint(STAGE)
    origin = JobExecution(research.store, {'id': handoff['origin_job_id']})
    if execution.read_checkpoint(collection_host.SCOPE) is None:
        execution.checkpoint_chunked(collection_host.SCOPE, handoff['original_scope'])
    collection_host.guard_scope(research)
    collection_host.sources_for(research)
    material_handoff.prepare(research, handoff['original_scope'])
    if execution.read_checkpoint(LEDGER) is None:
        CollectionLedger(execution, make_plan(round_id=execution.job['id'], project_id=research.project_id,
            binding=input_digest(handoff['original_scope']), mode='incremental', candidates=[], at=now()))
    if execution.read_checkpoint(bootstrap_collection.STAGE) is None:
        actions = (origin.read_checkpoint(bootstrap_collection.STAGE) or {}).get('actions', {})
        execution.checkpoint_chunked(bootstrap_collection.STAGE, dict(
            actions={key: {**value, 'origin_job_id': value.get('origin_job_id', origin.job['id'])} for key, value in actions.items()},
            pending=[], finished=None, phase='prepare'))
    # Provenance-checked transfer reuses a settled proposal, never an unknown.
    for task_id in handoff['pending_task_ids']:
        with closing(research.store._connect()) as db:
            row = db.execute('SELECT job_id,status FROM studio_evidence_tasks WHERE id=?', (task_id,)).fetchone()
        if row and row['status'] == 'pending' and row['job_id'] == origin.job['id']:
            take_over(execution, task_id)

"""One leased external update, from source receipts to reviewed projections.

The coordinator owns ordering, not storage, provider calls or admission rules.
Old started jobs keep their frozen outbox protocol. No automatic migration or
unknown recovery is implied by installing this module.
"""
from contextlib import closing, suppress

from project_watch.studio import event_insight_work
from project_watch.studio.dsh_bootstrap import digest
from project_watch.studio.http import HttpError
from project_watch.studio.store import now

OWNER = 'update_run_owner_v1'
STATE = 'update_run_state_v1'
PHASES = ('sources', 'events', 'analysis', 'reports')


def freeze(execution):
    """Claim only untouched material jobs, before their first external call."""
    with closing(execution.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        execution._guard(db)
        old = execution.read_checkpoint(OWNER, db=db)
        if old:
            return old
        material = execution.read_checkpoint('material_pipeline_v1', db=db) or {}
        request = execution.read_checkpoint('collection_request_v1', db=db) or {}
        # Selected research questions retain their distinct closure contract.
        if material.get('owner') != 'materials' or request.get('mode') == 'topic':
            return None
        allowed = {'material_pipeline_v1', 'collection_request_v1', 'external_first_ready',
                   'external_followup_input', 'public_stage_input_v1',
                   'independent_public_collection_v1', 'independent_public_research_v1'}
        stages = {r[0] for r in db.execute('SELECT stage FROM studio_job_checkpoints WHERE job_id=?',
                                          (execution.job['id'],))}
        if stages - allowed or db.execute('SELECT 1 FROM studio_job_calls WHERE job_id=? LIMIT 1',
                                         (execution.job['id'],)).fetchone():
            return None
        value = dict(protocol=2, entry=material['entry'], created_at=now(), owner_job_id=execution.job['id'])
        event_insight_work._put(db, execution.job['id'], OWNER, value)
        from project_watch.studio import question_work

        event_insight_work._put(db, execution.job['id'], question_work.PROTOCOL, True)
        return value


def guard(pipeline, execution):
    from project_watch.studio.material_ownership import project_guard
    from project_watch.studio.recovery import input_binding

    with closing(execution.store._connect()) as db:
        execution._guard(db)
        project_guard(db, execution.job['project_id'], execution.job['id'])
        project = db.execute('SELECT status FROM studio_projects WHERE project_id=?',
                             (execution.job['project_id'],)).fetchone()
        if not project or project[0] != 'active':
            raise HttpError('project_paused')
        expected = execution.read_checkpoint('recovery_inputs', db=db)
        if expected and expected['binding'] != input_binding(pipeline, execution.job['project_id'], db):
            raise ValueError('update input changed')
    if any(v['unknown'] for v in execution.usage().values()):
        raise HttpError('request_outcome_unknown')


def stage(pipeline, execution, name, inputs, action):
    """Durable input/output boundary; replay never repeats a completed action."""
    guard(pipeline, execution)
    state = execution.read_checkpoint(STATE) or dict(protocol=1, phases={}, status='running')
    signature = digest(inputs)
    saved = state['phases'].get(name)
    if saved and saved['input_fingerprint'] != signature:
        raise ValueError('update stage input changed')
    if saved and saved['status'] in ('complete', 'partial'):
        return saved['output']
    phases = ('sources', 'analysis', 'events', 'reports') if (execution.read_checkpoint(OWNER) or {}).get('protocol') == 2 else PHASES
    index = phases.index(name)
    row = dict(status='running', input_fingerprint=signature, inputs=inputs,
               started_at=(saved or {}).get('started_at', now()),
               next_stage=phases[index + 1] if index + 1 < len(phases) else None)
    state['phases'][name] = row
    state.update(status='running', active_stage=name)
    execution.checkpoint_chunked(STATE, state)
    try:
        output, complete = action()
        guard(pipeline, execution)
    except Exception as exc:
        # Do not stringify provider bodies, silently retry or erase prior output.
        row.update(status='blocked', error=getattr(exc, 'code', type(exc).__name__), stopped_at=now())
        state['status'] = 'blocked'
        # Losing the lease cannot grant permission to write a receipt.
        with suppress(Exception):
            execution.checkpoint_chunked(STATE, state)
        raise
    row.update(status='complete' if complete else 'partial', output=output,
               output_fingerprint=digest(output), finished_at=now())
    execution.checkpoint_chunked(STATE, state)
    return output


def _analyze(pipeline, execution, runtime_root, versions, cap, publication_complete=True):
    from project_watch.studio import event_insight
    from project_watch.studio.recovery import input_binding

    manifest = execution.read_checkpoint(event_insight_work.INPUT)
    if manifest is None:
        with closing(pipeline.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            execution._guard(db)
            pid, jid = execution.job['project_id'], execution.job['id']
            state = execution.read_checkpoint(event_insight_work.ROUND, db=db)
            root = state.get('round_root_job_id', jid)
            budget = event_insight_work.JobExecution(pipeline.store, {'id': root}).read_checkpoint(event_insight_work.BUDGET, db=db)
            if budget and (budget['cap'] != cap or budget['tail_job_id'] != jid):
                raise ValueError('inline analysis round tail changed')
            pipeline.objects.ensure_subject(pid, db=db)
            objects, unresolved = event_insight_work.inputs(pipeline, pid, versions, db=db)
            if not publication_complete:
                unresolved.append(dict(reason='event_publication_pending'))
            manifest = dict(origin_job_id=jid, round_root_job_id=root, predecessor_job_id=None,
                cap=cap, objects=objects, unresolved=unresolved, rebuild=None,
                input_version='update-analysis:' + digest(versions),
                budget_basis='same_leased_update', settings_binding=input_binding(pipeline, pid, db))
            event_insight_work._put(db, jid, event_insight_work.INPUT, manifest)
            event_insight_work._put(db, root, event_insight_work.BUDGET, dict(cap=cap, tail_job_id=jid))
    from project_watch.studio.update_handoff import reuse_analysis

    reuse_analysis(execution, manifest)
    # This is the original total, not a fresh per-stage allowance. Every model
    # call already made by this root remains in budget_used and the cost ledger.
    execution.budgets['model'] = cap
    execution._research_budget_phase = 'analyze'
    try:
        if manifest['objects']:
            event_insight.run(pipeline, execution, runtime_root)
            result = execution.read_checkpoint(event_insight_work.RESULT) or {}
        else:
            result = dict(status='pending' if manifest['unresolved'] else 'complete', objects=[],
                          reason='no_confirmed_object_input' if manifest['unresolved'] else 'no_new_events')
            execution.checkpoint(event_insight_work.RESULT, result)
    finally:
        del execution._research_budget_phase
    complete = result.get('status') == 'complete' and not manifest['unresolved']
    state = execution.read_checkpoint(event_insight_work.ROUND)
    state.update(phase='settled' if complete else 'waiting_inputs', analysis_pending=not complete,
                 unresolved=manifest['unresolved'], analysis_owner=execution.job['id'])
    execution.checkpoint(event_insight_work.ROUND, state)
    return dict(result=result, unresolved=manifest['unresolved'], event_versions=versions), complete


def _reports(pipeline, execution, analysis):
    from project_watch.studio import insight, news_window

    pid = execution.job['project_id']
    saved = insight.load(pipeline.store, pid)
    # The same stored-only date filter used by the public feed/digest. A source
    # retrieved today is not necessarily an event that happened today.
    windows = {}
    for label, days in (('today', 1), ('week', 7)):
        rows = list(news_window.iter_events(pipeline.store, project_id=pid, since_days=days, dated_only=True))
        windows[label] = dict(days=days, event_ids=[r['id'] for r in rows],
            important_ids=[r['id'] for r in rows if r['importance'] in ('major', 'notable')],
            dates={r['id']: r['news_date'] for r in rows})
    return dict(generated_at=now(), windows=windows,
                analysis_status=analysis['result'].get('status'),
                current_analysis_status=saved.get('status') if saved else None,
                event_versions=analysis['event_versions']), True


def run(pipeline, execution, runtime_root):
    from project_watch.studio import (
        bootstrap_collection,
        collection_host,
        collection_round,
        dsh_runtime,
        progressive_bootstrap,
        progressive_monitor,
    )

    owner = execution.read_checkpoint(OWNER)
    if not owner:
        raise ValueError('update owner missing')
    guard(pipeline, execution)
    entry = owner['entry']
    mode = 'first' if entry == 'bootstrap' else (execution.read_checkpoint('collection_request_v1') or {}).get('mode', 'incremental')
    admission = collection_host.admit(execution, mode=mode)
    if not admission['enabled']:
        raise ValueError('update requires collection admission')
    shared = collection_round.prepare(pipeline, execution)
    handoff = execution.read_checkpoint('update_handoff_v1')
    if handoff:
        for kind, maximum in handoff['caps'].items():
            current = execution.budgets.get(kind)
            if maximum is not None:
                execution.budgets[kind] = maximum if current is None else min(current, maximum)
        shared = handoff['cap']
    elif shared is None:
        collection_host.limit_budgets(execution, mode)
    cap = execution.budgets.get('model') if owner.get('protocol') == 2 else (40 if entry == 'bootstrap' else 16)
    frozen = event_insight_work.begin(execution, cap=shared if shared is not None else cap, shared=shared is not None)
    if handoff and handoff.get('resume_phase') == 'analysis':
        # Explicitly carried publication is immutable. No collector, extractor
        # or model generation is started to recover a pending review.
        old = handoff['prior_update']['phases']
        for name in ('sources', 'events'):
            previous = old[name]
            stage(pipeline, execution, name, {'origin': handoff['origin_job_id'],
                  'output_fingerprint': previous['output_fingerprint']},
                  lambda previous=previous: (previous['output'], previous['status'] == 'complete'))
        published = old['events']['output']
        frozen.update(publication_complete=published['publication_complete'],
                      event_versions=published['event_versions'])
        execution.checkpoint(event_insight_work.ROUND, frozen)
        return finish(pipeline, execution, runtime_root, published, frozen)
    # Explicit migration keeps old round checkpoints immutable; its declared
    # descendant inherits every settled call and the original account limits.
    settings, http, _, collectors = pipeline.tools()
    cls = progressive_monitor.MaterialBootstrap if entry == 'bootstrap' else progressive_monitor.MaterialMonitor
    research = cls(pipeline, execution, collectors, pipeline.reader())
    http = dsh_runtime.guarded_http(research, http)
    collectors.http = http
    dsh_runtime.Gateway(research, settings.model, http)
    directory = dsh_runtime.session_directory(research)

    def sources():
        if handoff:
            from project_watch.studio import update_handoff

            update_handoff.prepare(research)
        else:
            collection_host.prepare(research)
        bootstrap_collection.run(research, settings.model, http, runtime_root, directory)
        receipt = execution.read_checkpoint('bootstrap_collection_v1') or {}
        return dict(checkpoint='bootstrap_collection_v1', receipt_fingerprint=digest(receipt),
                    finished=receipt.get('finished')), (receipt.get('finished') or {}).get('status') == 'collected'

    publication_resume = handoff and handoff.get('resume_phase') == 'publication'
    if publication_resume:
        previous = handoff['prior_update']['phases']['sources']
        source = stage(pipeline, execution, 'sources',
            {'origin': handoff['origin_job_id'], 'output_fingerprint': previous['output_fingerprint']},
            lambda: (previous['output'], previous['status'] == 'complete'))
    else:
        source = stage(pipeline, execution, 'sources', dict(entry=entry, mode=mode), sources)

    # Source delivery is independently durable even if event admission or
    # judgment subsequently fails. Never advance a published analysis head here.
    from project_watch.studio import material_snapshot

    snapshot = material_snapshot.capture(execution)
    if owner.get('protocol') == 2:
        from project_watch.studio import snapshot_work
        analysis = stage(pipeline, execution, 'analysis', {'snapshot_id': snapshot['id']},
            lambda: _saved_analysis(pipeline, execution, snapshot, snapshot_work))

    def events():
        if publication_resume:
            from project_watch.studio.update_publication import publish

            result = publish(research)
        else:
            result = progressive_bootstrap.run(research, settings.model, http, runtime_root, directory)
        publication = execution.read_checkpoint(event_insight_work.ROUND) or {}
        versions = {**(handoff or {}).get('original_event_versions', {}), **publication.get('event_versions', {})}
        return dict(event_versions=versions, result=result,
                    publication_complete=bool(publication.get('publication_complete')),
                    closeout=execution.read_checkpoint(progressive_bootstrap.CLOSEOUT)), (
                        publication.get('publication_complete') and result.get('status') != 'partial')

    published = stage(pipeline, execution, 'events', source, events)
    if owner.get('protocol') == 2:
        return finish_saved(pipeline, execution, published, analysis)
    # Material adoption may tighten the original total and move the shared tail.
    # Never use the pre-adoption in-memory allowance for analysis.
    frozen = execution.read_checkpoint(event_insight_work.ROUND)
    return finish(pipeline, execution, runtime_root, published, frozen)


def _saved_analysis(pipeline, execution, snapshot, work):
    from project_watch.studio.llm import ModelError

    if not snapshot['materials']:
        return dict(result={'status': 'partial', 'reason': 'no_permitted_material'}, event_versions={}), False
    try:
        result = work.inline(pipeline, execution, snapshot)
    except ModelError as exc:
        if exc.code not in ('model_output_invalid_shape', 'model_output_invalid_json', 'model_output_truncated'):
            raise
        # A settled malformed draft does not invalidate sources or prevent
        # independently verified events. Unknown/permission/cost errors escape.
        guard(pipeline, execution)
        result = dict(status='partial', reason=exc.code, snapshot_id=snapshot['id'])
    return dict(result=result, event_versions={}), result['status'] == 'completed'


def finish_saved(pipeline, execution, published, analysis):
    """No second event-gated analysis or analysis outbox for new update owners."""
    from project_watch.studio import question_work, scope_maintenance

    analysis = {**analysis, 'event_versions': published['event_versions']}
    stage(pipeline, execution, 'reports', analysis, lambda: _reports(pipeline, execution, analysis))
    budget = execution.read_checkpoint(event_insight_work.ROUND)
    budget.update(phase='settled', analysis_pending=analysis['result']['status'] != 'completed',
                  analysis_owner=execution.job['id'])
    execution.checkpoint(event_insight_work.ROUND, budget)
    question_work.settle(pipeline, execution, published['result'])
    scope_maintenance.dispatch(pipeline, execution.job['project_id'], execution)
    state = execution.read_checkpoint(STATE)
    state.update(status='complete' if all(v['status'] == 'complete' for v in state['phases'].values()) else 'partial',
                 active_stage=None, finished_at=now())
    execution.checkpoint_chunked(STATE, state)
    return {**published['result'], 'update_status': state['status'], 'analysis_work': analysis['result']}


def finish(pipeline, execution, runtime_root, published, frozen):
    from project_watch.studio import scope_maintenance

    analysis = stage(pipeline, execution, 'analysis', dict(event_versions=published['event_versions'], cap=frozen['cap'],
                     publication_complete=published['publication_complete']),
                     lambda: _analyze(pipeline, execution, runtime_root, published['event_versions'], frozen['cap'], published['publication_complete']))
    stage(pipeline, execution, 'reports', analysis, lambda: _reports(pipeline, execution, analysis))
    # Existing scope maintenance remains a separate, explicitly budgeted task.
    # It must never dispatch a second analysis for this coordinator's round.
    scope_maintenance.dispatch(pipeline, execution.job['project_id'], execution)
    state = execution.read_checkpoint(STATE)
    state.update(status='complete' if all(v['status'] == 'complete' for v in state['phases'].values()) else 'partial',
                 active_stage=None, finished_at=now())
    execution.checkpoint_chunked(STATE, state)
    return {**published['result'], 'update_status': state['status'], 'analysis_work': analysis['result']}

"""Frozen event-to-object work on the existing insight queue and round budget."""
import json
from contextlib import closing
from datetime import UTC, datetime

from project_watch.studio import insight_versions
from project_watch.studio.dsh_bootstrap import digest
from project_watch.studio.execution import JobExecution
from project_watch.studio.material_ownership import project_guard
from project_watch.studio.store import now

ROUND = 'event_insight_round_v1'
INPUT = 'event_insight_input_v1'
RESULT = 'event_insight_result_v1'
BUDGET = 'event_insight_budget_tail_v1'
MAX_OBJECTS, MAX_EVENTS, MAX_REFS = 8, 20, 100


def _put(db, job_id, stage, value):
    db.execute('INSERT INTO studio_job_checkpoints VALUES (?,?,?) ON CONFLICT(job_id,stage) DO UPDATE SET data=excluded.data',
               (job_id, stage, json.dumps(value, ensure_ascii=False)))


def begin(execution, *, cap=16, shared=False):
    """Persist intent before publication so a failed writer cannot lose analysis."""
    with closing(execution.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        execution._guard(db)
        project_guard(db, execution.job['project_id'], execution.job['id'])
        saved = execution.read_checkpoint(ROUND, db=db)
        if saved is None:
            if not shared:
                cap = min(cap, execution.budgets.get('model') if execution.budgets.get('model') is not None else cap)
            before = dict(db.execute('SELECT h.event_id,h.revision_id FROM studio_event_revision_heads h '
                'JOIN studio_events e ON e.id=h.event_id WHERE e.project_id=?', (execution.job['project_id'],)))
            saved = dict(cap=cap, collection_cap=max(0, cap - min(6, cap // 2)), before=before,
                         phase='collecting', origin_job_id=execution.job['id'], created_at=now())
            if (execution.read_checkpoint('collection_request_v1', db=db) or {}).get('mode') == 'topic':
                saved['analysis_enabled'] = False
            _put(db, execution.job['id'], ROUND, saved)
    current = execution.budgets.get('model')
    execution.budgets['model'] = saved['collection_cap'] if current is None else min(current, saved['collection_cap'])
    return saved


def inputs(pipeline, project_id, versions, *, db, only_object=None):
    """Exact admitted claim subjects route; missing/ambiguous identities wait."""
    objects = [pipeline.objects._disclose(json.loads(r[0]), False) for r in db.execute(
        "SELECT record FROM studio_monitor_objects WHERE project_id=? AND visibility='public' ORDER BY id", (project_id,))]
    names = {}
    today = datetime.now(UTC).date().isoformat()
    for obj in objects:
        aliases_current = obj['user_override'] or bool(obj['refs'])
        for ref in obj['refs']:
            eid = ref['evidence_version_id']
            aliases_current &= bool(db.execute('SELECT 1 FROM studio_evidence_heads h JOIN studio_evidence_versions e '
                'ON e.id=h.evidence_version_id LEFT JOIN studio_evidence_lifecycle l ON l.evidence_version_id=e.id '
                "AND l.project_id=e.project_id WHERE e.project_id=? AND e.id=? AND COALESCE(l.status,'')!='withdrawn'",
                (project_id, eid)).fetchone())
        aliases = [a['name'] for a in obj['aliases'] if aliases_current
                   and (not a.get('valid_from') or a['valid_from'] <= today)
                   and (not a.get('valid_to') or a['valid_to'] >= today)]
        for name in [obj['name'], *aliases]:
            names.setdefault(name.strip().casefold(), set()).add(obj['id'])
    grouped, unresolved = {}, []
    for event_id, revision in versions.items():
        row = db.execute('SELECT record FROM studio_event_revisions WHERE project_id=? AND event_id=? AND id=?',
                         (project_id, event_id, revision)).fetchone()
        if not row:
            raise ValueError('frozen event revision missing')
        event = json.loads(row[0])
        if event['withdrawn']:
            unresolved.append(dict(event_id=event_id, reason='event_withdrawn'))
            continue
        matched = set()
        for source in event['sources']:
            if source['withdrawn']:
                continue
            for claim in json.loads(source['extra'] or '{}').get('dsh_claims', []):
                targets = names.get(str(claim.get('subject') or '').strip().casefold(), set())
                if len(targets) != 1:
                    unresolved.append(dict(event_id=event_id, subject=claim.get('subject'), reason='object_route_unconfirmed'))
                    continue
                target = next(iter(targets))
                if only_object and target != only_object:
                    continue
                item = grouped.setdefault(target, {'event_versions': {}, 'numbered': {}, 'evidence_ids': []})
                for ref in claim['refs']:
                    evidence = pipeline.evidence.get(project_id, ref['evidence_version_id'], include_internal=False, db=db)
                    part = evidence['parts'][ref['part_index']]['text']
                    if part[ref['start']:ref['end']] != ref['quote']:
                        raise ValueError('event claim evidence changed')
                    identity = digest([event_id, ref['evidence_version_id'], ref['part_index'], ref['start'], ref['end']])
                    item['numbered'].setdefault(identity, dict(kind='public', id=event_id, text=claim['text'],
                        title=source['title'], context=ref['quote'], url=source['url'], source=source['source'],
                        group=event['fields']['category'], date=source.get('published_at') or '',
                        event_at=event['fields'].get('event_at'), read_scope=evidence['read_scope'],
                        source_type=evidence['source_type'], evidence_version_id=evidence['id'], located_ref=ref))
                    item['evidence_ids'].append(evidence['id'])
                item['event_versions'][event_id] = revision
                matched.add(target)
        if not matched and not any(r['event_id'] == event_id for r in unresolved):
            unresolved.append(dict(event_id=event_id, reason='no_explicit_object_claim'))
    result = []
    for obj in objects:
        work = grouped.get(obj['id'])
        if not work:
            continue
        if (len(result) >= MAX_OBJECTS or len(work['event_versions']) > MAX_EVENTS
                or len(work['numbered']) > MAX_REFS or sum(len(v['context']) for v in work['numbered'].values()) > 60000):
            unresolved.append(dict(object_id=obj['id'], reason='bounded_analysis_input_limit'))
            continue
        work.update(object=obj, object_versions={obj['id']: obj['version']},
                    model_binding=pipeline.model_binding(), analysis_policy=insight_versions.policy(pipeline),
                    relationships=insight_versions.relations(pipeline, project_id, obj['id'], db=db),
                    evidence_ids=sorted(set(work['evidence_ids'] + [r['evidence_version_id'] for r in obj['refs']])),
                    numbered={f'N{i + 1}': value for i, value in enumerate(work['numbered'].values())})
        # Other relationship participants are part of the interpretation input.
        for relation in work['relationships']:
            work['object_versions'].update(relation['object_versions'])
            work['evidence_ids'].extend(relation.get('evidence_ids', []))
        work['evidence_ids'] = sorted(set(work['evidence_ids']))
        work['fingerprint'] = digest(work)
        try:
            insight_versions.validate(pipeline, project_id, work, db=db)
        except ValueError as exc:
            unresolved.append(dict(object_id=obj['id'], reason='input_not_current', detail=str(exc)))
            continue
        result.append(work)
    return result, unresolved


def publication(execution, news):
    """Freeze exact admitted claim inputs before the first event projection write."""
    with closing(execution.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        execution._guard(db)
        state = execution.read_checkpoint(ROUND, db=db)
        sources = {url: sorted({ref['evidence_version_id'] for claim in claims for ref in claim['refs']})
                   for url, claims in news.items()}
        if 'publication_sources' in state and state['publication_sources'] != sources:
            raise ValueError('event publication input changed')
        state['publication_sources'] = sources
        _put(db, execution.job['id'], ROUND, state)


def _changed(db, project_id, state):
    # A terminal failed round may be reconstructed much later. Only events
    # carrying its admitted evidence qualify, never arbitrary project changes.
    result = {}
    for row in db.execute('SELECT r.id,r.event_id,r.record FROM studio_event_revision_heads h '
                          'JOIN studio_event_revisions r ON r.id=h.revision_id WHERE r.project_id=?', (project_id,)):
        if state['before'].get(row['event_id']) == row['id']:
            continue
        for source in json.loads(row['record'])['sources']:
            expected = state.get('publication_sources', {}).get(source['url'].split('#')[0], [])
            actual = {ref['evidence_version_id'] for claim in json.loads(source['extra'] or '{}').get('dsh_claims', [])
                      for ref in claim['refs']}
            if actual.intersection(expected):
                result[row['event_id']] = row['id']
    return result


def publication_complete(execution, result):
    """Seal membership only after every event projection has returned."""
    with closing(execution.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        execution._guard(db)
        project_guard(db, execution.job['project_id'], execution.job['id'])
        state = execution.read_checkpoint(ROUND, db=db)
        if 'publication_sources' not in state:
            raise ValueError('event publication intent missing')
        if not state.get('publication_complete'):
            state['event_versions'] = _changed(db, execution.job['project_id'], state)
            state['publication_complete'] = True
            state['publication_result'] = result
            _put(db, execution.job['id'], ROUND, state)


def _enqueue(pipeline, db, project_id, origin, state, *, rebuild=None):
    from project_watch.studio.recovery import input_binding

    ancestor = JobExecution(pipeline.store, {'id': origin}).read_checkpoint(INPUT, db=db) or {}
    if not ancestor:
        ancestor = JobExecution(pipeline.store, {'id': origin}).read_checkpoint('public_stage_input_v1', db=db) or {}
    root = ancestor.get('round_root_job_id', state.get('round_root_job_id', origin))
    budget = JobExecution(pipeline.store, {'id': root}).read_checkpoint(BUDGET, db=db)
    if budget and budget['cap'] != state['cap']:
        raise ValueError('insight round cap changed')
    predecessor = budget['tail_job_id'] if budget else origin
    used = db.execute("""SELECT count(*) FROM studio_job_calls c JOIN (
        SELECT job_id,call_key FROM studio_job_calls WHERE job_id IN (?,?)
        UNION SELECT origin_job_id,call_key FROM studio_job_budget_links WHERE job_id IN (?,?)
        ) b ON b.job_id=c.job_id AND b.call_key=c.call_key WHERE c.kind='model'""",
        (origin, predecessor, origin, predecessor)).fetchone()[0]
    if rebuild is None and used >= state['cap']:
        state['blocked_reason'] = 'round_budget_exhausted'
        return None
    fingerprint = digest([origin, [w['fingerprint'] for w in state['objects']], rebuild])
    version = 'event-insight:' + fingerprint
    job = pipeline.store.enqueue(project_id, 'insight', automatic=True, input_version=version, db=db, parent_job_id=origin)
    if job.get('input_version') != version or job.get('status') not in ('queued', 'running'):
        return None
    existing = db.execute('SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage=?', (job['id'], INPUT)).fetchone()
    if existing:
        return job  # An existing admission retains its exact predecessor and input.
    payload = dict(origin_job_id=origin, cap=state['cap'], objects=state['objects'],
                   unresolved=state.get('unresolved', []),
                   input_version=version, rebuild=rebuild, budget_basis='shared_round_including_origin',
                   round_root_job_id=root, predecessor_job_id=predecessor,
                   settings_binding=(JobExecution(pipeline.store, {'id': root}).read_checkpoint('recovery_inputs', db=db) or {}).get(
                       'binding', input_binding(pipeline, project_id, db)))
    _put(db, job['id'], INPUT, payload)
    # All object rebuilds advance this one transactionally frozen round tail.
    # B cannot spend the same balance after A rebuilt from the same old parent.
    for owner in {origin, predecessor}:
        db.execute('INSERT OR IGNORE INTO studio_job_budget_links SELECT ?,job_id,call_key FROM studio_job_calls WHERE job_id=?',
                   (job['id'], owner))
        db.execute('INSERT OR IGNORE INTO studio_job_budget_links SELECT ?,origin_job_id,call_key FROM studio_job_budget_links WHERE job_id=?',
                   (job['id'], owner))
    _put(db, root, BUDGET, dict(cap=state['cap'], tail_job_id=job['id']))
    return job


def admit_manual(pipeline, execution):
    """A user-requested new analysis is a distinct bounded round, never recovery."""
    from project_watch.studio.recovery import input_binding

    with closing(pipeline.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        execution._guard(db)
        project_guard(db, execution.job['project_id'], execution.job['id'])
        if execution.read_checkpoint(INPUT, db=db):
            return True
        stages = {r[0] for r in db.execute('SELECT stage FROM studio_job_checkpoints WHERE job_id=?', (execution.job['id'],))}
        if stages or db.execute('SELECT 1 FROM studio_job_calls WHERE job_id=?', (execution.job['id'],)).fetchone():
            return False
        pid = execution.job['project_id']
        pipeline.objects.ensure_subject(pid, db=db)
        versions = dict(db.execute('SELECT r.event_id,r.id FROM studio_event_revision_heads h '
                                   'JOIN studio_event_revisions r ON r.id=h.revision_id WHERE r.project_id=?', (pid,)))
        work, unresolved = inputs(pipeline, pid, versions, db=db)
        cap = min(16, execution.budgets['model'] if execution.budgets['model'] is not None else 16)
        _put(db, execution.job['id'], INPUT, dict(origin_job_id=execution.job['id'], round_root_job_id=execution.job['id'],
            predecessor_job_id=None, cap=cap, objects=work, unresolved=unresolved, rebuild=None,
            input_version=execution.job['input_version'], budget_basis='explicit_new_analysis_round',
            settings_binding=input_binding(pipeline, pid, db)))
        _put(db, execution.job['id'], BUDGET, dict(cap=cap, tail_job_id=execution.job['id']))
        return True


def _wake_binding(pipeline, pid, state, db):
    """Read a dependency signature; never run a model just to check readiness."""
    return digest([
        [tuple(r) for r in db.execute('SELECT id,version FROM studio_monitor_objects WHERE project_id=? ORDER BY id', (pid,))],
        [tuple(r) for r in db.execute('SELECT h.event_id,h.revision_id FROM studio_event_revision_heads h JOIN studio_events e ON e.id=h.event_id WHERE e.project_id=? ORDER BY h.event_id', (pid,))],
        [tuple(r) for r in db.execute('SELECT source_key,evidence_version_id FROM studio_evidence_heads WHERE project_id=? ORDER BY source_key', (pid,))],
        [tuple(r) for r in db.execute('SELECT evidence_version_id,status FROM studio_evidence_lifecycle WHERE project_id=? ORDER BY evidence_version_id', (pid,))],
        [tuple(r) for r in db.execute('SELECT scope_key,version FROM studio_relationship_candidates WHERE project_id=? ORDER BY scope_key', (pid,))],
        [tuple(r) for r in db.execute('SELECT scope_key,version FROM studio_relationship_overrides WHERE project_id=? ORDER BY scope_key', (pid,))],
    ])


def _route(pipeline, pid, state, db):
    from project_watch.studio.event_revisions import current_membership_events

    if 'event_versions' not in state:
        state['event_versions'] = _changed(db, pid, state)
    versions = {}
    for eid in current_membership_events(db, pid, state['event_versions']):
        row = db.execute('SELECT h.revision_id FROM studio_event_revision_heads h JOIN studio_events e ON e.id=h.event_id WHERE e.project_id=? AND h.event_id=?', (pid, eid)).fetchone()
        if row:
            versions[eid] = row[0]
    try:
        work, unresolved = inputs(pipeline, pid, versions, db=db) if state.get('analysis_enabled', True) else ([], [])
    except ValueError as exc:
        work, unresolved = [], [dict(reason='input_not_current', detail=str(exc))]
    attempted = set(state.get('attempted_fingerprints', []))
    state.update(objects=[w for w in work if w['fingerprint'] not in attempted], unresolved=unresolved,
                 wake_binding=_wake_binding(pipeline, pid, state, db))
    state['phase'] = ('awaiting_queue' if state['objects'] else
                      'waiting_inputs' if unresolved or state.get('analysis_pending') else 'settled')


def _advance(pipeline, pid, origin, state, db):
    if state['phase'] == 'collecting':
        if not state.get('publication_complete'):
            return None  # Intent or partially written events are not publication completion.
        pipeline.objects.ensure_subject(pid, db=db)
        _route(pipeline, pid, state, db)
    if state['phase'] == 'scheduled':
        child = db.execute('SELECT status FROM studio_jobs WHERE id=? AND project_id=?', (state['insight_job_id'], pid)).fetchone()
        if not child or child[0] in ('queued', 'running'):
            return None
        result = JobExecution(pipeline.store, {'id': state['insight_job_id']}).read_checkpoint(RESULT, db=db) or {}
        state['analysis_pending'] = child[0] == 'failed' or result.get('status') != 'complete'
        state['phase'] = 'waiting_inputs' if state['analysis_pending'] or state.get('unresolved') else 'settled'
    if state['phase'] == 'waiting_inputs' and state.get('wake_binding') != _wake_binding(pipeline, pid, state, db):
        _route(pipeline, pid, state, db)
    if state['phase'] == 'awaiting_queue':
        job = _enqueue(pipeline, db, pid, origin, state)
        if job:
            state.update(phase='scheduled', insight_job_id=job['id'],
                         attempted_fingerprints=sorted(set(state.get('attempted_fingerprints', [])) |
                                                       {w['fingerprint'] for w in state['objects']}))
            return job
        if state.get('blocked_reason'):
            state.update(phase='waiting_inputs', analysis_pending=True)
    return None


def settle(pipeline, execution):
    with closing(pipeline.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        execution._guard(db)
        project_guard(db, execution.job['project_id'], execution.job['id'])
        state = execution.read_checkpoint(ROUND, db=db)
        _advance(pipeline, execution.job['project_id'], execution.job['id'], state, db)
        _put(db, execution.job['id'], ROUND, state)
        return state


def dispatch_pending(pipeline, project_id):
    """Reconstruct terminal publication intents without replaying collection."""
    with closing(pipeline.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        project_guard(db, project_id, '')
        rounds = db.execute("SELECT j.*,c.data FROM studio_job_checkpoints c JOIN studio_jobs j ON j.id=c.job_id "
            "WHERE j.project_id=? AND c.stage=? AND j.status IN ('done','failed') ORDER BY j.created_at", (project_id, ROUND)).fetchall()
        for row in rounds:
            state = json.loads(row['data'])
            # Upgrade the old false-settled marker without inventing input or calls.
            if state['phase'] == 'settled' and state.get('unresolved'):
                state['phase'] = 'waiting_inputs'
            job = _advance(pipeline, project_id, row['id'], state, db)
            _put(db, row['id'], ROUND, state)
            if job:
                return job
    return None


def rebuild(pipeline, project_id, request):
    """One coalesced rebuild inherits the last analysis's remaining round cap."""
    object_id = request['artifact_key'].removeprefix('insight:')
    with closing(pipeline.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        project_guard(db, project_id, '')
        old = insight_versions.current(pipeline.store, project_id, object_id, db=db)
        if not old:
            return None
        versions = {}
        from project_watch.studio.event_revisions import current_membership_events

        for event_id in current_membership_events(db, project_id, old['input']['event_versions']):
            row = db.execute('SELECT revision_id FROM studio_event_revision_heads WHERE event_id=?', (event_id,)).fetchone()
            if row:
                versions[event_id] = row[0]
        work, _unresolved = inputs(pipeline, project_id, versions, db=db, only_object=object_id)
        if not work:
            return None
        origin = JobExecution(pipeline.store, {'id': old['job_id']}).read_checkpoint(INPUT, db=db)
        return _enqueue(pipeline, db, project_id, old['job_id'], dict(objects=work, cap=origin['cap']),
                        rebuild=dict(artifact_key=request['artifact_key'], generation=request['generation']))

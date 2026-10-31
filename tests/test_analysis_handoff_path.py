"""Whole entry/queue boundaries with isolated stores and scripted providers."""
import json
from contextlib import closing
from types import SimpleNamespace

import pytest
from test_bootstrap_collection import BODY, LATE
from test_event_insight import install, next_insight, revise_event, source

from project_watch.studio import collection_round, public_stage_work
from project_watch.studio import event_insight_work as work
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.monitoring_contracts import ObjectInput


def unresolved(tmp_path, monkeypatch):
    pipeline, execution, seen = install(tmp_path, monkeypatch)
    original = pipeline.http_factory(None).transport
    def send(method, url, headers, raw, timeout):
        result = original(method, url, headers, raw, timeout)
        body = json.loads(raw) if raw and 'model.example.invalid' in url else {}
        if {t['function']['name'] for t in body.get('tools', [])} == {'pw_submit_records'}:
            reply = result.json()
            fn = reply['choices'][0]['message']['tool_calls'][0]['function']
            payload = json.loads(fn['arguments'])
            for row in payload['observations']:
                row['subject'] = 'Unconfirmed Host'
            fn['arguments'] = json.dumps(payload)
            result = Response(200, {}, json.dumps(reply).encode())
        return result
    pipeline.http_factory = lambda _: Http(send)
    return pipeline, execution, seen


def finish(pipeline, execution):
    assert pipeline.store.finish(execution.job['id'], lease_token=execution.job['lease_token'])


def confirm(pipeline, pid):
    return pipeline.objects.save(pid, ObjectInput(kind='company', name='Unconfirmed Host',
        purpose='related', visibility='public'), actor='user')


def test_first_publication_and_followup_share_root_and_final_predecessor_usage(tmp_path, monkeypatch):
    pipeline, first, seen = install(tmp_path, monkeypatch, monitor=False)
    monkeypatch.setenv('PROJECT_WATCH_DSH_MONITOR', '1')
    state = source(pipeline, first)
    assert state['phase'] == 'scheduled' and state['cap'] == 40 and state['event_versions']
    child = next_insight(pipeline, first)
    pipeline.run(child.job)
    assert child.read_checkpoint(work.RESULT)['status'] == 'complete'
    finish(pipeline, child)
    with closing(pipeline.store._connect()) as db:
        jobs = [dict(r) for r in db.execute('SELECT id,kind,status,input_version,error FROM studio_jobs')]
    follow = JobExecution(pipeline.store, pipeline.store.claim())
    assert follow.job, jobs
    assert follow.job['kind'] == 'external_followup'
    manifest = follow.read_checkpoint(public_stage_work.INPUT)
    assert manifest['round_root_job_id'] == first.job['id'] and manifest['predecessor_job_id'] == child.job['id']
    before = child.budget_used('model')
    # The bootstrap fixture asserts reading precedes its first extraction.
    # A real later stage can read again after publication; keep its synthetic
    # body identical without applying that first-stage-only assertion.
    monkeypatch.setattr(pipeline, 'reader', lambda: SimpleNamespace(read=lambda _url:
        {'status': 'article_body', 'text': BODY + '\n' + 'a' * 20500 + '\n' + LATE}))
    original = pipeline.http_factory(None).transport
    def followup_send(method, url, headers, raw, timeout):
        body = json.loads(raw) if raw and 'model.example.invalid' in url else {}
        if any(t['function']['name'] == 'pw_collect' for t in body.get('tools', [])):
            # A normal collection turn after the first publication, journalled
            # through the actual gateway. No new scope in this synthetic turn.
            seen['models'].append(body)
            message = {'role': 'assistant', 'tool_calls': [{'id': 'follow-finish', 'type': 'function',
                'function': {'name': 'pw_finish', 'arguments': json.dumps({'gaps': {'company': 'No new synthetic scope'}})}}]}
            return Response(200, {}, json.dumps({'choices': [{'message': message, 'finish_reason': 'tool_calls'}]}).encode())
        return original(method, url, headers, raw, timeout)
    pipeline.http_factory = lambda _: Http(followup_send)
    pipeline.run(follow.job)
    assert follow.read_checkpoint('material_pipeline_v1')['owner'] == 'materials'
    assert follow.read_checkpoint(work.ROUND)['cap'] == 40
    assert not follow.read_checkpoint(work.ROUND)['event_versions']  # Duplicate source, no new event.
    assert follow.budget_used('model') >= before
    own = follow.usage().get('model', {}).get('requests', 0)
    assert follow.budget_used('model') == before + own <= 40
    count = len(seen['models']), len(seen['insight'])
    pipeline.run(follow.job)
    assert count == (len(seen['models']), len(seen['insight']))
    assert first.read_checkpoint(work.ROUND)['insight_job_id'] == child.job['id']


def test_unresolved_waits_and_object_confirmation_wakes_once_without_new_events(tmp_path, monkeypatch):
    pipeline, execution, seen = unresolved(tmp_path, monkeypatch)
    pid = execution.job['project_id']
    state = source(pipeline, execution)
    assert state['phase'] == 'waiting_inputs' and state['unresolved'] and not state['objects']
    finish(pipeline, execution)
    count = len(seen['models']), len(seen['insight'])
    for _ in range(3):
        assert pipeline.schedule_insight(pid) is None
    assert count == (len(seen['models']), len(seen['insight']))
    confirm(pipeline, pid)
    queued = pipeline.schedule_insight(pid)
    assert queued and pipeline.schedule_insight(pid) is None
    child = JobExecution(pipeline.store, pipeline.store.claim())
    assert child.job['id'] == queued['id']
    manifest = child.read_checkpoint(work.INPUT)
    assert manifest['round_root_job_id'] == execution.job['id']
    assert manifest['objects'][0]['event_versions'] == state['event_versions']
    pipeline.run(child.job)
    assert child.read_checkpoint(work.RESULT)['status'] == 'complete'
    finish(pipeline, child)
    count = len(seen['models']), len(seen['insight'])
    for _ in range(3):
        assert pipeline.schedule_insight(pid) is None
    assert count == (len(seen['models']), len(seen['insight']))
    assert execution.read_checkpoint(work.ROUND)['phase'] == 'settled'


def test_exhausted_unresolved_round_keeps_reason_without_new_job_or_started(tmp_path, monkeypatch):
    pipeline, execution, seen = unresolved(tmp_path, monkeypatch)
    pid = execution.job['project_id']
    state = source(pipeline, execution)
    # Consume the frozen round through its real journal with synthetic replies.
    # The collection gate remains lower; consume the analysis tail as a bound
    # terminal child, exactly as actual analysis would account for it.
    finish(pipeline, execution)
    with closing(pipeline.store._connect()) as db, db:
        public_stage_work.enqueue(pipeline, db, pid, 'scope', {'synthetic': True},
                                  origin=execution.job['id'], cap=state['cap'])
    tail = JobExecution(pipeline.store, pipeline.store.claim(), budgets={'model': state['cap']})
    for index in range(tail.budget_used('model'), state['cap']):
        tail.call('model', 'synthetic-consumed-' + str(index), lambda: Response(200, {}, b'{}'), provider='synthetic')
    finish(pipeline, tail)
    confirm(pipeline, pid)
    count = len(seen['models']), len(seen['insight'])
    for _ in range(3):
        assert pipeline.schedule_insight(pid) is None
    assert execution.read_checkpoint(work.ROUND)['blocked_reason'] == 'round_budget_exhausted'
    assert count == (len(seen['models']), len(seen['insight']))
    with closing(pipeline.store._connect()) as db:
        assert db.execute("SELECT count(*) FROM studio_jobs WHERE kind='insight'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM studio_job_calls WHERE status='started'").fetchone()[0] == 0


def test_pending_analysis_never_disappears_or_retries_on_unchanged_ticks(tmp_path, monkeypatch):
    pipeline, execution, _seen = install(tmp_path, monkeypatch)
    source(pipeline, execution)
    child = next_insight(pipeline, execution)
    child.checkpoint(work.RESULT, {'status': 'pending', 'objects': [], 'reason': 'synthetic_dependency'})
    finish(pipeline, child)
    for _ in range(3):
        assert pipeline.schedule_insight(execution.job['project_id']) is None
    state = execution.read_checkpoint(work.ROUND)
    assert state['phase'] == 'waiting_inputs' and state['analysis_pending']
    with closing(pipeline.store._connect()) as db:
        assert db.execute("SELECT count(*) FROM studio_jobs WHERE kind='insight'").fetchone()[0] == 1


def test_prepublication_failure_keeps_intent_until_same_job_resumes(tmp_path, monkeypatch):
    pipeline, execution, seen = install(tmp_path, monkeypatch, monitor=False)
    from project_watch.studio import progressive_bootstrap

    progressive_bootstrap.freeze(execution, 'bootstrap', materials=True)
    publication = work.publication
    def interrupted(*_args):
        raise RuntimeError('synthetic pre-publication interruption')
    monkeypatch.setattr(work, 'publication', interrupted)
    with pytest.raises(RuntimeError, match='pre-publication interruption'):
        pipeline.run(execution.job)
    pipeline.store.finish(execution.job['id'], error='synthetic interruption', lease_token=execution.job['lease_token'])
    assert work.dispatch_pending(pipeline, execution.job['project_id']) is None
    assert execution.read_checkpoint(work.ROUND)['phase'] == 'collecting'
    assert 'event_versions' not in execution.read_checkpoint(work.ROUND)
    before = len(seen['models'])
    monkeypatch.setattr(work, 'publication', publication)
    # Synthetic recovery of the same job, preserving all existing call keys.
    with closing(pipeline.store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET status='queued' WHERE id=?", (execution.job['id'],))
    resumed = JobExecution(pipeline.store, pipeline.store.claim())
    assert resumed.job['id'] == execution.job['id']
    pipeline.run(resumed.job)
    state = resumed.read_checkpoint(work.ROUND)
    assert state['phase'] == 'scheduled' and state['event_versions']
    assert len(seen['models']) == before


@pytest.mark.parametrize('window', ['after_intent', 'partial_events', 'after_complete'])
@pytest.mark.parametrize('inline', [False, True])
def test_publication_crash_windows_resume_full_membership_once(tmp_path, monkeypatch, window, inline):
    pipeline, execution, seen = install(tmp_path, monkeypatch, monitor=False, source_count=2)
    from project_watch.studio import progressive_bootstrap

    if not inline:
        progressive_bootstrap.freeze(execution, 'bootstrap', materials=True)
    pid = execution.job['project_id']
    transport = pipeline.http_factory(None).transport
    extracted = []

    def send(method, url, headers, raw, timeout):
        response = transport(method, url, headers, raw, timeout)
        body = json.loads(raw) if raw and 'model.example.invalid' in url else {}
        if {t['function']['name'] for t in body.get('tools', [])} == {'pw_submit_records'}:
            reply = response.json()
            fn = reply['choices'][0]['message']['tool_calls'][0]['function']
            payload = json.loads(fn['arguments'])
            extracted.append(payload)
            for record in payload['observations']:
                record['kind'] = 'company' if len(extracted) == 1 else 'industry'
            fn['arguments'] = json.dumps(payload)
            return Response(200, {}, json.dumps(reply).encode())
        return response

    pipeline.http_factory = lambda _: Http(send)
    target, name = ((pipeline.store, 'create_event') if window == 'partial_events' else
                    (work, 'publication_complete' if window == 'after_complete' else 'publication'))
    original = getattr(target, name)

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('synthetic publication window')

    monkeypatch.setattr(target, name, interrupted)
    with pytest.raises(RuntimeError, match='publication window'):
        pipeline.run(execution.job)
    pipeline.store.finish(execution.job['id'], error='synthetic interruption', lease_token=execution.job['lease_token'])
    with closing(pipeline.store._connect()) as db:
        count = db.execute('SELECT count(*) FROM studio_events WHERE project_id=?', (pid,)).fetchone()[0]
    assert count == {'after_intent': 0, 'partial_events': 1, 'after_complete': 2}[window]
    queued = work.dispatch_pending(pipeline, pid)
    state = execution.read_checkpoint(work.ROUND)
    if window == 'after_complete' and not inline:
        assert queued and len(state['event_versions']) == 2
    elif window == 'after_complete':
        assert queued is None and len(state['event_versions']) == 2
    else:
        assert queued is None and state['phase'] == 'collecting' and 'event_versions' not in state
    monkeypatch.setattr(target, name, original)
    # Unrelated events arriving during the gap never become this round's work.
    unrelated = pipeline.store.create_event(pid, dict(title='Unrelated later event', category='company',
        importance='routine'), [])
    before = len(seen['models'])
    monkeypatch.setattr(target, name, original)
    with closing(pipeline.store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET status='queued' WHERE id=?", (execution.job['id'],))
    resumed = JobExecution(pipeline.store, pipeline.store.claim())
    assert resumed.job['id'] == execution.job['id']
    pipeline.run(resumed.job)
    state = resumed.read_checkpoint(work.ROUND)
    assert state['publication_complete'] and len(state['event_versions']) == 2
    assert unrelated not in state['event_versions'] and state['phase'] == ('settled' if inline else 'scheduled')
    assert state['cap'] == (None if inline else 40)
    assert len(extracted) == 2  # Restoring publication never repeats extraction.
    if inline:
        # New owners analyzed the saved sources before this publication crash.
        # Restoring events must not regenerate or re-review those sections.
        from project_watch.studio import material_snapshot, snapshot_work, update_run
        assert len(seen['models']) == before
        assert resumed.read_checkpoint(update_run.OWNER)['protocol'] == 2
        published = resumed.read_checkpoint(snapshot_work.PUBLICATION)
        assert published['status'] == 'completed' and len(published['published']) == 5
        snapshot = material_snapshot.read(pipeline.store, pid, published['snapshot_id'])
        assert snapshot['materials']
        assert list(resumed.read_checkpoint(update_run.STATE)['phases']) == ['sources', 'analysis', 'events', 'reports']
        assert not resumed.read_checkpoint(work.INPUT)
        assert not resumed.read_checkpoint(work.RESULT)
    else:
        assert len(seen['models']) == before
    child = resumed if inline else next_insight(pipeline, resumed)
    if not inline:
        manifest = child.read_checkpoint(work.INPUT)
        assert set(manifest['objects'][0]['event_versions']) == set(state['event_versions'])
        for ref in manifest['objects'][0]['numbered'].values():
            located = ref['located_ref']
            evidence = pipeline.evidence.get(pid, located['evidence_version_id'], include_internal=False)
            assert evidence['parts'][located['part_index']]['text'][located['start']:located['end']] == located['quote']
        pipeline.run(child.job)
        assert child.read_checkpoint(work.RESULT)['status'] == 'complete'
        assert child.budget_used('model') <= state['cap']
    finish(pipeline, child)
    assert work.dispatch_pending(pipeline, pid) is None
    with closing(pipeline.store._connect()) as db:
        assert db.execute("SELECT count(*) FROM studio_jobs WHERE kind='insight'").fetchone()[0] == (0 if inline else 1)


def test_followup_preserves_lower_explicit_limits_and_original_root_cap(tmp_path, monkeypatch):
    pipeline, first, _seen = install(tmp_path, monkeypatch, monitor=False)
    monkeypatch.setenv('PROJECT_WATCH_DSH_MONITOR', '1')
    source(pipeline, first)
    child = next_insight(pipeline, first)
    pipeline.run(child.job)
    finish(pipeline, child)
    job = pipeline.store.claim()
    assert job['kind'] == 'external_followup'
    lower = {'model': 0, 'search': 0, 'page': 0, 'open_data': 0, 'site': 0}
    execution = JobExecution(pipeline.store, job, budgets=lower)
    cap = collection_round.prepare(pipeline, execution)
    assert cap == 40
    assert all(execution.budgets[k] == v for k, v in lower.items())
    state = work.begin(execution, cap=cap, shared=True)
    assert state['cap'] == 40 and execution.budgets['model'] == 0
    called = []
    for kind in lower:
        with pytest.raises(HttpError, match='request_budget'):
            execution.call(kind, 'forbidden-' + kind, lambda kind=kind: called.append(kind), provider='synthetic')
    assert not called
    with closing(pipeline.store._connect()) as db:
        assert db.execute('SELECT count(*) FROM studio_job_calls WHERE job_id=?', (job['id'],)).fetchone()[0] == 0


@pytest.mark.parametrize('change', ['object', 'event'])
def test_changed_dependency_wakes_pending_once_and_inherits_previous_tail(tmp_path, monkeypatch, change):
    pipeline, execution, _seen = install(tmp_path, monkeypatch)
    pid = execution.job['project_id']
    state = source(pipeline, execution)
    child = next_insight(pipeline, execution)
    child.checkpoint(work.RESULT, {'status': 'pending', 'objects': [], 'reason': 'synthetic_dependency'})
    child.call('model', 'synthetic-paid-before-pending', lambda: Response(200, {}, b'{}'), provider='synthetic')
    finish(pipeline, child)
    assert pipeline.schedule_insight(pid) is None
    if change == 'event':
        revise_event(pipeline, pid)
    else:
        obj = state['objects'][0]['object']
        pipeline.objects.save(pid, ObjectInput(kind='company', name=obj['name'], purpose='subject',
            visibility='public', website='https://changed.example.invalid'), actor='user',
            object_id=obj['id'], expected_version=obj['version'])
    queued = pipeline.schedule_insight(pid)
    assert queued and pipeline.schedule_insight(pid) is None
    fresh = JobExecution(pipeline.store, pipeline.store.claim())
    manifest = fresh.read_checkpoint(work.INPUT)
    assert manifest['round_root_job_id'] == execution.job['id']
    assert manifest['predecessor_job_id'] == child.job['id']
    assert manifest['objects'][0]['fingerprint'] != state['objects'][0]['fingerprint']
    assert fresh.budget_used('model') == child.budget_used('model')
    assert JobExecution(pipeline.store, queued).read_checkpoint(work.INPUT) == manifest


@pytest.mark.parametrize('boundary', ['paused', 'unknown'])
def test_wakeup_preserves_project_protections(tmp_path, monkeypatch, boundary):
    pipeline, execution, seen = unresolved(tmp_path, monkeypatch)
    pid = execution.job['project_id']
    source(pipeline, execution)
    finish(pipeline, execution)
    confirm(pipeline, pid)
    with closing(pipeline.store._connect()) as db, db:
        if boundary == 'paused':
            db.execute("UPDATE studio_projects SET status='paused' WHERE project_id=?", (pid,))
        else:
            db.execute("UPDATE studio_job_calls SET status='unknown' WHERE job_id=? AND kind='model'", (execution.job['id'],))
    count = len(seen['models']), len(seen['insight'])
    assert pipeline.schedule_insight(pid) is None
    assert count == (len(seen['models']), len(seen['insight']))

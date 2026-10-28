"""Completed history is immutable; owner grants bind new checkpoint continuations."""
import json
from contextlib import closing

import pytest
import test_studio_dsh_monitor as fixtures
import test_studio_dsh_recovery as helpers

from project_watch.studio import recovery
from project_watch.studio.dsh_monitor import PublicMonitor
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError, Response


@pytest.fixture
def terminal(tmp_path, monkeypatch):
    monkeypatch.setenv('PROJECT_WATCH_DSH_RUNTIME', 'synthetic-runtime')
    monkeypatch.setenv('PROJECT_WATCH_DSH_MONITOR', '1')
    loop = fixtures.loop.__wrapped__(tmp_path)
    loop.pipeline.settings_store.update({'model': {'base_url':'https://model.example/v1',
        'model':'synthetic', 'api_key':'synthetic'}})
    loop.execution.checkpoint('recovery_inputs', {'binding': recovery.input_binding(loop.pipeline, loop.project_id)})
    loop.execution.call('model', 'bad', lambda: Response(500, {}, b'{}'))
    loop.execution.call('model', 'unrelated-later-success', helpers.ok)
    loop.execution.checkpoint('dispatch_completed', {'completed':True})
    loop.store.finish(loop.execution.job['id'], lease_token=loop.execution.job['lease_token'])
    # Reproduce old release history, not a new successful finish.
    with loop.store._connect() as db, db:
        db.execute("UPDATE studio_jobs SET status='done',error='',retry_after=NULL WHERE id=?", (loop.execution.job['id'],))
    return loop


def accept(loop, key='synthetic-once'):
    p = recovery.preview(loop.pipeline, loop.project_id, loop.execution.job['id'])
    assert p['can_recover'], p
    return recovery.authorize(loop.pipeline, loop.project_id, loop.execution.job['id'],
        expected_version=p['version'], idempotency_key=key, accept_duplicate_cost=True)


def test_terminal_recovery_keeps_history_and_links_new_request(terminal):
    loop = terminal
    with closing(loop.store._connect()) as db:
        original = dict(db.execute('SELECT * FROM studio_jobs WHERE id=?', (loop.execution.job['id'],)).fetchone())
    state = recovery.preview(loop.pipeline, loop.project_id, original['id'])
    assert state['mode'] == 'checkpoint_continuation'
    result = accept(loop)
    assert result['job_id'] != original['id']
    assert recovery.authorize(loop.pipeline, loop.project_id, original['id'], expected_version=state['version'],
        idempotency_key='synthetic-once', accept_duplicate_cost=True) == result
    assert not recovery.preview(loop.pipeline, loop.project_id, original['id'])['can_recover']
    execution = JobExecution(loop.store, loop.store.claim())
    assert execution.read_checkpoint('dispatch_completed') is None
    restored = PublicMonitor(loop.pipeline, execution, loop.collectors, loop.reader)
    calls = []
    def send(*args):
        calls.append(1)
        return helpers.ok()
    helpers.gateway(restored, send).model_call(helpers.request())
    helpers.gateway(restored, send).model_call(helpers.request())
    restored.guard()
    with closing(loop.store._connect()) as db:
        assert dict(db.execute('SELECT * FROM studio_jobs WHERE id=?', (original['id'],)).fetchone()) == original
        assert db.execute('SELECT count(*) FROM studio_job_calls WHERE job_id=?', (original['id'],)).fetchone()[0] == 2
        assert db.execute('SELECT consumed_at FROM studio_call_recoveries WHERE job_id=?', (original['id'],)).fetchone()[0]
        link = json.loads(db.execute("SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage LIKE 'dsh_continuation:%'",
            (execution.job['id'],)).fetchone()[0])
        assert link['origin_job_id'] == original['id']
    assert calls == [1]


@pytest.mark.parametrize('failure', ['unknown', '500'])
def test_continuation_failure_never_inherits_previous_authorization(terminal, failure):
    loop = terminal
    accept(loop)
    execution = JobExecution(loop.store, loop.store.claim())
    restored = PublicMonitor(loop.pipeline, execution, loop.collectors, loop.reader)
    calls = []
    def send(*args):
        calls.append(1)
        if failure == 'unknown':
            raise HttpError('connection_failed')
        return Response(500, {}, b'{}')
    gateway = helpers.gateway(restored, send)
    for _ in range(2):
        with pytest.raises(HttpError):
            gateway.model_call(helpers.request())
    assert calls == [1]
    assert recovery.dsh_model_continuation(loop.pipeline, execution) is None


@pytest.mark.parametrize('boundary', ['paused', 'configuration', 'other_unknown', 'no_dsh'])
def test_terminal_preview_respects_boundaries(terminal, monkeypatch, boundary):
    loop = terminal
    if boundary == 'paused':
        loop.store.set_status(loop.project_id, 'paused')
    elif boundary == 'configuration':
        loop.pipeline.settings_store.update({'model':{'model':'changed'}})
    elif boundary == 'no_dsh':
        monkeypatch.delenv('PROJECT_WATCH_DSH_RUNTIME')
    else:
        loop.store.enqueue(loop.project_id, 'details')
        ex = JobExecution(loop.store, loop.store.claim())
        with pytest.raises(HttpError):
            ex.call('search', 'other', helpers.lost)
        loop.store.finish(ex.job['id'], error='unknown', lease_token=ex.job['lease_token'])
    assert not recovery.preview(loop.pipeline, loop.project_id, loop.execution.job['id'])['can_recover']


def test_project_recovery_index_finds_completed_blocker_not_latest_failure(terminal):
    loop = terminal
    loop.store.enqueue(loop.project_id, 'details')
    job = loop.store.claim()
    loop.store.finish(job['id'], error='blocked by previous request', lease_token=job['lease_token'])
    rows = recovery.project_preview(loop.pipeline, loop.project_id)['items']
    assert [r['job_id'] for r in rows] == [loop.execution.job['id']]
    assert rows[0]['job_status'] == 'done' and rows[0]['requests'] == {'model':1}
    from project_watch.studio.taskboard import board
    shown = next(j for j in board(loop.store, worker_state='running', model_ready=True)['jobs']
                 if j['id'] == loop.execution.job['id'])
    assert shown['status'] == 'done' and shown['runtime']['state'] == 'needs_confirmation'
    assert shown['advice']['action'] == 'recover' and 'HTTP 5xx' in shown['advice']['why']


def test_new_finish_cannot_report_done_with_received_model_failure(terminal):
    loop = terminal
    loop.store.enqueue(loop.project_id, 'details')
    execution = JobExecution(loop.store, loop.store.claim())
    execution.call('model', 'new-500', lambda: Response(500, {}, b'{}'))
    loop.store.finish(execution.job['id'], lease_token=execution.job['lease_token'])
    job = next(j for j in loop.store.jobs(loop.project_id) if j['id'] == execution.job['id'])
    assert job['status'] == 'failed' and job['retry_after'] == '9999-12-31'
    assert 'model_http_5xx' in job['error']

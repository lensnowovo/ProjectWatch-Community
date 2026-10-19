"""A received transient failure can be retried explicitly without replaying paid successes."""
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from test_studio_unknown_recovery import authorize, endpoint

from project_watch.studio.app import create_studio_app
from project_watch.studio.http import HttpError, Response
from project_watch.studio.llm import ModelError


def failed_round(tmp_path, status=500):
    app = create_studio_app(tmp_path, start_worker=False)
    p = app.state.studio_pipeline
    p.settings_store.update({'model': {'base_url': 'https://synthetic.invalid/v1', 'model': 'synthetic', 'api_key': 'synthetic'}})
    pid = p.repository.create_project('Synthetic Received Failure').id
    p.store.upsert_project(pid, {}, consent=True)
    sends = []
    outcome = [status]

    def send(key):
        sends.append(key)
        if key == 'first':
            return Response(200, {}, b'{}')
        if outcome[0] == 'unknown':
            raise HttpError('connection_failed')
        return Response(outcome[0], {}, b'{"error":"synthetic"}')

    def dispatch(job):
        execution = p._execution_state.current
        execution.call('model', 'first', lambda: send('first'))
        response = execution.call('model', 'second', lambda: send('second'))
        if response.status != 200:
            raise ModelError('synthetic service failure', status=response.status)

    p.run_internal = dispatch
    p.store.enqueue(pid, 'internal')
    job = p.store.claim()
    with pytest.raises(ModelError):
        p.run(job)
    p.store.finish(job['id'], error='synthetic received failure', lease_token=job['lease_token'])
    return app, p, pid, job, sends, outcome


@pytest.mark.parametrize('status', [500, 502, 503, 504, 529])
def test_received_server_failure_resumes_only_failed_call_after_confirmation(tmp_path, status):
    app, p, pid, job, sends, outcome = failed_round(tmp_path, status)
    web = TestClient(app)
    state = web.get(endpoint(pid, job)).json()
    assert state['can_recover'] and state['requests'] == {'model': 1}
    assert p.store.enqueue(pid, 'internal', automatic=True)['id'] == job['id']
    assert p.store.claim() is None and sends == ['first', 'second']
    denied = web.post(endpoint(pid, job), json={'expected_version': state['version'],
        'idempotency_key': 'no-consent', 'accept_duplicate_cost': False})
    assert denied.status_code == 409 and sends == ['first', 'second']
    _, accepted, body = authorize(web, endpoint(pid, job))
    assert accepted.status_code == 202, accepted.text
    assert web.post(endpoint(pid, job), json=body).json() == accepted.json()
    outcome[0] = 200
    retried = p.store.claim()
    assert retried['id'] == job['id']
    p.run(retried)
    p.store.finish(retried['id'], lease_token=retried['lease_token'])
    assert sends == ['first', 'second', 'second']
    with closing(p.store._connect()) as db:
        assert db.execute('SELECT count(*) FROM studio_job_calls WHERE response_status=?', (status,)).fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM studio_job_calls').fetchone()[0] == 3
    assert not web.get(endpoint(pid, job)).json()['can_recover']


@pytest.mark.parametrize('status', [400, 401, 403, 422, 429])
def test_rejected_credentials_content_and_quota_do_not_enter_transient_recovery(tmp_path, status):
    app, p, pid, job, sends, _ = failed_round(tmp_path, status)
    assert not TestClient(app).get(endpoint(pid, job)).json()['can_recover']
    assert p.store.claim() is None and sends == ['first', 'second']


def test_received_failure_followed_by_unknown_requires_new_confirmation(tmp_path):
    app, p, pid, job, sends, outcome = failed_round(tmp_path)
    web = TestClient(app)
    _, accepted, _ = authorize(web, endpoint(pid, job))
    assert accepted.status_code == 202
    outcome[0] = 'unknown'
    retried = p.store.claim()
    with pytest.raises(HttpError):
        p.run(retried)
    p.store.finish(retried['id'], error='request_outcome_unknown', lease_token=retried['lease_token'])
    state = web.get(endpoint(pid, job)).json()
    assert state['can_recover'] and state['requests'] == {'model': 1}
    assert sends == ['first', 'second', 'second'] and p.store.claim() is None


def test_configuration_change_still_blocks_received_failure_recovery(tmp_path):
    app, p, pid, job, sends, _ = failed_round(tmp_path)
    p.settings_store.update({'model': {'model': 'changed'}})
    state = TestClient(app).get(endpoint(pid, job)).json()
    assert not state['can_recover'] and '已变化' in state['reason']
    assert sends == ['first', 'second']

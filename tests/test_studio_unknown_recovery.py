"""Explicit local recovery, synthetic transports and no provider credentials."""
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from project_watch.studio.app import create_studio_app
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError, Response


def setup(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    pipeline = app.state.studio_pipeline
    pid = pipeline.repository.create_project('Synthetic Recovery').id
    pipeline.store.upsert_project(pid, {}, consent=True)
    pipeline.store.enqueue(pid, 'external')
    attempts = []
    def dispatch(_job):
        execution = pipeline._execution_state.current
        execution.call('model', 'completed', lambda: Response(200, {}, b'{}'))
        def send():
            attempts.append(1)
            if len(attempts) == 1:
                raise HttpError('connection_failed')
            return Response(200, {}, b'{}')
        execution.call('search', 'unknown', send)
    pipeline.run_external = dispatch
    job = pipeline.store.claim()
    with pytest.raises(HttpError):
        pipeline.run(job)
    pipeline.store.finish(job['id'], error='request_outcome_unknown', lease_token=job['lease_token'])
    return app, pipeline, pid, job, attempts


def endpoint(pid, job):
    return f'/api/studio/projects/{pid}/jobs/{job["id"]}/recovery'


def authorize(client, path, *, key='synthetic-recovery-1'):
    preview = client.get(path)
    assert preview.status_code == 200
    body = {'expected_version': preview.json()['version'],
            'idempotency_key': key, 'accept_duplicate_cost': True}
    response = client.post(path, json=body)
    return preview.json(), response, body


def test_explicit_recovery_preserves_completed_calls_and_unknown_history(tmp_path):
    app, pipeline, pid, job, attempts = setup(tmp_path)
    client = TestClient(app)
    preview, accepted, body = authorize(client, endpoint(pid, job))
    assert preview['can_recover'] and preview['requests'] == {'search': 1}
    assert accepted.status_code == 202, accepted.text
    assert attempts == [1]  # A user confirmation queues; never sends HTTP in API.
    assert client.post(endpoint(pid, job), json=body).json() == accepted.json()
    retried = pipeline.store.claim()
    assert retried['id'] == job['id']
    pipeline.run(retried)
    pipeline.store.finish(retried['id'], lease_token=retried['lease_token'])
    assert len(attempts) == 2
    with closing(pipeline.store._connect()) as db:
        assert db.execute('SELECT count(*) FROM studio_job_calls WHERE job_id=?', (job['id'],)).fetchone()[0] == 3
        assert db.execute("SELECT count(*) FROM studio_job_calls WHERE job_id=? AND status='unknown'", (job['id'],)).fetchone()[0] == 1
    assert JobExecution(pipeline.store, retried).usage()['search']['unknown'] == 0
    assert client.get(endpoint(pid, job)).json()['can_recover'] is False
    assert client.post(endpoint(pid, job), json=body).json() == accepted.json()
    assert pipeline.store.claim() is None


@pytest.mark.parametrize('change', ['model', 'identity', 'paused', 'version', 'acceptance'])
def test_recovery_rejects_changed_state_without_dispatch(tmp_path, change):
    app, pipeline, pid, job, attempts = setup(tmp_path)
    client = TestClient(app)
    path = endpoint(pid, job)
    preview = client.get(path).json()
    body = {'expected_version': preview['version'], 'idempotency_key': 'synthetic-reject',
            'accept_duplicate_cost': True}
    if change == 'model':
        pipeline.settings_store.update({'model': {'model': 'different-model'}})
    elif change == 'identity':
        pipeline.store.upsert_project(pid, {'legal_name': 'Different Company'}, consent=False)
    elif change == 'paused':
        pipeline.store.set_status(pid, 'paused')
    elif change == 'version':
        body['expected_version'] = 'stale'
    else:
        body['accept_duplicate_cost'] = False
    assert client.post(path, json=body).status_code in (409, 422)
    assert attempts == [1] and pipeline.store.claim() is None


def test_other_project_cannot_recover_job(tmp_path):
    app, pipeline, _pid, job, attempts = setup(tmp_path)
    other = pipeline.repository.create_project('Other Synthetic Company').id
    pipeline.store.upsert_project(other, {}, consent=True)
    client = TestClient(app)
    assert client.get(endpoint(other, job)).status_code == 404
    assert client.post(endpoint(other, job), json={'expected_version': 'x',
        'idempotency_key': 'synthetic-other', 'accept_duplicate_cost': True}).status_code == 404
    assert attempts == [1]


def test_new_unknown_after_recovery_does_not_inherit_consent(tmp_path):
    app, pipeline, pid, job, attempts = setup(tmp_path)
    client = TestClient(app)
    authorize(client, endpoint(pid, job))
    retried = pipeline.store.claim()
    execution = JobExecution(pipeline.store, retried)
    with pytest.raises(HttpError):
        execution.call('search', 'unknown', lambda: (_ for _ in ()).throw(HttpError('connection_failed')))
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        execution.call('search', 'unknown', lambda: pytest.fail('cannot use previous consent'))
    pipeline.store.finish(retried['id'], error='request_outcome_unknown', lease_token=retried['lease_token'])
    preview = client.get(endpoint(pid, job)).json()
    assert preview['can_recover'] and preview['requests'] == {'search': 1}
    assert attempts == [1]


def test_recovery_keeps_request_budget_and_pending_unknown_visible(tmp_path):
    app, pipeline, pid, job, _ = setup(tmp_path)
    client = TestClient(app)
    authorize(client, endpoint(pid, job))
    retried = pipeline.store.claim()
    execution = JobExecution(pipeline.store, retried, budgets={'search': 1})
    assert execution.usage()['search']['unknown'] == 1  # Consent is not a response.
    with pytest.raises(HttpError, match='request_budget'):
        execution.call('search', 'unknown', lambda: pytest.fail('must not reset budget'))
    assert execution.usage()['search']['requests'] == 1


def test_recovery_fences_changes_between_confirmation_and_dispatch(tmp_path):
    app, pipeline, pid, job, attempts = setup(tmp_path)
    authorize(TestClient(app), endpoint(pid, job))
    pipeline.settings_store.update({'model': {'model': 'changed-after-consent'}})
    retried = pipeline.store.claim()
    with pytest.raises(ValueError, match='监控依据已变化'):
        pipeline.run(retried)
    assert attempts == [1]


def test_action_idempotency_cannot_be_reused_for_different_state(tmp_path):
    app, _pipeline, pid, job, _ = setup(tmp_path)
    client = TestClient(app)
    _preview, result, body = authorize(client, endpoint(pid, job))
    assert result.status_code == 202
    body['expected_version'] = 'different-version'
    assert client.post(endpoint(pid, job), json=body).status_code == 409


def test_old_task_without_binding_cannot_be_recovered_or_read_secrets(tmp_path):
    app, pipeline, pid, job, attempts = setup(tmp_path)
    with closing(pipeline.store._connect()) as db, db:
        db.execute("DELETE FROM studio_job_checkpoints WHERE job_id=? AND stage='recovery_inputs'", (job['id'],))
    preview, response, _ = authorize(TestClient(app), endpoint(pid, job))
    assert response.status_code == 409 and not preview['can_recover']
    assert set(preview) == {'job_id', 'kind', 'version', 'can_recover', 'reason', 'requests',
                            'requires_duplicate_cost_consent', 'mode', 'job_status', 'notice', 'continuation_job_id'}
    assert attempts == [1]


def test_pending_authorization_survives_restart_without_erasing_unknown(tmp_path):
    from project_watch.studio.store import StudioStore

    app, pipeline, pid, job, attempts = setup(tmp_path)
    authorize(TestClient(app), endpoint(pid, job))
    pipeline.store = StudioStore(pipeline.store.db_path)
    retried = pipeline.store.claim()
    assert retried['id'] == job['id']
    pipeline.run(retried)
    assert attempts == [1, 1]
    # Old request never becomes a known success for historical usage accounting.
    kinds = TestClient(app).get(f'/api/studio/projects/{pid}/usage').json()['jobs'][0]['kinds']
    search = next(kind for kind in kinds if kind['kind'] == 'search')
    assert search['requests'] == 2 and search['outcome_unknown'] == 1


def test_query_retry_is_bound_to_original_task_and_new_unknown_use(tmp_path):
    from project_watch.studio.query_ledger import ProjectQueryCache

    app, pipeline, pid, job, _ = setup(tmp_path)
    # Make a real query-cache unknown tied to this synthetic journalled task.
    with closing(pipeline.store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET status='queued',retry_after=NULL WHERE id=?", (job['id'],))
    original = pipeline.store.claim()
    execution = JobExecution(pipeline.store, original)
    cache = ProjectQueryCache(pipeline.store, pid, purpose='external', job_id=job['id'], guard=execution._guard)
    signature = ('tavily', 'synthetic-account', 'synthetic company', False, 5)
    with pytest.raises(HttpError):
        cache.get(signature, lambda: (_ for _ in ()).throw(HttpError('connection_failed')))
    pipeline.store.finish(original['id'], error='request_outcome_unknown', lease_token=original['lease_token'])
    other_cache = ProjectQueryCache(pipeline.store, pid, purpose='competition', job_id='other-round')
    with pytest.raises(HttpError):
        other_cache.get(signature, lambda: pytest.fail('not consented'))
    client = TestClient(app)
    _, response, _ = authorize(client, endpoint(pid, job))
    assert response.status_code == 202
    with pytest.raises(HttpError):
        other_cache.get(signature, lambda: pytest.fail('wrong task cannot use consent'))
    retried = pipeline.store.claim()
    recovered_cache = ProjectQueryCache(pipeline.store, pid, purpose='external', job_id=job['id'],
                                         guard=JobExecution(pipeline.store, retried)._guard)
    with pytest.raises(HttpError):
        recovered_cache.get(signature, lambda: (_ for _ in ()).throw(HttpError('connection_failed')))
    with pytest.raises(HttpError):
        recovered_cache.get(signature, lambda: pytest.fail('second unknown requires fresh consent'))


def test_material_arrival_invalidates_recovery_preview(tmp_path):
    app, _pipeline, pid, job, attempts = setup(tmp_path)
    client = TestClient(app)
    path = endpoint(pid, job)
    version = client.get(path).json()['version']
    uploaded = client.post(f'/api/studio/projects/{pid}/files',
        files={'file': ('synthetic.txt', b'synthetic update', 'text/plain')})
    assert uploaded.status_code == 202
    result = client.post(path, json={'expected_version': version,
        'idempotency_key': 'synthetic-material', 'accept_duplicate_cost': True})
    assert result.status_code == 409 and attempts == [1]

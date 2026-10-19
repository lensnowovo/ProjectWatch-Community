"""Offline interactive-operation receipts and all-provider completion checks."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from distribution_journey_support import COMPANY, configure, make_app
from fastapi.testclient import TestClient

from project_watch.studio.app import create_studio_app
from project_watch.studio.direct_operations import DirectOperations, OperationBlocked
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.runtime_health import projection
from project_watch.studio.taskboard import board


@pytest.mark.parametrize('endpoint,body', [('/api/studio/resolve', {'name': COMPANY}),
                                         ('/api/studio/settings/test', None)])
def test_repeated_clicks_replay_without_provider_calls(tmp_path, endpoint, body):
    app, transport = make_app(tmp_path)
    with TestClient(app) as client:
        configure(client)
        first = client.post(endpoint, json=body)
        assert first.status_code == 200, first.text
        count = len(transport.calls)
        second = client.post(endpoint, json=body)
        assert second.json() == first.json()
        assert second.headers['X-ProjectWatch-Replayed'] == 'true'
        assert second.headers['X-ProjectWatch-Operation'] == first.headers['X-ProjectWatch-Operation']
        assert len(transport.calls) == count
        records = client.get('/api/studio/operations').json()
        assert records[0]['status'] == 'done'
        assert 'synthetic-model-key' not in json.dumps(records)
        assert COMPANY not in json.dumps(records, ensure_ascii=False)


def test_concurrent_claims_dispatch_only_once_and_do_not_block_other_inputs(tmp_path):
    app, _ = make_app(tmp_path)
    pipeline = app.state.studio_pipeline
    operations, settings = pipeline.direct_operations, pipeline.settings_store.load()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def work():
        calls.append(True)
        entered.set()
        assert release.wait(5)
        return {'value': 1}

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(operations.run, 'resolve', {'name': 'a'}, settings, work)
        assert entered.wait(5)
        try:
            with pytest.raises(OperationBlocked) as caught:
                operations.run('resolve', {'name': 'a'}, settings, work)
            assert caught.value.status == 'running'
            with pytest.raises(OperationBlocked):
                operations.authorize_retry(caught.value.operation_id, accept_duplicate_cost=True)
            assert operations.run('resolve', {'name': 'b'}, settings, lambda: {'value': 2})[0] == {'value': 2}
        finally:
            release.set()
        assert future.result()[0] == {'value': 1}
    assert calls == [True]


def test_unknown_response_survives_restart_and_requires_explicit_retry(tmp_path):
    app, transport = make_app(tmp_path)
    transport.model_failure = 'offline'
    with TestClient(app) as client:
        configure(client)
        response = client.post('/api/studio/resolve', json={'name': COMPANY})
        assert response.status_code == 409, response.text
        operation_id = response.json()['operation_id']
        count = len(transport.calls)
        for _ in range(3):
            assert client.post('/api/studio/resolve', json={'name': COMPANY}).status_code == 409
        assert len(transport.calls) == count
    reopened, fresh_transport = make_app(tmp_path)
    with TestClient(reopened) as client:
        assert client.post('/api/studio/resolve', json={'name': COMPANY}).status_code == 409
        assert fresh_transport.calls == []
        retry = f'/api/studio/operations/{operation_id}/retry'
        assert client.post(retry, json={'accept_duplicate_cost': False}).status_code == 422
        assert client.post(retry, json={'accept_duplicate_cost': 'true'}).status_code == 422
        assert client.post(retry, json={'accept_duplicate_cost': True}).status_code == 200
        assert client.post(retry, json={'accept_duplicate_cost': True}).status_code == 200
        assert fresh_transport.calls == []  # consent is not dispatch
        completed = client.post('/api/studio/resolve', json={'name': COMPANY})
        assert completed.status_code == 200, completed.text
        assert all(call['host'] != 'api.bochaai.com' for call in fresh_transport.calls)
        assert completed.headers['X-ProjectWatch-Operation'] != operation_id
        assert client.post(retry, json={'accept_duplicate_cost': True}).status_code == 409
        requests = len(fresh_transport.calls)
        assert client.post('/api/studio/resolve', json={'name': COMPANY}).status_code == 200
        assert len(fresh_transport.calls) == requests


def test_server_error_stops_model_retry_but_tests_other_services(tmp_path, monkeypatch):
    app, original = make_app(tmp_path)
    calls = []

    def server_error(method, url, headers, body, timeout):
        calls.append(url)
        if 'open.bigmodel.cn' in url:
            return Response(500, {}, b'{}')
        return original(method, url, headers, body, timeout)

    app.state.studio_pipeline.http_factory = lambda _: Http(server_error)
    monkeypatch.setattr('project_watch.studio.llm.time.sleep', lambda _: None)
    with TestClient(app) as client:
        configure(client)
        first = client.post('/api/studio/settings/test')
        assert first.status_code == 200
        assert first.json()['model']['ok'] is False
        assert sum('open.bigmodel.cn' in url for url in calls) == 1
        assert first.json()['search_bocha']['ok'] is True
        count = len(calls)
        assert client.get('/api/studio/operations').json()[0]['status'] == 'unknown'
        assert client.post('/api/studio/settings/test').status_code == 409
        assert len(calls) == count


def test_fingerprint_uses_actual_inputs_and_binding_not_unrelated_settings(tmp_path):
    app, _ = make_app(tmp_path)
    pipeline = app.state.studio_pipeline
    ops, settings = pipeline.direct_operations, pipeline.settings_store.load()

    def unknown():
        transport = ops.http(Http(lambda *args: (_ for _ in ()).throw(HttpError('connection_failed'))))
        transport.get('https://synthetic.example')

    with pytest.raises(OperationBlocked):
        ops.run('resolve', {'name': 'same', 'bp': 'v1'}, settings, unknown)
    settings.refresh_hours = 12
    with pytest.raises(OperationBlocked):
        ops.run('resolve', {'name': 'same', 'bp': 'v1'}, settings, lambda: {})
    assert ops.run('resolve', {'name': 'same', 'bp': 'v2'}, settings, lambda: {'new': True})[0]['new']
    settings.model.api_key = 'new-synthetic-key'
    assert ops.run('resolve', {'name': 'same', 'bp': 'v1'}, settings, lambda: {'new': True})[0]['new']
    with closing(pipeline.store._connect()) as db:
        rows = [dict(r) for r in db.execute('SELECT * FROM studio_direct_operations')]
    assert 'new-synthetic-key' not in json.dumps(rows)
    assert 'v1' not in json.dumps(rows)


def test_success_expires_but_unknown_never_expires(tmp_path):
    app, _ = make_app(tmp_path)
    pipe = app.state.studio_pipeline
    ops, settings = pipe.direct_operations, pipe.settings_store.load()
    _, first, _ = ops.run('test', {}, settings, lambda: {'value': 1})
    with closing(pipe.store._connect()) as db, db:
        db.execute("UPDATE studio_direct_operations SET valid_until='2000' WHERE id=?", (first,))
    result, second, replayed = ops.run('test', {}, settings, lambda: {'value': 2})
    assert result['value'] == 2 and second != first and not replayed
    with closing(pipe.store._connect()) as db, db:
        db.execute("UPDATE studio_direct_operations SET status='unknown',valid_until='2000' WHERE id=?", (second,))
    with pytest.raises(OperationBlocked):
        ops.run('test', {}, settings, lambda: {})


def test_normal_refresh_does_not_reuse_prior_recovery_calls(tmp_path):
    app, _ = make_app(tmp_path)
    pipe = app.state.studio_pipeline
    ops, settings = pipe.direct_operations, pipe.settings_store.load()
    calls = []

    def send(*args):
        calls.append(True)
        return Response(200, {'content-type': 'application/json', 'set-cookie': 'private-cookie'}, b'{}')

    def execute():
        return ops.http(Http(send)).get('https://synthetic.example').json()

    ops.run('test', {}, settings, execute, ttl_seconds=0)
    ops.run('test', {}, settings, execute, ttl_seconds=0)
    assert calls == [True, True]
    with closing(pipe.store._connect()) as db:
        assert 'private-cookie' not in str([tuple(r) for r in db.execute('SELECT * FROM studio_direct_calls')])


def test_error_response_body_is_not_saved_in_direct_replay_cache(tmp_path):
    app, _ = make_app(tmp_path)
    pipe = app.state.studio_pipeline
    ops = pipe.direct_operations
    def execute():
        response = ops.http(Http(lambda *args: Response(401, {}, b'synthetic-echoed-key'))).get('https://synthetic.example')
        return {'ok': response.status == 200}
    ops.run('test', {}, pipe.settings_store.load(), execute)
    with closing(pipe.store._connect()) as db:
        row = db.execute('SELECT response_status,response_body FROM studio_direct_calls').fetchone()
    assert row['response_status'] == 401 and row['response_body'] == b''


def test_interrupted_running_operation_only_recovers_under_lifespan_lock(tmp_path):
    app, _ = make_app(tmp_path)
    store = app.state.studio_store
    with closing(store._connect()) as db, db:
        db.execute("INSERT INTO studio_direct_operations(id,fingerprint,kind,status,created_at,pending) VALUES ('crash','x','resolve','running','2000',1)")
    assert DirectOperations(store).list()[0]['status'] == 'running'
    fresh = create_studio_app(tmp_path, start_worker=False)
    assert fresh.state.studio_pipeline.direct_operations.list()[0]['status'] == 'running'
    with TestClient(fresh) as client:
        operation = client.get('/api/studio/operations').json()[0]
        assert operation['status'] == 'unknown' and operation['unknown'] == 1


@pytest.mark.parametrize('kind', ['model', 'search', 'commercial', 'page'])
def test_finish_does_not_call_unknown_provider_done(tmp_path, kind):
    app, _ = make_app(tmp_path)
    pipe, store = app.state.studio_pipeline, app.state.studio_store
    project = pipe.repository.create_project('Synthetic')
    store.upsert_project(project.id, {'legal_name': 'Synthetic'}, consent=False)
    store.enqueue(project.id, 'brief')
    job = store.claim()
    with closing(store._connect()) as db, db:
        db.execute('INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at) '
                   "VALUES (?,'unknown',?,'synthetic','unknown','2000')", (job['id'], kind))
    assert store.finish(job['id'], lease_token=job['lease_token'])
    finished = store.jobs(project.id)[0]
    assert finished['status'] == 'failed' and finished['retry_after'] == '9999-12-31'
    view = board(store, worker_state='running', model_ready=True)
    assert view['jobs'][0]['unknown'] == 1
    assert view['jobs'][0]['runtime']['state'] == 'needs_confirmation'
    assert view['summary']['done_24h'] == 0
    # Old rows are projected honestly without silently rewriting historical jobs.
    with closing(store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET status='done' WHERE id=?", (job['id'],))
    view = board(store, worker_state='running', model_ready=True)
    assert view['jobs'][0]['runtime']['state'] == 'partial'
    assert view['summary']['partial_24h'] == 1 and view['summary']['done_24h'] == 0


def test_partial_commercial_receipt_is_not_full_success(tmp_path):
    app, _ = make_app(tmp_path)
    pipe, store = app.state.studio_pipeline, app.state.studio_store
    project = pipe.repository.create_project('Synthetic')
    store.upsert_project(project.id, {'legal_name': 'Synthetic'}, consent=False)
    store.enqueue(project.id, 'commercial')
    job = store.claim()
    JobExecution(store, job).checkpoint('commercial_coverage', {'partial': True})
    store.finish(job['id'], lease_token=job['lease_token'])
    with closing(store._connect()) as db:
        view = projection(db, store.jobs(project.id)[0])
    assert view['state'] == 'partial' and '部分来源' in view['label']

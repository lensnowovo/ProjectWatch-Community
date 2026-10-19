"""Received commercial failures must not be recorded as ambiguous dispatches."""
import json
from contextlib import closing

import pytest
from test_studio_commercial import KEY, fixture

from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError
from project_watch.studio.settings import CommercialConfig


@pytest.mark.parametrize(('code', 'http_status'), [
    ('commercial_http', 401), ('commercial_http', 429), ('commercial_http', 500),
    ('commercial_protocol', None), ('commercial_tool_error', None),
    ('commercial_shape', None), ('commercial_encoding', None), ('commercial_size', None),
])
def test_received_error_is_durable_and_replayed_without_dispatch(tmp_path, code, http_status):
    pipeline, pid, service = fixture(tmp_path)
    pipeline.store.enqueue(pid, 'commercial')
    job = pipeline.store.claim()
    execution = JobExecution(pipeline.store, job, budgets={'commercial': 9})
    calls = []
    class Client:
        def fetch(self, *args):
            calls.append(True)
            raise HttpError(code, status=http_status)
    cfg = CommercialConfig(['xiniu'], {'xiniu': KEY})
    report = service.collect(pipeline.store.get_project(pid), cfg, execution=execution,
                             client_factory=lambda *args: Client())
    assert report['xiniu']['status'] == 'error'
    with closing(pipeline.store._connect()) as db, db:
        row = db.execute('SELECT * FROM studio_job_calls WHERE job_id=?', (job['id'],)).fetchone()
        assert row['status'] == 'completed'
        assert row['response_status'] == (http_status or 200)
        assert KEY.encode() not in row['response_body']
        assert json.loads(row['response_body'])['_commercial_error']['code'] == code
        # Crash between durable response and cache publication: replay even with backoff.
        db.execute("UPDATE studio_commercial_calls SET status='started',data='{}' WHERE project_id=?", (pid,))
    replay = service.collect(pipeline.store.get_project(pid), cfg, execution=execution,
                             client_factory=lambda *args: pytest.fail('must not dispatch again'))
    assert replay['xiniu']['status'] == 'error' and len(calls) == 1
    execution.checkpoint('dispatch_completed', {'ok': True})


def test_connection_uncertainty_stays_held_and_is_not_retried(tmp_path):
    pipeline, pid, service = fixture(tmp_path)
    pipeline.store.enqueue(pid, 'commercial')
    job = pipeline.store.claim()
    execution = JobExecution(pipeline.store, job, budgets={'commercial': 9})
    class Client:
        def fetch(self, *args):
            raise HttpError('commercial_connection')
    cfg = CommercialConfig(['xiniu'], {'xiniu': KEY})
    result = service.collect(pipeline.store.get_project(pid), cfg, execution=execution,
                             client_factory=lambda *args: Client())
    assert result['xiniu']['status'] == 'held'
    with closing(pipeline.store._connect()) as db:
        assert db.execute('SELECT status FROM studio_job_calls WHERE job_id=?', (job['id'],)).fetchone()[0] == 'unknown'
    result = service.collect(pipeline.store.get_project(pid), cfg, execution=execution,
                             client_factory=lambda *args: pytest.fail('unknown result must not retry'))
    assert result['xiniu']['status'] == 'held'
    # An old build published cache=error despite the journal being unknown.
    with closing(pipeline.store._connect()) as db, db:
        db.execute("UPDATE studio_commercial_calls SET status='error',expires_at='2000-01-01' WHERE project_id=?", (pid,))
    result = service.collect(pipeline.store.get_project(pid), cfg, execution=execution,
                             client_factory=lambda *args: pytest.fail('legacy ambiguity must not retry'))
    assert result['xiniu']['status'] == 'held'


@pytest.mark.parametrize('failure', ['http', 'protocol', 'shape'])
def test_real_mcp_adapter_received_failures_preserve_other_provider_results(tmp_path, failure):
    from test_studio_commercial import reply

    from project_watch.studio.commercial import MCPClient
    from project_watch.studio.http import Response
    pipeline, pid, service = fixture(tmp_path)
    pipeline.store.enqueue(pid, 'commercial')
    job = pipeline.store.claim()
    execution = JobExecution(pipeline.store, job, budgets={'commercial': 9})
    def transport(method, url, headers, body, timeout):
        request = json.loads(body)
        if request['method'] == 'initialize':
            result = {'protocolVersion': '2025-03-26'}
        elif request['method'] == 'notifications/initialized':
            return Response(204, {}, b'')
        elif 'xiniudata' in url:
            if failure == 'http':
                return Response(429, {}, KEY.encode())
            if failure == 'protocol':
                return Response(200, {}, KEY.encode())
            result = reply('invalid shape ' + KEY)
        else:
            result = reply({'data': {'items': []}})
        return Response(200, {}, json.dumps({'id': request['id'], 'result': result}).encode())
    report = service.collect(pipeline.store.get_project(pid),
        CommercialConfig(['xiniu', 'wind'], {'xiniu': KEY, 'wind': KEY}), execution=execution,
        client_factory=lambda provider, key, proxy: MCPClient(provider, key, transport))
    assert report['xiniu']['status'] == 'error' and report['wind']['status'] == 'ok'
    execution.checkpoint('dispatch_completed', {'partial': True})
    assert pipeline.store.finish(job['id'], lease_token=job['lease_token'])
    with closing(pipeline.store._connect()) as db:
        rows = db.execute('SELECT status,response_body FROM studio_job_calls WHERE job_id=?', (job['id'],)).fetchall()
    assert len(rows) == 2 and all(r['status'] == 'completed' for r in rows)
    assert all(KEY.encode() not in r['response_body'] for r in rows)

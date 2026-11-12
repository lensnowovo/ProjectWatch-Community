"""Fault injection with isolated storage; no provider calls or production data."""
import socket
import threading
import urllib.error
from types import SimpleNamespace

import pytest
import test_dispatch_rule as fixtures
from test_dispatch_rule import job, ok, receipt, rows

from project_watch.studio import dispatch
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError, Response, urllib_transport
from project_watch.studio.llm import ModelError
from project_watch.studio.pipeline import StudioWorker


@pytest.fixture
def env(tmp_path, monkeypatch):
    return fixtures.env.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.parametrize('status', [401, 402, 403, 429, 500, 502, 503, 504, 529])
def test_rejection_then_recovery_keeps_each_receipt_and_replays_success(env, status):
    store, pid = env
    work, sent = job(store, pid, 'page_summary'), []
    work.call('model', 'same', lambda: Response(status, {'Retry-After': '120'}, b'{}'))
    before = rows(store)
    with pytest.raises(dispatch.NotSent):
        work.call('model', 'same', ok(sent))
    assert not sent
    with store._connect() as db:
        db.execute('DELETE FROM studio_provider_backoff')
    work.call('model', 'same', ok(sent))
    work.call('model', 'same', ok(sent))
    assert sent == [1]
    assert rows(store)[:1] == before


def test_not_sent_reconnects_without_counting_as_dispatched(env):
    store, pid = env
    work, sent = job(store, pid, 'page_summary'), []
    def disconnected():
        raise HttpError('connection_not_sent')
    with pytest.raises(HttpError):
        work.call('model', 'same', disconnected)
    with store._connect() as db:
        assert dispatch.spent(db, pid, 'model') == 0
        db.execute('DELETE FROM studio_provider_backoff')
    work.call('model', 'same', ok(sent))
    assert sent == [1]
    with store._connect() as db:
        assert [r[0] for r in db.execute('SELECT status FROM studio_job_calls ORDER BY rowid')] == ['not_sent', 'completed']


@pytest.mark.parametrize('old_status', ['unknown', 'started'])
def test_old_unknown_never_retries_even_with_retry_flag(env, old_status):
    store, pid = env
    work, sent = job(store, pid, 'page_summary'), []
    receipt(store, work.job['id'], 'model', 'same', old_status)
    with store._connect() as db:
        db.execute('UPDATE studio_job_calls SET retryable=1')
    before = rows(store)
    with pytest.raises(dispatch.NotSent):
        work.call('model', 'same', ok(sent))
    assert not sent and rows(store) == before


def test_historical_5xx_is_not_implicitly_replayed(env):
    store, pid = env
    work, sent = job(store, pid, 'analyze'), []
    work.call('model', 'same', lambda: Response(503, {}, b'{}'))
    with store._connect() as db:
        db.execute('UPDATE studio_job_calls SET retryable=0')
        db.execute('DELETE FROM studio_provider_backoff')
    assert work.call('model', 'same', ok(sent)).status == 503
    assert not sent


@pytest.mark.parametrize('reason,expected', [
    (socket.gaierror(-2, 'synthetic'), 'connection_not_sent'),
    (ConnectionRefusedError(), 'connection_not_sent'),
    (TimeoutError(), 'connection_failed'),
    (ConnectionResetError(), 'connection_failed'),
])
def test_transport_only_classifies_proven_pre_send_errors(monkeypatch, reason, expected):
    def open_request(*a, **kw):
        raise urllib.error.URLError(reason)
    monkeypatch.setattr('urllib.request.build_opener', lambda *a: SimpleNamespace(open=open_request))
    with pytest.raises(HttpError) as error:
        urllib_transport()('POST', 'https://synthetic.invalid', {}, b'{}', 1)
    assert error.value.code == expected


@pytest.mark.parametrize('reason', [
    TimeoutError(10060, 'synthetic'),  # TCP connect timeout (Windows)
    OSError('Tunnel connection failed: 502'),  # proxy CONNECT refused
    ConnectionResetError(10054, 'synthetic'),  # reset during TLS handshake
])
def test_patient_setup_failure_before_connected_is_not_sent(env, monkeypatch, reason):
    store, pid = env
    def setup_fails(connection):
        raise reason
    monkeypatch.setattr('http.client.HTTPSConnection.connect', setup_fails)
    with pytest.raises(HttpError) as error:
        urllib_transport()('POST', 'https://synthetic.invalid', {}, b'{}', None)
    assert error.value.code == 'connection_not_sent'
    assert error.value.diagnostics['stage'] == 'connect'
    assert 'connected_ms' not in error.value.diagnostics
    work, sent = job(store, pid, 'page_summary'), []
    with pytest.raises(HttpError, match='connection_not_sent'):
        work.call('model', 'same', lambda: urllib_transport()('POST', 'https://synthetic.invalid', {}, b'{}', None))
    with store._connect() as db:
        assert [tuple(r) for r in db.execute('SELECT status,retryable FROM studio_job_calls')] == [('not_sent', 1)]
        db.execute('DELETE FROM studio_provider_backoff')
    work.call('model', 'same', ok(sent))
    assert sent == [1]


def test_patient_failure_after_connected_stays_unknown(monkeypatch):
    client, peer = socket.socketpair()
    def connected(connection):
        connection.sock = client
    def no_reply(connection):
        raise TimeoutError(10060, 'synthetic')
    monkeypatch.setattr('http.client.HTTPSConnection.connect', connected)
    monkeypatch.setattr('http.client.HTTPConnection.getresponse', no_reply)
    try:
        with pytest.raises(HttpError) as error:
            urllib_transport()('POST', 'https://synthetic.invalid', {}, b'{}', None)
    finally:
        client.close()
        peer.close()
    assert error.value.code == 'connection_failed'
    assert 'connected_ms' in error.value.diagnostics


def test_bounded_request_timeout_is_not_proven_unsent(monkeypatch):
    # Without a patient connection the trace has no connect stage evidence.
    def setup_fails(connection):
        raise TimeoutError(10060, 'synthetic')
    monkeypatch.setattr('http.client.HTTPSConnection.connect', setup_fails)
    with pytest.raises(HttpError, match='connection_failed'):
        urllib_transport()('POST', 'https://synthetic.invalid', {}, b'{}', 1)


def test_redirect_then_dns_failure_stays_unknown(monkeypatch):
    def opener(*handlers):
        def open_request(*a, **kw):
            handlers[-1].seen_redirect = True
            raise urllib.error.URLError(socket.gaierror(-2, 'synthetic'))
        return SimpleNamespace(open=open_request)
    monkeypatch.setattr('urllib.request.build_opener', opener)
    with pytest.raises(HttpError, match='connection_failed'):
        urllib_transport()('POST', 'https://synthetic.invalid', {}, b'{}', 1)


def test_worker_requeues_same_job_and_resumes_saved_progress(env):
    store, pid = env
    queued = store.enqueue(pid, 'page_summary', input_version='fault-test')
    sent = []
    def run(claimed):
        execution = JobExecution(store, claimed)
        if execution.read_checkpoint('first-page') is None:
            sent.append('first')
            execution.checkpoint('first-page', {'published': True})
            raise ModelError('synthetic', code='model_rate_limited')
        sent.append('second')
    worker = StudioWorker(SimpleNamespace(store=store, run=run, _execution_state=threading.local()), schedules=False)
    assert worker.step()
    with store._connect() as db:
        result = db.execute('SELECT status,retry_after FROM studio_jobs WHERE id=?', (queued['id'],)).fetchone()
        assert result['status'] == 'queued' and result['retry_after']
        db.execute('UPDATE studio_jobs SET retry_after=NULL WHERE id=?', (queued['id'],))
    assert worker.step()
    assert sent == ['first', 'second']
    assert store.jobs(pid)[0]['status'] == 'done'


def test_old_402_adoption_preserves_unknown_and_pause(env):
    store, pid = env
    work = job(store, pid, 'page_summary')
    work.call('model', 'same', lambda: Response(402, {}, b'{}'))
    store.finish(work.job['id'], error='模型服务返回错误（402）', lease_token=work.job['lease_token'])
    receipt(store, work.job['id'], 'model', 'old-unknown', 'unknown')
    before = rows(store)
    assert store.resume_recoverable() == 0
    assert rows(store) == before


def test_retry_after_honored_without_application_attempt_limit():
    assert dispatch.retry_delay(429, headers={'Retry-After': '7200'}) == 7200
    assert dispatch.retry_delay(503, 1000) == 1800
    assert dispatch.retry_delay(402) == 3600


def test_repeated_disconnect_backoff_increases_across_claims(env):
    from datetime import UTC, datetime
    store, pid = env
    work = job(store, pid, 'page_summary')
    delays = []
    def disconnected():
        raise HttpError('connection_not_sent')
    for _ in range(3):
        with pytest.raises(HttpError):
            work.call('model', 'same', disconnected)
        assert store.defer_recoverable(work.job, 'connection_not_sent')
        with store._connect() as db:
            due = db.execute('SELECT retry_after FROM studio_jobs WHERE id=?', (work.job['id'],)).fetchone()[0]
            delays.append(round((datetime.fromisoformat(due) - datetime.now(UTC)).total_seconds()))
            db.execute('DELETE FROM studio_provider_backoff')
            db.execute('UPDATE studio_jobs SET retry_after=NULL WHERE id=?', (work.job['id'],))
        work = JobExecution(store, store.claim())
    assert delays == [30, 60, 120]


@pytest.mark.parametrize('new_version', ['page:papers', 'competition-summary:synthetic'])
def test_different_page_does_not_suppress_old_definite_failure(env, new_version):
    store, pid = env
    store.enqueue(pid, 'page_summary', input_version='page:trials')
    work = JobExecution(store, store.claim())
    work.call('model', 'same', lambda: Response(402, {}, b'{}'))
    store.finish(work.job['id'], error='模型服务返回错误（402）', lease_token=work.job['lease_token'])
    store.enqueue(pid, 'page_summary', input_version=new_version)
    before = rows(store)
    assert store.resume_recoverable() == 1
    assert store.resume_recoverable() == 0
    assert rows(store) == before

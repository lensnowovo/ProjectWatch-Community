"""Fault injection with isolated storage; no provider calls or production data."""
import errno
import hashlib
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


def test_manual_recovery_only_clears_providers_the_active_project_waited_for(env):
    from project_watch.studio import spine
    store, pid = env
    work = job(store, pid, 'collect')
    target, other, limited = [hashlib.sha256(s.encode()).hexdigest() for s in ('target', 'other', 'limited')]
    with store._connect() as db:
        db.executemany('INSERT INTO studio_provider_backoff VALUES (?,?,?)',
                       [(target, '2999-01-01', 0), (other, '2999-01-01', 0), (limited, '2999-01-01', 429)])
    # No call row exists: the wait was inherited from another project's request.
    with pytest.raises(dispatch.NotSent):
        work.call('search', 'query', lambda: pytest.fail('blocked request was sent'), provider='target')
    with pytest.raises(dispatch.NotSent):
        work.call('search', 'limited-query', lambda: pytest.fail('blocked request was sent'), provider='limited')
    receipt(store, work.job['id'], 'model', 'old-unknown', 'unknown')
    before = rows(store)
    assert spine.request_round(store, pid, 'manual')['id'] == work.job['id']
    with store._connect() as db:
        assert {r[0] for r in db.execute('SELECT provider FROM studio_provider_backoff')} == {other, limited}
    assert rows(store) == before


def test_manual_recovery_without_active_work_does_not_clear_historical_providers(env):
    from project_watch.studio import spine
    store, pid = env
    work = job(store, pid, 'collect')
    def disconnected():
        raise HttpError('connection_not_sent')
    with pytest.raises(HttpError):
        work.call('search', 'query', disconnected, provider='historical')
    store.finish(work.job['id'], error='synthetic', lease_token=work.job['lease_token'])
    spine.request_round(store, pid, 'manual')
    with store._connect() as db:
        assert db.execute('SELECT count(*) FROM studio_provider_backoff').fetchone()[0] == 1


def test_news_retains_secondary_provider_identity_for_offline_recovery(env, monkeypatch):
    from test_collection_plan import plan

    from project_watch.studio.collection_ledger import CollectionLedger
    from project_watch.studio.collection_runner import run_collection, settle_not_sent
    from project_watch.studio.http import Http
    from project_watch.studio.settings import SearchConfig
    from project_watch.studio.sources import Collectors
    from project_watch.studio.store import now

    store, pid = env
    work = job(store, pid, 'collect')
    bing = hashlib.sha256(b'bing').hexdigest()
    search = hashlib.sha256(b'search').hexdigest()
    with store._connect() as db:
        db.execute('INSERT INTO studio_provider_backoff VALUES (?,?,429)', (bing, '2999-01-01'))
    collectors = Collectors(Http(lambda *a: pytest.fail('unexpected transport')),
                            SearchConfig(provider='deepseek', api_key='synthetic'))
    monkeypatch.setattr(collectors, 'bing_news', lambda query: work.call(
        'search', 'bing-query', lambda: pytest.fail('rate-limited request sent'), provider='bing'))
    def disconnected():
        raise HttpError('connection_not_sent')
    monkeypatch.setattr(collectors, '_provider_search', lambda *a, **kw: work.call(
        'search', 'search-query', disconnected, provider='search'))
    ledger = CollectionLedger(work, plan(round_id=work.job['id'], project_id=pid, at=now()))
    coverage = run_collection(ledger, collect=lambda unit: collectors.news(unit['query'], search_count=1),
                              ingest=lambda *a: pytest.fail('no payload to ingest'), guard=lambda: None)
    assert set(ledger.receipts[0]['provider_hashes']) == {bing, search}
    with pytest.raises(HttpError, match='connection_not_sent'):
        settle_not_sent(ledger, coverage)
    # An unchanged retry only meets backoffs; it must still retain both identities.
    coverage = run_collection(ledger, collect=lambda unit: collectors.news(unit['query'], search_count=1),
                              ingest=lambda *a: pytest.fail('no payload to ingest'), guard=lambda: None)
    with pytest.raises(HttpError, match='connection_not_sent'):
        settle_not_sent(ledger, coverage)


def test_a_resumed_pass_skips_units_whose_provider_still_backs_off(env):
    """A long backoff (a daily budget) must not starve the round: later passes reach the
    remaining units without retrying or rewriting the waiting ones, and the pass settles."""
    from test_collection_plan import candidate, plan

    from project_watch.studio.collection_ledger import CollectionLedger
    from project_watch.studio.collection_runner import run_collection, settle_not_sent
    from project_watch.studio.execution import JobYield
    from project_watch.studio.store import now

    store, pid = env
    work = job(store, pid, 'collect')
    limited = hashlib.sha256(b'limited').hexdigest()
    with store._connect() as db:
        db.execute('INSERT INTO studio_provider_backoff VALUES (?,?,429)', (limited, '2999-01-01'))
    tried = []

    def collect(unit):
        tried.append(unit['query'])
        if unit['query'] == 'limited':
            return work.call('open_data', 'limited-query', lambda: pytest.fail('backing-off request sent'),
                             provider='limited')
        return dict(items=[], request_ids=['request-' + unit['query']], truncated=False)

    ledger = CollectionLedger(work, plan([candidate() | {'query': 'limited', 'query_rank': 0}, candidate() | {'query': 'open', 'query_rank': 1}],
                                         round_id=work.job['id'], project_id=pid, at=now()))
    work.slice_seconds = 0  # yield after the first unit, as a long round does
    with pytest.raises(JobYield):
        run_collection(ledger, collect=collect, ingest=lambda *a: 0, guard=lambda: None)
    assert tried == ['limited']
    waiting = ledger.receipt(ledger.plan['units'][0]['id'])
    assert (waiting['outcome'], waiting['reason'], waiting['provider_hashes']) == ('running', 'provider_backoff', [limited])

    def manifest():
        with store._connect() as db:
            return db.execute("SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage='collection_round_v1'",
                              (work.job['id'],)).fetchone()[0]
    before = manifest()
    work.slice_seconds = 3600
    tried.clear()
    resumed = CollectionLedger(work, ledger.plan)
    original = resumed.execution.checkpoint_chunked
    writes = []
    resumed.execution.checkpoint_chunked = lambda stage, data: (writes.append(stage), original(stage, data))
    coverage = run_collection(resumed, collect=collect, ingest=lambda *a: 0, guard=lambda: None)
    assert tried == ['open'] and writes.count('collection_round_v1') == 2  # open unit only: running, success
    assert manifest() != before
    settled = {r['unit_id']: r for r in settle_not_sent(resumed, coverage)['rows']}
    assert [settled[u['id']]['outcome'] for u in resumed.plan['units']] == ['failed', 'success']

    # Once the backoff ends, a unit still open is tried again.
    with store._connect() as db:
        db.execute('DELETE FROM studio_provider_backoff')
    store.finish(work.job['id'], lease_token=work.job['lease_token'])
    other = job(store, pid, 'collect')
    again = CollectionLedger(other, plan([candidate() | {'query': 'open'}], round_id=other.job['id'], project_id=pid, at=now()))
    again.record(dict(round_id=other.job['id'], unit_id=again.plan['units'][0]['id'], outcome='running',
                      reason='provider_backoff', provider_hashes=[limited]))
    tried.clear()
    run_collection(again, collect=collect, ingest=lambda *a: 0, guard=lambda: None)
    assert tried == ['open']


def test_a_unit_is_retried_while_any_of_its_providers_is_available(env):
    from test_collection_plan import candidate, plan

    from project_watch.studio.collection_ledger import CollectionLedger
    from project_watch.studio.collection_runner import run_collection
    from project_watch.studio.store import now

    store, pid = env
    work = job(store, pid, 'collect')
    limited, free = (hashlib.sha256(s).hexdigest() for s in (b'limited', b'free'))
    with store._connect() as db:
        db.execute('INSERT INTO studio_provider_backoff VALUES (?,?,429)', (limited, '2999-01-01'))
    ledger = CollectionLedger(work, plan([candidate()], round_id=work.job['id'], project_id=pid, at=now()))
    unit = ledger.plan['units'][0]['id']
    ledger.record(dict(round_id=work.job['id'], unit_id=unit, outcome='running',
                       reason='provider_backoff', provider_hashes=[limited, free]))
    tried = []
    run_collection(ledger, collect=lambda u: tried.append(u['id']) or dict(items=[], request_ids=['r'], truncated=False),
                   ingest=lambda *a: 0, guard=lambda: None)
    assert tried == [unit] and ledger.receipt(unit)['outcome'] == 'success'


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


def _winerror(code):
    error = OSError(code, 'synthetic Windows socket error')
    error.winerror = code
    return error


@pytest.mark.parametrize('reason,expected', [
    (socket.gaierror(-2, 'synthetic'), 'connection_not_sent'),
    (ConnectionRefusedError(), 'connection_not_sent'),
    (TimeoutError(), 'connection_failed'),
    (ConnectionResetError(), 'connection_failed'),
    (OSError(errno.EADDRINUSE, 'no free local port'), 'connection_not_sent'),  # only connect() raises these
    (OSError(errno.EADDRNOTAVAIL, 'address not available'), 'connection_not_sent'),
    (_winerror(10048), 'connection_not_sent'),
    (OSError(errno.ENOBUFS, 'no buffer space'), 'connection_failed'),  # can also come from a send
])
def test_transport_only_classifies_proven_pre_send_errors(monkeypatch, reason, expected):
    def opener(*handlers):
        def open_request(*a, **kw):
            # The connection is set up; only the error itself can prove nothing was sent.
            next(h for h in handlers if hasattr(h, 'trace')).trace.mark('connected')
            raise urllib.error.URLError(reason)
        return SimpleNamespace(open=open_request)
    monkeypatch.setattr('urllib.request.build_opener', opener)
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


@pytest.mark.parametrize('reason', [
    TimeoutError(10060, 'synthetic'),
    OSError(8, 'synthetic EOF in TLS handshake'),  # live: a proxy cut 9 searches and reads here
])
def test_bounded_request_setup_failure_before_connected_is_not_sent(monkeypatch, reason):
    # Ordinary reads and searches also mark when TCP, proxy tunnel and TLS setup finished.
    def setup_fails(connection):
        raise reason
    monkeypatch.setattr('http.client.HTTPSConnection.connect', setup_fails)
    with pytest.raises(HttpError) as error:
        urllib_transport()('GET', 'https://synthetic.invalid', {}, None, 1)
    assert error.value.code == 'connection_not_sent'
    assert 'connected_ms' not in error.value.diagnostics


def test_bounded_request_failure_after_connected_stays_unknown(monkeypatch):
    client, peer = socket.socketpair()
    def connected(connection):
        connection.sock = client
    def no_reply(connection):
        raise ConnectionResetError(10054, 'synthetic')
    monkeypatch.setattr('http.client.HTTPSConnection.connect', connected)
    monkeypatch.setattr('http.client.HTTPConnection.getresponse', no_reply)
    try:
        with pytest.raises(HttpError) as error:
            urllib_transport()('POST', 'https://synthetic.invalid', {}, b'{}', 1)
    finally:
        client.close()
        peer.close()
    assert error.value.code == 'connection_failed'
    assert 'connected_ms' in error.value.diagnostics and 'sent_ms' in error.value.diagnostics


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


def test_a_disconnect_does_not_wait_for_another_providers_daily_budget(env):
    """A round that lost its connection briefly resumes in minutes, not when an unrelated
    provider's daily budget resets (a pw-night round waited 17 h this way)."""
    from datetime import UTC, datetime, timedelta

    from project_watch.studio.store import now
    store, pid = env
    work = job(store, pid, 'collect')
    with store._connect() as db:
        for provider in ('daily-search', 'model'):
            db.execute("INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at) "
                       "VALUES (?,?,?,?,?,?)", (work.job['id'], provider, 'open_data', provider, 'completed', now()))
        db.execute("INSERT INTO studio_provider_backoff VALUES ('daily-search','2999-01-01T00:00:00+00:00',429)")
        db.execute("INSERT INTO studio_provider_backoff VALUES ('model',?,0)",
                   ((datetime.now(UTC) + timedelta(minutes=5)).isoformat(),))

    def due():
        with store._connect() as db:
            value = db.execute('SELECT retry_after FROM studio_jobs WHERE id=?', (work.job['id'],)).fetchone()[0]
            db.execute('UPDATE studio_jobs SET retry_after=NULL WHERE id=?', (work.job['id'],))
        return datetime.fromisoformat(value) - datetime.now(UTC)
    assert store.defer_recoverable(work.job, 'connection_not_sent')
    assert timedelta(minutes=4) < due() < timedelta(minutes=6)  # the connection backoff, not 2999
    work = JobExecution(store, store.claim())
    assert store.defer_recoverable(work.job, 'provider_backoff')
    assert due() > timedelta(days=300)  # a job waiting on a backing-off provider still waits for it


def test_a_job_refused_by_a_backing_off_provider_waits_for_it_without_a_call_row(env):
    """Refused before any request (another job's backoff): wait for that backoff, not 30 s polls."""
    from datetime import UTC, datetime, timedelta
    store, pid = env
    work = job(store, pid, 'page_summary')
    with store._connect() as db:
        db.execute("INSERT INTO studio_provider_backoff VALUES (?,'2999-01-01T00:00:00+00:00',429)",
                   (hashlib.sha256(b'limited').hexdigest(),))
    with pytest.raises(dispatch.NotSent, match='provider_backoff'):
        work.call('open_data', 'refused', lambda: pytest.fail('sent during a backoff'), provider='limited')
    assert store.defer_recoverable(work.job, 'provider_backoff')
    with store._connect() as db:
        assert not db.execute('SELECT 1 FROM studio_job_calls WHERE job_id=?', (work.job['id'],)).fetchone()
        due = db.execute('SELECT retry_after FROM studio_jobs WHERE id=?', (work.job['id'],)).fetchone()[0]
    assert datetime.fromisoformat(due) - datetime.now(UTC) > timedelta(days=300)


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

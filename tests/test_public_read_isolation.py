"""Public transport failures are terminal source receipts, never paid retries."""
import json
from urllib.parse import parse_qs, urlsplit

import pytest
import test_collection_host as host_fixtures
import test_collection_reading as reading_fixtures
import test_studio_dsh_monitor as fixtures
from test_collection_host import response
from test_collection_pages import work
from test_collection_runner import candidate, plan

from project_watch.feeds import FeedError
from project_watch.studio import dsh_runtime
from project_watch.studio.collection_ledger import CollectionLedger
from project_watch.studio.collection_pages import public_read_request
from project_watch.studio.collection_runner import run_collection
from project_watch.studio.collection_sources import CollectionSources
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.settings import SearchConfig
from project_watch.studio.sources import Collectors


@pytest.fixture
def setup(tmp_path):
    return host_fixtures.setup.__wrapped__(tmp_path)


def timeout(phase='connect_or_headers'):
    return HttpError('connection_failed', detail='private credential must not persist',
                     transport_phase=phase, failure_kind='timeout')


@pytest.mark.parametrize('host', ['www.cninfo.com.cn', 'api.openalex.org'])
@pytest.mark.parametrize('phase', ['connect_or_headers', 'response_body'])
def test_public_failure_reaches_dsh_and_other_sources_without_resend(setup, monkeypatch, host, phase):
    pipeline, execution = setup
    calls, models = [], []

    def send(_method, url, *_args):
        calls.append(url)
        if urlsplit(url).hostname == host:
            raise timeout(phase)
        return response(url)

    pipeline.http_factory = lambda _: Http(send)

    def model_turn(research, *_args):
        result = research.tool('pw_status', {})
        models.append(result)
        return result

    monkeypatch.setattr(dsh_runtime, 'run_dsh', model_turn)
    for _ in range(2):
        dsh_runtime.run_bootstrap(pipeline, execution, None)
    assert len(models) == 2 and len(calls) == 3
    assert execution.usage()['open_data']['unknown'] == 0
    coverage = models[-1]['collection']
    assert coverage['status'] == 'partial'
    assert any(r['outcome'] == 'success' for r in coverage['rows'])
    assert any(r['outcome'] == 'failed' and r['reason'] == 'public_read_failed' for r in coverage['rows'])
    with pipeline.store._connect() as db:
        errors = [json.loads(r[0]) for r in db.execute(
            "SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage LIKE 'call_error:%'",
            (execution.job['id'],))]
    assert errors and all(e['phase'] == phase and e['code'] == 'public_read_failed' for e in errors)
    assert 'private' not in json.dumps(errors)


@pytest.mark.parametrize('kind,method,declared', [
    ('model', 'GET', True), ('search', 'GET', True), ('page', 'GET', False),
    ('open_data', 'POST', True), ('open_data', 'GET', False),
])
@pytest.mark.parametrize('error', [timeout(), FeedError('connection_failed'), FeedError('tls_verification_failed')])
def test_paid_write_and_undeclared_reads_still_hold(tmp_path, kind, method, declared, error):
    execution = fixtures.loop.__wrapped__(tmp_path).execution
    calls = []

    def send(*_args):
        calls.append(1)
        raise error

    http = execution.http(Http(send), lambda _: (kind, 'synthetic'), public_read=lambda *_: declared)
    with pytest.raises((HttpError, FeedError)):
        http.transport(method, 'https://example.invalid/query', {}, None, 20)
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        http.transport(method, 'https://example.invalid/query', {}, None, 20)
    assert calls == [1] and execution.usage()[kind]['unknown'] == 1


@pytest.mark.parametrize('error', [RuntimeError('synthetic bug'), OSError('unclassified failure')])
def test_public_read_declaration_does_not_hide_unclassified_errors(tmp_path, error):
    execution = fixtures.loop.__wrapped__(tmp_path).execution
    calls = []

    def send():
        calls.append(1)
        raise error

    with pytest.raises(type(error)):
        execution.call('page', 'same-request', send, read_only=True, public_read=True)
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        execution.call('page', 'same-request', send, read_only=True, public_read=True)
    assert calls == [1] and execution.usage()['page']['unknown'] == 1


@pytest.mark.parametrize('error', [timeout(), TimeoutError(), ConnectionResetError()])
def test_restart_replays_failure_without_dispatch_and_keeps_historical_unknown(tmp_path, error):
    execution = fixtures.loop.__wrapped__(tmp_path).execution
    calls = []

    def send():
        calls.append(1)
        raise error

    for _ in range(2):
        execution = JobExecution(execution.store, execution.job)
        with pytest.raises(HttpError, match='public_read_failed'):
            execution.call('page', 'same-request', send, read_only=True, public_read=True)
    assert calls == [1]
    with execution.store._connect() as db:
        db.execute("UPDATE studio_job_calls SET status='unknown' WHERE job_id=?", (execution.job['id'],))
        db.commit()
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        execution.call('page', 'same-request', send, read_only=True, public_read=True)
    assert calls == [1] and execution.usage()['page']['unknown'] == 1


def test_later_page_timeout_retains_prior_page_and_partial_coverage(tmp_path):
    research = fixtures.loop.__wrapped__(tmp_path)
    execution, calls, saved = research.execution, [], []

    def send(_method, url, *_args):
        page = int(parse_qs(urlsplit(url).query)['page'][0])
        calls.append(page)
        if page == 2:
            raise timeout('response_body')
        return Response(200, {}, json.dumps({'results': [work(n) for n in range(20)],
                                           'meta': {'count': 40}}).encode())

    ledger = CollectionLedger(execution, plan([candidate() | {'source': 'openalex_topic'}],
        round_id=execution.job['id'], project_id=execution.job['project_id']))
    http = execution.http(Http(send), lambda _: ('open_data', 'synthetic'), public_read=public_read_request)
    adapter = CollectionSources(execution, Collectors(http, SearchConfig()))

    def ingest(_unit, items):
        saved.extend(items)
        return len(items)

    for _ in range(2):
        result = run_collection(ledger, collect=adapter.collect, ingest=ingest, guard=research.guard)
    row = result['rows'][0]
    assert calls == [1, 2] and len(saved) == 20
    assert result['status'] == 'partial' and row['outcome'] == 'failed'
    assert row['reason'] == 'public_read_failed'
    assert ledger.receipts[0]['truncated'] and ledger.receipts[0]['ingestion_complete']
    assert ledger.receipts[0]['pages_completed'] == 1
    assert execution.usage()['open_data']['unknown'] == 0


@pytest.mark.parametrize('method,url,headers', [
    ('POST', 'https://api.openalex.org/works', {}),
    ('GET', 'https://api.openalex.org/write', {}),
    ('GET', 'https://api.openalex.org/works?api_key=synthetic', {}),
    ('GET', 'https://api.openalex.org/works', {'Authorization': 'synthetic'}),
    ('GET', 'https://api.openalex.org:8443/works', {}),
    ('GET', 'https://api.openalex.org.evil.invalid/works', {}),
])
def test_public_contract_does_not_leak_to_other_requests(method, url, headers):
    assert not public_read_request(method, url, headers, None)


def test_public_timeout_cannot_bypass_expired_lease(tmp_path):
    execution = fixtures.loop.__wrapped__(tmp_path).execution

    def send():
        with execution.store._connect() as db:
            db.execute("UPDATE studio_jobs SET lease_until='2000-01-01' WHERE id=?", (execution.job['id'],))
            db.commit()
        raise timeout()

    with pytest.raises(RuntimeError, match='lease'):
        execution.call('page', 'expired', send, read_only=True, public_read=True)
    assert execution.usage()['page']['unknown'] == 1


def test_dsh_page_timeout_allows_other_tools_without_reopening_page(tmp_path):
    loop, entry, searches, pages = reading_fixtures.research.__wrapped__(tmp_path)

    def read(url):
        pages.append(url)
        raise timeout('response_body')

    loop.reader.read = read
    for _ in range(2):
        assert loop.tool('pw_read', {'source_id': entry['id'], 'view': 'page'}) == {
            'error': 'public_read_failed', 'terminal': False}
    loop.tool('pw_search', {'query': 'extra', 'category': 'company'})
    assert pages == [entry['url']] and searches == ['base', 'extra']
    assert loop.execution.usage()['page']['unknown'] == 0
    assert not loop.execution.read_checkpoint('dsh_hold')

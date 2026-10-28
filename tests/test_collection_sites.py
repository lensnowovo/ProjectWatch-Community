"""Host website collection through pinned-IP transport, with no real network."""
from types import SimpleNamespace

import pytest
import test_collection_host as host_fixtures

from project_watch.feeds import GuardedHTTPS
from project_watch.studio import dsh_runtime
from project_watch.studio.collection_ledger import STAGE
from project_watch.studio.http import Http, HttpError


@pytest.fixture
def setup(tmp_path):
    return host_fixtures.setup.__wrapped__(tmp_path)

HOME = '<h1>Synthetic Host</h1><p>Public products</p><p>Research overview</p><a href="/news">新闻动态</a>'
NEWS = '<h1>Public news</h1><p>First source item</p><p>Second source item</p>'


def configure(setup, monkeypatch, *, news_status=200, robots='', redirect=False):
    pipeline, execution = setup
    pipeline.store.upsert_project(execution.job['project_id'], {'website': 'https://company.example/'}, consent=True)
    calls = []

    def connector(host, _ip, path):
        calls.append((host, path))
        if path == '/robots.txt':
            return 200, {}, robots.encode()
        if path == '/':
            return 200, {'content-type': 'text/html'}, HOME.encode()
        if path == '/news' and redirect:
            return 302, {'location': '/updates'}, b''
        if path in ('/news', '/updates'):
            return news_status, {'content-type': 'text/html'}, NEWS.encode()
        pytest.fail('unexpected site page: ' + path)

    pipeline.article_reader = SimpleNamespace(transport=GuardedHTTPS(
        resolver=lambda _: ['8.8.8.8'], connector=connector))
    pipeline.http_factory = lambda _: Http(lambda _method, url, *_args: host_fixtures.response(url))
    monkeypatch.setattr(dsh_runtime, 'run_dsh', lambda research, *_: research.tool('pw_status', {}))
    return pipeline, execution, calls


def site_row(result):
    return next(row for row in result['collection']['rows'] if row['source'] == 'site')


def test_site_snapshots_are_imported_before_dsh_and_replay_does_not_resend(setup, monkeypatch):
    pipeline, execution, calls = configure(setup, monkeypatch)
    result = dsh_runtime.run_bootstrap(pipeline, execution, None)
    row = site_row(result)
    assert row['outcome'] == 'success' and row['saved_count'] == 2
    assert len(calls) == 3
    assert execution.usage()['page']['requests'] == 3  # includes robots, no second site journal
    assert 'site' not in execution.usage()
    dsh_runtime.run_bootstrap(pipeline, execution, None)
    assert len(calls) == 3
    assert pipeline.store.list_events(project_id=execution.job['project_id']) == []


@pytest.mark.parametrize('redirect', [False, True])
def test_physical_site_requests_including_redirects_share_page_budget(setup, monkeypatch, redirect):
    pipeline, execution, calls = configure(setup, monkeypatch, redirect=redirect)
    execution.budgets['page'] = 3
    result = dsh_runtime.run_bootstrap(pipeline, execution, None)
    assert len(calls) == 3
    assert execution.usage()['page']['requests'] == 3
    row = site_row(result)
    assert row['outcome'] == ('budget_deferred' if redirect else 'success')
    assert row['saved_count'] == (1 if redirect else 2)


def test_failed_page_preserves_successful_snapshot_without_successful_empty(setup, monkeypatch):
    pipeline, execution, calls = configure(setup, monkeypatch, news_status=404)
    result = dsh_runtime.run_bootstrap(pipeline, execution, None)
    row = site_row(result)
    assert row['outcome'] == 'failed' and row['saved_count'] == 1
    receipt = next(r for r in execution.read_checkpoint(STAGE)['receipts'] if r['unit_id'] == row['unit_id'])
    assert receipt['truncated'] and receipt['ingestion_complete']
    assert len(calls) == 3


def test_robots_denial_is_failed_collection_and_no_page_request(setup, monkeypatch):
    pipeline, execution, calls = configure(setup, monkeypatch, robots='User-agent: *\nDisallow: /')
    result = dsh_runtime.run_bootstrap(pipeline, execution, None)
    row = site_row(result)
    assert row['outcome'] == 'failed' and row['result_count'] == 0
    assert calls == [('company.example', '/robots.txt')]


def test_zero_page_budget_makes_no_site_request(setup, monkeypatch):
    pipeline, execution, calls = configure(setup, monkeypatch)
    execution.budgets['page'] = 0
    result = dsh_runtime.run_bootstrap(pipeline, execution, None)
    assert site_row(result)['outcome'] == 'budget_deferred'
    assert calls == []


def test_site_unknown_stops_and_cannot_be_resent(setup, monkeypatch):
    pipeline, execution, _ = configure(setup, monkeypatch)
    attempts = []

    def lost(*_args):
        attempts.append(1)
        raise OSError('synthetic lost response')

    pipeline.article_reader.transport.connector = lost
    monkeypatch.setattr(dsh_runtime, 'run_dsh', lambda *_: pytest.fail('unknown must block model'))
    for _ in range(2):
        with pytest.raises(HttpError, match='request_outcome_unknown'):
            dsh_runtime.run_bootstrap(pipeline, execution, None)
    assert attempts == [1]
    assert execution.usage()['page']['unknown'] == 1


def test_resume_after_snapshot_replays_redirect_headers_without_network(setup, monkeypatch):
    pipeline, execution, calls = configure(setup, monkeypatch, redirect=True)
    original = pipeline.monitor.save_check
    interrupted = []

    def save_check(page_id, **kwargs):
        pages = pipeline.monitor.pages(execution.job['project_id'])
        page = next(p for p in pages if p['id'] == page_id)
        if page['kind'] == 'news' and not interrupted:
            interrupted.append(1)
            raise RuntimeError('synthetic checkpoint interruption')
        return original(page_id, **kwargs)

    monkeypatch.setattr(pipeline.monitor, 'save_check', save_check)
    with pytest.raises(RuntimeError, match='synthetic checkpoint'):
        dsh_runtime.run_bootstrap(pipeline, execution, None)
    assert len(calls) == 4
    result = dsh_runtime.run_bootstrap(pipeline, execution, None)
    assert site_row(result)['outcome'] == 'success'
    assert site_row(result)['saved_count'] == 2 and len(calls) == 4


def test_existing_site_change_items_are_preserved_alongside_current_snapshot(setup, monkeypatch):
    pipeline, execution, _ = configure(setup, monkeypatch)
    project_id = execution.job['project_id']
    page = pipeline.monitor.add_page(project_id, 'https://company.example/', 'Home', 'home', 'user')
    for text in ('First old event', 'Second old event'):
        pipeline.monitor.add_change(project_id, page, [text], [])
    original = pipeline.monitor.changes(project_id)
    result = dsh_runtime.run_bootstrap(pipeline, execution, None)
    assert site_row(result)['outcome'] == 'success' and site_row(result)['saved_count'] == 1
    assert pipeline.monitor.changes(project_id) == original


def test_scope_change_on_redirect_stops_before_destination_request(setup, monkeypatch):
    pipeline, execution, calls = configure(setup, monkeypatch, redirect=True)
    transport = pipeline.article_reader.transport
    original = transport.connector

    def send(host, ip, path):
        result = original(host, ip, path)
        if path == '/news':
            pipeline.store.upsert_project(execution.job['project_id'],
                                          {'website': 'https://changed.example/'}, consent=True)
        return result

    transport.connector = send
    with pytest.raises(RuntimeError, match='scope changed'):
        dsh_runtime.run_bootstrap(pipeline, execution, None)
    assert calls == [('company.example', '/robots.txt'), ('company.example', '/'), ('company.example', '/news')]


@pytest.mark.parametrize('failure', ['connect_timeout', 'tls_timeout', 'certificate'])
def test_pinned_connector_failure_isolated_before_dsh_without_resend(setup, monkeypatch, failure):
    """Exercise actual connector wrapping, not a pre-normalized HttpError."""
    import socket
    import ssl

    from project_watch.feeds import _pinned_request

    pipeline, execution, calls = configure(setup, monkeypatch)
    original = pipeline.article_reader.transport.connector
    attempts, closed = [], []

    def connect(*_args, **_kwargs):
        attempts.append(1)
        if failure == 'connect_timeout':
            raise TimeoutError('synthetic connection timeout')
        return SimpleNamespace(close=lambda: closed.append(1))

    def tls(*_args, **_kwargs):
        if failure == 'certificate':
            raise ssl.SSLCertVerificationError('synthetic invalid certificate')
        raise TimeoutError('synthetic TLS timeout')

    def connector(host, ip, path):
        if path == '/robots.txt':
            return original(host, ip, path)
        return _pinned_request(host, ip, path)

    monkeypatch.setattr(socket, 'create_connection', connect)
    monkeypatch.setattr(ssl.SSLContext, 'wrap_socket', tls)
    pipeline.article_reader.transport.connector = connector
    for _ in range(2):
        result = dsh_runtime.run_bootstrap(pipeline, execution, None)
        assert site_row(result)['outcome'] == 'failed'
        # The connector failed before ingestion; an absent count is not a
        # successful empty search result.
        assert site_row(result)['saved_count'] is None
        assert site_row(result)['reason'] == 'public_read_failed'
        assert result['collection']['status'] == 'partial'
        assert any(r['outcome'] == 'success' for r in result['collection']['rows'])
    assert attempts == [1]
    assert closed == ([] if failure == 'connect_timeout' else [1])
    assert calls == [('company.example', '/robots.txt')]
    assert execution.usage()['page']['unknown'] == 0
    with pipeline.store._connect() as db:
        assert db.execute("SELECT count(*) FROM studio_job_calls WHERE status='failed_read'").fetchone()[0] == 1

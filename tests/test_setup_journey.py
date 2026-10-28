"""First-use setup uses fake provider HTTP only; no user's home or credentials."""
import pytest
from distribution_journey_support import configure, make_app
from fastapi.testclient import TestClient

from project_watch.studio.desktop import start_page


def test_first_use_page_and_two_service_test(tmp_path):
    app, transport = make_app(tmp_path)
    assert start_page(app) == 'setup'
    with TestClient(app) as client:
        assert client.get('/setup').status_code == 200
        assert transport.calls == []
        configure(client)
        assert start_page(app) == ''
        result = client.post('/api/studio/settings/test?scope=setup')
        assert result.status_code == 200
        assert set(result.json()) == {'model', 'search_bocha'}
        assert all(row['ok'] for row in result.json().values())
        assert {c['host'] for c in transport.calls} == {'open.bigmodel.cn', 'api.bochaai.com'}
        assert len(transport.calls) == 2
        # Durable receipt replay, not duplicate paid calls on double click.
        second = client.post('/api/studio/settings/test?scope=setup')
        assert second.headers['X-ProjectWatch-Replayed'] == 'true'
        assert len(transport.calls) == 2


def test_setup_preserves_other_settings_and_existing_home(tmp_path):
    app, transport = make_app(tmp_path)
    with TestClient(app) as client:
        configure(client)
        client.put('/api/studio/settings', json={'refresh_hours': 72,
            'commercial': {'providers': ['xiniu'], 'keys': {'xiniu': 'synthetic-never-sent'}},
            'search': {'providers': ['tavily'], 'keys': {'tavily': 'synthetic-retained'}}})
        client.put('/api/studio/settings', json={'search': {'providers': ['bocha'],
            'keys': {'bocha': 'synthetic-search-key-distribution-only'}}})
        saved = client.get('/api/studio/settings').json()
        assert saved['refresh_hours'] == 72
        assert saved['commercial']['keys']['xiniu']
        assert saved['search']['keys']['tavily']
        result = client.post('/api/studio/settings/test?scope=setup')
        assert result.status_code == 200
        assert all(c['host'] not in ('vip.xiniudata.com', 'api.tavily.com') for c in transport.calls)


@pytest.mark.parametrize('failure', ['invalid', 'quota', 'offline'])
def test_setup_reports_search_failures_without_empty_success(tmp_path, failure):
    app, transport = make_app(tmp_path)
    transport.search_failure = failure
    with TestClient(app) as client:
        configure(client)
        result = client.post('/api/studio/settings/test?scope=setup').json()
        assert result['search_bocha']['ok'] is False
        assert result['search_bocha']['message']
        assert len(transport.calls) == 2


def test_setup_rejects_multiple_sources_before_paid_work(tmp_path):
    app, transport = make_app(tmp_path)
    with TestClient(app) as client:
        configure(client)
        client.put('/api/studio/settings', json={'search': {'providers': ['bocha', 'tavily']}})
        assert client.post('/api/studio/settings/test?scope=setup').status_code == 422
        assert transport.calls == []


def test_existing_project_not_forced_through_setup():
    from types import SimpleNamespace
    app = SimpleNamespace(state=SimpleNamespace(studio_store=SimpleNamespace(list_projects=lambda: [{'id': 'existing'}])))
    assert start_page(app) == ''


def test_setup_unknown_response_is_not_retried(tmp_path):
    from project_watch.studio.http import Http, Response
    app, original = make_app(tmp_path)
    calls = []
    def uncertain(method, url, headers, body, timeout):
        calls.append(url)
        if 'open.bigmodel.cn' in url:
            return Response(500, {}, b'{}')
        return original(method, url, headers, body, timeout)
    app.state.studio_pipeline.http_factory = lambda _: Http(uncertain)
    with TestClient(app) as client:
        configure(client)
        first = client.post('/api/studio/settings/test?scope=setup')
        assert first.status_code == 200 and first.json()['model']['ok'] is False
        assert len(calls) == 2
        assert client.get('/api/studio/operations').json()[0]['status'] == 'unknown'
        assert client.post('/api/studio/settings/test?scope=setup').status_code == 409
        assert client.post('/api/studio/settings/test').status_code == 409
        assert len(calls) == 2

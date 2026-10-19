"""New-user journey via public HTTP endpoints and the real leased worker, offline."""

import json
import runpy
import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SUPPORT = runpy.run_path(str(Path(__file__).with_name('distribution_journey_support.py')))
make_app = SUPPORT['make_app']
configure = SUPPORT['configure']
run_name_journey = SUPPORT['run_name_journey']
MODEL_KEY, SEARCH_KEY = SUPPORT['MODEL_KEY'], SUPPORT['SEARCH_KEY']


@pytest.fixture
def journey(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError('Outbound network is forbidden in distribution tests')

    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    app, transport = make_app(tmp_path / 'fresh-data')
    with TestClient(app) as client:
        yield client, app, transport


def test_first_open_reports_configuration_instead_of_blank(journey):
    client, _app, transport = journey
    assert client.get('/').status_code == 200
    settings = client.get('/api/studio/settings')
    assert settings.status_code == 200
    assert settings.json()['model']['ready'] is False
    assert settings.json()['search']['ready'] is False
    assert client.get('/api/studio/projects').json() == []
    tests = client.post('/api/studio/settings/test').json()
    assert '填写模型' in tests['model']['message']
    assert '未配置' in tests['search']['message']
    assert all(call['host'] != 'open.bigmodel.cn' for call in transport.calls)


def test_saving_settings_and_connection_tests_do_not_echo_keys(journey, caplog):
    client, _app, transport = journey
    saved = configure(client)
    tested = client.post('/api/studio/settings/test')
    assert tested.status_code == 200
    assert all(tested.json()[name]['ok'] for name in ('model', 'news', 'search_bocha'))
    settings = client.get('/api/studio/settings')
    for text in (saved.text, tested.text, settings.text, client.get('/').text, caplog.text,
                 json.dumps(transport.calls)):
        assert MODEL_KEY not in text and SEARCH_KEY not in text
    assert settings.json()['commercial']['ready'] == []


@pytest.mark.parametrize(('failure', 'expected'), [
    ('invalid', ('Key', '检查')),
    ('quota', ('额度', '充值')),
    ('offline', ('网络', '检查')),
])
def test_search_failures_are_readable_and_actionable(journey, failure, expected, caplog):
    client, _app, transport = journey
    configure(client)
    transport.search_failure = failure
    response = client.post('/api/studio/settings/test')
    result = response.json()['search_bocha']
    assert result['ok'] is False
    assert all(word in result['message'] for word in expected), result
    assert SEARCH_KEY not in response.text and SEARCH_KEY not in caplog.text


@pytest.mark.parametrize(('failure', 'expected'), [
    ('invalid', ('Key', '检查')),
    ('refusal', ('拒绝', '检查')),
    ('offline', ('结果不明', '不要重复')),
])
def test_model_failures_are_readable_and_not_automatically_resent(journey, failure, expected):
    client, _app, transport = journey
    configure(client)
    transport.model_failure = failure
    response = client.post('/api/studio/settings/test')
    result = response.json()['model']
    assert result['ok'] is False
    assert all(word in result['message'] for word in expected), result
    assert MODEL_KEY not in response.text
    assert sum(c['host'] == 'open.bigmodel.cn' for c in transport.calls) == 1


def test_name_entry_to_first_round_results_without_commercial_sources(journey, caplog):
    client, app, transport = journey
    configure(client)
    pid, detail = run_name_journey(client, app)
    assert detail['monitoring_progress']['state'] == 'observed', detail['jobs']
    assert not [j for j in detail['jobs'] if j['status'] == 'failed'], detail['jobs']
    for category in ('company', 'competitor', 'industry', 'policy'):
        feed = client.get('/api/studio/feed', params={'project_id': pid, 'category': category}).json()
        assert feed, category
    assert detail['brief']['headline'] == '合成公司首轮进展'
    assert detail['counts']['company'] >= 1
    assert not any(c['host'] in ('vip.xiniudata.com', 'mcp-openapi.pharmcube.com', 'mcp.wind.com.cn')
                   for c in transport.calls)
    assert MODEL_KEY not in caplog.text and SEARCH_KEY not in caplog.text


@pytest.mark.parametrize('failure', ['quota', 'offline'])
def test_all_search_failures_keep_visible_non_success_progress(journey, failure):
    client, app, transport = journey
    configure(client)
    transport.search_failure = failure
    transport.news_failure = failure
    _pid, detail = run_name_journey(client, app)
    assert detail['monitoring_progress']['state'] in ('failed', 'partial')
    assert '暂无动态' not in detail['monitoring_progress']['empty_text']
    assert '未见重要变化' not in detail['monitoring_progress']['empty_text']


def test_quota_exhaustion_never_stamps_a_completed_round(journey):
    client, app, transport = journey
    configure(client)
    transport.search_failure = 'quota'
    transport.news_failure = 'quota'
    _pid, detail = run_name_journey(client, app)
    assert detail['monitoring_progress']['state'] == 'partial'  # Known baseline, not a blank result.
    assert detail['plan']['last_report']['company']['errors']
    assert detail['project']['last_external_at'] is None


def test_network_failure_updates_source_health(journey):
    client, app, transport = journey
    configure(client)
    transport.search_failure = 'offline'
    transport.news_failure = 'offline'
    _pid, detail = run_name_journey(client, app)
    assert detail['monitoring_progress']['state'] == 'failed', detail['monitoring_progress']
    assert detail['project']['last_external_at'] is None
    assert any(j['error'] for j in detail['jobs'])
    health = app.state.studio_pipeline.health.read(detail['project']['id'])
    assert health['coverage'] == 'unavailable', health


def test_news_failure_message_never_echoes_network_exception(journey):
    client, _app, transport = journey
    configure(client)
    transport.news_failure = 'offline'
    response = client.post('/api/studio/settings/test')
    assert response.json()['news']['ok'] is False
    assert '检查网络' in response.json()['news']['message']
    assert SEARCH_KEY not in response.text

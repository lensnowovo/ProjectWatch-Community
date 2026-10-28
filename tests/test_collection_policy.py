"""Owner-configured lookback policy, isolated settings and actual host dispatch."""
import json
from dataclasses import asdict

import pytest
import test_collection_host as host_fixtures
from fastapi.testclient import TestClient

from project_watch.studio import collection_host, dsh_runtime
from project_watch.studio.app import create_studio_app
from project_watch.studio.dsh_bootstrap import digest
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http
from project_watch.studio.settings import (
    SettingsStore,
    SettingsUnavailable,
    research_binding_settings,
)


def test_old_settings_are_read_without_rewrite_and_default_binding_is_unchanged(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text('{"refresh_hours":72}', encoding='utf-8')
    before = path.read_bytes()
    store = SettingsStore(path)
    settings = store.load()
    old = asdict(settings)
    old.pop('collection_overlap_days')
    old.pop('collection_baseline_days')
    assert research_binding_settings(settings) == old
    assert settings.collection_overlap_days == 7 and settings.collection_baseline_days == 30
    assert path.read_bytes() == before and not path.with_suffix('.json.bak').exists()
    saved = store.update({'collection_overlap_days': 7, 'collection_baseline_days': 30})
    assert research_binding_settings(saved) == old


@pytest.mark.parametrize(('name', 'value'), [
    ('collection_overlap_days', -1), ('collection_overlap_days', 91),
    ('collection_overlap_days', True), ('collection_overlap_days', 2.5),
    ('collection_overlap_days', '7'), ('collection_overlap_days', None),
    ('collection_baseline_days', 0), ('collection_baseline_days', 366),
    ('collection_baseline_days', False), ('collection_baseline_days', 30.5),
    ('collection_baseline_days', '30'), ('collection_baseline_days', None),
])
def test_invalid_policy_cannot_replace_configuration_or_load_as_defaults(tmp_path, name, value):
    store = SettingsStore(tmp_path / 'settings.json')
    store.update({'refresh_hours': 72})
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.update({name: value})
    assert store.path.read_bytes() == before
    store.path.write_text(json.dumps({name: value}), encoding='utf-8')
    bad = store.path.read_bytes()
    with pytest.raises(SettingsUnavailable):
        store.load()
    assert store.path.read_bytes() == bad


def test_policy_api_preserves_credentials_and_does_not_start_jobs(tmp_path, monkeypatch):
    app = create_studio_app(tmp_path, start_worker=False)
    pipeline = app.state.studio_pipeline
    monkeypatch.setattr(pipeline, 'tools', lambda *_: pytest.fail('settings must not dispatch'))
    with TestClient(app, base_url='http://127.0.0.1:19431') as web:
        first = web.put('/api/studio/settings', json={'model': {
            'base_url': 'https://model.example.invalid/v1', 'model': 'synthetic', 'api_key': 'synthetic-secret'},
            'refresh_hours': 72})
        assert first.status_code == 200
        for overlap, baseline in ((0, 1), (90, 365), (3, 20)):
            saved = web.put('/api/studio/settings', json={
                'collection_overlap_days': overlap, 'collection_baseline_days': baseline})
            assert saved.status_code == 200
            view = web.get('/api/studio/settings').json()
            assert view['collection_overlap_days'] == overlap and view['collection_baseline_days'] == baseline
            assert view['refresh_hours'] == 72 and view['model']['ready']
            assert 'synthetic-secret' not in saved.text
        invalid = web.put('/api/studio/settings', json={'collection_overlap_days': -1})
        assert invalid.status_code == 422
        assert pipeline.settings_store.load().model.api_key == 'synthetic-secret'
        assert web.get('/api/studio/projects').json() == []


def test_legacy_dsh_binding_resumes_with_defaults_without_new_collection(tmp_path, monkeypatch):
    pipeline, execution = host_fixtures.setup.__wrapped__(tmp_path)
    old = asdict(pipeline.settings_store.load())
    old.pop('collection_overlap_days')
    old.pop('collection_baseline_days')
    binding = digest({'entity': 'Synthetic Host', 'settings': old})
    execution.checkpoint('dsh_binding', binding)
    pipeline.http_factory = lambda _: Http(lambda *_: pytest.fail('legacy resume must not add collection'))
    monkeypatch.setattr(dsh_runtime, 'run_dsh', lambda research, *_: research.guard())
    dsh_runtime.run_bootstrap(pipeline, execution, None)
    assert execution.read_checkpoint('dsh_binding') == binding
    assert not execution.usage()


def test_policy_change_during_collection_stops_before_next_request(tmp_path, monkeypatch):
    pipeline, execution = host_fixtures.setup.__wrapped__(tmp_path)
    calls = []

    def send(_method, url, *_):
        calls.append(url)
        pipeline.settings_store.update({'collection_overlap_days': 3})
        return host_fixtures.response(url)

    pipeline.http_factory = lambda _: Http(send)
    monkeypatch.setattr(dsh_runtime, 'run_dsh', lambda *_: pytest.fail('changed policy must stop'))
    with pytest.raises(ValueError, match='dsh_input_changed'):
        dsh_runtime.run_bootstrap(pipeline, execution, None)
    assert len(calls) == 1
    assert execution.read_checkpoint(collection_host.SCOPE)['history_policy'] == dict(overlap_days=7, baseline_days=30)


def test_configured_policy_drives_incremental_and_periodic_host_windows(tmp_path, monkeypatch):
    pipeline, execution = host_fixtures.setup.__wrapped__(tmp_path)
    pipeline.settings_store.update({'collection_overlap_days': 3, 'collection_baseline_days': 2})
    calls = []

    def send(_method, url, *_):
        calls.append(url)
        return host_fixtures.response(url)

    pipeline.http_factory = lambda _: Http(send)
    monkeypatch.setattr(dsh_runtime, 'run_dsh', lambda *_: None)
    plans = []
    for index, day in enumerate((25, 26, 27)):
        monkeypatch.setattr(collection_host, 'now', lambda day=day: f'2026-10-{day}T04:00:00+00:00')
        if index:
            pipeline.store.enqueue(execution.job['project_id'], 'external', automatic=True)
            execution = JobExecution(pipeline.store, pipeline.store.claim())
            pipeline._execution_state.current = execution
            dsh_runtime.run_monitor(pipeline, execution, None)
        else:
            dsh_runtime.run_bootstrap(pipeline, execution, None)
        plan = execution.read_checkpoint('collection_round_v1')['plan']
        assert plan['history_policy'] == dict(overlap_days=3, baseline_days=2)
        plans.append(next(u for u in plan['units'] if u['source'] == 'openalex_topic'))
        assert pipeline.store.finish(execution.job['id'], lease_token=execution.job['lease_token'])
    assert plans[0]['full_scan'] is True
    assert plans[1]['full_scan'] is False and plans[1]['window_start'] == '2026-10-22T04:00:00+00:00'
    assert plans[2]['full_scan'] is True and plans[2]['window_start'] < plans[1]['window_start']
    assert any('2026-10-22' in url for url in calls)

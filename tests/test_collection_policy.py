"""Owner-configured lookback policy, isolated settings and actual host dispatch."""
import json
from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from project_watch.studio.app import create_studio_app
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







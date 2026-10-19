"""Offline regression coverage for the first backend closeout batch."""

import json
import urllib.request

import pytest
from fastapi.testclient import TestClient

from project_watch.studio.app import create_studio_app
from project_watch.studio.host import instance_lock
from project_watch.studio.http import Http, HttpError, urllib_transport
from project_watch.studio.llm import LLM, ModelError
from project_watch.studio.settings import ModelConfig, SettingsStore, StudioSettings


@pytest.mark.parametrize('broken', ['{broken', '[]', '{"model":"bad"}',
                                    '{"search":{"keys":[]}}',
                                    '{"notify":{"weekly":"false"}}'])
def test_corrupt_settings_are_not_overwritten(tmp_path, broken):
    path = tmp_path / 'settings.json'
    store = SettingsStore(path)
    store.update({'model': {'api_key': 'synthetic-key'}, 'refresh_hours': 12})
    path.write_text(broken, encoding='utf-8')
    with pytest.raises(RuntimeError, match='配置'):
        store.load()
    with pytest.raises(RuntimeError, match='配置'):
        store.update({'refresh_hours': 24})
    assert path.read_text(encoding='utf-8') == broken
    assert json.loads(path.with_suffix('.json.bak').read_text())['model']['api_key'] == 'synthetic-key'


def test_unreadable_settings_do_not_become_defaults(tmp_path, monkeypatch):
    path = tmp_path / 'settings.json'
    path.write_text('{}')
    original = type(path).read_text

    def read(self, *args, **kwargs):
        if self == path:
            raise PermissionError('private path must not escape')
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(path), 'read_text', read)
    with pytest.raises(RuntimeError, match='配置') as caught:
        SettingsStore(path).load()
    assert 'private path' not in str(caught.value)


def test_backup_keeps_previous_valid_config_and_direct_save_refuses_corruption(tmp_path):
    path = tmp_path / 'settings.json'
    store = SettingsStore(path)
    store.update({'refresh_hours': 12})
    store.update({'refresh_hours': 24})
    backup = path.with_suffix('.json.bak')
    assert json.loads(backup.read_text())['refresh_hours'] == 12
    path.write_bytes(b'\xff\xfe')
    with pytest.raises(RuntimeError):
        store.save(StudioSettings())
    assert path.read_bytes() == b'\xff\xfe'
    assert json.loads(backup.read_text())['refresh_hours'] == 12


def test_search_key_update_preserves_opted_in_google_news(tmp_path):
    store = SettingsStore(tmp_path / 'settings.json')
    store.update({'search': {'google_news': True}})
    store.update({'search': {'providers': ['bocha'], 'keys': {'bocha': 'synthetic'}}})
    assert store.load().search.google_news is True


@pytest.mark.parametrize('patch', [
    {'model': 'unexpected-string'}, {'model': {'base_url': []}},
    {'model': {'api_key': 123}}, {'search': {'providers': [{}]}},
    {'notify': {'kind': []}}, {'notify': {'weekly': 'false'}},
    {'refresh_hours': None}, {'refresh_hours': True},
    {'commercial': {'keys': {'xiniu': []}}},
])
def test_settings_invalid_shape_is_safe_422(tmp_path, patch):
    app = create_studio_app(tmp_path, start_worker=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.put('/api/studio/settings', json=patch)
        assert response.status_code == 422
        assert 'unexpected-string' not in response.text
    assert not (tmp_path / 'settings.json').exists()


def test_corrupt_settings_api_returns_actionable_503(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    path = tmp_path / 'settings.json'
    path.write_text('{broken-secret')
    with TestClient(app, raise_server_exceptions=False) as client:
        for response in (client.get('/api/studio/settings'),
                         client.put('/api/studio/settings', json={'refresh_hours': 12})):
            assert response.status_code == 503
            assert 'settings_unavailable' in response.text
            assert 'broken-secret' not in response.text
    assert path.read_text() == '{broken-secret'


def test_failed_atomic_replace_preserves_current_settings(tmp_path, monkeypatch):
    import project_watch.studio.settings as module

    path = tmp_path / 'settings.json'
    store = SettingsStore(path)
    store.update({'refresh_hours': 12})
    before = path.read_bytes()
    original = module.os.replace

    def replace(source, target):
        if target == path:
            raise PermissionError('synthetic')
        return original(source, target)

    monkeypatch.setattr(module.os, 'replace', replace)
    with pytest.raises(RuntimeError, match='配置'):
        store.update({'refresh_hours': 24})
    assert path.read_bytes() == before
    assert not list(tmp_path.glob('*.tmp'))


def test_initialization_respects_instance_lock(tmp_path):
    with instance_lock(tmp_path):
        with pytest.raises(RuntimeError):
            create_studio_app(tmp_path, start_worker=False)
        assert not (tmp_path / 'projectwatch.sqlite3').exists()
    app = create_studio_app(tmp_path, start_worker=False)
    with TestClient(app):
        before = (tmp_path / 'projectwatch.sqlite3').read_bytes()
        with pytest.raises(RuntimeError):
            create_studio_app(tmp_path, start_worker=False)
        assert (tmp_path / 'projectwatch.sqlite3').read_bytes() == before


def redirect_handler(monkeypatch):
    """Inspect the actual transport's handlers; never open the network."""
    handlers = []
    original = urllib.request.build_opener

    def build(*args):
        opener = original(*args)
        handlers.extend(opener.handlers)
        return opener

    monkeypatch.setattr(urllib.request, 'build_opener', build)
    urllib_transport()
    return next(h for h in handlers if isinstance(h, urllib.request.HTTPRedirectHandler))


@pytest.mark.parametrize('code', [301, 302, 303, 307, 308])
@pytest.mark.parametrize('target', ['https://other.example/path', 'http://api.example/path',
                                    'https://api.example:444/path',
                                    'https://user:password@api.example/path'])
def test_api_redirect_does_not_forward_credentials(monkeypatch, target, code):
    handler = redirect_handler(monkeypatch)
    request = urllib.request.Request('https://api.example/original', headers={
        'Authorization': 'Bearer synthetic-secret', 'X-Api-Key': 'synthetic-key'})
    with pytest.raises(HttpError) as caught:
        handler.redirect_request(request, None, code, 'Found', {}, target)
    assert caught.value.code == 'unsafe_redirect'
    assert 'secret' not in str(caught.value)


def test_same_origin_https_redirect_retains_headers(monkeypatch):
    handler = redirect_handler(monkeypatch)
    request = urllib.request.Request('https://api.example/original', headers={
        'Authorization': 'Bearer synthetic'})
    redirected = handler.redirect_request(request, None, 302, 'Found', {},
                                          'https://API.example:443/new')
    assert redirected.get_header('Authorization') == 'Bearer synthetic'


def test_model_does_not_retry_blocked_redirect():
    calls = []

    def send(*args):
        calls.append(True)
        raise HttpError('unsafe_redirect')

    model = LLM(ModelConfig(base_url='https://model.example', model='synthetic', api_key='synthetic'), Http(send))
    with pytest.raises(ModelError) as caught:
        model.complete('system', 'user')
    assert caught.value.code == 'unsafe_redirect'
    assert len(calls) == 1

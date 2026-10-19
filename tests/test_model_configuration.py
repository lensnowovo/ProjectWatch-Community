"""Synthetic configuration lifecycle without a DSH import or provider requests."""
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_generic_configuration_does_not_import_dsh_or_legacy_methods():
    import project_watch

    program = '''
import importlib.abc
import sys
sys.path.insert(0, sys.argv[1])
blocked = {'project_watch.dsh_executor', 'project_watch.external_analysis',
           'project_watch.model_settings', 'project_watch.company_state',
           'project_watch.monitor_briefing'}
class RejectLegacy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked:
            raise AssertionError('generic settings imported legacy: ' + fullname)
sys.meta_path.insert(0, RejectLegacy())
from project_watch.model_configuration import ModelConfiguration, ModelInput
from fastapi import HTTPException
settings = ModelConfiguration()
assert settings.status() == {'enabled': False, 'storage': 'session'}
try:
    settings.test(ModelInput(base_url='https://synthetic.example', protocol='openai-completions',
                             model='synthetic', key='synthetic-key'))
except HTTPException as exc:
    assert exc.status_code == 503
else:
    raise AssertionError('missing executor must not silently enable a service')
assert not blocked.intersection(sys.modules)
'''
    result = subprocess.run([sys.executable, '-I', '-c', program,
                             str(Path(project_watch.__file__).parent.parent)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


class SyntheticModel:
    def __init__(self):
        self.receipt = {'requests': 1}

    def disclosure(self):
        return {'enabled': True, 'model': 'synthetic'}

    def propose(self, packet):
        return asdict(packet)


@pytest.fixture
def setup(monkeypatch):
    from project_watch import model_configuration as config

    monkeypatch.setenv('PROJECT_WATCH_SETTINGS_TOKEN', 'synthetic-operator')
    monkeypatch.setattr(config.GuardedHTTPS, '_target', lambda _: None)
    clock = [100.0]
    monkeypatch.setattr(config.time, 'monotonic', lambda: clock[0])
    calls = []
    models = []

    def factory(provider, key, *, max_runs):
        calls.append((provider, key, max_runs))
        model = SyntheticModel()
        models.append(model)
        return model

    old = SyntheticModel()
    settings = config.ModelConfiguration(old, model_factory=factory, executor_name='synthetic')
    app = FastAPI()
    config.attach_model_settings(app, settings)
    client = TestClient(app)
    headers = {'Origin': 'http://testserver', 'X-Settings-Token': 'synthetic-operator'}
    body = {'base_url': 'https://synthetic.example', 'protocol': 'openai-completions',
            'model': 'synthetic', 'key': 'synthetic-secret-only', 'max_runs': 3}
    return config, settings, old, client, headers, body, calls, models, clock


def test_success_single_use_activation_and_status_never_calls_model(setup):
    _, settings, old, client, headers, body, calls, models, _ = setup
    assert client.get('/api/settings/status').json()['executor'] == 'synthetic'
    assert not calls
    revision = settings.revision
    response = client.post('/api/settings/model/test', headers=headers, json=body)
    assert response.status_code == 200 and len(calls) == 1
    assert calls[0][1:] == (body['key'], 3)
    assert settings.get() is old and settings.revision == revision
    assert body['key'] not in response.text
    ticket = response.json()['ticket']
    activated = client.post('/api/settings/model/activate', headers=headers, json={'ticket': ticket})
    assert activated.status_code == 200 and settings.get() is models[0]
    assert settings.revision != revision and body['key'] not in activated.text
    assert client.post('/api/settings/model/activate', headers=headers, json={'ticket': ticket}).status_code == 409
    assert client.get('/api/settings/model', headers=headers).status_code == 200 and len(calls) == 1


def test_expired_ticket_busy_and_rate_limit_keep_previous_model(setup):
    _, settings, old, client, headers, body, calls, _, clock = setup
    settings._lock.acquire()
    try:
        assert client.post('/api/settings/model/test', headers=headers, json=body).status_code == 409
        assert not calls
    finally:
        settings._lock.release()
    ticket = client.post('/api/settings/model/test', headers=headers, json=body).json()['ticket']
    assert client.post('/api/settings/model/test', headers=headers, json=body).status_code == 429
    assert len(calls) == 1
    clock[0] += 601
    assert client.post('/api/settings/model/activate', headers=headers, json={'ticket': ticket}).status_code == 409
    assert settings.get() is old


@pytest.mark.parametrize('failure', ['unknown', 'invalid_binding'])
def test_failure_preserves_configuration_and_does_not_retry(setup, monkeypatch, failure):
    config, settings, old, client, headers, body, calls, _, _ = setup
    sent = []

    def fail(_self, packet):
        sent.append(packet)
        if failure == 'unknown':
            raise config.ExecutorUnavailable(body['key'])
        return {**asdict(packet), 'source_sha256': 'wrong'}

    monkeypatch.setattr(SyntheticModel, 'propose', fail)
    revision = settings.revision
    response = client.post('/api/settings/model/test', headers=headers, json=body)
    assert response.status_code == 502 and body['key'] not in response.text
    assert len(calls) == len(sent) == 1
    assert settings.get() is old and settings.revision == revision and settings._tested is None
    assert not settings._lock.locked()


def test_unauthorized_and_invalid_inputs_never_construct_executor(setup):
    _, _, _, client, headers, body, calls, _, _ = setup
    for wrong in ({}, {**headers, 'Origin': 'https://foreign.example'}, {**headers, 'X-Settings-Token': 'wrong'}):
        assert client.post('/api/settings/model/test', headers=wrong, json=body).status_code == 403
    for wrong in ({**body, 'base_url': 'http://localhost'}, {**body, 'key': 'bad\nsecret'},
                  {**body, 'key': {'synthetic-secret-only': 'wrong-type'}}):
        response = client.post('/api/settings/model/test', headers=headers, json=wrong)
        assert response.status_code == 422 and body['key'] not in response.text
    assert not calls


def test_missing_factory_is_readable_but_not_editable(monkeypatch):
    from project_watch.model_configuration import ModelConfiguration, attach_model_settings

    monkeypatch.setenv('PROJECT_WATCH_SETTINGS_TOKEN', 'synthetic-operator')
    app = FastAPI()
    attach_model_settings(app, ModelConfiguration())
    status = TestClient(app).get('/api/settings/status').json()
    assert status['enabled'] is False and status['editable'] is False


def test_legacy_constructor_retains_default_dsh_factory(monkeypatch):
    from project_watch import model_configuration, model_settings
    from project_watch.dsh_executor import DshProposalModel

    assert model_settings.ModelInput is model_configuration.ModelInput
    assert model_settings.attach_model_settings is model_configuration.attach_model_settings
    monkeypatch.setattr(model_configuration.GuardedHTTPS, '_target', lambda _: None)
    sent = []

    def propose(model, packet):
        sent.append(model)
        return asdict(packet)

    monkeypatch.setattr(DshProposalModel, 'propose', propose)
    settings = model_settings.ModelSettings()
    result = settings.test(model_settings.ModelInput(base_url='https://synthetic.example',
                           protocol='openai-completions', model='synthetic', key='synthetic-key', max_runs=7))
    assert result['status'] == 'ready' and len(sent) == 1
    assert type(sent[0]) is DshProposalModel and sent[0].max_runs == 7
    assert settings.get() is None  # Test does not implicitly activate.
    assert settings.executor_name == 'DSH'

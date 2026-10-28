"""Unavailable DSH holds research visibly; it never runs the legacy collector. Synthetic, offline."""
import json

import pytest
from fastapi.testclient import TestClient

from project_watch.studio import public_executor
from project_watch.studio.app import create_studio_app


@pytest.fixture
def app(tmp_path, monkeypatch):
    for key in (public_executor.EXECUTOR, public_executor.SOURCE, public_executor.UNAVAILABLE,
                'PROJECT_WATCH_DSH_RUNTIME', 'PROJECT_WATCH_DSH_MONITOR', 'PROJECT_WATCH_NODE_VERSION'):
        monkeypatch.delenv(key, raising=False)
    return create_studio_app(tmp_path, start_worker=False)


def seed(app):
    pipeline = app.state.studio_pipeline
    pid = pipeline.repository.create_project('Synthetic Executor').id
    pipeline.store.upsert_project(pid, {}, consent=True)
    return pipeline, pid


@pytest.mark.parametrize('kind', ['run_external', 'run_external_followup', 'run_insight'])
def test_unavailable_never_reaches_legacy_tools(app, monkeypatch, kind):
    pipeline, pid = seed(app)
    monkeypatch.setenv(public_executor.UNAVAILABLE, 'bundled_missing')

    def legacy(*args, **kwargs):
        raise AssertionError('legacy collector must not run')

    monkeypatch.setattr(pipeline, 'tools', legacy)
    if kind == 'run_external_followup':
        class Execution:
            def __init__(self):
                self.job = {'id': 'synthetic', 'project_id': pid}

            def read_checkpoint(self, stage):
                return None
        monkeypatch.setattr(pipeline._execution_state, 'current', Execution(), raising=False)
    with pytest.raises(ValueError, match='dsh_unavailable'):
        getattr(pipeline, kind)({'id': 'synthetic', 'project_id': pid, 'kind': 'external'})


def test_unavailable_does_not_schedule_the_legacy_insight_loop(app, monkeypatch):
    pipeline, pid = seed(app)
    monkeypatch.setenv(public_executor.UNAVAILABLE, 'node_missing')
    assert pipeline.schedule_insight(pid) is None


def test_settings_switch_is_desktop_only_and_saved_for_restart(app, tmp_path, monkeypatch):
    client = TestClient(app)
    view = client.get('/api/studio/settings/executor').json()
    assert view['executor'] == 'classic' and view['configurable'] is False
    assert client.put('/api/studio/settings/executor', json={'enabled': False}).status_code == 409
    assert not (tmp_path/'desktop-runtime.json').exists()

    monkeypatch.setenv(public_executor.EXECUTOR, 'unavailable')
    monkeypatch.setenv(public_executor.SOURCE, 'bundled')
    monkeypatch.setenv(public_executor.UNAVAILABLE, 'node_missing')
    view = client.get('/api/studio/settings/executor').json()
    assert view['reason'] == 'node_missing' and '重新安装' in view['reason_text']
    assert client.put('/api/studio/settings/executor', json={'enabled': 'yes'}).status_code == 422
    saved = client.put('/api/studio/settings/executor', json={'enabled': False}).json()
    assert saved['saved_enabled'] is False
    # The running process keeps its executor until restart.
    assert saved['executor'] == 'unavailable'
    assert json.loads((tmp_path/'desktop-runtime.json').read_text(encoding='utf-8')) == {'dsh_enabled': False}

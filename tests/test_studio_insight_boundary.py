"""Generation is optional; stored/Agent judgments are not. Offline synthetic data."""
import json

import pytest
from fastapi.testclient import TestClient

from project_watch.studio import insight
from project_watch.studio.app import create_studio_app


def test_core_result_module_contains_no_generator():
    for name in ('SYSTEM', 'gather', 'normalize', 'run', 'due'):
        assert not hasattr(insight, name), name


def test_no_generator_keeps_records_but_blocks_all_generation(tmp_path, monkeypatch):
    app = create_studio_app(tmp_path, start_worker=False)
    pipe = app.state.studio_pipeline
    pipe.insight_generator = None
    client = TestClient(app)
    pid = client.post('/api/studio/projects', json={
        'entity': {'legal_name': '合成测试公司'}, 'consent': True,
    }).json()['id']
    pipe.settings_store.update({'model': {'provider': 'openai', 'api_key': 'synthetic-key',
                                           'model': 'synthetic-model'}})
    client.post(f'/api/studio/projects/{pid}/activate')
    record = {'analysis': {'headline': '合成历史判断'}, 'generated_at': '2026-01-01T00:00:00+00:00'}
    past = [{'headline': '合成历史判断', 'generated_at': record['generated_at']}]
    pipe.store.set_state(insight.STATE_KEY.format(pid), json.dumps(record))
    pipe.store.set_state(insight.HISTORY_KEY.format(pid), json.dumps(past))
    before = pipe.store.jobs(pid)
    def no_tools():
        raise AssertionError('Unavailable generation must not construct provider tools')
    monkeypatch.setattr(pipe, 'tools', no_tools)
    with monkeypatch.context() as ctx:
        ctx.setattr(pipe.settings_store, 'load', no_tools)
        assert pipe.schedule_insight(pid) is None  # no evidence gathering or settings work, either
    with pytest.raises(ValueError, match='insight_unavailable'):
        pipe.run_insight({'project_id': pid})
    response = client.post(f'/api/studio/projects/{pid}/refresh', json={'kind': 'insight'})
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'insight_unavailable'
    assert pipe.store.jobs(pid) == before
    view = client.get(f'/api/studio/projects/{pid}/insight').json()
    assert view['generation_available'] is False
    assert view['record'] == record and view['history'] == past
    assert client.get(f'/api/studio/projects/{pid}').json()['capabilities']['insight_generation'] is False


def test_full_app_injects_optional_generator(tmp_path):
    from project_watch.studio import insight_generation

    app = create_studio_app(tmp_path, start_worker=False)
    assert app.state.studio_pipeline.insight_generator is insight_generation


def test_pipeline_dispatches_only_to_injected_generator(tmp_path, monkeypatch):
    from types import SimpleNamespace

    app = create_studio_app(tmp_path, start_worker=False)
    pipe = app.state.studio_pipeline
    calls = []
    model = SimpleNamespace(ready=True)
    pipe.insight_generator = SimpleNamespace(run=lambda p, llm, pid: calls.append((p, llm, pid)))
    monkeypatch.setattr(pipe, 'tools', lambda: (None, None, model, None))
    pipe.run_insight({'project_id': 'synthetic-project'})
    assert calls == [(pipe, model, 'synthetic-project')]

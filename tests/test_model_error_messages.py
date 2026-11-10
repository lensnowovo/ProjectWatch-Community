"""Provider failures use fixed copy; synthetic responses only."""
import json
from types import SimpleNamespace

import pytest
import test_dispatch_rule as fixtures
from distribution_journey_support import SyntheticTransport, configure, make_app
from fastapi.testclient import TestClient
from test_dispatch_rule import job

from project_watch.studio.http import Http, Response
from project_watch.studio.llm import LLM, ModelError
from project_watch.studio.model_errors import ACCOUNT_ERRORS, MESSAGES
from project_watch.studio.pipeline import StudioWorker
from project_watch.studio.settings import ModelConfig


@pytest.fixture
def env(tmp_path, monkeypatch):
    return fixtures.env.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.parametrize('provider_code,expected', [
    ('credit_balance_exhausted', 'model_balance_insufficient'),
    ('insufficient_quota', 'model_account_unavailable'),
    ('project_spend_limit_exceeded', 'model_account_unavailable'),
])
def test_openai_billing_is_not_rate_limit(provider_code, expected):
    from project_watch.studio.model_errors import classify
    response = Response(429, {}, json.dumps({'error': {'code': provider_code}}).encode())
    assert classify(response, 'https://api.openai.com/v1') == expected
    assert classify(response, 'https://synthetic.invalid') is None


def test_glm_1113_with_nonretryable_status_keeps_http_fallback():
    from project_watch.studio.model_errors import classify
    response = Response(400, {}, b'{"error":{"code":"1113"}}')
    assert classify(response, 'https://open.bigmodel.cn/api/coding/paas/v4') is None

CASES = [
    (401, {}, 'model_key_invalid'),
    (403, {}, 'model_access_denied'),
    (402, {}, 'model_account_unavailable'),
    (402, {'type': 'insufficient_balance_error'}, 'model_balance_insufficient'),
    (429, {'code': '1113'}, 'model_resource_unavailable'),
    (429, {}, 'model_rate_limited'),
    (503, {}, 'model_service_unavailable'),
]


@pytest.mark.parametrize('status,error,code', CASES)
def test_settings_endpoint_uses_safe_specific_copy(tmp_path, monkeypatch, status, error, code):
    original = SyntheticTransport.__call__
    calls = []
    def transport(self, method, url, headers, body, timeout):
        if 'open.bigmodel.cn' in url:
            calls.append(1)
            return Response(status, {}, json.dumps({'error': {**error, 'message': 'PRIVATE-secret'}}).encode())
        return original(self, method, url, headers, body, timeout)
    monkeypatch.setattr(SyntheticTransport, '__call__', transport)
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        configure(client)
        result = client.post('/api/studio/settings/test?scope=setup')
        assert result.status_code == 200
        assert result.json()['model'] == {'ok': False, 'message': MESSAGES[code]}
        assert 'PRIVATE-secret' not in result.text
        assert calls == [1]


@pytest.mark.parametrize('body', [b'[]', b'null', b'not json', b'{"error":"secret"}'])
def test_malformed_error_still_has_http_fallback(body):
    model = LLM(ModelConfig(base_url='https://synthetic.invalid', model='synthetic', api_key='fake'),
                Http(transport=lambda *args: Response(401, {}, body)))
    with pytest.raises(ModelError) as failure:
        model.complete('synthetic', 'synthetic')
    assert failure.value.code == 'model_key_invalid'


def test_other_provider_1113_is_not_assumed_to_be_glm_quota():
    model = LLM(ModelConfig(base_url='https://synthetic.invalid', model='synthetic', api_key='fake'),
                Http(transport=lambda *args: Response(429, {}, b'{"error":{"code":"1113"}}')))
    with pytest.raises(ModelError) as failure:
        model.complete('synthetic', 'synthetic')
    assert failure.value.code == 'model_rate_limited'


@pytest.mark.parametrize('code', sorted(ACCOUNT_ERRORS))
def test_worker_preserves_specific_account_reason_and_saved_progress(env, code):
    store, pid = env
    work = job(store, pid, 'page_summary')
    store.finish(work.job['id'], lease_token=work.job['lease_token'])
    queued = store.enqueue(pid, 'page_summary', input_version=code)
    def run(claimed):
        raise ModelError(MESSAGES[code], code=code)
    import threading
    worker = StudioWorker(SimpleNamespace(store=store, run=run, _execution_state=threading.local()), schedules=False)
    assert worker.step()
    with store._connect() as db:
        row = db.execute('SELECT status,stage,retry_after FROM studio_jobs WHERE id=?', (queued['id'],)).fetchone()
        assert row['status'] == 'queued' and row['retry_after']
        assert row['stage'] == MESSAGES[code]

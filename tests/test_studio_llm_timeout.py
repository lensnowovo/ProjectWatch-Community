"""Synthetic provider requests: bound GLM JSON thinking and do not resend long timeouts."""
import json

import pytest

from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.llm import LLM, ModelError
from project_watch.studio.settings import ModelConfig


@pytest.mark.parametrize('host,model,protocol,expected', [
    ('open.bigmodel.cn', 'glm-5.3', 'openai', 'low'),
    ('gateway.example', 'glm-5.3', 'openai', None),
    ('open.bigmodel.cn', 'glm-4.7', 'openai', None),
    ('open.bigmodel.cn', 'glm-5.3', 'anthropic', None),
])
def test_json_reasoning_is_provider_model_and_protocol_scoped(host, model, protocol, expected):
    payloads = []

    def send(method, url, headers, body, timeout):
        payloads.append(json.loads(body))
        data = ({'content': [{'type': 'text', 'text': '{"ok":true}'}]} if protocol == 'anthropic'
                else {'choices': [{'message': {'content': '{"ok":true}'}}]})
        return Response(200, {}, json.dumps(data).encode())

    llm = LLM(ModelConfig(protocol, 'https://' + host + '/v1', model, 'synthetic'), Http(send))
    assert llm.json('s', 'u') == {'ok': True}
    assert payloads[0].get('reasoning_effort') == expected
    assert payloads[0].get('response_format') == ({'type': 'json_object'} if expected else None)
    assert 'thinking' not in payloads[0]
    llm.complete('s', 'u')
    assert 'reasoning_effort' not in payloads[1]
    assert 'response_format' not in payloads[1]


def test_read_timeout_is_not_repeated_or_exposed(monkeypatch):
    calls = []

    def send(*args):
        calls.append(args)
        raise HttpError('connection_failed', detail='The read operation timed out: PRIVATE')

    monkeypatch.setattr('project_watch.studio.llm.time.sleep', lambda _: pytest.fail('unexpected retry'))
    llm = LLM(ModelConfig('openai', 'https://model.example', 'm', 'synthetic'), Http(send))
    with pytest.raises(ModelError) as exc:
        llm.json('s', 'u')
    assert len(calls) == 1
    assert exc.value.code == 'model_timeout'
    assert 'PRIVATE' not in str(exc.value)

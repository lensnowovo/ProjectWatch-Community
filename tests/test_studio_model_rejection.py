"""Synthetic refusal receipts: no provider payload leaks and no automatic resend."""
import json

import pytest

from project_watch.studio.http import Http, Response
from project_watch.studio.llm import LLM, ModelError
from project_watch.studio.settings import ModelConfig


@pytest.mark.parametrize('code', ['1301', 1301])
def test_official_glm_content_refusal_is_named_without_retry_or_payload(code):
    calls = []

    def transport(*args):
        calls.append(args)
        return Response(400, {}, json.dumps({'error': {'code': code,
            'message': 'synthetic-private-text max_tokens sensitive rejection'}}).encode())

    cfg = ModelConfig(base_url='https://open.bigmodel.cn/api/coding/paas/v4', model='glm-5.3', api_key='synthetic')
    with pytest.raises(ModelError) as caught:
        LLM(cfg, Http(transport)).json('synthetic instructions', 'synthetic report')
    assert caught.value.code == 'model_content_rejected'
    assert caught.value.status == 400
    assert 'synthetic-private-text' not in str(caught.value)
    assert len(calls) == 1


def test_same_numeric_code_on_other_provider_is_not_guessed_as_glm_refusal():
    cfg = ModelConfig(base_url='https://model.example/v1', model='m', api_key='synthetic')
    def transport(*args):
        return Response(400, {}, b'{"error":{"code":"1301","message":"invalid parameter"}}')
    with pytest.raises(ModelError) as caught:
        LLM(cfg, Http(transport)).complete('s', 'u')
    assert caught.value.code == 'model_http_failure'

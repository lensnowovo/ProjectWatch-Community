"""Fresh tool requests adapt reasoning; historical wires never become new sends."""
import json
import os
from contextlib import closing
from pathlib import Path

import pytest
import test_collection_host as fixtures
from test_dsh_extraction import role_inputs
from test_material_output_limit import truncated

from project_watch.studio import dsh_extraction, evidence_tasks
from project_watch.studio.http import Http, HttpError, Response


@pytest.fixture
def setup(tmp_path):
    return fixtures.setup.__wrapped__(tmp_path)


def gateway(setup, *, host='open.bigmodel.cn', model='glm-5.3', protocol='openai', cap=8000):
    parent, settings, _, _ = role_inputs(setup)
    settings.model.base_url = 'https://' + host + '/api/paas/v4'
    settings.model.model, settings.model.protocol = model, protocol
    settings.model.max_output_tokens = cap
    sent = []
    http = parent.execution.http(Http(lambda *args: (
        sent.append(json.loads(args[3])) or Response(200, {}, json.dumps(truncated()).encode()))),
        lambda _: ('model', host + settings.model.api_key))
    role = dsh_extraction.Extraction(parent, {'task_id': 'a' * 64, 'refs': {}})
    gate = dsh_extraction.ExtractionGateway(role, settings.model, http)
    body = dict(model=model, messages=[], tools=[{'function': {'name': role.tools[0]}}])
    return gate, body, sent


@pytest.mark.parametrize('host,model,protocol,options', [
    ('open.bigmodel.cn', 'glm-5.3', 'openai', {'reasoning_effort': 'low'}),
    ('gateway.example', 'glm-5.3', 'openai', {}),
    ('open.bigmodel.cn', 'other-model', 'openai', {}),
    ('api.deepseek.com', 'deepseek-v4-pro', 'openai', {'thinking': {'type': 'disabled'}}),
])
def test_fresh_tool_body_only_applies_matching_reasoning_options(setup, host, model, protocol, options):
    gate, body, sent = gateway(setup, host=host, model=model, protocol=protocol, cap=2100)
    gate.model_call(body)
    assert len(sent) == 1 and sent[0]['max_tokens'] == 2100
    assert {k: sent[0][k] for k in ('reasoning_effort', 'thinking') if k in sent[0]} == options
    assert 'response_format' not in sent[0] and sent[0]['tools'] == body['tools']
    assert gate.research.execution.read_checkpoint(gate.research.deferred_stage)['code'] == 'material_output_limit'


def test_non_openai_protocol_still_rejected_before_dispatch(setup):
    with pytest.raises(ValueError, match='OpenAI-compatible'):
        gateway(setup, protocol='anthropic')


@pytest.mark.parametrize('cap', [4000, 8000])
@pytest.mark.parametrize('status', ['completed', 'unknown', 'started'])
def test_legacy_receipt_cannot_turn_into_adapted_new_request(setup, cap, status):
    gate, body, sent = gateway(setup)
    legacy = {**body, 'stream': False, 'max_tokens': cap}
    gate._send_model(legacy)
    if status != 'completed':
        with closing(gate.research.store._connect()) as db, db:
            db.execute('UPDATE studio_job_calls SET status=?', (status,))
        with pytest.raises(HttpError, match='request_outcome_unknown'):
            gate.model_call(body)
    else:
        gate.model_call(body)
        assert gate.research.execution.usage()['model']['cache_hits'] == 1
    assert sent == [legacy]


def test_adapted_length_is_replayed_without_another_call(setup):
    gate, body, sent = gateway(setup)
    gate.model_call(body)
    source = {'task_id': 'a' * 64, 'refs': {}}
    with pytest.raises(evidence_tasks.MaterialDeferred):
        dsh_extraction.extract(gate.research.parent, source, gate.model, gate.http, Path('.'), Path('.'))
    assert len(sent) == 1


@pytest.mark.skipif(not os.environ.get('PW_DSH_TEST_RUNTIME'), reason='requires pinned official DSH')
def test_native_new_wire_preserves_cwd_tools_and_accepts_arguments(setup, tmp_path):
    parent, settings, _, _ = role_inputs(setup)
    settings.model.base_url, settings.model.model = 'https://open.bigmodel.cn/api/paas/v4', 'glm-5.3'
    sent, selected = [], {}
    def send(_method, _url, _headers, raw, _timeout):
        payload = json.loads(raw)
        sent.append(payload)
        assert payload['reasoning_effort'] == 'low' and 'response_format' not in payload
        proposal = {'observations': [{'text': 'Synthetic assertion', 'refs': [next(iter(selected['source']['refs']))]}]}
        return Response(200, {}, json.dumps({'choices': [{'message': {'role': 'assistant', 'content': None,
            'tool_calls': [{'id': 'synthetic', 'type': 'function', 'function': {
                'name': dsh_extraction.TOOL, 'arguments': json.dumps(proposal)}}]}, 'finish_reason': 'tool_calls'}]}).encode())
    parent.pipeline.http_factory = lambda _: Http(send)
    _, http, _, _ = parent.pipeline.tools(execution=parent.execution)
    directory = tmp_path / 'native'
    def extract(source, material):
        selected['source'] = source
        return dsh_extraction.extract(parent, source, settings.model, http,
            Path(os.environ['PW_DSH_TEST_RUNTIME']), directory, execution=material)
    assert evidence_tasks.run_pending(parent.execution, extract) == 1
    assert len(sent) == 1
    expected_cwd = str((directory / ('extract-' + selected['source']['task_id'])).resolve())
    assert json.dumps(expected_cwd) in sent[0]['messages'][-1]['content']

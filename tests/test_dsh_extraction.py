"""Fresh extraction role uses pinned DSH only with synthetic model transport."""
import json
import os
from pathlib import Path

import pytest
import test_collection_host as fixtures

from project_watch.studio import dsh_extraction, evidence_tasks
from project_watch.studio.dsh_bootstrap import PublicBootstrap
from project_watch.studio.http import Http, Response


@pytest.fixture
def setup(tmp_path):
    return fixtures.setup.__wrapped__(tmp_path)


def role_inputs(setup):
    pipeline, execution = setup
    # The active isolated fixture already owns a job; use that lease directly.
    from test_collection_ingest import trial
    from test_collection_plan import candidate, plan

    from project_watch.studio.collection_ingest import CollectionIngest
    from project_watch.studio.collection_view import discovered_sources
    unit = plan([candidate()], project_id=execution.job['project_id'], round_id=execution.job['id'])['units'][0]
    CollectionIngest(execution)(unit, [trial()])
    settings, http, _, collectors = pipeline.tools()
    parent = PublicBootstrap(pipeline, execution, collectors, None)
    source = discovered_sources(pipeline.store, execution.job['project_id'])['items'][0]
    return parent, settings, http, source


def test_roles_have_separate_contexts_and_reject_nonrole_tools(setup):
    parent, settings, http, _ = role_inputs(setup)
    one = dsh_extraction.Extraction(parent, {'task_id': 'a' * 64, 'refs': {'one': 'synthetic'}})
    two = dsh_extraction.Extraction(parent, {'task_id': 'b' * 64, 'refs': {'two': 'synthetic'}})
    assert one.session_id != two.session_id and one.runtime_receipt_stage != two.runtime_receipt_stage
    assert 'one' not in two.task and one.execution is two.execution is parent.execution
    gateway = dsh_extraction.ExtractionGateway(one, settings.model, http)
    with pytest.raises(ValueError, match='unexpected extraction tool'):
        gateway.model_call({'model': settings.model.model, 'tools': [{'function': {'name': 'pw_search'}}]})
    with pytest.raises(ValueError, match='role_tool_denied'):
        one.tool('pw_search', {})
    assert not parent.execution.usage()
    one.tool(dsh_extraction.TOOL, {'observations': []})
    assert two.state['finished'] is None
    assert parent.execution.read_checkpoint('dsh_state') is None


def test_turn_limit_retains_rejected_material_without_new_request(setup):
    parent, settings, http, _ = role_inputs(setup)
    role = dsh_extraction.Extraction(parent, {'task_id': 'c' * 64, 'refs': {}})
    gateway = dsh_extraction.ExtractionGateway(role, settings.model, http)
    gateway.model_requests = 2
    result = gateway.model_call({'model': settings.model.model, 'tools': [{'function': {'name': dsh_extraction.TOOL}}]})
    assert result['choices'][0]['finish_reason'] == 'stop'
    assert role.state['finished']['proposal'] == {'invalid': 'material_turn_limit'}
    assert not parent.execution.usage()
    assert dsh_extraction.Extraction(parent, {'task_id': 'c' * 64, 'refs': {}}).state == role.state


@pytest.mark.skipif(not os.environ.get('PW_DSH_TEST_RUNTIME'), reason='requires pinned official DSH')
def test_pinned_runtime_extracts_with_single_tool_and_parent_journal(setup, tmp_path):
    pipeline, execution = setup
    sends = []
    selected = {}

    def send(_method, _url, _headers, raw, _timeout):
        payload = json.loads(raw)
        sends.append(payload)
        assert [t['function']['name'] for t in payload['tools']] == [dsh_extraction.TOOL]
        # Source metadata is in the fresh role prompt, never a reused conversation.
        source = selected['source']
        assert source['task_id'] in json.dumps(payload['messages'])
        result = {'observations': [{'text': 'Synthetic source assertion', 'refs': [next(iter(source['refs']))]}]}
        return Response(200, {}, json.dumps({'choices': [{'message': {'role': 'assistant', 'content': None,
            'tool_calls': [{'id': 'synthetic-extract', 'type': 'function', 'function': {
                'name': dsh_extraction.TOOL, 'arguments': json.dumps(result)}}]}, 'finish_reason': 'tool_calls'}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 10}}).encode())

    pipeline.http_factory = lambda _: Http(send)
    parent, settings, http, _ = role_inputs(setup)
    def extract(source, _):
        selected['source'] = source
        return dsh_extraction.extract(parent, source, settings.model,
            http, Path(os.environ['PW_DSH_TEST_RUNTIME']), tmp_path / 'role')
    evidence_tasks.run_pending(execution, extract)
    assert len(sends) == 1 and execution.usage()['model']['completed'] == 1
    assert execution.read_checkpoint('dsh_runtime') is None  # parent runtime receipt is not overwritten
    assert execution.read_checkpoint('dsh_state') is None
    assert evidence_tasks.run_pending(execution, lambda *_: pytest.fail('completed stage must not rerun')) == 0

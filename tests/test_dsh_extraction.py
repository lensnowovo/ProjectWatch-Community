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
    def extract(source, material):
        selected['source'] = source
        return dsh_extraction.extract(parent, source, settings.model,
            http, Path(os.environ['PW_DSH_TEST_RUNTIME']), tmp_path / 'role', execution=material)
    evidence_tasks.run_pending(execution, extract)
    assert len(sends) == 1 and execution.usage()['model']['completed'] == 1
    assert execution.read_checkpoint('dsh_runtime') is None  # parent runtime receipt is not overwritten
    assert execution.read_checkpoint('dsh_state') is None
    assert evidence_tasks.run_pending(execution, lambda *_: pytest.fail('completed stage must not rerun')) == 0


@pytest.mark.skipif(not os.environ.get('PW_DSH_TEST_RUNTIME'), reason='requires pinned official DSH')
def test_normal_stop_rejects_first_material_and_continues_second(setup, tmp_path):
    from contextlib import closing

    from test_collection_ingest import trial
    from test_collection_plan import candidate, plan

    from project_watch.studio.collection_ingest import CollectionIngest

    pipeline, execution = setup
    selected, sends = {}, []
    def send(_method, _url, _headers, raw, _timeout):
        sends.append(json.loads(raw))
        if len(sends) == 1:
            message, finish = {'role': 'assistant', 'content': 'No structured submission.'}, 'stop'
        else:
            proposal = {'observations': [{'text': 'Synthetic assertion', 'refs': [next(iter(selected['source']['refs']))]}]}
            message, finish = {'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': 'second-material', 'type': 'function', 'function': {
                    'name': dsh_extraction.TOOL, 'arguments': json.dumps(proposal)}}]}, 'tool_calls'
        return Response(200, {}, json.dumps({'choices': [{'message': message, 'finish_reason': finish}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 10}}).encode())
    pipeline.http_factory = lambda _: Http(send)
    parent, settings, http, _ = role_inputs(setup)
    unit = plan([candidate()], project_id=execution.job['project_id'], round_id=execution.job['id'])['units'][0]
    other = trial(url='https://example.org/second')
    other['extra']['source_identity'] = 'clinical:NCT00000002'
    CollectionIngest(execution)(unit, [other])
    def extract(source, material):
        selected['source'] = source
        return dsh_extraction.extract(parent, source, settings.model, http,
            Path(os.environ['PW_DSH_TEST_RUNTIME']), tmp_path / 'roles', execution=material)
    assert evidence_tasks.run_pending(execution, extract) == 2
    with closing(pipeline.store._connect()) as db:
        assert [r[0] for r in db.execute('SELECT status FROM studio_evidence_tasks ORDER BY rowid')] == ['rejected', 'complete']
        assert db.execute('SELECT count(*) FROM studio_material_calls').fetchone()[0] == 2
    assert len(sends) == 2 and execution.usage()['model']['completed'] == 2
    assert evidence_tasks.run_pending(execution, extract) == 0
    assert len(sends) == 2


def test_persisted_attempt_limit_survives_gateway_restart(setup):
    parent, settings, http, _ = role_inputs(setup)
    source = {'task_id': 'e' * 64, 'refs': {}}
    first = dsh_extraction.Extraction(parent, source)
    parent.execution.checkpoint(first.attempt_stage, ['old-one', 'old-two'])
    restarted = dsh_extraction.Extraction(parent, source)
    gateway = dsh_extraction.ExtractionGateway(restarted, settings.model, http)
    gateway.model_call({'model': settings.model.model, 'messages': [],
                        'tools': [{'function': {'name': dsh_extraction.TOOL}}]})
    assert restarted.state['finished']['proposal'] == {'invalid': 'material_turn_limit'}
    assert not parent.execution.usage()


def test_actual_two_dispatches_remain_capped_across_three_gateways(setup):
    pipeline, execution = setup
    sent = []
    def send(*_args):
        sent.append(1)
        return Response(200, {}, json.dumps({'choices': [{'message': {'role': 'assistant', 'content': 'ordinary text'},
                                                        'finish_reason': 'stop'}]}).encode())
    pipeline.http_factory = lambda _: Http(send)
    parent, settings, http, _ = role_inputs(setup)
    source = {'task_id': 'd' * 64, 'refs': {}}
    for turn in range(3):
        role = dsh_extraction.Extraction(parent, source)
        gateway = dsh_extraction.ExtractionGateway(role, settings.model, http)
        gateway.model_call({'model': settings.model.model,
            'messages': [{'role': 'user', 'content': f'synthetic turn {turn}'}],
            'tools': [{'function': {'name': dsh_extraction.TOOL}}]})
    assert len(sent) == execution.usage()['model']['completed'] == 2
    assert role.state['finished']['proposal'] == {'invalid': 'material_turn_limit'}


@pytest.mark.parametrize('code', ['request_outcome_unknown', 'request_budget', 'dsh_runtime_failed'])
def test_no_submission_never_converts_existing_hold_to_local_rejection(setup, tmp_path, monkeypatch, code):
    from project_watch.studio.http import HttpError

    parent, settings, http, _ = role_inputs(setup)
    source = {'task_id': 'f' * 64, 'refs': {}}
    def ended(role, *_args, **_kwargs):
        role.execution.checkpoint(role.runtime_receipt_stage, dict(initialized=True, prompted=True,
            idle=True, drained=True, runtime_failure=False, error='dsh_no_valid_submission'))
        role.hold(code)
        raise HttpError('dsh_no_valid_submission')
    monkeypatch.setattr(dsh_extraction.dsh_runtime, 'run_dsh', ended)
    with pytest.raises(HttpError, match=code):
        dsh_extraction.extract(parent, source, settings.model, http, tmp_path, tmp_path)
    assert parent.execution.read_checkpoint('extract_result:' + source['task_id']) is None
    assert parent.execution.read_checkpoint('dsh_hold') == {'code': code}

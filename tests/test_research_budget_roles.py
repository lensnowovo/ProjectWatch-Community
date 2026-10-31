"""Actual extraction gateway: settled truncation is distinct from unknown."""
import json
from datetime import UTC, datetime, timedelta

import pytest
from test_collection_host import build
from test_dsh_extraction import role_inputs
from test_research_budget import contract

from project_watch.studio import (
    acceptance_batch,
    bootstrap_collection,
    dsh_extraction,
    event_insight,
)
from project_watch.studio import research_budget as cost
from project_watch.studio.http import Http, HttpError, Response


def admitted(tmp_path, provider, *, output_tokens=20000):
    pipeline, execution = build(tmp_path)
    spec = contract(1_000_000)
    spec['tariffs'][0].update(context_tokens=32000, output_tokens=output_tokens, wire_bytes=131072)
    caps = dict.fromkeys(acceptance_batch.KINDS, 80)
    acceptance_batch.create(pipeline.store, 'roles',
        deadline=(datetime.now(UTC) + timedelta(hours=1)).isoformat(), caps=caps,
        journeys=[dict(project_id=execution.job['project_id'], roots=['first'], caps=caps)],
        plan_sha256='b' * 64, cost_policy=spec)
    acceptance_batch.bind_root(pipeline.store, 'roles', execution.job['id'], 'first')
    pipeline.http_factory = lambda _: Http(provider)
    parent, settings, http, _ = role_inputs((pipeline, execution))
    return parent, settings, http


@pytest.mark.parametrize('second', ['stop', 'length', 'unknown', 'error'])
def test_received_truncation_has_one_durable_correction_and_unknown_never_retries(tmp_path, second):
    sent = []
    def provider(method, url, headers, raw, timeout):
        sent.append(json.loads(raw))
        if len(sent) == 2 and second == 'unknown':
            raise TimeoutError('synthetic lost second reply')
        if len(sent) == 2 and second == 'error':
            return Response(500, {}, b'{}')
        finish = 'length' if len(sent) == 1 else second
        return Response(200, {}, json.dumps(dict(choices=[dict(message=dict(role='assistant',
            content='Synthetic partial text'), finish_reason=finish)],
            usage=dict(prompt_tokens=10, completion_tokens=10))).encode())
    parent, settings, http = admitted(tmp_path, provider)
    source = dict(task_id='f' * 64, refs={})
    role = dsh_extraction.Extraction(parent, source)
    gateway = dsh_extraction.ExtractionGateway(role, settings.model, http)
    payload = dict(model=settings.model.model, messages=[dict(role='user', content='Fixed synthetic source')],
                   tools=[dict(function=dict(name=dsh_extraction.TOOL))])
    if second in ('unknown', 'error'):
        with pytest.raises((TimeoutError, HttpError)):
            gateway.model_call(payload)
        assert parent.execution.read_checkpoint('extract_result:' + source['task_id']) is None
        if second == 'unknown':
            assert parent.execution.usage()['model']['unknown'] == 1
            assert cost.summary(parent.store, 'roles')['retained'] == 144000
        with pytest.raises(HttpError):
            dsh_extraction.ExtractionGateway(dsh_extraction.Extraction(parent, source),
                                             settings.model, http).model_call(payload)
    else:
        result = gateway.model_call(payload)
        assert result['choices'][0]['finish_reason'] == 'stop'
        if second == 'length':
            receipt = parent.execution.read_checkpoint(role.deferred_stage)
            assert receipt['code'] == 'material_output_limit' and receipt['dispatch'] == 'completed'
        else:
            assert parent.execution.read_checkpoint(role.deferred_stage) is None
            dsh_extraction.ExtractionGateway(dsh_extraction.Extraction(parent, source),
                                             settings.model, http).model_call(payload)
            assert parent.execution.usage()['model']['cache_hits'] == 2
    assert len(sent) == 2
    assert [body['max_tokens'] for body in sent] == [10000, 20000]
    assert sent[0]['messages'] == sent[1]['messages']
    assert len(parent.execution.read_checkpoint(role.attempt_stage)) == 2


def test_collection_has_no_turn_or_action_quota_and_keeps_historical_metadata(tmp_path):
    parent, _, _ = admitted(tmp_path, lambda *a: pytest.fail('no provider'))
    role = bootstrap_collection.Collection(parent)
    assert role.limits == dict(turns=None, actions=None, pages=bootstrap_collection.MAX_PAGES)
    assert '模型最多' not in role.prompt and '外部动作最多' not in role.prompt
    parent.execution.budgets['model'] = 5
    reopened = bootstrap_collection.Collection(parent)
    assert reopened.limits == role.limits
    # Historical count remains audit metadata; dispatch does not enforce it.
    assert parent.execution.budgets['model'] == 5


def test_low_output_contract_replays_actual_bounded_wire_after_restart(tmp_path):
    sent = []
    def provider(method, url, headers, raw, timeout):
        sent.append(json.loads(raw))
        return Response(200, {}, json.dumps(dict(choices=[dict(message=dict(role='assistant',
            content='Synthetic received text'), finish_reason='stop')],
            usage=dict(prompt_tokens=10, completion_tokens=10))).encode())
    parent, settings, http = admitted(tmp_path, provider, output_tokens=1000)
    source = dict(task_id='d' * 64, refs={})
    payload = dict(model=settings.model.model, messages=[dict(role='user', content='Fixed source')],
                   tools=[dict(function=dict(name=dsh_extraction.TOOL))])
    for _ in range(2):
        role = dsh_extraction.Extraction(parent, source)
        gateway = dsh_extraction.ExtractionGateway(role, settings.model, http)
        gateway.model_call(payload)
    assert len(sent) == 1 and sent[0]['max_tokens'] == 1000
    assert parent.execution.usage()['model']['cache_hits'] == 1


@pytest.mark.parametrize('legacy', [False, True])
def test_collection_uses_frozen_dynamic_output_without_changing_old_attempts(tmp_path, legacy):
    sent = []
    def provider(method, url, headers, raw, timeout):
        sent.append(json.loads(raw))
        return Response(200, {}, json.dumps(dict(choices=[dict(message=dict(role='assistant', content='Synthetic'),
            finish_reason='stop')], usage=dict(prompt_tokens=10, completion_tokens=10))).encode())
    parent, settings, http = admitted(tmp_path, provider)
    role = bootstrap_collection.Collection(parent)
    role.task = '{"sources":[]}'
    if legacy:
        parent.execution.checkpoint(role.attempt_stage, ['old-attempt-retained'])
    payload = dict(model=settings.model.model, messages=[dict(role='user', content='Synthetic public collection')],
                   tools=[dict(function=dict(name=n)) for n in bootstrap_collection.TOOLS])
    for _ in range(2):
        bootstrap_collection.CollectionGateway(role, settings.model, http).model_call(payload)
    assert len(sent) == 1
    assert sent[0]['max_tokens'] == (2000 if legacy else 10000)
    policy = parent.execution.read_checkpoint('collection_output_policy_v1')
    assert (policy is None) == legacy
    if legacy:
        assert 'old-attempt-retained' in parent.execution.read_checkpoint(role.attempt_stage)


def test_collection_unknown_is_not_resent_with_a_larger_output(tmp_path):
    sent=[]
    def provider(method, url, headers, raw, timeout):
        sent.append(json.loads(raw))
        raise TimeoutError('synthetic ambiguous response')
    parent, settings, http = admitted(tmp_path, provider)
    role = bootstrap_collection.Collection(parent)
    role.task = '{"sources":[]}'
    payload = dict(model=settings.model.model, messages=[dict(role='user', content='Synthetic')],
                   tools=[dict(function=dict(name=n)) for n in bootstrap_collection.TOOLS])
    with pytest.raises((HttpError, TimeoutError)):
        bootstrap_collection.CollectionGateway(role, settings.model, http).model_call(payload)
    with pytest.raises(HttpError):
        bootstrap_collection.CollectionGateway(role, settings.model, http).model_call(payload)
    assert len(sent) == 1 and parent.execution.usage()['model']['unknown'] == 1


@pytest.mark.parametrize('legacy', [False, True])
def test_insight_freezes_dynamic_limits_and_retains_legacy_wire(tmp_path, legacy):
    sent = []
    def provider(method, url, headers, raw, timeout):
        sent.append(json.loads(raw))
        return Response(200, {}, json.dumps(dict(choices=[dict(message=dict(role='assistant', content='Synthetic'),
            finish_reason='stop')], usage=dict(prompt_tokens=10, completion_tokens=10))).encode())
    role, settings, http = admitted(tmp_path, provider)
    role.check_inputs = role.guard
    role.execution.budgets['model'] = 80  # event_insight.run installs the round cap before the gateway.
    role.defer = lambda reason: role.state.update(finished=dict(status='pending', reason=reason))
    if legacy:
        role.execution.checkpoint('object_insight_attempts', ['legacy-attempt'])
    payload = dict(model=settings.model.model, messages=[dict(role='user', content='Synthetic fixed evidence')],
        tools=[dict(function=dict(name=n)) for n in event_insight.TOOLS])
    for _ in range(2):
        event_insight.InsightGateway(role, settings.model, http).model_call(payload)
    assert len(sent) == 1 and sent[0]['max_tokens'] == (8192 if legacy else 10000)
    policy = role.execution.read_checkpoint('object_insight_policy_v1')
    assert (policy is None) == legacy
    if not legacy:
        assert policy['turns'] is None
        # Independent requests continue beyond the historical eight-turn ceiling.
        for i in range(1, 9):
            payload['messages'] = [dict(role='user', content=f'Synthetic turn {i}')]
            event_insight.InsightGateway(role, settings.model, http).model_call(payload)
        assert len(sent) == 9
        assert role.state.get('finished') is None
        assert role.execution.budget_used('model') == 9


def test_insight_unknown_never_gets_new_output_identity(tmp_path):
    sent = []
    def provider(method, url, headers, raw, timeout):
        sent.append(json.loads(raw))
        raise TimeoutError('synthetic unknown')
    role, settings, http = admitted(tmp_path, provider)
    role.check_inputs = role.guard
    role.execution.budgets['model'] = 80
    payload = dict(model=settings.model.model, messages=[dict(role='user', content='Synthetic evidence')],
        tools=[dict(function=dict(name=n)) for n in event_insight.TOOLS])
    for _ in range(2):
        with pytest.raises((HttpError, TimeoutError)):
            event_insight.InsightGateway(role, settings.model, http).model_call(payload)
    assert len(sent) == 1 and role.execution.usage()['model']['unknown'] == 1


@pytest.mark.parametrize('kind, expected', [('collection', 8000), ('insight', 8192)])
def test_normal_new_roles_receive_output_policy_without_acceptance_contract(tmp_path, kind, expected):
    sent = []
    def provider(method, url, headers, raw, timeout):
        sent.append(json.loads(raw))
        return Response(200, {}, json.dumps(dict(choices=[dict(message=dict(role='assistant', content='Synthetic'),
            finish_reason='stop')], usage=dict(prompt_tokens=10, completion_tokens=10))).encode())
    pipeline, execution = build(tmp_path)
    pipeline.http_factory = lambda _: Http(provider)
    parent, settings, http, _ = role_inputs((pipeline, execution))
    execution.budgets['model'] = 16
    if kind == 'collection':
        role = bootstrap_collection.Collection(parent)
        role.task = '{"sources":[]}'
        gateway = bootstrap_collection.CollectionGateway(role, settings.model, http)
        tools = bootstrap_collection.TOOLS
    else:
        role = parent
        role.check_inputs = role.guard
        gateway = event_insight.InsightGateway(role, settings.model, http)
        tools = event_insight.TOOLS
    payload = dict(model=settings.model.model, messages=[dict(role='user', content='Synthetic evidence')],
        tools=[dict(function=dict(name=n)) for n in tools])
    gateway.model_call(payload)
    gateway.model_call(payload)
    assert len(sent) == 1 and sent[0]['max_tokens'] == expected

"""Budget-aware closeout, synthetic evidence and transport only."""
import json
import os
from pathlib import Path

import pytest
import test_studio_dsh_bootstrap as fixtures
from test_studio_dsh_runtime import gateway_for, payload

from project_watch.studio.http import HttpError, Response


@pytest.fixture
def research(tmp_path):
    obj = fixtures.research.__wrapped__(tmp_path)
    obj.execution.checkpoint('collection_admission_v1', {'enabled': True, 'mode': 'first', 'reason': ''})
    obj.execution.budgets['model'] = 10
    return obj


def request(turn):
    return {**payload(), 'messages': [{'role': 'user', 'content': str(turn)}],
            'tools': [{'type': 'function', 'function': {'name': name}}
                      for name in ('pw_search', 'pw_status', 'pw_finish')]}


def transport(seen):
    def send(_method, _url, _headers, body, _timeout):
        seen.append(json.loads(body))
        if 'tools' not in seen[-1]:
            return review_response(seen[-1])
        return Response(200, {}, b'{"choices":[{"message":{"role":"assistant","content":"synthetic"},"finish_reason":"stop"}]}')
    return send


def review_response(body):
    from test_studio_dsh_monitor import synthetic_review
    review = json.loads(body['messages'][-1]['content'])
    assert review['purpose'] == 'public_record_admission'
    return Response(200, {}, json.dumps({'choices': [{'message': {'content': json.dumps(synthetic_review(review))},
        'finish_reason': 'stop'}]}).encode())


def test_status_exposes_real_remaining_budget_without_provider_call(research):
    status = research.tool('pw_status', {})
    assert status['budget']['requests']['model'] == {'limit': 10, 'used': 0, 'remaining': 10}
    assert research.execution.usage() == {}


def test_record_review_cost_enters_closeout_before_it_consumes_submission_turns(research):
    from test_dsh_record_review import prepare

    from project_watch.studio import dsh_closeout, dsh_record_review
    prepare(research, 25)
    research.execution.budgets['model'] = 16
    seen = []
    gateway = gateway_for(research, transport(seen))
    assert dsh_record_review.pending_calls(research) == 4  # Three field batches and consistency.
    for turn in range(4):
        gateway.model_call(request(turn))
        assert 'pw_search' in {t['function']['name'] for t in seen[-1]['tools']}
    gateway.model_call(request(4))
    assert 'pw_search' not in {t['function']['name'] for t in seen[-1]['tools']}
    assert dsh_closeout.budget(research)['reserve_model_calls'] == 12
    dsh_record_review.admit(research, research.state['records'], [])
    # Review used the reserved four calls, leaving seven for submission after
    # the first closeout turn. The old fixed reserve allowed research too long.
    assert research.execution.usage()['model']['requests'] == 9
    assert research.execution.budgets['model'] == 16
    assert dsh_record_review.pending_calls(research) == 0
    assert dsh_closeout.budget(research)['reserve_model_calls'] == 8


def test_review_cost_includes_previous_facts_and_preserves_partial_receipts(research):
    from test_dsh_record_review import prepare
    from test_studio_dsh_monitor import synthetic_review

    from project_watch.studio import dsh_record_review
    prepare(research, 1)
    row = research.state['records'][0]
    old = [{**row, 'label': 'Previous ' + str(i)} for i in range(24)]
    research.store.put_public_profile(research.project_id, {'facts': old})
    assert dsh_record_review.pending_calls(research) == 4
    calls = []
    def interrupted(batch):
        calls.append(batch)
        if len(calls) == 2:
            raise RuntimeError('synthetic pre-dispatch interruption')
        return synthetic_review(batch)
    research.record_reviewer = interrupted
    with pytest.raises(RuntimeError, match='pre-dispatch'):
        dsh_record_review.admit(research, research.state['records'], old)
    assert dsh_record_review.pending_calls(research) == 3
    assert dsh_record_review.pending_calls(research) == 3
    assert len(calls) == 2  # Accounting never dispatches.
    old[0]['label'] = 'Changed input'
    research.store.put_public_profile(research.project_id, {'facts': old})
    assert dsh_record_review.pending_calls(research) == 4


@pytest.mark.parametrize('state', ['no_records', 'published', 'finished', 'topic'])
def test_review_reserve_respects_projection_lifecycle(research, state):
    from test_dsh_record_review import prepare

    from project_watch.studio import dsh_record_review
    from project_watch.studio.dsh_bootstrap import digest
    prepare(research)
    row = research.state['records'][0]
    research.store.put_public_profile(research.project_id,
        {'facts': [{**row, 'label': 'Previous ' + str(i)} for i in range(24)]})
    if state == 'no_records':
        research.state['records'] = []
    elif state == 'published':
        research.state['published'] = digest(research.state['records'])
    elif state == 'finished':
        research.state['finished'] = {'status': 'partial'}
    else:
        research.topic_projection = True
    assert dsh_record_review.pending_calls(research) == (1 if state == 'topic' else 0)
    assert research.execution.usage().get('model', {}).get('requests', 0) == 0


def test_closeout_preserves_cached_model_reply_and_blocks_more_collection(research):
    fixtures.call(research, 'pw_search', query='Synthetic Optics', category='company')
    fixtures.record(research)
    seen = []
    gateway = gateway_for(research, transport(seen))
    original = request(0)
    saved = json.dumps(original)
    gateway.model_call(original)
    gateway.model_call(original)  # Exact request replay remains free even as usage changes.
    assert len(seen) == 1
    assert json.dumps(original) == saved
    gateway.model_call(request(1))
    gateway.model_call(request(2))  # Eight calls left, reserved for structured closeout.
    assert {x['function']['name'] for x in seen[-1]['tools']} == {'pw_finish'}
    assert '"remaining": 8' in seen[-1]['messages'][0]['content']
    assert research.state['finished'] is None
    with pytest.raises(ValueError, match='closeout'):
        research.tool('pw_search', {'query': 'must not dispatch', 'category': 'company'})
    result = research.tool('pw_finish', {'gaps': {'company': 'bounded round'}})
    assert result['status'] == 'partial'
    assert research.store.get_public_profile(research.project_id)['facts']
    assert research.execution.usage()['model']['requests'] == 4  # Three orchestration calls plus required admission.
    gateway.model_call(request(3))  # Finished state needs no final paid prose.
    assert len(seen) == 4


def test_unknown_blocks_closeout_and_model_dispatch(research):
    research.execution.checkpoint('dsh_closeout_v1', {'phase': 'closeout'})
    research.hold('request_outcome_unknown')
    gateway = gateway_for(research, lambda *args: pytest.fail('must not dispatch'))
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        gateway.model_call(request(0))
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        research.tool('pw_finish', {})
    assert research.state['finished'] is None


def test_budget_exhaustion_does_not_fabricate_completion(research):
    research.execution.budgets['model'] = 1
    seen = []
    gateway = gateway_for(research, transport(seen))
    gateway.model_call(request(0))
    with pytest.raises(HttpError, match='request_budget'):
        gateway.model_call(request(1))
    assert len(seen) == 1
    assert research.state['finished'] is None
    assert research.execution.usage()['model']['requests'] == 1


def test_no_remaining_budget_cannot_skip_record_admission(research):
    fixtures.call(research, 'pw_search', query='Synthetic Optics', category='company')
    fixtures.record(research)
    research.execution.budgets['model'] = 0
    gateway_for(research, lambda *args: pytest.fail('must not dispatch'))
    with pytest.raises(HttpError, match='request_budget'):
        research.tool('pw_finish', {'gaps': {}})
    assert research.state['records'] and research.state['finished'] is None
    assert not research.store.get_public_profile(research.project_id)


def test_monitor_closeout_keeps_judgment_and_revision_requirements(tmp_path):
    from test_studio_dsh_monitor import loop
    obj = loop.__wrapped__(tmp_path)
    obj.execution.checkpoint('collection_admission_v1', {'enabled': True, 'mode': 'incremental', 'reason': ''})
    obj.execution.budgets['model'] = 4
    seen = []
    gateway = gateway_for(obj, transport(seen))
    body = request(0)
    body['tools'] = [{'type': 'function', 'function': {'name': n}} for n in obj.tools]
    gateway.model_call(body)
    names = {x['function']['name'] for x in seen[-1]['tools']}
    assert names == {'pw_context'}
    with pytest.raises(ValueError, match='judgment and revision'):
        obj.tool('pw_finish', {'reason': 'cannot bypass'})
    assert obj.state['finished'] is None


def ready_monitor(tmp_path):
    from test_studio_dsh_monitor import collect, judge, loop
    obj = loop.__wrapped__(tmp_path)
    _, _, ref = collect(obj)
    judge(obj, ref)
    obj.tool('pw_revise', {'changes_json': '[]', 'reason': 'Synthetic unchanged scope'})
    obj.execution.checkpoint('collection_admission_v1', {'enabled': True, 'mode': 'incremental', 'reason': ''})
    obj.execution.checkpoint('dsh_closeout_v1', {'phase': 'closeout'})
    obj.execution.budgets['model'] = 1
    seen = []
    gateway = gateway_for(obj, transport(seen))
    gateway.model_call(request('last allowed request'))
    assert len(seen) == 1
    return obj, gateway, seen


def test_exhausted_budget_finishes_validated_monitor_without_another_model(tmp_path):
    obj, gateway, seen = ready_monitor(tmp_path)
    result = gateway.model_call(request('request after revision'))
    assert result['choices'][0]['finish_reason'] == 'stop'
    assert obj.state['finished']['executor'] == 'dsh'
    assert obj.state['finished']['status'] == 'partial'
    assert obj.state['events'][-1]['tool'] == 'pw_finish'
    assert obj.execution.read_checkpoint('dsh_host_finish_v1')['origin'] == 'budget_closeout'
    assert obj.execution.usage()['model']['requests'] == len(seen) == 1
    gateway.model_call(request('sdk final prose'))
    assert len(seen) == 1
    assert sum(e['tool'] == 'pw_finish' for e in obj.state['events']) == 1


@pytest.mark.parametrize('missing', ['judged', 'reviewed'])
def test_exhausted_budget_cannot_finish_incomplete_monitor(tmp_path, missing):
    obj, gateway, seen = ready_monitor(tmp_path)
    obj.state[missing] = None
    obj.save()
    with pytest.raises(HttpError, match='request_budget'):
        gateway.model_call(request('missing prerequisite'))
    assert obj.state['finished'] is None and len(seen) == 1


@pytest.mark.parametrize('hold', ['request_outcome_unknown', 'project_paused'])
def test_ready_budget_finish_does_not_bypass_existing_hold(tmp_path, hold):
    obj, gateway, seen = ready_monitor(tmp_path)
    obj.hold(hold)
    with pytest.raises(HttpError, match=hold):
        gateway.model_call(request('blocked finish'))
    assert obj.state['finished'] is None and len(seen) == 1


def test_ready_budget_finish_still_detects_changed_evidence(tmp_path):
    from test_studio_dsh_monitor import add_public_event
    obj, gateway, seen = ready_monitor(tmp_path)
    add_public_event(obj, 'later', '合成公司新增公开信息')
    gateway.model_call(request('changed evidence'))
    assert obj.state['finished']['status'] == 'partial'
    assert obj.state['finished']['new_evidence_after_judgment']
    assert len(seen) == 1


@pytest.mark.skipif(not os.environ.get('PW_DSH_TEST_RUNTIME'), reason='requires pinned official DSH')
def test_official_dsh_accepts_local_budget_closeout(tmp_path):
    from project_watch.studio.dsh_runtime import run_dsh
    obj, gateway, seen = ready_monitor(tmp_path)
    result = run_dsh(obj, gateway, Path(os.environ['PW_DSH_TEST_RUNTIME']),
                     tmp_path / 'runtime', timeout=120)
    assert result['status'] == 'partial'
    assert obj.execution.read_checkpoint('dsh_host_finish_v1')['origin'] == 'budget_closeout'
    assert obj.execution.read_checkpoint('dsh_runtime')['drained']
    assert obj.execution.usage()['model']['requests'] == len(seen) == 1


@pytest.mark.skipif(not os.environ.get('PW_DSH_TEST_RUNTIME'), reason='requires pinned official DSH')
def test_official_dsh_submits_saved_evidence_in_reserved_turns(research, tmp_path):
    from project_watch.studio.dsh_runtime import run_dsh
    fixtures.call(research, 'pw_search', query='Synthetic Optics', category='company')
    fixtures.record(research)
    research.execution.budgets['model'] = 2  # Submit once plus mandatory admission; no status polling turn.
    calls = []

    def scripted(_method, _url, _headers, raw, _timeout):
        body = json.loads(raw)
        if 'tools' not in body:
            return review_response(body)
        assert {t['function']['name'] for t in body['tools']} == {'pw_finish'}
        index = len(calls)
        calls.append(body)
        assert index == 0
        tool, args = 'pw_finish', {'gaps': {'company': 'synthetic partial'}}
        return Response(200, {}, json.dumps({'choices': [{'message': {
            'role': 'assistant', 'content': None, 'tool_calls': [{'id': str(index), 'type': 'function',
                'function': {'name': tool, 'arguments': json.dumps(args)}}]}, 'finish_reason': 'tool_calls'}]}).encode())

    result = run_dsh(research, gateway_for(research, scripted),
                     Path(os.environ['PW_DSH_TEST_RUNTIME']), tmp_path / 'runtime', timeout=120)
    assert result['status'] == 'partial'
    assert len(calls) == 1
    assert research.execution.usage()['model']['requests'] == 2
    assert research.execution.read_checkpoint('dsh_runtime')['drained']
    assert research.store.get_public_profile(research.project_id)['facts']


def test_closeout_replay_freezes_submission_choice_and_missing_tool_stops(research):
    from project_watch.studio.dsh_closeout import prepare_model
    research.execution.budgets['model'] = 4
    body = request('closeout')
    first = prepare_model(research, body)
    research.state['events'].append({'tool': 'pw_status', 'ordinal': 1})
    assert prepare_model(research, body) == first
    assert [t['function']['name'] for t in first['tools']] == ['pw_finish']
    assert body['tools'] != first['tools']
    missing = request('missing required submission')
    missing['tools'] = [t for t in missing['tools'] if t['function']['name'] != 'pw_finish']
    with pytest.raises(HttpError, match='dsh_tools_unavailable'):
        prepare_model(research, missing)
    assert research.execution.usage() == {}


def test_legacy_frozen_closeout_advice_preserves_request_fingerprint(research):
    import hashlib

    from project_watch.studio.dsh_closeout import budget, prepare_model
    body = request('frozen before upgrade')
    advice = {**budget(research), 'phase': 'closeout'}
    key = 'dsh_budget_advice:' + hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    research.execution.checkpoint(key, advice)
    result = prepare_model(research, body)
    assert {t['function']['name'] for t in result['tools']} == {'pw_status', 'pw_finish'}
    assert research.execution.read_checkpoint(key) == advice


def test_closeout_monitor_routes_by_validated_prerequisites(tmp_path):
    from test_studio_dsh_monitor import collect, judge, loop

    from project_watch.studio.dsh_closeout import next_submission
    obj = loop.__wrapped__(tmp_path)
    assert next_submission(obj) == 'pw_context'
    _, _, ref = collect(obj)
    assert next_submission(obj) == 'pw_assess'
    for pending in obj.pending_assessments(obj.frozen()):
        obj.tool('pw_assess', {'watch_id': pending['watch_id'], 'refs': pending['refs'],
                              'status': 'insufficient', 'note': 'Synthetic unknown'})
    assert next_submission(obj) == 'pw_judge'
    judge(obj, ref)
    assert next_submission(obj) == 'pw_revise'
    obj.tool('pw_revise', {'changes_json': '[]', 'reason': 'Synthetic unchanged scope'})
    assert next_submission(obj) == 'pw_finish'
    assert not obj.state['finished']  # Routing alone never submits a result.


def test_closeout_refreshes_unpublished_or_changed_context(tmp_path):
    from test_studio_dsh_monitor import add_public_event, collect, loop

    from project_watch.studio.dsh_closeout import next_submission
    obj = loop.__wrapped__(tmp_path)
    collect(obj)
    add_public_event(obj, 'changed', 'Synthetic newer evidence')
    assert next_submission(obj) == 'pw_context'
    obj.tool('pw_context', {})
    obj.state['records'].append({'unpublished': 'synthetic'})
    assert next_submission(obj) == 'pw_context'

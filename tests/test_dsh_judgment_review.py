"""Synthetic admission/receipt tests, not a claim that a stub proves semantics."""
import json
from copy import deepcopy

import pytest
from test_studio_dsh_monitor import collect, synthetic_review
from test_studio_dsh_monitor import loop as loop_fixture

from project_watch.studio import dsh_judgment_review as review
from project_watch.studio import insight
from project_watch.studio.dsh_bootstrap import ToolValidationError
from project_watch.studio.dsh_runtime import Gateway, guarded_http
from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.settings import ModelConfig

loop = loop_fixture


def draft(loop):
    _, _, ref = collect(loop)
    for row in loop.pending_assessments(loop.frozen()):
        loop.tool('pw_assess', {'watch_id': row['watch_id'], 'refs': row['refs'],
                              'status': 'insufficient', 'note': '合成场景：保留未知'})
    return {'headline': '取得许可，量产仍在计划中', 'operations': {
        'verdict': 'improving', 'summary': '取得许可，量产仍在计划中',
        'for': [{'text': '取得许可，量产仍在计划中', 'refs': [ref]}]}}


def submit(loop, data):
    return loop.tool('pw_judge', {'analysis_json': json.dumps(data)})


def attach(loop, send):
    http = guarded_http(loop, loop.execution.http(Http(send), lambda _: ('model', 'synthetic')))
    return Gateway(loop, ModelConfig(api_key='synthetic', model='synthetic',
        base_url='https://example.invalid/v1'), http)


def response(value, finish='stop'):
    return Response(200, {}, json.dumps({'choices': [{'message': {'role': 'assistant',
        'content': json.dumps(value)}, 'finish_reason': finish}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}).encode())


def test_actual_gateway_reviews_before_save_without_generation_history(loop):
    data = draft(loop)
    calls = []
    def send(method, url, headers, raw, timeout):
        body = json.loads(raw)
        assert 'tools' not in body and len(body['messages']) == 2
        assert body['messages'][0]['content'] in (review.SYSTEM, review.CONSISTENCY_SYSTEM)
        assert not insight.load(loop.store, loop.project_id)
        payload = json.loads(body['messages'][1]['content'])
        assert payload['fields']
        assert payload['evidence'] == loop.frozen()['numbered']
        calls.append(payload)
        return response(synthetic_review(payload))
    attach(loop, send)
    result = submit(loop, data)
    assert result['saved'] and result['evidence_review']['passed']
    assert insight.load(loop.store, loop.project_id)['analysis']['headline'] == data['headline']
    covered = [call['field_paths'][p] for call in calls if call['phase'] == 'fields' for p in call['fields']]
    assert len(covered) == len(set(covered))
    assert 'headline' in covered and 'operations.verdict' in covered
    assert all(len(call['fields']) <= review.MAX_FIELDS for call in calls if call['phase'] == 'fields')
    assert calls[-1]['phase'] == 'consistency'
    count = len(calls)
    assert loop.execution.usage()['model']['completed'] == count
    assert submit(loop, data)['saved'] and len(calls) == count


def test_unavailable_reviewer_cannot_save(loop):
    assert submit(loop, draft(loop))['error'] == 'judgment_review_unavailable'
    assert not loop.state.get('judged') and not insight.load(loop.store, loop.project_id)


def test_rejection_is_cached_then_corrected_draft_is_reviewed_before_save(loop):
    data = draft(loop)
    calls = []
    def reviewer(payload):
        calls.append(payload)
        result = synthetic_review(payload)
        if len(calls) == 1:
            result['checks'][0].update(status='contradicted', reason='合成反证：结论范围超出所引用证据')
        return result
    loop.judgment_reviewer = reviewer
    for _ in range(2):
        with pytest.raises(ToolValidationError) as error:
            submit(loop, data)
        assert error.value.issues[0]['field'] == 'headline'
        assert not loop.state.get('judged') and not insight.load(loop.store, loop.project_id)
        assert not loop.store.get_state('dsh-judgment-proof:' + loop.project_id)
    assert len(calls) == 1
    data['headline'] = '量产仍在计划中'
    assert submit(loop, data)['saved']
    assert len(calls) > 2  # Corrected draft runs every batch plus cross-section review.


def test_two_draft_limit_does_not_publish_or_dispatch_third(loop):
    data = draft(loop)
    calls = []
    def reviewer(payload):
        calls.append(1)
        result = synthetic_review(payload)
        result['checks'][0].update(status='insufficient', reason='合成证据不足')
        return result
    loop.judgment_reviewer = reviewer
    for text in ('候选判断甲', '候选判断乙'):
        data['headline'] = text
        with pytest.raises(ToolValidationError):
            submit(loop, data)
    data['headline'] = '候选判断丙'
    assert submit(loop, data)['error'] == 'judgment_review_limit'
    assert len(calls) == 2 and not insight.load(loop.store, loop.project_id)
    with pytest.raises(HttpError, match='judgment_review_limit'):
        submit(loop, data)


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'unknown_field', 'reference', 'quote', 'empty_quote', 'no_evidence', 'status'])
def test_invalid_review_fails_closed(loop, change):
    data = draft(loop)
    def reviewer(payload):
        result = synthetic_review(payload)
        first = result['checks'][0]
        if change == 'missing':
            result['checks'].pop()
        elif change == 'duplicate':
            result['checks'].append(deepcopy(first))
        elif change == 'unknown_field':
            first['field'] = 'unprovided.field'
        elif change == 'reference':
            first['evidence'][0]['ref'] = 'invented'
        elif change == 'quote':
            first['evidence'][0]['quote'] = 'not present in source'
        elif change == 'empty_quote':
            first['evidence'][0]['quote'] = ' '
        elif change == 'no_evidence':
            first['evidence'] = []
        else:
            first['status'] = 'approved'
        return result
    loop.judgment_reviewer = reviewer
    assert submit(loop, data)['error'] == 'judgment_review_invalid'
    assert not insight.load(loop.store, loop.project_id)
    with pytest.raises(HttpError, match='judgment_review_invalid'):
        submit(loop, data)


@pytest.mark.parametrize('failure', ['budget', 'unknown', 'truncated', 'invalid_json'])
def test_review_uses_existing_request_guards(loop, failure):
    data, calls = draft(loop), []
    if failure == 'budget':
        loop.execution.budgets['model'] = 0
    def send(*args):
        calls.append(1)
        if failure == 'unknown':
            raise OSError('synthetic lost response')
        if failure == 'invalid_json':
            return response('not an object')
        return response({}, finish='length')
    attach(loop, send)
    try:
        result = submit(loop, data)
        assert 'error' in result
    except HttpError:
        pass
    assert not insight.load(loop.store, loop.project_id)
    assert len(calls) == (0 if failure == 'budget' else 1)
    with pytest.raises(HttpError):
        submit(loop, data)
    assert len(calls) == (0 if failure == 'budget' else 1)
    if failure == 'unknown':
        assert loop.execution.usage()['model']['unknown'] == 1


def test_context_change_while_reviewing_prevents_save(loop, monkeypatch):
    data = draft(loop)
    def reviewer(payload):
        monkeypatch.setattr(loop, 'frozen', lambda: {'fingerprint': 'changed'})
        return synthetic_review(payload)
    loop.judgment_reviewer = reviewer
    with pytest.raises(ValueError, match='inputs changed'):
        submit(loop, data)
    assert not insight.load(loop.store, loop.project_id)


def test_field_coverage_includes_each_assertion_and_uncertainty():
    analysis = {'headline': '一项比较结论', 'technology': {'route': '方案甲',
                'maturity': 'unclear', 'risks': [{'text': '原始材料尚不足', 'refs': ['N1']}],
                'change_if': ['若有可比数据再判断']}, 'gaps': ['尚缺独立来源']}
    payload = review.review_input(analysis, {'numbered': {'N1': {'context': '材料'}}})
    assert set(payload['fields']) == {'headline', 'technology.route', 'technology.maturity',
        'technology.risks.0.text', 'technology.change_if.0', 'gaps.0'}
    assert payload['analysis']['technology']['risks'][0]['refs'] == ['N1']


def test_topic_review_binds_the_selected_question_without_global_fields():
    payload = review.review_input({'watch_id': 'q', 'text': '尚无答案', 'status': 'insufficient',
        'gaps': ['缺少资料']}, {'numbered': {}, 'watches': [{'id': 'q', 'text': '是否满足条件？'}]})
    assert set(payload['fields']) == {'text', 'status', 'gaps.0'}
    assert payload['question'] == '是否满足条件？'


def test_old_unreviewed_checkpoint_cannot_resume_as_accepted(loop):
    from project_watch.studio.dsh_monitor import PublicMonitor

    data = draft(loop)
    loop.judgment_reviewer = synthetic_review
    assert submit(loop, data)['saved']
    old = {k: v for k, v in loop.state.items() if k != 'judgment_review_version'}
    loop.execution.checkpoint('dsh_state', old)
    with pytest.raises(HttpError, match='judgment_review_required'):
        PublicMonitor(loop.pipeline, loop.execution, loop.collectors, loop.reader)


class MemoryExecution:
    def __init__(self):
        self.receipts = {}

    def read_checkpoint(self, key):
        return deepcopy(self.receipts.get(key))

    def checkpoint(self, key, value):
        self.receipts[key] = deepcopy(value)

    checkpoint_chunked = checkpoint


def bounded_research(candidate, evidence=None):
    from types import SimpleNamespace
    ctx = {'fingerprint': 'frozen', 'numbered': evidence or {'N1': {'context': 'Synthetic evidence'}}}
    execution = MemoryExecution()
    research = SimpleNamespace(execution=execution, binding='synthetic', frozen=lambda: deepcopy(ctx),
                               guard=lambda: None, hold=lambda code: None, judgment_reviewer=synthetic_review)
    return research, ctx


def large_candidate():
    return {'headline': '合成标题', 'operations': {'for': [
        {'text': f'合成字段 {i}', 'refs': ['N1']} for i in range(27)]}}


def test_batches_preserve_all_fields_and_counterevidence_without_truncation():
    candidate = large_candidate()
    evidence = {'N1': {'context': 'support'}, 'N2': {'context': 'counterevidence not cited'}}
    payload = review.review_input(candidate, {'numbered': evidence})
    batches = review.review_batches(payload)
    assert len(batches) == 4
    assert all(b['analysis'] == payload['analysis'] and b['evidence'] == evidence for b in batches)
    paths = [p for b in batches[:-1] for p in b['fields']]
    assert paths == list(payload['fields']) and len(paths) == len(set(paths)) == 28
    assert batches[-1]['phase'] == 'consistency' and batches[-1]['fields'] == payload['fields']


def test_cross_section_conflict_prevents_admission_after_field_checks_pass():
    candidate = large_candidate()
    research, ctx = bounded_research(candidate)
    calls = []
    def reviewer(payload):
        calls.append(payload['phase'])
        if payload['phase'] == 'consistency':
            return {'consistent': False, 'reason': 'contradiction', 'conflicts': [
                {'fields': ['headline', 'operations.for.0.text'], 'reason': 'conflicting scopes'}]}
        return synthetic_review(payload)
    research.judgment_reviewer = reviewer
    with pytest.raises(ToolValidationError) as exc:
        review.verify(research, candidate, ctx)
    assert exc.value.issues[0]['code'] == 'evidence_cross_section_conflict'
    assert calls == ['fields', 'fields', 'fields', 'consistency']
    with pytest.raises(ToolValidationError):
        review.verify(research, candidate, ctx)
    assert len(calls) == 4


def test_completed_batches_reused_only_for_exact_input_after_local_interruption():
    candidate = large_candidate()
    research, ctx = bounded_research(candidate)
    dispatched = []
    def reviewer(payload):
        if len(dispatched) == 1:
            raise RuntimeError('local stop before dispatch')
        dispatched.append(deepcopy(payload))
        return synthetic_review(payload)
    research.judgment_reviewer = reviewer
    with pytest.raises(RuntimeError):
        review.verify(research, candidate, ctx)
    assert len(dispatched) == 1
    def finish(payload):
        dispatched.append(deepcopy(payload))
        return synthetic_review(payload)
    research.judgment_reviewer = finish
    assert review.verify(research, candidate, ctx)['passed']
    assert len(dispatched) == 4 and dispatched[0]['fields'] != dispatched[1]['fields']
    changed = deepcopy(candidate)
    changed['headline'] = '新候选'
    assert review.verify(research, changed, ctx)['passed']
    assert len(dispatched) == 8


def test_changed_context_stops_before_next_batch():
    candidate = large_candidate()
    research, ctx = bounded_research(candidate)
    calls = []
    def reviewer(payload):
        calls.append(1)
        research.frozen = lambda: {'fingerprint': 'new source'}
        return synthetic_review(payload)
    research.judgment_reviewer = reviewer
    with pytest.raises(ValueError, match='inputs changed'):
        review.verify(research, candidate, ctx)
    assert len(calls) == 1


@pytest.mark.parametrize('value', [
    {'consistent': True, 'reason': 'x', 'conflicts': [{'fields': ['headline', 'fake'], 'reason': 'x'}]},
    {'consistent': False, 'reason': 'x', 'conflicts': []},
    {'consistent': 'true', 'reason': 'x', 'conflicts': []},
    {'consistent': True, 'reason': '', 'conflicts': []},
    {'consistent': False, 'reason': 'x', 'conflicts': [{'fields': ['headline', 'headline'], 'reason': 'x'}]},
])
def test_malformed_consistency_cannot_pass(value):
    payload = review.review_input(large_candidate(), {'numbered': {}})
    with pytest.raises(ValueError):
        review.validate_consistency(value, payload)


def test_plan_limits_reject_instead_of_silently_dropping_fields():
    with pytest.raises(ValueError, match='field_too_large'):
        review.review_batches({'fields': {'headline': 'x' * (review.MAX_FIELD_CHARS + 1)}})
    with pytest.raises(ValueError, match='plan_limit'):
        review.review_batches({'fields': {str(i): 'x' for i in range(300)}})


def test_second_batch_unknown_stops_all_later_calls_and_cannot_resubmit(loop, monkeypatch):
    data = draft(loop)
    monkeypatch.setattr(review, 'MAX_FIELDS', 2)
    calls = []
    def send(method, url, headers, raw, timeout):
        calls.append(json.loads(raw))
        if len(calls) == 2:
            raise HttpError('connection_failed', detail='SECRET', transport_phase='response_body', failure_kind='timeout')
        return response(synthetic_review(json.loads(json.loads(raw)['messages'][1]['content'])))
    attach(loop, send)
    with pytest.raises(HttpError):
        submit(loop, data)
    assert len(calls) == 2 and loop.execution.usage()['model']['unknown'] == 1
    assert not insight.load(loop.store, loop.project_id)
    with pytest.raises(HttpError):
        submit(loop, data)
    assert len(calls) == 2
    with loop.store._connect() as db:
        diagnostics = [json.loads(r[0]) for r in db.execute("SELECT data FROM studio_job_checkpoints WHERE stage LIKE 'call_error:%'")]
    assert len(diagnostics) == 1 and diagnostics[0]['phase'] == 'response_body'
    assert diagnostics[0]['failure'] == 'timeout' and 'SECRET' not in json.dumps(diagnostics)


@pytest.mark.parametrize('host,model,expected', [
    ('https://open.bigmodel.cn/api/coding/paas/v4', 'glm-5.3', {'reasoning_effort': 'low'}),
    ('https://api.deepseek.com/v1', 'deepseek-v4-pro', {'thinking': {'type': 'disabled'}, 'response_format': {'type': 'json_object'}}),
    ('https://example.invalid/v1', 'other', {}),
])
def test_review_gateway_has_bounded_provider_compatible_options(loop, host, model, expected):
    calls = []
    def send(method, url, headers, raw, timeout):
        body = json.loads(raw)
        calls.append(body)
        assert body['max_tokens'] == review.OUTPUT_TOKENS and timeout == 300
        for key in ('reasoning_effort', 'thinking', 'response_format'):
            assert body.get(key) == expected.get(key)
        return response(synthetic_review(json.loads(body['messages'][1]['content'])))
    gateway = attach(loop, send)
    gateway.model = ModelConfig(api_key='synthetic', model=model, base_url=host)
    gateway.review_judgment(review.review_input({'headline': '合成'}, {'numbered': {'N1': {'context': '合成'}}}))
    assert len(calls) == 1


@pytest.mark.parametrize('where', ['connect_or_headers', 'response_body'])
def test_transport_timeout_preserves_phase_without_changing_unknown_semantics(monkeypatch, where):
    import urllib.error
    from types import SimpleNamespace

    from project_watch.studio import http
    class Reply:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self, size):
            raise TimeoutError('synthetic private diagnostic')
    def open_request(*args, **kwargs):
        if where == 'connect_or_headers':
            raise urllib.error.URLError(TimeoutError('synthetic private diagnostic'))
        return Reply()
    monkeypatch.setattr(http.urllib.request, 'build_opener', lambda *args: SimpleNamespace(open=open_request))
    with pytest.raises(HttpError) as caught:
        http.urllib_transport()('POST', 'https://example.invalid', {}, b'{}', 300)
    assert caught.value.code == 'connection_failed'
    assert caught.value.transport_phase == where and caught.value.failure_kind == 'timeout'

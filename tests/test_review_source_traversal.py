"""Large evidence review traverses the tail, preserves counterevidence and receipts."""
import json
from types import SimpleNamespace

import pytest

from project_watch.studio import review_source_traversal as r
from project_watch.studio.llm import ModelError


class Journal:
    def __init__(self):
        self.saved = {}

    def read_checkpoint(self, key):
        return self.saved.get(key)

    def checkpoint_chunked(self, key, value):
        self.saved[key] = json.loads(json.dumps(value))


@pytest.mark.parametrize('version', [1, 2])
def test_review_protocol_freezes_metric_scope_and_replays_receipts(version):
    from project_watch.studio import analyze_job
    journal = Journal()
    journal.saved[analyze_job.REVIEW_PROTOCOL] = {'version': version}
    sent = []
    def model(system, user, **kwargs):
        sent.append(system)
        assert (r.METRIC_SCOPE in system) == (version == 2)
        data = json.loads(user)
        if 'checked_fields' in system:
            return {'checked_fields': list(data['fields']), 'checks': []}
        return answer(data)
    reviewer = analyze_job._reviewer(None, journal, SimpleNamespace(json=model), lambda: None)
    data = payload()
    data['analysis'] = {'operations': {'summary': 'synthetic assertion', 'for': [{'text': 'synthetic assertion', 'refs': ['S0']}]}}
    assert reviewer.prepare(data)['issues'] == []
    reviewer('source review', data)
    count = len(sent)
    reviewer.prepare(data)
    reviewer('source review', data)
    assert len(sent) == count


def test_legacy_unknown_request_is_not_replaced_with_new_review_prompt():
    from project_watch.studio import analyze_job
    journal = Journal()
    prompts = []
    def model(system, user, **kwargs):
        prompts.append(system)
        raise ModelError('retained unknown', code='unknown_not_resent')
    reviewer = analyze_job._reviewer(None, journal, SimpleNamespace(json=model), lambda: None)
    for _ in range(2):
        with pytest.raises(ModelError, match='retained unknown'):
            reviewer.prepare(payload())
    assert prompts[0] == prompts[1] == r.COUNTER_SYSTEM + r.review.SPAN_GUIDANCE


def payload():
    return {'phase': 'fields', 'analysis': {'operations': {'summary': 'synthetic assertion'}},
            'fields': {'operations.summary': 'synthetic assertion'},
            'evidence': {f'S{i}': {'context': ('SOURCE' + str(i) + ' ') * 500, 'title': f'title {i}'} for i in range(24)}}


def answer(body, status='supported'):
    ref, source = next(iter(body['evidence'].items()))
    return {'checks': [{'field': f, 'status': status, 'inference': 'direct', 'reason': 'synthetic',
                       'evidence': [{'ref': ref, 'quote': source['context'][:30]}]}
                      for f in body['fields']]}


def test_all_sources_with_actual_serialized_bound_and_resume():
    journal, sent = Journal(), []
    def model(system, user, **kwargs):
        assert len(system) + len(user) <= r.MAX_REQUEST_CHARS
        body = json.loads(user)
        sent.extend(body['evidence'])
        return answer(body)
    data = payload()
    first = r.run(journal, SimpleNamespace(json=model), 'system', data, lambda: None)
    assert sent == list(data['evidence'])
    assert len(journal.saved) > 1
    assert first['checks'][0]['status'] == 'supported'
    assert r.run(journal, SimpleNamespace(json=model), 'system', data, lambda: None) == first
    assert sent == list(data['evidence'])


def test_tail_counterevidence_vetoes_earlier_support():
    def model(system, user, **kwargs):
        body = json.loads(user)
        return answer(body, 'contradicted' if 'S23' in body['evidence'] else 'supported')
    result = r.run(Journal(), SimpleNamespace(json=model), 'system', payload(), lambda: None)
    assert result['checks'][0]['status'] == 'contradicted'


def test_missing_support_in_other_chunks_does_not_erase_valid_support():
    def model(system, user, **kwargs):
        body = json.loads(user)
        return answer(body, 'supported' if 'S0' in body['evidence'] else 'insufficient')
    result = r.run(Journal(), SimpleNamespace(json=model), 'system', payload(), lambda: None)
    assert result['checks'][0]['status'] == 'supported'


def test_invalid_quote_cannot_supply_support():
    def model(system, user, **kwargs):
        result = answer(json.loads(user))
        result['checks'][0]['evidence'][0]['quote'] = 'FABRICATED'
        return result
    result = r.run(Journal(), SimpleNamespace(json=model), 'system', payload(), lambda: None)
    assert result['checks'][0]['status'] == 'insufficient'


def test_partial_support_is_not_promoted_to_a_full_summary_across_chunks():
    def model(system, user, **kwargs):
        result = answer(json.loads(user))
        result['checks'][0]['inference'] = 'partial'
        return result
    result = r.run(Journal(), SimpleNamespace(json=model), 'system', payload(), lambda: None)
    assert result['checks'][0]['status'] == 'insufficient'


def long_payload():
    data = payload()
    data['evidence'] = {f'S{i}': {'context': ('SOURCE' + str(i) + ' ') * 500, 'title': f'title {i}'} for i in range(42)}
    return data


def recording(calls, fail=None, code='request_outcome_unknown', invalid=None):
    def model(system, user, **kwargs):
        body = json.loads(user)
        evidence = set(body['evidence'])
        calls.append(evidence)
        if evidence == fail:
            raise ModelError('synthetic failure', code=code)
        if evidence == invalid:
            return {'checks': []}  # no verdict for the field
        return answer(body)
    return model


def test_unknown_keeps_completed_receipts_and_stops_tail():
    """Requests of one wave go out together; after an unknown one no further request is sent,
    and the answers already received are kept."""
    plan = list(r.batches('system', long_payload()))
    assert len(plan) > 1 + r.PARALLEL  # a tail exists beyond the first two waves
    journal, calls = Journal(), []
    with pytest.raises(ModelError):
        r.run(journal, SimpleNamespace(json=recording(calls, fail=set(plan[1]['evidence']))), 'system',
              long_payload(), lambda: None)
    assert len(calls) == 1 + r.PARALLEL and len(journal.saved) == r.PARALLEL
    assert all(set(batch['evidence']) in calls for batch in plan[:1 + r.PARALLEL])


def test_the_first_unanswered_request_goes_alone():
    """On resume it may be the unknown one; nothing else is paid for if it cannot complete."""
    plan = list(r.batches('system', long_payload()))
    journal, calls = Journal(), []
    with pytest.raises(ModelError):
        r.run(journal, SimpleNamespace(json=recording(calls, fail=set(plan[0]['evidence']), code='unknown_not_resent')),
              'system', long_payload(), lambda: None)
    assert len(calls) == 1 and journal.saved == {}


def test_an_invalid_answer_stops_the_later_waves():
    plan = list(r.batches('system', long_payload()))
    journal, calls = Journal(), []
    with pytest.raises(ValueError):
        r.run(journal, SimpleNamespace(json=recording(calls, invalid=set(plan[0]['evidence']))), 'system',
              long_payload(), lambda: None)
    assert len(calls) == 1  # the old sequential cost of an invalid first answer, not the whole plan


def test_a_saved_invalid_answer_stops_a_resumed_traversal_before_any_request():
    plan = list(r.batches('system', long_payload()))
    journal, calls = Journal(), []
    with pytest.raises(ValueError):
        r.run(journal, SimpleNamespace(json=recording(calls, invalid=set(plan[0]['evidence']))), 'system',
              long_payload(), lambda: None)
    sent = len(calls)
    with pytest.raises(ValueError):
        r.run(journal, SimpleNamespace(json=recording(calls)), 'system', long_payload(), lambda: None)
    assert len(calls) == sent


def test_a_resumed_traversal_sends_only_the_unanswered_requests():
    plan = list(r.batches('system', long_payload()))
    journal, calls = Journal(), []
    with pytest.raises(ModelError):
        r.run(journal, SimpleNamespace(json=recording(calls, fail=set(plan[1]['evidence']), code='model_timeout')),
              'system', long_payload(), lambda: None)
    sent_before, saved_before = len(calls), len(journal.saved)
    assert saved_before == r.PARALLEL
    result = r.run(journal, SimpleNamespace(json=recording(calls)), 'system', long_payload(), lambda: None)
    assert len(calls) - sent_before == len(plan) - saved_before  # answered requests replay from the journal
    assert len(result['checks']) == len(payload()['fields'])


def test_consistency_checks_tail_and_validates_fields():
    data = payload()
    data.update(phase='consistency', fields={'operations.summary': 'A', 'position.summary': 'B'})
    sent = []
    def model(system, user, **kwargs):
        body = json.loads(user)
        sent.extend(body['evidence'])
        conflict = 'S23' in body['evidence']
        return {'consistent': not conflict, 'reason': 'synthetic', 'conflicts':
                [{'fields': list(body['fields']), 'reason': 'contradiction'}] if conflict else []}
    result = r.run(Journal(), SimpleNamespace(json=model), 'system', data, lambda: None)
    assert not result['consistent'] and len(result['conflicts']) == 1
    assert sent == list(data['evidence'])


def test_single_oversized_source_is_losslessly_reviewed():
    data = payload()
    data['evidence'] = {'S0': {'context': 'Synthetic project retains its original status.\n' * 2000, 'title': ''}}
    pieces = []
    def model(system, user, **kwargs):
        assert len(system) + len(user) <= r.MAX_REQUEST_CHARS
        body = json.loads(user)
        pieces.append(body['evidence']['S0']['context'])
        return answer(body)
    result = r.run(Journal(), SimpleNamespace(json=model), 'system', data, lambda: None)
    assert ''.join(pieces) == data['evidence']['S0']['context']
    assert result['checks'][0]['status'] == 'supported'


def test_indivisible_negation_is_not_cut_into_an_approval():
    data = payload()
    data['evidence'] = {'S0': {'context': 'X' * 31998 + '未获批准' + 'X' * 31998, 'title': ''}}
    with pytest.raises(ValueError, match='indivisible'):
        r.run(Journal(), SimpleNamespace(json=lambda *a, **k: pytest.fail('must not send partial assertion')),
              'system', data, lambda: None)


def test_unreferenced_tail_counterevidence_is_seen_once_for_all_fields():
    data = payload()
    data['analysis']['operations']['refs'] = ['S0']
    data['fields']['operations.for.0.text'] = 'another assertion'
    sent = []
    def model(system, user, **kwargs):
        assert len(system) + len(user) <= r.MAX_REQUEST_CHARS
        body = json.loads(user)
        sent.extend(body['evidence'])
        rows = answer(body, 'contradicted')['checks'] if 'S23' in body['evidence'] else []
        return {'checked_fields': list(body['fields']), 'checks': rows}
    result = r.counterevidence(Journal(), SimpleNamespace(json=model), data, lambda: None)
    assert sent == list(data['evidence'])
    assert {i['field'] for i in result['issues']} == set(data['fields'])
    assert all(i['code'] == 'evidence_contradicted' for i in result['issues'])
    assert list(r.cited_payload(data)['evidence']) == ['S0']


def test_counterevidence_missing_fields_is_not_a_clean_review():
    def model(system, user, **kwargs):
        return {'checked_fields': [], 'checks': []}
    with pytest.raises(ValueError, match='coverage'):
        r.counterevidence(Journal(), SimpleNamespace(json=model), payload(), lambda: None)


def test_counterevidence_resume_keeps_receipts_and_no_calls_repeat():
    journal, sent = Journal(), []
    def model(system, user, **kwargs):
        body = json.loads(user)
        sent.extend(body['evidence'])
        return {'checked_fields': list(body['fields']), 'checks': []}
    first = r.counterevidence(journal, SimpleNamespace(json=model), payload(), lambda: None)
    assert r.counterevidence(journal, SimpleNamespace(json=model), payload(), lambda: None) == first
    assert sent == list(payload()['evidence'])


def test_production_reviewer_scans_all_then_verifies_cited_evidence():
    from project_watch.studio.analyze_job import _reviewer
    data = payload()
    data['analysis']['operations']['refs'] = ['S0']
    scanned, supported = [], []
    def model(system, user, **kwargs):
        body = json.loads(user)
        if body['phase'] == 'counterevidence':
            scanned.extend(body['evidence'])
            return {'checked_fields': list(body['fields']), 'checks': []}
        supported.extend(body['evidence'])
        return answer(body)
    reviewer = _reviewer(None, Journal(), SimpleNamespace(json=model), lambda: None)
    assert reviewer.prepare(data)['issues'] == []
    assert reviewer('system', data)['checks'][0]['status'] == 'supported'
    assert scanned == list(data['evidence']) and supported == ['S0']

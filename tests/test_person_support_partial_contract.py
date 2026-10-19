"""Synthetic structural controls; not live semantic acceptance."""
from copy import deepcopy

import pytest

from scripts.person_support_context import (
    bind_review_request,
    context_digest,
    review_payload,
    validate_person_proposal_partial,
    validate_review_partial,
)
from tests.test_person_support_context_contract import context, proposal, reviewed


def bound(original=None):
    original = proposal() if original is None else original
    frozen = context()
    payload = review_payload(frozen, original, context_digest(frozen))
    return bind_review_request(frozen, original, payload, request_id='synthetic-call-1')


def check(raw=None, original=None, binding=None, response_request_id='synthetic-call-1'):
    original = proposal() if original is None else original
    return validate_review_partial(context(), original, reviewed() if raw is None else raw,
                                   binding=bound(original) if binding is None else binding,
                                   response_request_id=response_request_id)


def test_bad_date_isolates_one_row_with_original_ids_and_raw_output():
    bad = proposal(date='2024-01-01')['facts'][0]
    raw = {'facts': [bad, proposal()['facts'][0]]}
    before = deepcopy(raw)
    out = validate_person_proposal_partial(context(), raw)
    assert out['status'] == 'partial'
    assert [f['id'] for f in out['facts']] == ['F02']
    assert out['rejections'] == [{'id': 'F01', 'code': 'person_occurrence', 'raw': bad}]
    assert out['raw_facts'] == before['facts'] and raw == before
    assert out['semantic_verification'] == 'not_proven'


@pytest.mark.parametrize('bad,code', [
    (proposal(subject='另一公司')['facts'][0], 'person_subject'),
    (proposal(refs=[{'ref': 'P2', 'quote': '不存在的正文片段'}])['facts'][0], 'person_refs'),
    (None, 'person_shape'),
    ('not-a-fact', 'person_shape'),
])
def test_invalid_row_does_not_discard_valid_sibling(bad, code):
    out = validate_person_proposal_partial(context(), {'facts': [bad, proposal()['facts'][0]]})
    assert out['facts'][0]['id'] == 'F02'
    assert out['rejections'][0]['raw'] == bad and out['rejections'][0]['code'] == code


@pytest.mark.parametrize('raw', [None, {}, {'facts': None}, {'facts': [None] * 31}])
def test_invalid_envelope_is_not_partial_success(raw):
    with pytest.raises(ValueError, match='person_shape'):
        validate_person_proposal_partial(context(), raw)


def test_empty_extraction_and_all_rejected_are_distinct():
    empty = validate_person_proposal_partial(context(), {'facts': []})
    rejected = validate_person_proposal_partial(context(), proposal(date='2024-01-01'))
    assert empty['status'] == 'empty' and empty['coverage_status'] == 'needs_extraction'
    assert rejected['status'] == 'rejected' and len(rejected['rejections']) == 1


def test_complete_result_is_not_verified():
    out = validate_person_proposal_partial(context(), proposal(relation='departure'))
    assert out['status'] == 'complete' and out['facts'][0]['relation'] == 'departure'
    assert out['semantic_verification'] == 'not_proven'


def test_model_digest_echo_is_diagnostic_not_request_identity():
    raw = reviewed()
    raw['context_sha256'] = 'bad-model-echo'
    out = check(raw)
    assert out['facts'][0]['id'] == 'F01-01'
    assert out['binding_status'] == 'matched'
    assert out['diagnostics'] == ['model_context_echo_mismatch']
    assert raw['context_sha256'] == 'bad-model-echo'


def test_missing_echo_is_allowed_only_with_trusted_binding():
    raw = reviewed()
    del raw['context_sha256']
    assert check(raw)['diagnostics'] == ['model_context_echo_missing']
    with pytest.raises(ValueError, match='request_binding'):
        check(raw, binding={})


@pytest.mark.parametrize('request_id', ['', 'other-request', None])
def test_wrong_response_receipt_cannot_use_correct_model_echo(request_id):
    with pytest.raises(ValueError, match='request_binding'):
        check(response_request_id=request_id)


@pytest.mark.parametrize('field', ['context_sha256', 'payload_sha256', 'request_id'])
def test_mutated_local_binding_is_rejected(field):
    binding = bound()
    binding[field] = 'wrong'
    with pytest.raises(ValueError, match='request_binding'):
        check(binding=binding)


@pytest.mark.parametrize('mutation', ['body', 'claim', 'id', 'task', 'extra'])
def test_binding_rejects_payload_not_equal_to_expected_request(mutation):
    original, frozen = proposal(), context()
    payload = review_payload(frozen, original, context_digest(frozen))
    if mutation == 'body':
        payload['context']['sources'][0]['body']['text'] = '另一正文'
    elif mutation == 'claim':
        payload['claims'][0]['fact']['text'] = '另一说法'
    elif mutation == 'id':
        payload['claims'][0]['id'] = 'F02'
    elif mutation == 'task':
        payload['task'] = 'other-task'
    else:
        payload['expected_scope'] = 'not-allowed'
    with pytest.raises(ValueError, match='request_binding'):
        bind_review_request(frozen, original, payload, request_id='synthetic-call-1')


def test_original_changed_after_dispatch_invalidates_binding():
    original = proposal(text='另一陈述')
    with pytest.raises(ValueError, match='request_binding'):
        check(original=original, binding=bound())


@pytest.mark.parametrize('judgments', [[], [{'id': 'F02'}], [{'id': 'F01'}, {'id': 'F01'}]])
def test_bad_digest_does_not_hide_missing_duplicate_or_foreign_ids(judgments):
    raw = {'context_sha256': 'wrong', 'judgments': judgments}
    with pytest.raises(ValueError, match='review_coverage'):
        check(raw)


def test_review_partial_keeps_corrected_siblings_and_parent_ids():
    bad = proposal(date='2024-01-01')['facts'][0]
    raw = reviewed(facts=[bad, proposal()['facts'][0]])
    before = deepcopy(raw)
    out = check(raw)
    assert out['status'] == 'partial'
    assert out['facts'][0]['id'] == 'F01-02'
    assert out['facts'][0]['parent_id'] == 'F01'
    assert out['rejections'][0] == {'id': 'F01-01', 'parent_id': 'F01',
                                   'code': 'person_occurrence', 'raw': bad}
    assert out['judgments'] == before['judgments'] and raw == before


def test_bad_judgment_does_not_discard_valid_other_claim():
    original = {'facts': proposal()['facts'] * 2}
    raw = reviewed(facts=[])
    raw['judgments'].append({**reviewed()['judgments'][0], 'id': 'F02'})
    out = check(raw, original=original)
    assert out['status'] == 'partial'
    assert out['facts'][0]['parent_id'] == 'F02'
    assert out['rejections'][0]['id'] == 'F01'


def test_empty_claim_only_review_does_not_recover_missing_extraction():
    out = check({'judgments': []}, original={'facts': []})
    assert out['status'] == 'empty' and out['coverage_status'] == 'needs_extraction'
    assert out['facts'] == []


def test_explicit_insufficient_empty_is_not_missing_coverage():
    out = check(reviewed(status='insufficient', facts=[]))
    assert out['status'] == 'empty' and out['coverage_status'] == 'reviewed'
    assert out['judgments'][0]['status'] == 'insufficient'


def test_no_supported_facts_is_rejected_not_empty_success():
    out = check(reviewed(facts=proposal(date='2024-01-01')['facts']))
    assert out['status'] == 'rejected' and out['facts'] == []


def test_private_context_still_rejected_before_partial_processing():
    frozen = context()
    frozen['sources'][0]['visibility'] = 'internal'
    with pytest.raises(ValueError, match='context_scope'):
        validate_person_proposal_partial(frozen, proposal())


def test_results_do_not_share_mutable_raw_objects():
    raw = proposal()
    out = validate_person_proposal_partial(context(), raw)
    out['raw_facts'][0]['text'] = 'changed'
    out['facts'][0]['refs'][0]['quote'] = 'changed'
    assert raw == proposal()

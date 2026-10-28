"""A malformed exact quote rejects its record, never relaxes source identity."""
from copy import deepcopy

import pytest
from test_dsh_record_review import prepare
from test_studio_dsh_bootstrap import call
from test_studio_dsh_bootstrap import research as research_fixture
from test_studio_dsh_monitor import synthetic_review

from project_watch.studio import dsh_record_review as review
from project_watch.studio.http import HttpError

research = research_fixture


@pytest.mark.parametrize('quote', ['First fact … last fact.', 'First fact. Second fact.', 'Invented quote'])
def test_bad_exact_quote_rejects_only_its_record_and_receipt_is_reusable(research, quote):
    prepare(research, 2)
    calls = []
    def check(payload):
        calls.append(payload)
        value = synthetic_review(payload)
        value['checks'][0]['evidence'][0]['quote'] = quote
        return value
    research.record_reviewer = check
    before = deepcopy(research.state['records'])
    accepted, _, receipt = review.admit(research, before, [])
    assert [r['key'] for r in accepted] == ['1']
    assert receipt['rejected'] == [{'key': 'record:0', 'code': 'evidence_quote_invalid'}]
    again = review.admit(research, before, [])
    assert again == (accepted, [], receipt) and len(calls) == 1
    assert research.state['records'] == before


def test_invalid_quote_cannot_publish_a_fact_but_valid_sibling_can(research):
    prepare(research, 2)
    def check(payload):
        value = synthetic_review(payload)
        value['checks'][0]['evidence'][0]['quote'] = 'Unquoted assertion'
        return value
    research.record_reviewer = check
    result = call(research, 'pw_finish', gaps={})
    assert result['status'] == 'partial' and result['records'] == 1
    assert result['retained_records'] == 2
    profile = research.store.get_public_profile(research.project_id)
    assert [f['label'] for f in profile['facts']] == ['Synthetic assertion 1']
    assert profile['outcome']['record_admission']['rejected'][0]['code'] == 'evidence_quote_invalid'


@pytest.mark.parametrize('fault', ['missing', 'duplicate', 'wrong_field', 'unknown_source',
    'malformed_quote', 'malformed_witness', 'missing_evidence', 'missing_reason', 'wrong_status'])
def test_structural_or_identity_fault_still_stops_whole_batch(research, fault):
    prepare(research, 2)
    calls = []
    def check(payload):
        calls.append(1)
        value = synthetic_review(payload)
        value['checks'][0]['evidence'][0]['quote'] = 'Non-contiguous quote'
        row = value['checks'][1]
        if fault == 'missing':
            value['checks'].pop()
        elif fault == 'duplicate':
            row['field'] = value['checks'][0]['field']
        elif fault == 'wrong_field':
            row['field'] = 'record:another-project'
        elif fault == 'unknown_source':
            row['evidence'][0]['ref'] = 'unknown-source'
        elif fault == 'malformed_quote':
            row['evidence'][0]['quote'] = None
        elif fault == 'malformed_witness':
            row['evidence'].append(None)
        elif fault == 'missing_evidence':
            row['evidence'] = []
        elif fault == 'missing_reason':
            row['reason'] = ''
        elif fault == 'wrong_status':
            row['status'] = 'probably'
        return value
    research.record_reviewer = check
    with pytest.raises(HttpError, match='judgment_review_invalid'):
        review.admit(research, research.state['records'], [])
    assert len(calls) == 1
    assert not research.store.get_public_profile(research.project_id)
    assert not research.state['finished']


def test_later_malformed_witness_is_not_hidden_by_first_bad_quote(research):
    prepare(research, 2)
    def check(payload):
        value = synthetic_review(payload)
        value['checks'][0]['evidence'][0]['quote'] = 'Non-contiguous quote'
        value['checks'][0]['evidence'].append({'ref': 'missing', 'quote': 'Bad identity'})
        return value
    research.record_reviewer = check
    with pytest.raises(HttpError, match='judgment_review_invalid'):
        review.admit(research, research.state['records'], [])


def test_valid_witness_does_not_rescue_an_invalid_witness_on_same_record(research):
    prepare(research, 2)
    def check(payload):
        value = synthetic_review(payload)
        witness = deepcopy(value['checks'][0]['evidence'][0])
        witness['quote'] = 'Not exact'
        value['checks'][0]['evidence'].append(witness)
        return value
    research.record_reviewer = check
    accepted, _, receipt = review.admit(research, research.state['records'], [])
    assert [r['key'] for r in accepted] == ['1']
    assert receipt['rejected'][0]['code'] == 'evidence_quote_invalid'


@pytest.mark.parametrize('quote', ['First fact. … Second fact.', 'First fact.Second fact.'])
def test_joined_source_fragments_remain_invalid_even_if_each_fragment_exists(quote):
    payload = {'fields': {'a': 'First and second facts', 'b': 'Other evidence'},
               'evidence': {'E1': {'context': 'First fact.\nSecond fact.\nOther evidence.'}}}
    value = {'checks': [
        {'field': 'a', 'status': 'supported', 'reason': 'Two source fragments',
         'evidence': [{'ref': 'E1', 'quote': quote}]},
        {'field': 'b', 'status': 'supported', 'reason': 'Exact source fragment',
         'evidence': [{'ref': 'E1', 'quote': 'Other evidence.'}]}]}
    assert review.validate_records(value, payload) == [{'field': 'a', 'code': 'evidence_quote_invalid'}]
    value['checks'][0]['evidence'] = [
        {'ref': 'E1', 'quote': 'First fact.'}, {'ref': 'E1', 'quote': 'Second fact.'}]
    assert review.validate_records(value, payload) == []


def test_record_quote_isolation_does_not_relax_final_judgment_validation():
    payload = {'fields': {'a': 'Two facts'}, 'evidence': {'E1': {'context': 'First.\nSecond.'}}}
    value = {'checks': [{'field': 'a', 'status': 'supported', 'reason': 'Joined',
                        'evidence': [{'ref': 'E1', 'quote': 'First.Second.'}]}]}
    with pytest.raises(ValueError, match='review_evidence_invalid'):
        review.review.validate_review(value, payload)

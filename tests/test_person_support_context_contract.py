"""Synthetic conformance tests, not semantic or live-model acceptance."""
from copy import deepcopy
from datetime import date

import pytest

from scripts.person_support_context import (
    context_digest,
    extraction_payload,
    freeze_context,
    review_payload,
    validate_person_proposal,
    validate_review,
)

TODAY = date(2026, 10, 12)


def source(**changes):
    return {'ref': 'P1', 'title': '合成公司公告', 'body': '合成公司于2024年5月10日任命甲为首任总经理。',
            'published_at': '2025-04-01', 'visibility': 'public', 'commercial': False, **changes}


def context(**changes):
    return freeze_context('合成公司', [source(**changes)], today=TODAY)


def proposal(**changes):
    return {'facts': [{'subject': '合成公司', 'kind': 'person', 'label': '甲',
                      'relation': 'first_role', 'value': '总经理', 'polarity': 'positive',
                      'time_scope': 'as_reported', 'text': '甲为合成公司首任总经理',
                      'date': '2024-05-10', 'reported_on': '2025-04-01',
                      'refs': [{'ref': 'P1', 'quote': '2024年5月10日任命甲为首任总经理'}], **changes}]}


def test_two_stages_receive_the_same_full_source_snapshot():
    frozen = context()
    first = extraction_payload(frozen)
    second = review_payload(frozen, proposal(), first['context_sha256'])
    assert first['context'] == second['context'] == frozen
    assert first['context_sha256'] == second['context_sha256'] == context_digest(frozen)
    p1 = second['context']['sources'][0]
    assert p1['title'] == {'text': '合成公司公告', 'basis': 'source_title'}
    assert p1['body']['text'] == source()['body']
    assert p1['publication'] == {'date': '2025-04-01', 'basis': 'source_publication', 'is_occurrence_date': False}


def test_payload_copies_and_does_not_mutate_frozen_context_or_claims():
    frozen, raw = context(), proposal()
    first = extraction_payload(frozen)
    second = review_payload(frozen, raw, first['context_sha256'])
    second['context']['sources'][0]['body']['text'] = 'changed'
    second['claims'][0]['fact']['text'] = 'changed'
    assert frozen == context() and raw == proposal()
    assert first['context'] == frozen


def test_mutated_context_cannot_reuse_extraction_digest():
    frozen = context()
    expected = context_digest(frozen)
    frozen['sources'][0]['publication']['date'] = '2025-04-02'
    with pytest.raises(ValueError, match='context_changed'):
        review_payload(frozen, proposal(), expected)


@pytest.mark.parametrize('changes', [
    {'visibility': 'internal'}, {'visibility': 'commercial'}, {'commercial': True},
    {'visibility': None}, {'commercial': None},
])
def test_private_commercial_or_unclassified_context_rejected(changes):
    with pytest.raises(ValueError, match='context_scope'):
        context(**changes)


@pytest.mark.parametrize('changes', [{'body': 'x' * 4001}, {'title': 'x' * 401}, {'ref': 'bad'}])
def test_bounds_are_explicit_not_silent_truncation(changes):
    with pytest.raises(ValueError):
        context(**changes)


def test_duplicate_and_too_many_sources_rejected():
    with pytest.raises(ValueError, match='context_refs'):
        freeze_context('合成公司', [source(), source()], today=TODAY)
    with pytest.raises(ValueError, match='context_bound'):
        freeze_context('合成公司', [source(ref=f'P{i}') for i in range(1, 26)], today=TODAY)


@pytest.mark.parametrize('value', ['', None, 'not-date', '2025-02-29', '2027-01-01'])
def test_unknown_bad_future_publication_does_not_become_date(value):
    assert context(published_at=value)['sources'][0]['publication']['date'] is None


def test_body_title_and_publication_are_distinct_and_body_preserved():
    body = '\n合成公司：\n甲为外部顾问。\n'
    frozen = context(body=body)
    assert frozen['sources'][0]['body']['text'] == body
    assert frozen['sources'][0]['body']['basis'] == 'source_excerpt'


def test_review_preserves_raw_input_not_expected_answers():
    raw = {'facts': [{'kind': 'person', 'label': '甲', 'text': '甲为首任经理但缺证',
                     'refs': [{'ref': 'P1', 'quote': '合成公司'}]}]}
    frozen = context()
    out = review_payload(frozen, raw, context_digest(frozen))
    assert out['claims'] == [{'id': 'F01', 'fact': raw['facts'][0]}]
    assert 'expected_scope' not in out
    assert raw['facts'][0]['text'] == '甲为首任经理但缺证'


def test_valid_atomic_candidate_keeps_typed_report_and_occurrence_dates():
    original = proposal()
    result = validate_person_proposal(context(), original)
    assert result['facts'][0] == {'id': 'F01', **original['facts'][0]}
    assert 'id' not in original['facts'][0]


@pytest.mark.parametrize('changes,code', [
    ({'subject': '另一公司'}, 'person_subject'),
    ({'relation': ['role', 'departure']}, 'person_relation'),
    ({'relation': 'role+departure'}, 'person_relation'),
    ({'polarity': 'guess'}, 'person_shape'),
    ({'time_scope': 'current'}, 'person_shape'),
    ({'value': ''}, 'person_shape'),
    ({'date': '2025-04-01'}, 'person_occurrence'),
    ({'reported_on': '2024-05-10'}, 'person_reporting'),
    ({'refs': [{'ref': 'P2', 'quote': '2024年5月10日任命甲为首任总经理'}]}, 'person_refs'),
    ({'refs': [{'ref': 'P1', 'quote': '合成公司公告'}]}, 'person_refs'),
    ({'refs': [{'ref': 'P1', 'quote': '不存在的正文片段'}]}, 'person_refs'),
    ({'refs': [{'ref': ['P1'], 'quote': '2024年5月10日任命甲为首任总经理'}]}, 'person_refs'),
    ({'refs': [{'ref': 'P1', 'field': 'title', 'quote': '2024年5月10日任命甲为首任总经理'}]}, 'person_refs'),
])
def test_candidate_rejects_declared_scope_relation_time_and_reference_errors(changes, code):
    with pytest.raises(ValueError, match=code):
        validate_person_proposal(context(), proposal(**changes))


def test_one_body_date_is_presence_check_not_semantic_event_validation():
    result = validate_person_proposal(context(), proposal(text='错误地把任命日期说成离职日期', relation='departure'))
    # A structurally valid lie stays possible. Never claim the guard proves entailment.
    assert result['facts'][0]['relation'] == 'departure'


def reviewed(status='supported', facts=None):
    return {'context_sha256': context_digest(context()), 'judgments': [
        {'id': 'F01', 'status': status, 'reason': '合成校对理由',
         'facts': proposal()['facts'] if facts is None else facts}]}


def test_complete_review_has_parent_lineage_without_marking_verified():
    out = validate_review(context(), proposal(), reviewed())
    fact = out['facts'][0]
    assert fact['id'] == 'F01-01' and fact['parent_id'] == 'F01'
    assert fact['model_review_status'] == 'supported'
    assert out['semantic_verification'] == 'not_proven'
    assert 'verified' not in fact


@pytest.mark.parametrize('status', ['contradicted', 'insufficient'])
def test_negative_review_can_preserve_supported_parts_with_original_lineage(status):
    out = validate_review(context(), proposal(), reviewed(status))
    assert out['facts'][0]['parent_id'] == 'F01'
    assert out['facts'][0]['model_review_status'] == status


@pytest.mark.parametrize('judgments', [[], [{'id': 'F02'}], [{'id': 'F01'}, {'id': 'F01'}]])
def test_missing_duplicate_and_foreign_review_ids_fail(judgments):
    raw = reviewed()
    raw['judgments'] = judgments
    with pytest.raises(ValueError, match='review_coverage'):
        validate_review(context(), proposal(), raw)


def test_review_cannot_replace_source_snapshot():
    raw = reviewed()
    raw['context_sha256'] = 'wrong'
    with pytest.raises(ValueError, match='context_changed'):
        validate_review(context(), proposal(), raw)


def test_other_subject_in_corrected_facts_rejected_not_empty_success():
    raw = reviewed(facts=proposal(subject='另一公司')['facts'])
    with pytest.raises(ValueError, match='person_subject'):
        validate_review(context(), proposal(), raw)
    assert raw['judgments'][0]['facts'][0]['subject'] == '另一公司'


def test_explicit_rejection_is_recorded_and_empty_supported_is_invalid():
    assert validate_review(context(), proposal(), reviewed('insufficient', []))['facts'] == []
    with pytest.raises(ValueError, match='review_shape'):
        validate_review(context(), proposal(), reviewed('supported', []))


def test_review_no_input_requires_no_judgments():
    raw = {'context_sha256': context_digest(context()), 'judgments': []}
    assert validate_review(context(), {'facts': []}, raw)['facts'] == []


def test_correction_does_not_mutate_the_original_proposal():
    original = proposal()
    copy = deepcopy(original)
    validate_review(context(), original, reviewed())
    assert original == copy


@pytest.mark.parametrize('mutation', ['top_extra', 'source_extra', 'body_basis', 'publication_occurrence', 'private'])
def test_mutated_typed_context_is_revalidated_before_export(mutation):
    frozen = context()
    if mutation == 'top_extra':
        frozen['api_key'] = 'synthetic-secret'
    elif mutation == 'source_extra':
        frozen['sources'][0]['headers'] = {'Authorization': 'synthetic-secret'}
    elif mutation == 'body_basis':
        frozen['sources'][0]['body']['basis'] = 'source_publication'
    elif mutation == 'publication_occurrence':
        frozen['sources'][0]['publication']['is_occurrence_date'] = True
    else:
        frozen['sources'][0]['visibility'] = 'internal'
    with pytest.raises(ValueError, match='context_'):
        extraction_payload(frozen)


def test_source_input_extras_do_not_enter_export():
    frozen = context(api_key='synthetic-secret', headers={'Authorization': 'synthetic-secret'})
    assert 'api_key' not in frozen['sources'][0]
    assert 'headers' not in frozen['sources'][0]


def test_partial_rewrite_lineage_is_distinct_and_ordered():
    raw = reviewed('insufficient', [proposal()['facts'][0], proposal(relation='appointment')['facts'][0]])
    facts = validate_review(context(), proposal(), raw)['facts']
    assert [f['id'] for f in facts] == ['F01-01', 'F01-02']
    assert all(f['parent_id'] == 'F01' for f in facts)

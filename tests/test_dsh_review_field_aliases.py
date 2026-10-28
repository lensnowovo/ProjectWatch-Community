"""Short transport identifiers must not weaken durable admission or evidence."""
import json
from copy import deepcopy

import pytest
from test_dsh_judgment_review import attach, response
from test_dsh_record_review import prepare
from test_studio_dsh_bootstrap import research as research_fixture
from test_studio_dsh_monitor import synthetic_review

from project_watch.studio import dsh_judgment_review as judgment
from project_watch.studio import dsh_record_review as records
from project_watch.studio.http import HttpError

research = research_fixture


def payload():
    fields = {'record:' + 'a' * 64: 'First assertion.',
              'previous:' + 'b' * 64: 'Second assertion.'}
    return {'purpose': 'public_record_admission', 'phase': 'fields',
            'analysis': fields, 'fields': fields,
            'bound_evidence': {key: ['E123'] for key in fields},
            'evidence': {'E123': {'context': 'First assertion.\nSecond assertion.'}}}


def test_roundtrip_reorders_checks_without_changing_content_or_quotes():
    source = payload()
    before = deepcopy(source)
    aliases = judgment.ReviewFieldAliases(source)
    wire = aliases.payload
    assert list(wire['fields']) == ['F1', 'F2']
    assert wire['analysis'] == wire['fields']
    assert wire['bound_evidence'] == {'F1': ['E123'], 'F2': ['E123']}
    assert wire['evidence'] == source['evidence']
    assert 'record:' not in json.dumps(wire) and 'previous:' not in json.dumps(wire)
    value = synthetic_review(wire)
    value['checks'].reverse()
    result = aliases.decode(value)
    assert [c['field'] for c in result['checks']] == list(reversed(source['fields']))
    assert judgment.validate_review(result, source) == []
    assert source == before and value['checks'][0]['field'] == 'F2'
    assert result['checks'][0]['evidence'] == value['checks'][0]['evidence']


def test_batched_records_keep_full_context_but_reject_out_of_batch_alias():
    source = payload()
    second = list(source['fields'])[1]
    source['fields'] = {second: source['fields'][second]}
    aliases = judgment.ReviewFieldAliases(source)
    assert list(aliases.payload['analysis']) == ['F1', 'F2']
    assert list(aliases.payload['fields']) == ['F2']
    value = synthetic_review(aliases.payload)
    value['checks'][0]['field'] = 'F1'
    with pytest.raises(ValueError, match='coverage'):
        aliases.decode(value)


@pytest.mark.parametrize('bad', ['F01', 'f1', ' F1', 'F1 ', 'F3', 'record:' + 'a' * 63, None, 1, []])
def test_alias_errors_are_not_guessed_or_normalized(bad):
    aliases = judgment.ReviewFieldAliases(payload())
    value = synthetic_review(aliases.payload)
    value['checks'][0]['field'] = bad
    with pytest.raises(ValueError, match='coverage'):
        aliases.decode(value)


@pytest.mark.parametrize('fault', ['missing', 'duplicate'])
def test_complete_unique_coverage_is_still_required(fault):
    source = payload()
    aliases = judgment.ReviewFieldAliases(source)
    value = synthetic_review(aliases.payload)
    if fault == 'missing':
        value['checks'].pop()
    else:
        value['checks'][1]['field'] = value['checks'][0]['field']
    with pytest.raises(ValueError, match='coverage'):
        judgment.validate_review(aliases.decode(value), source)


def test_consistency_conflicts_map_back_to_exact_durable_keys():
    source = {**payload(), 'phase': 'consistency'}
    aliases = judgment.ReviewFieldAliases(source)
    result = aliases.decode({'consistent': False, 'reason': 'Synthetic conflict',
                            'conflicts': [{'fields': ['F2', 'F1'], 'reason': 'Different facts'}]})
    assert result['conflicts'][0]['fields'] == list(reversed(source['fields']))
    assert len(judgment.validate_consistency(result, source)) == 1
    with pytest.raises(ValueError, match='coverage'):
        aliases.decode({'conflicts': [{'fields': ['F1', 'F3']}]})


def test_semantic_paths_and_complete_analysis_remain_available():
    source = judgment.review_input({'headline': 'First assertion.', 'operations': {
        'summary': 'Second assertion.', 'for': [{'text': 'Third assertion.', 'refs': ['N1']}]}},
        {'numbered': {'N1': {'context': 'Evidence'}}})
    aliases = judgment.ReviewFieldAliases(source)
    assert aliases.payload['analysis'] == source['analysis']
    assert aliases.payload['field_paths'] == dict(zip(['F1', 'F2', 'F3'], source['fields'], strict=True))
    assert list(aliases.payload['fields'].values()) == list(source['fields'].values())


@pytest.mark.parametrize('bad', [None, [], {'checks': None}, {'checks': [None]},
                               {'checks': [{}]}])
def test_malformed_response_cannot_be_decoded(bad):
    with pytest.raises(ValueError):
        judgment.ReviewFieldAliases(payload()).decode(bad)


def test_actual_gateway_short_ids_preserve_record_quote_isolation(research):
    prepare(research, 2)
    for index, row in enumerate(research.state['records']):
        row['key'] = str(index) * 64
    calls = []
    def send(method, url, headers, raw, timeout):
        wire = json.loads(json.loads(raw)['messages'][1]['content'])
        calls.append(wire)
        assert list(wire['fields']) == ['F1', 'F2']
        assert 'record:' not in json.dumps(wire)
        value = synthetic_review(wire)
        value['checks'][0]['evidence'][0]['quote'] = 'Noncontiguous invented quote'
        return response(value)
    attach(research, send)
    accepted, _, receipt = records.admit(research, research.state['records'], [])
    assert [r['key'] for r in accepted] == ['1' * 64]
    assert receipt['rejected'] == [{'key': 'record:' + '0' * 64, 'code': 'evidence_quote_invalid'}]
    assert len(calls) == 1 and research.execution.usage()['model']['completed'] == 1


def test_actual_gateway_invalid_alias_holds_without_retry(research):
    prepare(research, 2)
    calls = []
    def send(method, url, headers, raw, timeout):
        wire = json.loads(json.loads(raw)['messages'][1]['content'])
        calls.append(wire)
        value = synthetic_review(wire)
        value['checks'][0]['field'] = 'F01'
        return response(value)
    attach(research, send)
    for _ in range(2):
        with pytest.raises(HttpError, match='judgment_review_invalid'):
            records.admit(research, research.state['records'], [])
    assert len(calls) == 1
    assert not research.store.get_public_profile(research.project_id)


def test_prior_policy_receipt_does_not_skip_new_record_review(research, monkeypatch):
    prepare(research, 2)
    calls = []
    research.record_reviewer = lambda batch: (calls.append(1), synthetic_review(batch))[1]
    current = judgment.VERSION
    monkeypatch.setattr(judgment, 'VERSION', 'judgment-evidence-review-v2')
    records.admit(research, research.state['records'], [])
    monkeypatch.setattr(judgment, 'VERSION', current)
    records.admit(research, research.state['records'], [])
    records.admit(research, research.state['records'], [])
    assert len(calls) == 2

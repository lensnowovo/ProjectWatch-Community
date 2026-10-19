"""Synthetic malformed rows must not discard independent located products."""

import json

import pytest
from pydantic import ValidationError
from test_studio_competition_profile import profile_inputs
from test_studio_evidence_versions import sample

from project_watch.studio.business_profile import Proposal


@pytest.mark.parametrize('invalid', [
    {'use_case': 'x' * 151}, {'buyer_type': 'x' * 101}, {'delivery': 'x' * 101},
    {'company': ''}, {'name': ''}, {'source': 'B' * 11}, {'quote': 'short'},
    {'ownership_quote': 'x' * 2001}, {'basis': 'announced'}, {'polarity': 'positive'},
    {'unexpected_private_text': 'DO NOT PERSIST'},
])
@pytest.mark.parametrize('bad_first', [True, False])
def test_invalid_row_does_not_discard_located_product(tmp_path, invalid, bad_first):
    service, pid, _, profiles, inputs, proposal = profile_inputs(tmp_path)
    good = proposal['products'][0]
    bad = {**good, **invalid}
    data = {'products': [bad, good] if bad_first else [good, bad]}
    result = profiles.apply(pid, inputs, data)
    assert result['status'] == 'extracted'
    assert result['accepted'] == 1
    assert result['rejected'] == result['rejected_schema'] == 1
    assert result['rejected_fields'] == 0
    products = [o for o in service.objects.page(pid) if o['kind'] == 'product']
    assert len(products) == 1 and products[0]['name'] == 'Lens A'
    assert {f['value'] for f in service.objects.facts(pid, products[0]['id'])} == {
        'drug screening', 'pharma', 'software'}
    assert 'DO NOT PERSIST' not in json.dumps(result)
    assert profiles.get(pid) == result


@pytest.mark.parametrize('bad', [None, 1, 'not a product', [], {'name': 'incomplete'}])
def test_bad_row_shape_is_counted_without_aborting(tmp_path, bad):
    _, pid, _, profiles, inputs, proposal = profile_inputs(tmp_path)
    proposal['products'].insert(0, bad)
    result = profiles.apply(pid, inputs, proposal)
    assert result['accepted'] == 1 and result['rejected_schema'] == 1


def test_all_invalid_is_completed_insufficient_not_fake_success(tmp_path):
    service, pid, _, profiles, inputs, _ = profile_inputs(tmp_path)
    result = profiles.apply(pid, inputs, {'products': [None, {'name': 'incomplete'}]})
    assert result['status'] == 'insufficient_product_evidence'
    assert result['accepted'] == 0
    assert result['rejected'] == result['rejected_schema'] == 2
    assert result['product_ids'] == result['fact_ids'] == []
    assert not [o for o in service.objects.page(pid) if o['kind'] == 'product']
    assert profiles.apply(pid, inputs, {'products': []}) == result


@pytest.mark.parametrize('data', [None, [], {}, {'products': None}, {'products': {}},
                                    {'products': [], 'unknown': 'secret'},
                                    {'products': [None] * 13}])
def test_invalid_envelope_remains_fatal_without_writes(tmp_path, data):
    service, pid, _, profiles, inputs, _ = profile_inputs(tmp_path)
    with pytest.raises(ValidationError):
        profiles.apply(pid, inputs, data)
    assert profiles.get(pid) is None
    assert not [o for o in service.objects.page(pid) if o['kind'] == 'product']


def test_schema_isolation_does_not_relax_ownership_or_unquoted_dimensions(tmp_path):
    _, pid, _, profiles, inputs, proposal = profile_inputs(tmp_path)
    good = proposal['products'][0]
    proposal['products'] = [{**good, 'use_case': 'x' * 101},
                            {**good, 'company': 'Other Company'},
                            {**good, 'buyer_type': 'unquoted buyer'}]
    result = profiles.apply(pid, inputs, proposal)
    assert result['accepted'] == 1 and result['rejected'] == 2
    assert result['rejected_schema'] == 1 and result['rejected_fields'] == 1


def test_stale_input_is_not_committed_even_with_invalid_rows(tmp_path):
    service, pid, _, profiles, inputs, proposal = profile_inputs(tmp_path)
    proposal['products'].insert(0, None)
    service.evidence.save(sample(pid, 'revised source').model_copy(update={
        'source_key': 'public-profile:company', 'visibility': 'public'}))
    assert profiles.apply(pid, inputs, proposal)['status'] == 'superseded'
    assert profiles.get(pid) is None
    assert not [o for o in service.objects.page(pid) if o['kind'] == 'product']


def test_guard_still_runs_for_all_invalid_rows(tmp_path):
    _, pid, _, profiles, inputs, _ = profile_inputs(tmp_path)
    def expired(_):
        raise RuntimeError('synthetic expired lease')
    with pytest.raises(RuntimeError, match='expired lease'):
        profiles.apply(pid, inputs, {'products': [None]}, guard=expired)
    assert profiles.get(pid) is None


def test_external_strict_proposal_contract_is_preserved():
    with pytest.raises(ValidationError):
        Proposal.model_validate({'products': [None]})

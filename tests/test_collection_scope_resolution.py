"""Evidence-bound scope resolution is distinct from successful collection."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_collection_plan import candidate, plan, success

from project_watch.studio import collection_dsh
from project_watch.studio.collection_ledger import STAGE, SUPPLEMENTS
from project_watch.studio.collection_plan import resolved_scopes


def manifests(parent_changes=None, child_changes=None, plan_changes=None):
    parent = candidate() | dict(source='clinical_sponsor', category='trial', target_id='project-1',
                               query='', capability='needs_scope', reason='No public query scope')
    child = candidate() | dict(source='clinical_term', category='trial', target_id='scope:term-1',
                              applicability_evidence_ids=['public-evidence-1'])
    return (plan([parent | (parent_changes or {})]),
            plan([child | (child_changes or {})], **(plan_changes or {})))


def research(base, child, receipts=None, *, indexed_only=False):
    saved = {STAGE: dict(plan=base, receipts=[]),
             SUPPLEMENTS: [dict(stage='collection_extra:synthetic', plan=child)]}
    if not indexed_only:
        saved['collection_extra:synthetic'] = dict(plan=child, receipts=receipts or [])
    execution = SimpleNamespace(job={'id': base['round_id']}, read_checkpoint=saved.get)
    return SimpleNamespace(execution=execution, project_id=base['project_id']), saved


def test_scope_resolution_keeps_original_requirement_and_does_not_count_it_as_request():
    base, child = manifests()
    loop, checkpoints = research(base, child, [success(child)])
    original = deepcopy(checkpoints)
    result = collection_dsh.coverage(loop)
    parent, actual = result['rows']
    assert result['status'] == 'complete'
    assert result['successful_count'] == result['required_count'] == 1
    assert parent['outcome'] == 'scope_resolved' and parent['initial_outcome'] == 'needs_scope'
    assert parent['initial_reason'] == 'No public query scope'
    assert parent['result_count'] is None and parent['saved_count'] is None
    assert parent['resolved_by'] == [actual['unit_id']]
    assert actual['source'] == 'clinical_term' and actual['outcome'] == 'success'
    assert checkpoints == original


@pytest.mark.parametrize('outcome', ['running', 'failed', 'unknown', 'budget_deferred'])
def test_scoped_but_unsuccessful_query_keeps_round_partial(outcome):
    base, child = manifests()
    receipt = dict(round_id=child['round_id'], unit_id=child['units'][0]['id'], outcome=outcome)
    loop, _ = research(base, child, [receipt])
    result = collection_dsh.coverage(loop)
    assert result['status'] == 'partial' and result['successful_count'] == 0
    assert result['unresolved_ids'] == [child['units'][0]['id']]
    assert result['rows'][0]['outcome'] == 'scope_resolved'


@pytest.mark.parametrize('change', [dict(truncated=True), dict(ingestion_complete=False)])
def test_truncation_and_pending_save_remain_unresolved(change):
    base, child = manifests()
    loop, _ = research(base, child, [success(child, **change)])
    result = collection_dsh.coverage(loop)
    assert result['status'] == 'partial' and result['successful_count'] == 0
    assert result['unresolved_ids'] == [child['units'][0]['id']]


def test_intent_before_ledger_creation_is_still_pending_after_scope_resolution():
    base, child = manifests()
    loop, _ = research(base, child, indexed_only=True)
    result = collection_dsh.coverage(loop)
    assert result['status'] == 'partial' and result['required_count'] == 1
    assert result['rows'][1]['outcome'] == 'pending'
    assert result['unresolved_ids'] == [child['units'][0]['id']]


@pytest.mark.parametrize('change', [dict(source='search'), dict(category='company'),
                                  dict(target_id='unrelated-watch'), dict(applicability_evidence_ids=[]),
                                  dict(schedule_due=False),
                                  dict(capability='disabled', reason='User disabled')])
def test_unrelated_or_excluded_query_cannot_resolve_requirement(change):
    base, child = manifests(child_changes=change)
    assert resolved_scopes(base, [child]) == {}
    loop, _ = research(base, child)
    result = collection_dsh.coverage(loop)
    assert result['rows'][0]['outcome'] == 'needs_scope' and result['status'] == 'partial'


@pytest.mark.parametrize('change', [dict(project_id='other'), dict(round_id='other'), dict(binding='other')])
def test_resolution_cannot_cross_project_round_or_configuration(change):
    base, child = manifests(plan_changes=change)
    assert resolved_scopes(base, [child]) == {}
    loop, _ = research(base, child)
    with pytest.raises(ValueError, match='outside research round'):
        collection_dsh.coverage(loop)


@pytest.mark.parametrize('change', [dict(query='Known sponsor'), dict(target_id='watch-1'),
                                  dict(capability='not_configured'), dict(capability='not_integrated')])
def test_concrete_scope_or_missing_configuration_cannot_be_removed_by_supplement(change):
    base, child = manifests(parent_changes=change)
    assert resolved_scopes(base, [child]) == {}
    loop, _ = research(base, child, [success(child)])
    result = collection_dsh.coverage(loop)
    assert result['status'] == 'partial' and base['units'][0]['id'] in result['unresolved_ids']


def test_one_success_does_not_hide_another_concrete_scope_failure():
    base, child = manifests()
    other = plan([candidate() | dict(source='clinical_sponsor', category='trial', target_id='scope:sponsor',
                                    applicability_evidence_ids=['public-evidence-2'])])
    loop, saved = research(base, child, [success(child)])
    saved[SUPPLEMENTS].append(dict(stage='collection_extra:other', plan=other))
    saved['collection_extra:other'] = dict(plan=other, receipts=[dict(
        round_id=other['round_id'], unit_id=other['units'][0]['id'], outcome='failed')])
    result = collection_dsh.coverage(loop)
    assert result['status'] == 'partial' and result['successful_count'] == 1
    assert result['required_count'] == 2
    assert result['unresolved_ids'] == [other['units'][0]['id']]

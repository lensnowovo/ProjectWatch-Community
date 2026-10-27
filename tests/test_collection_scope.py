"""Frozen synthetic scope scenarios, no settings or live project reads."""
from copy import deepcopy

import pytest

from project_watch.studio.collection_plan import reconcile
from project_watch.studio.collection_scope import BASE_DIRECTIONS, build_collection_plan

AT = '2026-10-26T12:00:00+08:00'
CAPABILITIES = dict.fromkeys(('search', 'site', 'cninfo', 'openalex_affiliation',
                             'openalex_topic', 'openalex_author', 'patent_search',
                             'clinical_sponsor', 'clinical_term', 'cn_regulatory'), 'available')


def build(**changes):
    args = dict(round_id='round-1', project={'id': 'company-1', 'legal_name': 'Synthetic Company'},
                binding='public-scope-v1', public_plan={}, watches=[], capabilities=CAPABILITIES,
                at=AT, mode='first')
    return build_collection_plan(**(args | changes))


def watch(**changes):
    return dict(id='relation-1', visibility='public', status='trial', scope_kind='relation',
                relation_type='supplier', axis='industry', queries=['Synthetic Supplier'],
                lifecycle_version='v1', cadence='low') | changes


def test_first_round_includes_all_directions_without_fabricating_known_relations():
    p = build()
    search = [u for u in p['units'] if u['source'] == 'search']
    assert {u['category'] for u in search} == set(BASE_DIRECTIONS)
    assert all(u['target_id'] == 'company-1' for u in search)
    assert all(u['required'] for u in search)
    assert reconcile(p, [])['status'] == 'partial'


def test_specialist_company_has_distinct_sponsor_and_intervention_routes():
    p = build(public_plan={'trial_sponsors': ['Synthetic Sponsor'],
                           'trial_terms': ['Synthetic Device'],
                           'affiliation_names': ['Synthetic Institute'],
                           'paper_queries': ['Synthetic Technique'],
                           'paper_authors': ['Synthetic Author']})
    by_source = {u['source']: u for u in p['units']}
    assert by_source['clinical_sponsor']['query'] == 'Synthetic Sponsor'
    assert by_source['clinical_term']['query'] == 'Synthetic Device'
    assert by_source['openalex_affiliation']['query'] == 'Synthetic Institute'
    assert by_source['openalex_author']['query'] == 'Synthetic Author'
    assert by_source['openalex_topic']['query'] == 'Synthetic Technique'
    assert by_source['clinical_term']['window_start'] is None
    assert by_source['openalex_topic']['window_start'] is not None


def test_hospital_led_trial_does_not_require_company_sponsor():
    p = build(public_plan={'trial_terms': ['Synthetic Intervention'], 'is_biomed': False})
    trial = [u for u in p['units'] if u['category'] == 'trial']
    assert any(u['source'] == 'clinical_term' and u['capability'] == 'available' for u in trial)
    assert not any(u['source'] == 'clinical_sponsor' for u in trial)


def test_nonclinical_exclusion_requires_explicit_recorded_basis():
    decision = {'capability': 'not_applicable', 'reason': 'Confirmed scope excludes clinical products',
                'evidence_ids': ['synthetic-scope-confirmation']}
    p = build(source_decisions={'clinical_sponsor': decision})
    unit = next(u for u in p['units'] if u['source'] == 'clinical_sponsor')
    assert unit['disposition'] == 'not_applicable'
    assert not unit['required']
    assert unit['applicability_evidence_ids'] == decision['evidence_ids']
    with pytest.raises(ValueError, match='recorded basis'):
        build(source_decisions={'clinical_sponsor': decision | {'evidence_ids': []}})


def test_industry_boolean_cannot_hide_missing_clinical_scope():
    for flag in (True, False):
        p = build(public_plan={'is_biomed': flag})
        clinical = next(u for u in p['units'] if u['source'] == 'clinical_sponsor')
        assert clinical['capability'] == 'needs_scope'


def test_missing_search_configuration_and_adapter_are_visible():
    p = build(capabilities={'search': 'not_configured'})
    assert all(u['capability'] == 'not_configured' for u in p['units'] if u['source'] == 'search')
    assert all(u['capability'] == 'not_integrated' for u in p['units'] if u['source'] != 'search')
    assert reconcile(p, [])['status'] == 'partial'


def test_existing_peers_keep_their_own_target_and_queries():
    p = build(public_plan={'competitors': [{'name': 'Synthetic Peer A'},
                                           {'name': 'Synthetic Peer B', 'query': 'Synthetic Peer B update'}]})
    peers = [u for u in p['units'] if u['category'] == 'competitor']
    assert {u['target_id'] for u in peers} == {'peer:Synthetic Peer A', 'peer:Synthetic Peer B'}


@pytest.mark.parametrize('mode,required', [('first', True), ('refresh', True), ('incremental', False)])
def test_saved_relation_cadence_preserved_and_refresh_checked(mode, required):
    w = watch()
    before = deepcopy(w)
    p = build(watches=[w], mode=mode)
    unit = next(u for u in p['units'] if u['target_id'] == 'watch:relation-1')
    assert unit['required'] is required
    assert unit['schedule_due'] is required
    assert unit['due_at'] is None  # no invented next-run date
    assert unit['category'] == 'chain'
    assert w == before


def test_refresh_does_not_wake_questions_or_expose_private_retired_scope():
    watches = [watch(id='sleeping', scope_kind='question'), watch(id='private', visibility='internal'),
               watch(id='retired', status='retired'), watch(id='due-question', scope_kind='question')]
    p = build(watches=watches, mode='refresh', due_watch_ids=['due-question'])
    targets = {u['target_id'] for u in p['units'] if u['target_id'].startswith('watch:')}
    assert targets == {'watch:due-question'}


def test_topic_only_admits_selected_runnable_questions():
    p = build(mode='topic', watches=[watch(), watch(id='question', scope_kind='question')],
              due_watch_ids=['relation-1', 'question'])
    assert len(p['units']) == 1
    assert p['units'][0]['target_id'] == 'watch:question'


def test_no_silent_query_limit_and_lifecycle_change_invalidates_identity():
    qs = [f'Synthetic scoped query {n}' for n in range(40)]
    p = build(watches=[watch(queries=qs)])
    units = [u for u in p['units'] if u['target_id'] == 'watch:relation-1']
    assert len(units) == 40
    q = build(watches=[watch(queries=qs, lifecycle_version='v2')])
    assert {u['id'] for u in units}.isdisjoint({u['id'] for u in q['units']})


def test_specialist_scopes_extend_company_baseline_and_keep_source_basis():
    scope = dict(id='scope-1', adapter='openalex_affiliation', query='Synthetic University',
                 capability='available', reason='Public source affiliation candidate', historical=True,
                 ref={'evidence_version_id': 'public-evidence-1'})
    p = build(source_queries=[scope])
    units = [u for u in p['units'] if u['source'] == 'openalex_affiliation']
    assert {u['query'] for u in units} == {'Synthetic Company', 'Synthetic University'}
    scoped = next(u for u in units if u['target_id'] == 'scope:scope-1')
    assert scoped['applicability_evidence_ids'] == ['public-evidence-1']


@pytest.mark.parametrize('capability', ['available', 'needs_scope', 'disabled'])
def test_equal_specialist_scope_has_one_visible_unit_without_default_bypass(capability):
    p = build(source_queries=[dict(id='scope-1', adapter='openalex_affiliation', query='Synthetic Company',
        capability=capability, reason='Synthetic explicit scope', historical=True,
        ref={'evidence_version_id': 'public-evidence-1'})])
    units = [u for u in p['units'] if u['source'] == 'openalex_affiliation']
    assert len(units) == 1 and units[0]['target_id'] == 'scope:scope-1'
    assert units[0]['capability'] == capability

"""Synthetic acceptance for host-owned collection coverage (no network)."""
from copy import deepcopy

import pytest

from project_watch.studio.collection_plan import make_plan, reconcile

NOW = '2026-10-26T12:00:00+08:00'


def candidate(**changes):
    return dict(source='news', target_id='entity-1', category='company',
                query_version='1', capability='available', query='Synthetic Company',
                window_start='2026-07-28T12:00:00+08:00', window_end=NOW,
                **changes)


def plan(candidates=None, **changes):
    args = dict(round_id='round-1', project_id='project-1', binding='scope-account-v1',
                mode='first', candidates=candidates if candidates is not None else [candidate()], at=NOW)
    return make_plan(**(args | changes))


def success(p, **changes):
    return dict(round_id=p['round_id'], unit_id=p['units'][0]['id'], outcome='success',
                result_count=0, saved_count=0, request_ids=['request-1'],
                ingestion_complete=True, truncated=False) | changes


@pytest.mark.parametrize('mode,due', [('first', True), ('refresh', True),
                                    ('incremental', False), ('topic', False)])
def test_cadence_is_explicit_and_manual_refresh_bypasses_only_schedule(mode, due):
    p = plan([candidate(due_at='2026-11-01T12:00:00+08:00')], mode=mode)
    assert p['units'][0]['required'] is due
    assert p['units'][0]['disposition'] == ('pending' if due else 'not_due')


@pytest.mark.parametrize('capability', ['not_configured', 'not_integrated', 'needs_scope'])
def test_unavailable_cannot_be_reported_as_successful_zero(capability):
    c = candidate() | dict(capability=capability, reason='Synthetic missing requirement')
    p = plan([c], mode='refresh')
    r = reconcile(p, [])
    assert r['status'] == 'partial'
    assert r['rows'][0]['outcome'] == capability
    assert r['rows'][0]['result_count'] is None
    with pytest.raises(ValueError, match='undispatched'):
        reconcile(p, [success(p)])


@pytest.mark.parametrize('capability', ['disabled', 'not_applicable'])
def test_explicit_exclusion_keeps_reason_without_dispatch(capability):
    p = plan([candidate() | dict(capability=capability, reason='Confirmed scope decision')])
    r = reconcile(p, [])
    assert r['required_count'] == 0
    assert r['rows'][0]['reason'] == 'Confirmed scope decision'
    assert r['unresolved_ids'] == []


def test_zero_results_require_actual_request_and_completed_ingestion():
    p = plan()
    assert reconcile(p, [])['status'] == 'partial'
    assert reconcile(p, [success(p)])['status'] == 'complete'
    for changes in ({'truncated': True}, {'ingestion_complete': False}, {'truncated': None}):
        assert reconcile(p, [success(p, **changes)])['status'] == 'partial'
    with pytest.raises(ValueError, match='provenance'):
        reconcile(p, [success(p, request_ids=[])])


@pytest.mark.parametrize('outcome', ['running', 'failed', 'unknown', 'budget_deferred'])
def test_unfinished_receipts_do_not_become_zero(outcome):
    p = plan()
    r = reconcile(p, [dict(round_id=p['round_id'], unit_id=p['units'][0]['id'], outcome=outcome)])
    assert r['status'] == 'partial'
    assert r['rows'][0]['result_count'] is None


def test_empty_plan_never_certifies_coverage():
    assert reconcile(plan([]), [])['status'] == 'partial'


def test_round_and_unit_binding_rejects_unrelated_or_duplicate_receipts():
    p = plan()
    for changes in ({'round_id': 'old-round'}, {'unit_id': 'other-query'}):
        with pytest.raises(ValueError, match='outside'):
            reconcile(p, [success(p, **changes)])
    with pytest.raises(ValueError, match='duplicate'):
        reconcile(p, [success(p), success(p)])


@pytest.mark.parametrize('changes', [{'result_count': True}, {'result_count': -1},
                                  {'saved_count': 1}, {'saved_count': False}])
def test_counts_do_not_accept_booleans_negative_or_impossible_totals(changes):
    p = plan()
    with pytest.raises(ValueError, match='counts'):
        reconcile(p, [success(p, **changes)])


def test_identity_is_stable_but_scope_account_project_and_window_bound():
    p = plan()
    key = p['units'][0]['id']
    assert plan(round_id='next-round')['units'][0]['id'] == key
    for changes in ({'binding': 'scope-account-v2'}, {'project_id': 'other-project'}):
        assert plan(**changes)['units'][0]['id'] != key
    for changes in ({'query_version': '2'}, {'window_start': None}, {'target_id': 'entity-2'}):
        assert plan([candidate() | changes])['units'][0]['id'] != key


def test_dedup_is_deterministic_and_does_not_mutate_inputs():
    cs = [candidate(priority=2), candidate() | dict(target_id='entity-2', priority=1)]
    original = deepcopy(cs)
    p = plan([*cs, cs[0]])
    assert len(p['units']) == 2
    assert p == plan(list(reversed(cs)))
    assert cs == original
    with pytest.raises(ValueError, match='conflicting'):
        plan([cs[0], cs[0] | {'priority': 0}])


@pytest.mark.parametrize('changes', [{'window_end': '2026-11-01T00:00:00+08:00'},
                                  {'window_start': '2026-11-01T00:00:00+08:00'},
                                  {'window_end': '2026-10-26T12:00:00'},
                                  {'query': ''}, {'priority': True}])
def test_invalid_scope_is_rejected_before_dispatch(changes):
    with pytest.raises(ValueError):
        plan([candidate() | changes])


def test_due_order_compares_instants_across_timezone_offsets():
    p = plan([candidate() | dict(
        target_id='earlier', due_at='2026-10-25T23:00:00-02:00'),
        candidate() | dict(target_id='later', due_at='2026-10-25T22:00:00-04:00')])
    assert [u['target_id'] for u in p['units']] == ['earlier', 'later']


def test_duplicate_manifest_units_cannot_disappear_during_reconciliation():
    p = plan()
    p['units'].append(deepcopy(p['units'][0]))
    with pytest.raises(ValueError, match='duplicate collection plan'):
        reconcile(p, [])

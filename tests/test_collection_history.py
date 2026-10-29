"""Cross-round windows and fairness use durable successes, never model claims."""
from contextlib import closing
from copy import deepcopy

import pytest
import test_collection_host as host_fixtures
from test_collection_plan import candidate, plan, success

from project_watch.studio import collection_history as history
from project_watch.studio import collection_host, dsh_runtime
from project_watch.studio.collection_ledger import CollectionLedger
from project_watch.studio.collection_plan import reconcile
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http


@pytest.fixture
def execution(tmp_path):
    return host_fixtures.setup.__wrapped__(tmp_path)[1]


def bound(execution, candidates=None):
    raw = plan(candidates, project_id=execution.job['project_id'], round_id=execution.job['id'])
    return history.schedule(raw, 'config-v1', {})


def prior(p, **changes):
    return {history.query_key(p['project_id'], 'config-v1', p['units'][0]): dict(
        last_success_at='2026-10-25T04:00:00+00:00', window_end='2026-10-25T04:00:00+00:00',
        baseline_at='2026-10-20T04:00:00+00:00') | changes}


def test_query_index_identity_survives_new_window_and_manifest_but_not_scope_or_config():
    p = plan()
    u = p['units'][0]
    key = history.query_key(p['project_id'], 'config-v1', u)
    assert history.query_key(p['project_id'], 'config-v1', u | {'window_end': 'next', 'id': 'new'}) == key
    for field in ('source', 'target_id', 'category', 'query', 'query_version'):
        assert history.query_key(p['project_id'], 'config-v1', u | {field: 'changed'}) != key
    assert history.query_key('other-project', 'config-v1', u) != key
    assert history.query_key(p['project_id'], 'other-config', u) != key


def test_incremental_overlap_and_periodic_full_lookback_are_frozen():
    p = plan(mode='incremental')
    original = deepcopy(p)
    next_plan = history.schedule(p, 'config-v1', prior(p), overlap_days=3, baseline_days=20)
    assert next_plan['units'][0]['window_start'] == '2026-10-22T04:00:00+00:00'
    assert next_plan['units'][0]['full_scan'] is False
    assert next_plan['history_policy'] == dict(overlap_days=3, baseline_days=20)
    assert next_plan['units'][0]['id'] != p['units'][0]['id']
    assert p == original
    broad = history.schedule(p, 'config-v1', prior(p, baseline_at='2026-09-01T04:00:00+00:00'))
    assert broad['units'][0]['window_start'] == p['units'][0]['window_start']
    assert broad['units'][0]['full_scan'] is True


def test_unfiltered_sources_and_historical_baselines_do_not_fake_an_incremental_window():
    p = plan()
    unfiltered = history.schedule(p, 'config-v1', prior(p, window_end=None))
    assert unfiltered['units'][0]['window_start'] == p['units'][0]['window_start']
    historical = plan([candidate() | {'window_start': None}])
    assert history.schedule(historical, 'config-v1', prior(historical))['units'][0]['window_start'] is None


def test_oldest_success_and_never_successful_queries_precede_recent_priority_queries():
    p = plan([candidate() | dict(target_id='recent-core', priority=0),
              candidate() | dict(target_id='old-background', priority=2),
              candidate() | dict(target_id='never', priority=2)])
    records = {history.query_key(p['project_id'], 'config-v1', u): dict(
        last_success_at='2026-10-25T00:00:00Z' if u['target_id'] == 'recent-core' else '2026-10-01T00:00:00Z')
        for u in p['units'] if u['target_id'] != 'never'}
    assert [u['target_id'] for u in history.schedule(p, 'config-v1', records)['units']] == [
        'never', 'old-background', 'recent-core']


def test_schedule_choices_and_unavailable_capabilities_are_not_overridden():
    p = plan([candidate(schedule_due=False), candidate() | dict(target_id='disabled',
             capability='disabled', reason='Owner choice')], mode='refresh')
    result = history.schedule(p, 'config-v1', {})
    assert {u['disposition'] for u in result['units']} == {'not_due', 'disabled'}
    assert reconcile(result, [])['required_count'] == 0


def test_successful_empty_window_advances_once_and_keeps_config_binding(execution):
    p = bound(execution)
    ledger = CollectionLedger(execution, p)
    receipt = success(p, source_scope={'window_applied': True})
    ledger.record(receipt)
    ledger.record(receipt)
    CollectionLedger(execution, p)
    records = history.read(execution.store, p['project_id'])
    [saved] = records.values()
    assert saved['window_end'] == saved['last_success_at'] == '2026-10-26T04:00:00+00:00'
    assert saved['baseline_at'] == saved['last_success_at']
    assert history.schedule(p, 'config-v2', records)['units'][0]['last_success_at'] is None
    with closing(execution.store._connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM studio_collection_successes').fetchone()[0] == 1


@pytest.mark.parametrize('outcome', ['failed', 'budget_deferred', 'unknown', 'running'])
def test_unsuccessful_receipts_never_advance_history(execution, outcome):
    p = bound(execution)
    ledger = CollectionLedger(execution, p)
    ledger.record(dict(round_id=p['round_id'], unit_id=p['units'][0]['id'], outcome=outcome))
    assert history.read(execution.store, p['project_id']) == {}


@pytest.mark.parametrize('change', [dict(truncated=True), dict(ingestion_complete=False)])
def test_truncated_or_unstored_success_does_not_advance(execution, change):
    p = bound(execution)
    CollectionLedger(execution, p).record(success(p, **change))
    assert history.read(execution.store, p['project_id']) == {}


def test_cached_result_uses_original_fetch_time_and_unknown_time_cannot_advance(execution):
    p = bound(execution)
    ledger = CollectionLedger(execution, p)
    ledger.record(success(p, cache_provenance=[{'completed_at': '2026-10-01T02:00:00Z'}],
                          source_scope={'window_applied': False}))
    [record] = history.read(execution.store, p['project_id']).values()
    assert record['last_success_at'] == '2026-10-01T02:00:00+00:00' and record['window_end'] is None
    other = bound(execution, [candidate() | dict(target_id='undated')])
    history.record(execution, other, [success(other, cache_provenance=[{}])])
    assert len(history.read(execution.store, p['project_id'])) == 1


def test_index_write_interruption_recovers_from_receipt_without_provider_replay(execution, monkeypatch):
    p = bound(execution)
    ledger = CollectionLedger(execution, p)
    original = history.record
    monkeypatch.setattr(history, 'record', lambda *_: (_ for _ in ()).throw(RuntimeError('synthetic index interruption')))
    with pytest.raises(RuntimeError, match='synthetic index interruption'):
        ledger.record(success(p, source_scope={'window_applied': True}))
    assert history.read(execution.store, p['project_id']) == {}
    monkeypatch.setattr(history, 'record', original)
    reopened = CollectionLedger(execution, p)
    assert reopened.coverage()['status'] == 'complete'
    assert len(history.read(execution.store, p['project_id'])) == 1
    assert not execution.usage()


def test_expired_lease_cannot_backfill_success_index(execution):
    p = bound(execution)
    assert execution.store.finish(execution.job['id'], lease_token=execution.job['lease_token'])
    with pytest.raises(RuntimeError, match='lease'):
        history.record(execution, p, [success(p)])
    assert history.read(execution.store, p['project_id']) == {}


def test_real_host_next_round_uses_own_success_window_and_budget_failure_keeps_it(tmp_path, monkeypatch):
    pipeline, execution = host_fixtures.setup.__wrapped__(tmp_path)
    calls = []

    def send(_method, url, *_args):
        calls.append(url)
        return host_fixtures.response(url)

    pipeline.http_factory = lambda _: Http(send)
    monkeypatch.setattr(dsh_runtime, 'run_dsh', lambda *_: None)
    monkeypatch.setattr(collection_host, 'now', lambda: '2026-10-25T12:00:00+08:00')
    collection_host.admit(execution, mode='first')  # Frozen legacy host-only fixture.
    dsh_runtime.run_bootstrap(pipeline, execution, None)
    first = history.read(pipeline.store, execution.job['project_id'])
    assert len(first) == 3  # Two paper query modes and public filings, not missing/disabled sources.
    assert pipeline.store.finish(execution.job['id'], lease_token=execution.job['lease_token'])
    pipeline.store.enqueue(execution.job['project_id'], 'external', automatic=True)
    second = JobExecution(pipeline.store, pipeline.store.claim())
    pipeline._execution_state.current = second
    monkeypatch.setattr(collection_host, 'now', lambda: '2026-10-26T12:00:00+08:00')
    collection_host.admit(second, mode='incremental')
    dsh_runtime.run_monitor(pipeline, second, None)
    manifest = second.read_checkpoint('collection_round_v1')['plan']
    topic = next(u for u in manifest['units'] if u['source'] == 'openalex_topic')
    affiliation = next(u for u in manifest['units'] if u['source'] == 'openalex_affiliation')
    assert topic['window_start'] == '2026-10-18T04:00:00+00:00'
    assert affiliation['window_start'] is None
    assert any('2026-10-18' in url for url in calls)
    after = history.read(pipeline.store, second.job['project_id'])
    assert after[topic['history_key']]['window_end'] == '2026-10-26T04:00:00+00:00'
    assert pipeline.store.finish(second.job['id'], lease_token=second.job['lease_token'])
    pipeline.store.enqueue(second.job['project_id'], 'external', automatic=True)
    third = JobExecution(pipeline.store, pipeline.store.claim())
    pipeline._execution_state.current = third
    third.budgets.update(search=0, model=0, page=0, open_data=0)
    count = len(calls)
    monkeypatch.setattr(collection_host, 'now', lambda: '2026-10-27T12:00:00+08:00')
    collection_host.admit(third, mode='incremental')
    dsh_runtime.run_monitor(pipeline, third, None)
    assert len(calls) == count and history.read(pipeline.store, third.job['project_id']) == after

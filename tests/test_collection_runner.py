"""Host dispatch fault injection with real leased request/checkpoint journals."""
import hashlib

import pytest
import test_studio_dsh_monitor as fixtures
from test_collection_plan import candidate, plan

from project_watch.studio.collection_ledger import CollectionLedger
from project_watch.studio.collection_runner import run_collection
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError, Response


@pytest.fixture
def host(tmp_path):
    research = fixtures.loop.__wrapped__(tmp_path)
    execution = research.execution
    p = plan([candidate(), candidate() | {'target_id': 'entity-2'}], round_id=execution.job['id'], project_id=execution.job['project_id'])
    return research, CollectionLedger(execution, p)


def collector(execution, calls, *, fail=False, truncated=False):
    def collect(unit):
        def transport():
            calls.append(unit['id'])
            if fail:
                raise HttpError('connection_failed')
            return Response(200, {}, b'{}')
        execution.call('search', unit['id'], transport, provider='synthetic')
        return dict(items=[{'key': unit['id'], 'title': 'Synthetic item'}],
                    request_ids=[hashlib.sha256(('search:' + unit['id']).encode()).hexdigest()],
                    truncated=truncated)
    return collect


def test_success_reuses_completed_units_without_dispatch(host):
    research, ledger = host
    calls = []
    collect = collector(ledger.execution, calls)
    for _ in range(2):
        result = run_collection(ledger, collect=collect, ingest=lambda _u, items: len(items), guard=research.guard)
        assert result['status'] == 'complete'
    assert len(calls) == 2
    assert ledger.execution.usage()['search']['requests'] == 2


def test_unknown_stops_following_units_and_restart_does_not_retry(host):
    research, ledger = host
    calls = []
    for _ in range(2):
        with pytest.raises(HttpError):
            run_collection(ledger, collect=collector(ledger.execution, calls, fail=True),
                           ingest=lambda *_: pytest.fail('no ingestion'), guard=research.guard)
        ledger = CollectionLedger(JobExecution(ledger.execution.store, ledger.execution.job), ledger.plan)
    assert len(calls) == 1
    assert ledger.receipts[0]['outcome'] == 'unknown'
    assert ledger.coverage()['status'] == 'partial'


def test_budget_exhaustion_is_deferred_not_success_zero(host):
    research, ledger = host
    ledger.execution.budgets['search'] = 1
    calls = []
    result = run_collection(ledger, collect=collector(ledger.execution, calls),
                            ingest=lambda _u, items: len(items), guard=research.guard)
    assert len(calls) == 1
    assert result['status'] == 'partial'
    assert {r['outcome'] for r in result['rows']} == {'success', 'budget_deferred'}


def test_truncated_page_preserves_partial_coverage(host):
    research, ledger = host
    result = run_collection(ledger, collect=collector(ledger.execution, [], truncated=True),
                            ingest=lambda _u, items: len(items), guard=research.guard)
    assert result['status'] == 'partial'
    assert all(r['outcome'] == 'partial' for r in result['rows'])


def test_crash_after_ingestion_reuses_payload_and_idempotent_save(host, monkeypatch):
    research, ledger = host
    calls, saved = [], {}

    def ingest(_unit, items):
        for item in items:
            saved[item['key']] = item
        return len(items)

    original = ledger.record

    def crash(receipt):
        if receipt['outcome'] == 'success':
            raise RuntimeError('synthetic crash after save')
        return original(receipt)

    monkeypatch.setattr(ledger, 'record', crash)
    with pytest.raises(RuntimeError, match='synthetic crash'):
        run_collection(ledger, collect=collector(ledger.execution, calls), ingest=ingest, guard=research.guard)
    assert len(calls) == len(saved) == 1
    restarted = CollectionLedger(JobExecution(ledger.execution.store, ledger.execution.job), ledger.plan)
    result = run_collection(restarted, collect=collector(restarted.execution, calls),
                            ingest=ingest, guard=research.guard)
    assert result['status'] == 'complete'
    assert len(calls) == len(saved) == 2


def test_known_failure_is_recorded_and_other_source_can_continue(host):
    research, ledger = host
    normal = collector(ledger.execution, [])
    first = ledger.plan['units'][0]['id']

    def collect(unit):
        if unit['id'] == first:
            raise HttpError('invalid_json')
        return normal(unit)

    result = run_collection(ledger, collect=collect, ingest=lambda _u, items: len(items), guard=research.guard)
    assert {r['outcome'] for r in result['rows']} == {'failed', 'success'}


def test_pause_before_collection_does_not_create_a_source_failure(host):
    _research, ledger = host

    def guard():
        raise HttpError('project_paused')

    with pytest.raises(HttpError, match='project_paused'):
        run_collection(ledger, collect=lambda *_: pytest.fail('no dispatch'),
                       ingest=lambda *_: pytest.fail('no ingestion'), guard=guard)
    assert ledger.receipts == []


def test_missing_payload_provenance_is_failure_not_successful_empty_query(host):
    research, ledger = host
    result = run_collection(ledger, collect=lambda _: {'items': [], 'truncated': False},
                            ingest=lambda *_: pytest.fail('unverified payload'), guard=research.guard)
    assert result['status'] == 'partial'
    assert all(r['outcome'] == 'failed' and r['result_count'] is None for r in result['rows'])

"""Host receipts, rather than a model tool call, advance relation cadence."""
import json
from contextlib import closing
from datetime import UTC, datetime

import pytest
import test_collection_host as fixtures
from test_collection_plan import candidate, plan, success

from project_watch.studio import collection_history, collection_host, dsh_runtime
from project_watch.studio.collection_ledger import CollectionLedger
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, Response
from project_watch.studio.settings import SearchConfig


@pytest.fixture
def setup(tmp_path):
    pipeline, execution = fixtures.setup.__wrapped__(tmp_path)
    watch = pipeline.monitoring_plan.add(execution.job['project_id'], dict(
        text='Synthetic supplier', queries=['Synthetic A', 'Synthetic B'], scope_kind='relation',
        reason='Public synthetic relationship', sources=['https://example.org/relation'],
        relation_type='supplier', relation_target='Synthetic supplier', axis='partners', cadence='medium'),
        actor='human', origin='human', visibility='public')
    return pipeline, execution, watch


def ledger_for(execution, watch, queries=None, *, at='2026-10-26T04:00:00+00:00'):
    units = [candidate() | dict(source='search', target_id='watch:' + watch['id'], category='chain',
             query=q, query_version='watch:' + str(watch['lifecycle_version']), window_end=at)
             for q in (queries or watch['queries'])]
    p = plan(units, project_id=execution.job['project_id'], round_id=execution.job['id'], at=at)
    return CollectionLedger(execution, collection_history.schedule(p, 'synthetic-config', {}))


def current(pipeline, execution, watch):
    return next(w for w in pipeline.monitoring_plan.items(execution.job['project_id']) if w['id'] == watch['id'])


def test_all_queries_required_and_reopening_does_not_advance_time(setup):
    pipeline, execution, watch = setup
    ledger = ledger_for(execution, watch)
    one, two = ledger.plan['units']
    ledger.record(success(ledger.plan, unit_id=one['id']))
    partial = current(pipeline, execution, watch)
    assert not partial['last_run_at'] and json.loads(partial['completed_queries']) == [one['query']]
    ledger.record(success(ledger.plan, unit_id=two['id']))
    complete = current(pipeline, execution, watch)
    assert complete['last_run_at'] == '2026-10-26T04:00:00+00:00'
    assert json.loads(complete['completed_queries']) == []
    CollectionLedger(execution, ledger.plan)
    assert current(pipeline, execution, watch) == complete
    assert not pipeline.monitoring_plan.due(execution.job['project_id'], at=datetime(2026, 10, 27, tzinfo=UTC))


@pytest.mark.parametrize('changes', [dict(outcome='failed'), dict(outcome='unknown'),
                                   dict(outcome='budget_deferred'), dict(truncated=True),
                                   dict(ingestion_complete=False)])
def test_incomplete_query_never_advances_relation(setup, changes):
    pipeline, execution, watch = setup
    ledger = ledger_for(execution, watch)
    one, two = ledger.plan['units']
    ledger.record(success(ledger.plan, unit_id=one['id']))
    ledger.record(success(ledger.plan, unit_id=two['id'], **changes))
    assert not current(pipeline, execution, watch)['last_run_at']


def test_successes_across_budget_limited_rounds_use_oldest_query_fetch_time(setup):
    pipeline, execution, watch = setup
    first = ledger_for(execution, watch, ['Synthetic A'])
    first.record(success(first.plan))
    assert not current(pipeline, execution, watch)['last_run_at']
    assert pipeline.store.finish(execution.job['id'], lease_token=execution.job['lease_token'])
    pipeline.store.enqueue(execution.job['project_id'], 'external')
    second_execution = JobExecution(pipeline.store, pipeline.store.claim())
    second = ledger_for(second_execution, watch, ['Synthetic B'], at='2026-10-27T04:00:00+00:00')
    second.record(success(second.plan))
    assert current(pipeline, second_execution, watch)['last_run_at'] == '2026-10-26T04:00:00+00:00'


def test_cached_success_is_not_new_freshness_and_cannot_rewind_existing_schedule(setup):
    pipeline, execution, watch = setup
    with closing(pipeline.store._connect()) as db, db:
        db.execute('UPDATE studio_watchpoints SET last_run_at=? WHERE id=?',
                   ('2026-10-24T04:00:00+00:00', watch['id']))
    ledger = ledger_for(execution, watch)
    for unit in ledger.plan['units']:
        ledger.record(success(ledger.plan, unit_id=unit['id'],
                              cache_provenance=[dict(completed_at='2026-10-01T00:00:00Z')]))
    after = current(pipeline, execution, watch)
    assert after['last_run_at'] == '2026-10-24T04:00:00+00:00'
    assert after['last_attempt_at'] == '2026-10-01T00:00:00+00:00'
    assert json.loads(after['completed_queries']) == []


def test_changed_watch_version_is_not_completed_by_old_inflight_results(setup):
    pipeline, execution, watch = setup
    ledger = ledger_for(execution, watch)
    pipeline.monitoring_plan.update(watch['id'],
                                    {'queries': ['Synthetic edited']}, actor='human', reason='Owner edit')
    for unit in ledger.plan['units']:
        ledger.record(success(ledger.plan, unit_id=unit['id']))
    assert not current(pipeline, execution, watch)['last_run_at']


def test_index_and_schedule_update_roll_back_together_and_recover_without_calls(setup, monkeypatch):
    pipeline, execution, watch = setup
    ledger = ledger_for(execution, watch)
    original = collection_history._advance_relations
    monkeypatch.setattr(collection_history, '_advance_relations',
                        lambda *_: (_ for _ in ()).throw(RuntimeError('synthetic schedule interruption')))
    with pytest.raises(RuntimeError, match='schedule interruption'):
        ledger.record(success(ledger.plan))
    assert not collection_history.read(execution.store, execution.job['project_id'])
    monkeypatch.setattr(collection_history, '_advance_relations', original)
    CollectionLedger(execution, ledger.plan)
    assert len(collection_history.read(execution.store, execution.job['project_id'])) == 1
    assert not current(pipeline, execution, watch)['last_run_at']
    assert not execution.usage()


def test_runtime_host_success_updates_cadence_before_model_and_explore_only_reuses(setup, monkeypatch):
    pipeline, execution, watch = setup
    settings = pipeline.settings_store.load()
    settings.search = SearchConfig(provider='tavily', api_key='synthetic')
    pipeline.settings_store.save(settings)
    calls = []

    def send(method, url, headers, body, timeout):
        if 'api.tavily.com' in url:
            calls.append(json.loads(body)['query'])
            return Response(200, {}, b'{"results":[]}')
        return fixtures.response(url)

    pipeline.http_factory = lambda _: Http(send)
    def model_turn(research, *_):
        before = current(pipeline, execution, watch)
        assert before['last_run_at']
        assert watch['id'] not in {w['id'] for w in pipeline.monitoring_plan.due(execution.job['project_id'])}
        sent = list(calls)
        for query in watch['queries']:
            response = research.tool('pw_explore', dict(watch_id=watch['id'], query=query))
            assert response['collection']['outcome'] == 'success'
        assert calls == sent
        assert current(pipeline, execution, watch) == before
        # A new relation discovered by DSH uses its own lifecycle-bound receipt.
        added = pipeline.monitoring_plan.add(execution.job['project_id'], dict(
            text='New supplier', queries=['Synthetic new'], scope_kind='relation',
            reason='Synthetic evidence', sources=['https://example.org/new'], relation_type='supplier',
            relation_target='Synthetic new', axis='partners'), actor='human', origin='human', visibility='public')
        response = research.tool('pw_explore', dict(watch_id=added['id'], query='Synthetic new'))
        assert response['collection']['outcome'] == 'success'
        assert current(pipeline, execution, added)['last_run_at']

    monkeypatch.setattr(dsh_runtime, 'run_dsh', model_turn)
    dsh_runtime.run_monitor(pipeline, execution, None)
    assert execution.read_checkpoint(collection_host.ADMISSION)['mode'] == 'refresh'

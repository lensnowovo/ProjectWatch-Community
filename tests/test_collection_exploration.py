"""Host question collection advances one cycle, not an answer or a cache clock."""
import json
from contextlib import closing

import pytest
import test_collection_host as fixtures
from test_collection_plan import candidate, plan, success

from project_watch.studio import collection_history, dsh_runtime
from project_watch.studio.collection_ledger import CollectionLedger
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, Response
from project_watch.studio.settings import SearchConfig


@pytest.fixture
def setup(tmp_path):
    pipeline, execution = fixtures.setup.__wrapped__(tmp_path)
    watch = pipeline.monitoring_plan.add(execution.job['project_id'], dict(
        text='Synthetic research question', queries=['Synthetic A', 'Synthetic B'],
        tier='topic', exit_rule='A public source answers the synthetic question', cadence='medium'),
        actor='human', origin='human', visibility='public')
    return pipeline, execution, watch


def manifest(execution, watch):
    units = [candidate() | dict(source='search', target_id='watch:' + watch['id'], category='company',
             query=q, query_version='watch:' + str(watch['lifecycle_version'])) for q in watch['queries']]
    return collection_history.schedule(plan(units, project_id=execution.job['project_id'],
                                           round_id=execution.job['id']), 'synthetic-config', {})


def cycle(pipeline, execution, watch):
    with closing(pipeline.store._connect()) as db:
        row = db.execute('SELECT * FROM studio_exploration_cycles WHERE watch_id=? AND cycle=?',
                         (watch['id'], execution.job['id'])).fetchone()
    return dict(row) if row else None


def state(pipeline, execution, watch):
    return pipeline.monitoring_plan.explorations.view(execution.job['project_id'])[watch['id']]


def test_host_complete_set_is_collected_then_finished_once_without_answering(setup):
    pipeline, execution, watch = setup
    engine = pipeline.monitoring_plan.explorations
    p = manifest(execution, watch)
    engine.admit_collection(execution, p)
    ledger = CollectionLedger(execution, p)
    first, second = p['units']
    ledger.record(success(p, unit_id=first['id']))
    assert cycle(pipeline, execution, watch)['state'] == 'collecting'
    ledger.record(success(p, unit_id=second['id']))
    assert cycle(pipeline, execution, watch)['state'] == 'collected'
    before = state(pipeline, execution, watch)
    assert before['state'] == 'active' and before['no_gain'] == 0 and before['reason'] == 'exploring'
    engine.admit_collection(execution, p)
    CollectionLedger(execution, p)
    engine.finish(execution.job['project_id'], execution.job['id'])
    assert state(pipeline, execution, watch)['no_gain'] == 1
    engine.finish(execution.job['project_id'], execution.job['id'])
    assert state(pipeline, execution, watch)['no_gain'] == 1
    assert state(pipeline, execution, watch)['reason'] != 'answered'


@pytest.mark.parametrize('changes', [dict(outcome='failed'), dict(outcome='unknown'),
                                   dict(outcome='budget_deferred'), dict(truncated=True),
                                   dict(ingestion_complete=False), dict(cache_provenance=[{}])])
def test_partial_unknown_truncated_or_undated_cache_cannot_exhaust_question(setup, changes):
    pipeline, execution, watch = setup
    engine = pipeline.monitoring_plan.explorations
    p = manifest(execution, watch)
    engine.admit_collection(execution, p)
    ledger = CollectionLedger(execution, p)
    ledger.record(success(p, unit_id=p['units'][0]['id']))
    ledger.record(success(p, unit_id=p['units'][1]['id'], **changes))
    assert cycle(pipeline, execution, watch)['state'] == 'collecting'
    engine.finish(execution.job['project_id'], execution.job['id'])
    assert state(pipeline, execution, watch)['no_gain'] == 0


def test_reused_prior_cycle_cache_does_not_add_another_no_gain_round(setup):
    pipeline, execution, watch = setup
    engine = pipeline.monitoring_plan.explorations
    p = manifest(execution, watch)
    engine.admit_collection(execution, p)
    ledger = CollectionLedger(execution, p)
    for unit in p['units']:
        ledger.record(success(p, unit_id=unit['id']))
    engine.finish(execution.job['project_id'], execution.job['id'])
    assert pipeline.store.finish(execution.job['id'], lease_token=execution.job['lease_token'])
    pipeline.store.enqueue(execution.job['project_id'], 'external')
    next_execution = JobExecution(pipeline.store, pipeline.store.claim())
    next_plan = manifest(next_execution, watch)
    engine.admit_collection(next_execution, next_plan)
    next_ledger = CollectionLedger(next_execution, next_plan)
    for unit in next_plan['units']:
        next_ledger.record(success(next_plan, unit_id=unit['id'],
                                   cache_provenance=[dict(completed_at='2026-10-26T04:00:00+00:00')]))
    assert cycle(pipeline, next_execution, watch)['state'] == 'reused'
    engine.finish(next_execution.job['project_id'], next_execution.job['id'])
    assert state(pipeline, next_execution, watch)['no_gain'] == 1


def test_sleeping_and_changed_generation_cannot_be_admitted_by_frozen_plan(setup):
    pipeline, execution, watch = setup
    engine = pipeline.monitoring_plan.explorations
    p = manifest(execution, watch)
    engine.refresh(execution.job['project_id'])
    engine.stop(watch, 'no_gain')
    with pytest.raises(ValueError, match='not runnable'):
        engine.admit_collection(execution, p)
    assert cycle(pipeline, execution, watch) is None
    # Owner editing the question does not authorize the old frozen query set.
    pipeline.monitoring_plan.update(watch['id'], {'queries': ['Edited question']}, actor='human')
    with pytest.raises(ValueError, match='scope changed'):
        engine.admit_collection(execution, p)


def test_receipt_projection_interruption_rolls_back_and_reopens_without_http(setup, monkeypatch):
    pipeline, execution, watch = setup
    engine = pipeline.monitoring_plan.explorations
    p = manifest(execution, watch)
    engine.admit_collection(execution, p)
    ledger = CollectionLedger(execution, p)
    ledger.record(success(p, unit_id=p['units'][0]['id']))
    original = type(engine).record_collection

    def interrupted(db, plan, successful, **kwargs):
        original(db, plan, successful, **kwargs)
        raise RuntimeError('synthetic projection interruption')

    monkeypatch.setattr(type(engine), 'record_collection', staticmethod(interrupted))
    with pytest.raises(RuntimeError, match='synthetic projection'):
        ledger.record(success(p, unit_id=p['units'][1]['id']))
    assert cycle(pipeline, execution, watch)['state'] == 'collecting'
    monkeypatch.setattr(type(engine), 'record_collection', staticmethod(original))
    CollectionLedger(execution, p)
    assert cycle(pipeline, execution, watch)['state'] == 'collected'
    assert not execution.usage()


def test_three_new_successful_cycles_stop_only_question_not_standing_relation(setup):
    pipeline, execution, watch = setup
    pid = execution.job['project_id']
    engine = pipeline.monitoring_plan.explorations
    relation = pipeline.monitoring_plan.add(pid, dict(
        text='Synthetic standing supplier', queries=['Supplier'], scope_kind='relation',
        relation_type='supplier', relation_target='Synthetic supplier', axis='partners',
        reason='Synthetic public relationship', sources=['https://example.org/supplier']),
        actor='human', origin='human', visibility='public')
    for index, day in enumerate((26, 27, 28)):
        if index:
            pipeline.store.enqueue(pid, 'external')
            execution = JobExecution(pipeline.store, pipeline.store.claim())
        p = manifest(execution, watch)
        # Freeze distinct later acquisitions, not three readings of one cache.
        p['created_at'] = f'2026-10-{day}T04:00:00+00:00'
        engine.admit_collection(execution, p)
        ledger = CollectionLedger(execution, p)
        for unit in p['units']:
            ledger.record(success(p, unit_id=unit['id']))
        engine.finish(pid, execution.job['id'])
        assert state(pipeline, execution, watch)['no_gain'] == index + 1
        assert pipeline.store.finish(execution.job['id'], lease_token=execution.job['lease_token'])
    assert state(pipeline, execution, watch)['state'] == 'stopped'
    assert state(pipeline, execution, watch)['reason'] == 'no_gain'
    assert relation['id'] not in engine.view(pid)
    assert next(w for w in pipeline.monitoring_plan.items(pid) if w['id'] == relation['id']) == relation


def test_changed_question_generation_cannot_be_completed_by_prior_receipts(setup):
    pipeline, execution, watch = setup
    engine = pipeline.monitoring_plan.explorations
    p = manifest(execution, watch)
    engine.admit_collection(execution, p)
    ledger = CollectionLedger(execution, p)
    pipeline.monitoring_plan.update(watch['id'], {'queries': ['Owner edited query']}, actor='human')
    engine.refresh(execution.job['project_id'])
    for unit in p['units']:
        ledger.record(success(p, unit_id=unit['id']))
    assert cycle(pipeline, execution, watch)['state'] == 'collecting'
    engine.finish(execution.job['project_id'], execution.job['id'])
    assert state(pipeline, execution, watch)['no_gain'] == 0


def test_actual_host_then_model_explore_reuses_receipt_and_cycle(setup, monkeypatch):
    pipeline, execution, watch = setup
    from project_watch.studio.progressive_bootstrap import freeze
    freeze(execution, 'monitor', materials=False)  # Resume the legacy explore protocol.
    settings = pipeline.settings_store.load()
    settings.search = SearchConfig(providers=['tavily'], keys={'tavily': 'synthetic'}, google_news=False)
    pipeline.settings_store.save(settings)
    calls = []

    def send(_method, url, _headers, body, _timeout):
        if 'api.tavily.com' in url:
            calls.append(json.loads(body)['query'])
            return Response(200, {}, b'{"results":[]}')
        return fixtures.response(url)

    pipeline.http_factory = lambda _: Http(send)

    def model_turn(research, *_):
        assert cycle(pipeline, execution, watch)['state'] == 'collected'
        before, sent = state(pipeline, execution, watch), list(calls)
        for query in watch['queries']:
            result = research.tool('pw_explore', {'watch_id': watch['id'], 'query': query})
            assert result['collection']['outcome'] == 'success'
        assert calls == sent and state(pipeline, execution, watch) == before

    monkeypatch.setattr(dsh_runtime, 'run_dsh', model_turn)
    dsh_runtime.run_monitor(pipeline, execution, None)
    assert calls.count('Synthetic A') == calls.count('Synthetic B') == 1

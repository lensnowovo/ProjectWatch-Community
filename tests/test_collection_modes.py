"""Manual versus scheduled intent is durable, never inferred from a UI label."""
from contextlib import closing

import pytest
import test_collection_host as fixtures

from project_watch.studio import collection_host, dsh_runtime
from project_watch.studio.collection_ledger import STAGE
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http


@pytest.fixture
def setup(tmp_path):
    return fixtures.setup.__wrapped__(tmp_path)


@pytest.mark.parametrize('automatic,mode', [(True, 'incremental'), (False, 'refresh')])
def test_runtime_uses_frozen_origin_and_refresh_bypasses_only_relation_cadence(setup, monkeypatch, automatic, mode):
    pipeline, old = setup
    assert pipeline.store.finish(old.job['id'], lease_token=old.job['lease_token'])
    watch = pipeline.monitoring_plan.add(old.job['project_id'], {
        'text': 'Synthetic supplier', 'queries': ['Synthetic supplier'], 'scope_kind': 'relation',
        'reason': 'Synthetic public relation', 'sources': ['https://example.org/relation'],
        'relation_type': 'supplier', 'relation_target': 'Synthetic supplier', 'axis': 'partners', 'cadence': 'low'},
        actor='human', origin='human', visibility='public')
    pipeline.monitoring_plan.mark_run([watch['id']])
    job = pipeline.store.enqueue(old.job['project_id'], 'external', automatic=automatic)
    execution = JobExecution(pipeline.store, pipeline.store.claim())
    pipeline._execution_state.current = execution
    execution.budgets.update(search=0, model=0, page=0, open_data=0)
    pipeline.http_factory = lambda _: Http(lambda *_: pytest.fail('zero budget must prevent provider calls'))
    monkeypatch.setattr(dsh_runtime, 'run_dsh', lambda research, *_: research.tool('pw_status', {}))
    dsh_runtime.run_monitor(pipeline, execution, None)
    assert execution.read_checkpoint(collection_host.ADMISSION)['mode'] == mode
    plan = execution.read_checkpoint(STAGE)['plan']
    assert plan['mode'] == mode and job['id'] == plan['round_id']
    [unit] = [u for u in plan['units'] if u['target_id'] == 'watch:' + watch['id']]
    assert unit['schedule_due'] is (not automatic)
    assert not execution.usage()
    after = next(w for w in pipeline.monitoring_plan.items(old.job['project_id']) if w['id'] == watch['id'])
    assert after['cadence'] == 'low'  # refresh is one round, not a user-setting mutation


def test_requesting_refresh_does_not_expand_an_already_admitted_scheduled_job(setup):
    pipeline, old = setup
    assert pipeline.store.finish(old.job['id'], lease_token=old.job['lease_token'])
    scheduled = pipeline.store.enqueue(old.job['project_id'], 'external', automatic=True)
    assert pipeline.store.enqueue(old.job['project_id'], 'external')['id'] == scheduled['id']
    execution = JobExecution(pipeline.store, pipeline.store.claim())
    assert execution.read_checkpoint('collection_request_v1') == dict(
        schema_version=1, mode='incremental', origin='scheduler')


def test_first_bootstrap_remains_first_even_when_entered_manually(setup, monkeypatch):
    pipeline, execution = setup
    assert execution.read_checkpoint('collection_request_v1')['mode'] == 'refresh'
    execution.budgets.update(search=0, model=0, page=0, open_data=0)
    pipeline.http_factory = lambda _: Http(lambda *_: pytest.fail('no providers'))
    monkeypatch.setattr(dsh_runtime, 'run_dsh', lambda *_: None)
    dsh_runtime.run_bootstrap(pipeline, execution, None)
    assert execution.read_checkpoint(STAGE)['plan']['mode'] == 'first'


def test_job_without_new_intent_does_not_gain_manual_refresh_authority(setup, monkeypatch):
    pipeline, execution = setup
    with closing(pipeline.store._connect()) as db, db:
        db.execute('DELETE FROM studio_job_checkpoints WHERE job_id=? AND stage=?',
                   (execution.job['id'], 'collection_request_v1'))
    execution.budgets.update(search=0, model=0, page=0, open_data=0)
    pipeline.http_factory = lambda _: Http(lambda *_: pytest.fail('no providers'))
    monkeypatch.setattr(dsh_runtime, 'run_dsh', lambda *_: None)
    dsh_runtime.run_monitor(pipeline, execution, None)
    assert execution.read_checkpoint(collection_host.ADMISSION)['mode'] == 'incremental'

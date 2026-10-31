"""The ordinary leased entry owns all phases, with no analysis outbox race."""
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from test_event_insight import install
from test_monitor_completion_journey import bind_cost, call_rows, cost_window, old_hold

from project_watch.studio import insight, snapshot_work, update_run
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError
from project_watch.studio.pipeline import StudioWorker


@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('cost_enabled', [False, True])
def test_ordinary_first_and_increment_reach_analysis_and_report_in_one_owner(tmp_path, monkeypatch, native, cost_enabled):
    pipeline, seed, seen = install(tmp_path, monkeypatch, monitor=False, native=native, user_entry=True)
    monkeypatch.setenv('PROJECT_WATCH_DSH_MONITOR', '1')
    pid = seed.job['project_id']
    if cost_enabled:
        cost_window(pipeline, pid)
    client = TestClient(pipeline._test_app)
    worker = StudioWorker(pipeline)
    monkeypatch.setattr(worker, 'schedule', lambda: None)
    first_ids = None
    for entry in ('bootstrap', 'monitor'):
        seen['baseline_events'] = pipeline.store.list_events(project_id=pid)
        seen['baseline_watches'] = pipeline.monitoring_plan.items(pid)
        seen['baseline_material_count'] = len(seen['materials'])
        if entry == 'bootstrap':
            jid = seed.job['id']
        else:
            reply = client.post(f'/api/studio/projects/{pid}/refresh', json={'kind': 'external'})
            assert reply.status_code == 202
            jid = reply.json()['id']
        if cost_enabled:
            bind_cost(pipeline, jid, entry)
        assert worker.step()
        root = JobExecution(pipeline.store, {'id': jid})
        with closing(pipeline.store._connect()) as db:
            status = db.execute('SELECT status,error FROM studio_jobs WHERE id=?', (jid,)).fetchone()
            assert status[0] == 'done', status[1]
            assert not db.execute("SELECT 1 FROM studio_jobs WHERE project_id=? AND kind='insight'", (pid,)).fetchone()
        assert root.read_checkpoint(update_run.OWNER)['entry'] == entry
        state = root.read_checkpoint(update_run.STATE)
        assert list(state['phases']) == ['sources', 'analysis', 'events', 'reports']
        assert state['phases']['analysis']['status'] == 'complete', state
        assert state['phases']['reports']['status'] == 'complete'
        publication = root.read_checkpoint(snapshot_work.PUBLICATION)
        assert publication['status'] == 'completed'
        assert len(publication['published']) == 5
        assert all(s['status'] == 'approved' and s['review_receipts'] for s in publication['sections'].values())
        analysis = insight.load(pipeline.store, pid)
        assert analysis['status'] == 'current' and len(analysis['section_versions']) == 5
        reports = state['phases']['reports']['output']
        assert reports['analysis_status'] == 'completed'
        assert set(reports['event_versions']) <= {e['id'] for e in pipeline.store.list_events(project_id=pid)}
        ids = {e['id'] for e in pipeline.store.list_events(project_id=pid)}
        assert ids
        if first_ids is not None:
            assert ids == first_ids
        first_ids = ids
        before = call_rows(pipeline.store, jid)
        pipeline.schedule_insight(pid)
        assert call_rows(pipeline.store, jid) == before
        with closing(pipeline.store._connect()) as db:
            assert not db.execute("SELECT 1 FROM studio_jobs WHERE project_id=? AND kind='insight'", (pid,)).fetchone()
        assert client.get(f'/api/studio/projects/{pid}').status_code == 200


def test_old_unrelated_unknown_is_preserved_through_new_update(tmp_path, monkeypatch):
    pipeline, seed, _ = install(tmp_path, monkeypatch)
    assert pipeline.store.finish(seed.job['id'], lease_token=seed.job['lease_token'])
    pid = seed.job['project_id']
    old = old_hold(pipeline.store, pid)
    before = call_rows(pipeline.store, old)
    client = TestClient(pipeline._test_app)
    reply = client.post(f'/api/studio/projects/{pid}/refresh', json={'kind': 'external'})
    assert reply.status_code == 202
    worker = StudioWorker(pipeline)
    monkeypatch.setattr(worker, 'schedule', lambda: None)
    assert worker.step()
    root = JobExecution(pipeline.store, {'id': reply.json()['id']})
    assert root.read_checkpoint(update_run.STATE)['phases']['analysis']['status'] == 'complete'
    assert call_rows(pipeline.store, old) == before


def test_stage_reuses_completed_output_and_rejects_changed_input(tmp_path, monkeypatch):
    pipeline, execution, _ = install(tmp_path, monkeypatch)
    calls = []
    def action():
        calls.append(1)
        return {'event_version': 'immutable'}, True
    first = update_run.stage(pipeline, execution, 'sources', {'version': 1}, action)
    assert update_run.stage(pipeline, execution, 'sources', {'version': 1}, action) == first
    assert calls == [1]
    with pytest.raises(ValueError, match='input changed'):
        update_run.stage(pipeline, execution, 'sources', {'version': 2}, action)
    assert calls == [1]


def test_stage_failure_keeps_previous_output_and_does_not_retry(tmp_path, monkeypatch):
    pipeline, execution, _ = install(tmp_path, monkeypatch)
    update_run.stage(pipeline, execution, 'sources', {}, lambda: ({'saved': ['evidence-1']}, True))
    calls = []

    def fail():
        calls.append(1)
        raise HttpError('request_outcome_unknown')

    with pytest.raises(HttpError, match='request_outcome_unknown'):
        update_run.stage(pipeline, execution, 'events', {}, fail)
    state = execution.read_checkpoint(update_run.STATE)
    assert state['phases']['sources']['output']['saved'] == ['evidence-1']
    assert state['phases']['events']['status'] == 'blocked' and calls == [1]


def test_started_old_job_is_not_adopted_or_changed(tmp_path, monkeypatch):
    from project_watch.studio import progressive_bootstrap

    pipeline, execution, _ = install(tmp_path, monkeypatch)
    progressive_bootstrap.freeze(execution, 'monitor', materials=True)
    execution.checkpoint('recovery_inputs', {'binding': 'old-frozen'})
    before = call_rows(pipeline.store, execution.job['id'])
    assert update_run.freeze(execution) is None
    assert execution.read_checkpoint('recovery_inputs') == {'binding': 'old-frozen'}
    assert execution.read_checkpoint(update_run.OWNER) is None
    assert call_rows(pipeline.store, execution.job['id']) == before

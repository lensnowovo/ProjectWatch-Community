"""Explicit old-to-new ownership, same account and immutable prior receipts."""
from contextlib import closing

import pytest
from test_event_insight import install, next_insight, source
from test_monitor_completion_journey import bind_cost, call_rows, cost_window, old_hold

from project_watch.studio import acceptance_batch, event_insight_work, update_handoff, update_run
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError
from project_watch.studio.pipeline import StudioWorker


def finish_old(pipeline, execution):
    source(pipeline, execution)
    child = next_insight(pipeline, execution)
    pipeline.run(child.job)
    assert pipeline.store.finish(child.job['id'], lease_token=child.job['lease_token'])
    pipeline.schedule_insight(execution.job['project_id'])
    return child


@pytest.mark.parametrize('cost_enabled', [False, True])
def test_handoff_reuses_events_and_account_then_runs_ordinary_worker(tmp_path, monkeypatch, cost_enabled):
    pipeline, root, seen = install(tmp_path, monkeypatch)
    pid = root.job['project_id']
    if cost_enabled:
        cost_window(pipeline, pid)
        bind_cost(pipeline, root.job['id'], 'monitor')
    child = finish_old(pipeline, root)
    old_calls = {j: call_rows(pipeline.store, j) for j in (root.job['id'], child.job['id'])}
    before_events = {e['id'] for e in pipeline.store.list_events(project_id=pid)}
    with closing(pipeline.store._connect()) as db:
        frozen = {j: [tuple(r) for r in db.execute('SELECT * FROM studio_job_checkpoints WHERE job_id=? ORDER BY stage', (j,))]
                  for j in old_calls}
        if cost_enabled:
            account = tuple(db.execute('SELECT specification,deadline FROM studio_acceptance_batches').fetchone())
            used = {k: acceptance_batch.usage(db, 'journey-cost', k) for k in acceptance_batch.KINDS}
    if cost_enabled:
        acceptance_batch.close(pipeline.store, 'journey-cost')
    job = update_handoff.admit(pipeline, root.job['id'], authorization='synthetic explicit migration')
    assert update_handoff.admit(pipeline, root.job['id'], authorization='same decision')['id'] == job['id']
    with closing(pipeline.store._connect()) as db:
        if cost_enabled:
            assert tuple(db.execute('SELECT specification,deadline FROM studio_acceptance_batches').fetchone()) == account
            assert {k: acceptance_batch.usage(db, 'journey-cost', k) for k in acceptance_batch.KINDS} == used
            membership = db.execute('SELECT * FROM studio_acceptance_jobs WHERE job_id=?', (job['id'],)).fetchone()
            assert membership['parent_job_id'] == root.job['id'] and membership['root_slot'] is None
    assert all(call_rows(pipeline.store, j) == rows for j, rows in old_calls.items())
    seen['baseline_events'] = pipeline.store.list_events(project_id=pid)
    seen['baseline_watches'] = pipeline.monitoring_plan.items(pid)
    seen['baseline_material_count'] = len(seen['materials'])
    worker = StudioWorker(pipeline)
    monkeypatch.setattr(worker, 'schedule', lambda: None)
    assert worker.step()
    new = JobExecution(pipeline.store, {'id': job['id']})
    with closing(pipeline.store._connect()) as db:
        row = db.execute('SELECT status,error FROM studio_jobs WHERE id=?', (job['id'],)).fetchone()
        assert row['status'] == 'done', row['error']
        for jid, rows in frozen.items():
            after = [tuple(r) for r in db.execute('SELECT * FROM studio_job_checkpoints WHERE job_id=? AND stage!=? ORDER BY stage', (jid, update_handoff.SUCCESSOR))]
            assert after == rows
    assert before_events <= {e['id'] for e in pipeline.store.list_events(project_id=pid)}
    assert all(call_rows(pipeline.store, j) == rows for j, rows in old_calls.items())
    state = new.read_checkpoint(update_run.STATE)
    assert state['phases']['analysis']['status'] == 'complete', state
    assert new.budget_used('model') <= new.read_checkpoint(update_handoff.STAGE)['cap']
    assert new.read_checkpoint(event_insight_work.RESULT)['status'] == 'complete'


def test_inflight_or_unknown_cannot_obtain_migration(tmp_path, monkeypatch):
    pipeline, root, _ = install(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match='terminal'):
        update_handoff.admit(pipeline, root.job['id'], authorization='test')
    child = finish_old(pipeline, root)
    with closing(pipeline.store._connect()) as db, db:
        db.execute("UPDATE studio_job_calls SET status='unknown' WHERE job_id=? AND kind='model'", (child.job['id'],))
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        update_handoff.admit(pipeline, root.job['id'], authorization='test')
    assert root.read_checkpoint(update_handoff.SUCCESSOR) is None


def test_unrelated_legacy_unknown_is_not_migrated(tmp_path, monkeypatch):
    from project_watch.studio import public_research_admission

    pipeline, root, _ = install(tmp_path, monkeypatch)
    old = old_hold(pipeline.store, root.job['project_id'])
    before = call_rows(pipeline.store, old)
    assert public_research_admission.admit(pipeline, root)
    finish_old(pipeline, root)
    job = update_handoff.admit(pipeline, root.job['id'], authorization='test')
    with closing(pipeline.store._connect()) as db:
        assert public_research_admission.held_jobs(db, job['project_id'], job['id']) == {old}
        assert not db.execute('SELECT 1 FROM studio_job_budget_links WHERE job_id=? AND origin_job_id=?', (job['id'], old)).fetchone()
    assert call_rows(pipeline.store, old) == before


@pytest.mark.parametrize('receipt', ['valid', 'missing', 'tampered', 'unknown'])
def test_partial_material_handoff_needs_durable_receipt_and_settled_calls(tmp_path, monkeypatch, receipt):
    from project_watch.studio.dsh_bootstrap import digest

    pipeline, root, _ = install(tmp_path, monkeypatch)
    finish_old(pipeline, root)
    round_state = root.read_checkpoint(event_insight_work.ROUND)
    output = dict(event_versions=round_state['event_versions'], publication_complete=False,
                  result={'status': 'partial', 'reason': 'material_output_limit'})
    phase = dict(status='partial', output=output, output_fingerprint=digest(output))
    if receipt == 'tampered':
        phase['output']['event_versions'] = {}
    with closing(pipeline.store._connect()) as db, db:
        event_insight_work._put(db, root.job['id'], event_insight_work.ROUND,
                               {**round_state, 'publication_complete': False})
        if receipt != 'missing':
            event_insight_work._put(db, root.job['id'], update_run.STATE, {'phases': {'events': phase}})
        if receipt == 'unknown':
            db.execute("UPDATE studio_job_calls SET status='unknown' WHERE job_id=? AND kind='model'", (root.job['id'],))
    before = call_rows(pipeline.store, root.job['id'])
    with pytest.raises(ValueError):
        update_handoff.admit(pipeline, root.job['id'], authorization='synthetic', resume='analysis')
    if receipt != 'valid':
        with pytest.raises((ValueError, HttpError)):
            update_handoff.admit(pipeline, root.job['id'], authorization='synthetic', resume='sources')
        assert root.read_checkpoint(update_handoff.SUCCESSOR) is None
    else:
        job = update_handoff.admit(pipeline, root.job['id'], authorization='synthetic', resume='sources')
        payload = JobExecution(pipeline.store, job).read_checkpoint(update_handoff.STAGE)
        assert payload['original_publication_complete'] is False
        assert payload['original_event_versions'] == output['event_versions']
    assert call_rows(pipeline.store, root.job['id']) == before

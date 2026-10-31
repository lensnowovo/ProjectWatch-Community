"""A settled publisher failure continues through the real coordinator."""
from contextlib import closing

import pytest
from test_event_insight import install
from test_monitor_completion_journey import bind_cost, call_rows, cost_window
from test_update_handoff import finish_old

from project_watch.studio import (
    event_insight_work,
    research_work_budget,
    update_handoff,
    update_run,
)
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError
from project_watch.studio.pipeline import StudioWorker
from project_watch.studio.progressive_monitor import MaterialBootstrap


@pytest.mark.parametrize('shared', [False, True])
def test_exhausted_count_forecast_does_not_block_shared_work_admission(tmp_path, monkeypatch, shared):
    p, root, _ = install(tmp_path, monkeypatch)
    cost_window(p, root.job['project_id'])
    bind_cost(p, root.job['id'], 'monitor')
    finish_old(p, root)
    with closing(p.store._connect()) as db, db:
        state = root.read_checkpoint(event_insight_work.ROUND, db=db)
        state['cap'] = root.budget_used('model', db=db)
        event_insight_work._put(db, root.job['id'], event_insight_work.ROUND, state)
    if shared:
        research_work_budget.enable(p.store, 'journey-cost', authorization='synthetic')
        # The shared policy's original allocation is exhausted too. Do not
        # enlarge the account or fabricate historical paid requests to admit.
        original = research_work_budget.limits
        def exhausted(db, row, spec, allocation):
            total, local = original(db, row, spec, allocation)
            from project_watch.studio.acceptance_batch import usage
            return {**total, 'model': usage(db, row['batch_id'], 'model')}, local
        monkeypatch.setattr(research_work_budget, 'limits', exhausted)
        job = update_handoff.admit(p, root.job['id'], authorization='synthetic')
        assert job['status'] == 'queued'
        assert not call_rows(p.store, job['id'])
    else:
        with pytest.raises(HttpError, match='request_budget'):
            update_handoff.admit(p, root.job['id'], authorization='synthetic')


@pytest.mark.parametrize('damage', [None, 'unknown', 'review', 'input'])
def test_publication_failure_keeps_records_and_finishes_without_recollecting(tmp_path, monkeypatch, damage):
    p, root, seen = install(tmp_path, monkeypatch, monitor=False, user_entry=True)
    monkeypatch.setenv('PROJECT_WATCH_DSH_MONITOR', '1')
    worker = StudioWorker(p)
    monkeypatch.setattr(worker, 'schedule', lambda: None)
    original = MaterialBootstrap.publication_intent
    def crash(research, news):
        original(research, news)
        raise RuntimeError('synthetic local publisher failure')
    monkeypatch.setattr(MaterialBootstrap, 'publication_intent', crash)
    assert worker.step()
    prior = root.read_checkpoint(update_run.STATE)
    assert prior['phases']['events']['status'] == 'blocked'
    assert root.read_checkpoint('dsh_record_admission')['accepted']
    calls = call_rows(p.store, root.job['id'])
    records = root.read_checkpoint('dsh_state')['records']
    counts = {k: len(seen[k]) for k in ('pages', 'materials', 'clinical')}
    monkeypatch.setattr(MaterialBootstrap, 'publication_intent', original)
    with closing(p.store._connect()) as db, db:
        if damage == 'unknown':
            db.execute("UPDATE studio_job_calls SET status='unknown' WHERE job_id=? AND kind='model'", (root.job['id'],))
        if damage == 'review':
            db.execute("DELETE FROM studio_job_checkpoints WHERE job_id=? AND stage LIKE 'dsh_record_review_%:%'", (root.job['id'],))
        if damage == 'input':
            state = root.read_checkpoint('dsh_state', db=db)
            state['records'][0]['text'] += ' changed'
            event_insight_work._put(db, root.job['id'], 'dsh_state', state)
    if damage == 'unknown':
        with pytest.raises(HttpError, match='request_outcome_unknown'):
            update_handoff.admit(p, root.job['id'], authorization='synthetic', resume='publication')
        assert root.read_checkpoint(update_handoff.SUCCESSOR) is None
        return
    job = update_handoff.admit(p, root.job['id'], authorization='synthetic', resume='publication')
    assert worker.step()
    new = JobExecution(p.store, job)
    state = new.read_checkpoint(update_run.STATE)
    with closing(p.store._connect()) as db:
        status = dict(db.execute('SELECT status,error FROM studio_jobs WHERE id=?', (job['id'],)).fetchone())
    assert state is not None, status
    assert counts == {k: len(seen[k]) for k in counts}
    assert call_rows(p.store, root.job['id']) == calls
    assert root.read_checkpoint(update_run.STATE) == prior
    if damage:
        assert state['phases']['events']['status'] == 'blocked'
        assert not call_rows(p.store, job['id'])
    else:
        assert root.read_checkpoint('dsh_state')['records'] == records
        assert 'analysis' in state['phases'], (status, state)
        assert state['phases']['analysis']['status'] == 'complete', state
        assert state['phases']['reports']['status'] == 'complete'
        assert new.read_checkpoint(event_insight_work.RESULT)['status'] == 'complete'
        assert p.store.list_events(project_id=job['project_id'])

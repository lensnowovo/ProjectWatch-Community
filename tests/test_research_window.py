"""Successor work keeps the cumulative account; old jobs never gain sends."""
import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from test_research_budget import contract, journal, send, setup

from project_watch.studio import acceptance_batch as batch
from project_watch.studio import research_budget, research_window, research_work_budget
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError


def close_expired(pipeline, root):
    with closing(pipeline.store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET status='done',finished_at='2000-01-01' WHERE id=?", (root.job['id'],))
        row = db.execute('SELECT specification FROM studio_acceptance_batches').fetchone()
        spec = json.loads(row[0])
        spec['cost_policy']['valid_until'] = '2000-01-01T00:00:00+00:00'
        db.execute("UPDATE studio_acceptance_batches SET state='closed',deadline='2000-01-01T00:00:00+00:00',specification=?",
                   (json.dumps(spec),))


def admit(pipeline, root, **overrides):
    kwargs = dict(window_id='next-work', deadline=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        work={root.job['project_id']: ['sources', 'events', 'analysis', 'reports']}, authorization='synthetic explicit continuation')
    return research_window.admit(pipeline.store, 'cost-window', **{**kwargs, **overrides})


def test_successor_preserves_contract_costs_and_fences_old_jobs(tmp_path):
    pipeline, root = setup(tmp_path)
    send(root)
    close_expired(pipeline, root)
    with closing(pipeline.store._connect()) as db:
        old_spec, old_deadline = tuple(db.execute('SELECT specification,deadline FROM studio_acceptance_batches').fetchone())
        costs = [tuple(r) for r in db.execute('SELECT * FROM studio_research_costs')]
    calls = journal(pipeline.store)
    value = admit(pipeline, root)
    assert value['consumed']['model'] == 1 and value['committed'] == 60
    with pytest.raises(HttpError, match='acceptance_not_ended'):
        batch.release(pipeline.store, 'cost-window')
    with closing(pipeline.store._connect()) as db:
        assert tuple(db.execute('SELECT specification,deadline FROM studio_acceptance_batches').fetchone()) == (old_spec, old_deadline)
        assert [tuple(r) for r in db.execute('SELECT * FROM studio_research_costs')] == costs
        with pytest.raises(HttpError, match='research_window_old_job'):
            batch.guard_send(db, root.job, 'model')
    assert journal(pipeline.store) == calls
    child = pipeline.store.enqueue(root.job['project_id'], 'external')
    batch.bind_root(pipeline.store, 'cost-window', child['id'], 'next')
    execution = JobExecution(pipeline.store, pipeline.store.claim())
    assert execution.job['id'] == child['id']
    send(execution, 'new independent request')
    assert research_budget.summary(pipeline.store, 'cost-window')['committed'] == 120
    assert journal(pipeline.store)[0] == calls[0]
    with pytest.raises(HttpError, match='research_window_not_closed'):
        admit(pipeline, root)
    batch.close(pipeline.store, 'cost-window')
    with pytest.raises(HttpError, match='acceptance_not_drained'):
        admit(pipeline, root)


@pytest.mark.parametrize('reason', ['unknown', 'unpriced', 'project', 'running', 'released'])
def test_successor_cannot_bypass_hard_boundaries(tmp_path, reason):
    pipeline, root = setup(tmp_path)
    send(root)
    close_expired(pipeline, root)
    with closing(pipeline.store._connect()) as db, db:
        if reason == 'unknown':
            db.execute("UPDATE studio_job_calls SET status='unknown'")
        elif reason == 'unpriced':
            db.execute('UPDATE studio_research_costs SET settled=-1')
        elif reason == 'cost':
            db.execute('UPDATE studio_research_costs SET charged=10000')
        elif reason == 'project':
            db.execute("UPDATE studio_projects SET legal_name='Changed'")
        elif reason == 'running':
            db.execute("UPDATE studio_jobs SET status='running'")
        else:
            db.execute("INSERT INTO studio_acceptance_releases VALUES ('cost-window',1,'2000-01-01')")
    with pytest.raises(HttpError):
        admit(pipeline, root)
    with closing(pipeline.store._connect()) as db:
        assert research_window.current(db, 'cost-window') is None
        assert db.execute('SELECT state FROM studio_acceptance_batches').fetchone()[0] == 'closed'


def test_work_forecasts_record_all_kinds_without_money_cap(tmp_path):
    pipeline, root = setup(tmp_path, contract(2500), n=1)
    research_work_budget.enable(pipeline.store, 'cost-window', authorization='synthetic work based allocation')
    first = research_work_budget.plan_work(root, phase='collect', counts={'model': 4, 'open_data': 6},
        basis={'queries': ['affiliation', 'applicant'], 'pages_per_query': 3})
    assert first['limits']['model'] == 4
    assert batch.remaining(pipeline.store, root.job)['open_data'] is None
    assert research_work_budget.plan_work(root, phase='collect', counts={'model': 4, 'open_data': 6},
        basis={'queries': ['affiliation', 'applicant'], 'pages_per_query': 3}) == first
    send(root, 'first')
    send(root, 'second')
    send(root, 'third')
    assert len(journal(pipeline.store)) == 3
    assert research_budget.summary(pipeline.store, 'cost-window')['committed'] == 180
    root.budgets['model'] = 1
    with closing(pipeline.store._connect()) as db:
        research_work_budget.restore(root, db)
    assert root.budgets['model'] == 4


def test_no_shared_opt_in_keeps_original_stage_limits(tmp_path):
    pipeline, root = setup(tmp_path, n=1)
    old = dict(root.budgets)
    assert research_work_budget.plan_work(root, phase='collect', counts={'model': 4}, basis={'sources': ['a']}) is None
    assert root.budgets == old
    assert batch.remaining(pipeline.store, root.job)['model'] is None


def test_successor_preserves_costs_above_historical_ceiling(tmp_path):
    pipeline, root = setup(tmp_path)
    send(root)
    close_expired(pipeline, root)
    with closing(pipeline.store._connect()) as db, db:
        db.execute('UPDATE studio_research_costs SET charged=1000000')
    receipt = admit(pipeline, root)
    assert receipt['committed'] == 1000000
    assert research_budget.summary(pipeline.store, 'cost-window')['committed'] == 1000000

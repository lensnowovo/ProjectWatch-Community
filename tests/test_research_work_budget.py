"""Shared work planning preserves money, settled calls and explicit opt-in."""
import json
from contextlib import closing

import pytest
from test_event_insight import install
from test_monitor_completion_journey import bind_cost, cost_window

from project_watch.studio import acceptance_batch, research_work_budget, runtime_health
from project_watch.studio.http import HttpError
from project_watch.studio.store import now


def setup(tmp_path, monkeypatch, *, ceiling=None):
    pipeline, root, _ = install(tmp_path, monkeypatch)
    cost_window(pipeline, root.job['project_id'])
    bind_cost(pipeline, root.job['id'], 'monitor')
    with closing(pipeline.store._connect()) as db, db:
        spec=json.loads(db.execute('SELECT specification FROM studio_acceptance_batches').fetchone()[0])
        spec['caps']['model']=64
        if ceiling is not None:
            spec['cost_policy']['ceiling']=ceiling
        db.execute('UPDATE studio_acceptance_batches SET specification=?',(json.dumps(spec),))
        allocation=json.loads(db.execute('SELECT allocation FROM studio_acceptance_projects').fetchone()[0])
        allocation['caps']['model']=64
        db.execute('UPDATE studio_acceptance_projects SET allocation=?',(json.dumps(allocation),))
        for i in range(61):
            db.execute("INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,response_status,created_at) VALUES(?,?,'model','synthetic','completed',200,?)",
                       (root.job['id'],str(i),now()))
            db.execute('INSERT INTO studio_research_costs VALUES (?,?,?,?,?,?,?,?,?)',
                       ('journey-cost',root.job['id'],str(i),root.job['id'],'collect','{}',1,1,1))
    root.budgets['model']=64
    return pipeline,root


def test_five_reviews_use_shared_pool_and_append_finite_forecast(tmp_path,monkeypatch):
    pipeline,root=setup(tmp_path,monkeypatch)
    with closing(pipeline.store._connect()) as db:
        original=tuple(db.execute('SELECT * FROM studio_acceptance_batches').fetchone())
        calls=[tuple(r) for r in db.execute('SELECT * FROM studio_job_calls')]
    research_work_budget.enable(pipeline.store,'journey-cost',authorization='owner allows shared engineering counts')
    assert research_work_budget.reserve_review(root,calls=5,binding='fixed-draft',output_tokens=8192)==66
    assert research_work_budget.reserve_review(root,calls=5,binding='fixed-draft',output_tokens=8192)==66
    with closing(pipeline.store._connect()) as db:
        policy=research_work_budget.current(db,'journey-cost')
        assert policy['model_cap']==66 and policy['version']==2
        assert tuple(db.execute('SELECT * FROM studio_acceptance_batches').fetchone())==original
        assert [tuple(r) for r in db.execute('SELECT * FROM studio_job_calls')]==calls
        acceptance_batch.guard_send(db,root.job,'model')
        root.budgets['model']=64
        root._review_work_binding='fixed-draft'
        research_work_budget.restore(root,db)
        assert root.budgets['model']==66
    assert acceptance_batch.remaining(pipeline.store,root.job)['model'] is None


def test_no_opt_in_preserves_lower_original_count(tmp_path,monkeypatch):
    pipeline,root=setup(tmp_path,monkeypatch)
    assert research_work_budget.reserve_review(root,calls=5,binding='draft',output_tokens=8192) is None
    assert root.budgets['model']==64
    assert acceptance_batch.remaining(pipeline.store,root.job)['model'] is None


@pytest.mark.parametrize('reason',['unknown','expired'])
def test_work_plan_does_not_bypass_hard_boundaries(tmp_path,monkeypatch,reason):
    pipeline,root=setup(tmp_path,monkeypatch)
    research_work_budget.enable(pipeline.store,'journey-cost',authorization='synthetic authorization')
    with closing(pipeline.store._connect()) as db,db:
        if reason=='cost':
            db.execute('UPDATE studio_research_costs SET charged=999900 WHERE call_key=\'0\'')
        elif reason=='unknown':
            db.execute("UPDATE studio_job_calls SET status='unknown' WHERE call_key='0'")
        else:
            db.execute("UPDATE studio_acceptance_batches SET deadline='2000-01-01T00:00:00+00:00'")
    expected={'cost':'research_cost_budget','unknown':'request_outcome_unknown','expired':'acceptance_expired'}[reason]
    with pytest.raises(HttpError,match=expected):
        research_work_budget.reserve_review(root,calls=5,binding='draft',output_tokens=8192)
    assert root.read_checkpoint(research_work_budget.PLAN) is None


def test_progress_uses_chunked_current_stage_without_writes(tmp_path,monkeypatch):
    pipeline,root,_=install(tmp_path,monkeypatch)
    root.checkpoint('dsh_runtime_phase',{'phase':'read'})
    root.checkpoint_chunked('update_run_state_v1',{'status':'running','active_stage':'analysis','padding':'x'*20000})
    with closing(pipeline.store._connect()) as db:
        before=db.total_changes
        assert runtime_health.job_phase(db,root.job)=='judge'
        assert db.total_changes==before


def test_parallel_work_plans_record_both_forecasts_beyond_historical_ceiling(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from test_research_budget import contract
    from test_research_budget import setup as cost_setup

    pipeline, root = cost_setup(tmp_path, contract(4500))
    research_work_budget.enable(pipeline.store, 'cost-window', authorization='synthetic shared allocation')
    def plan(binding):
        try:
            research_work_budget.reserve_review(root, calls=1, binding=binding, output_tokens=100)
            return 'reserved'
        except HttpError as exc:
            return exc.code
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(plan, ['one', 'two']))
    assert sorted(results) == ['reserved', 'reserved']
    with closing(pipeline.store._connect()) as db:
        assert research_work_budget.held(db, 'cost-window') == 4800


def test_dispatch_converts_own_hold_and_preserves_other_work(tmp_path):
    from test_research_budget import contract, send
    from test_research_budget import setup as cost_setup

    pipeline, root = cost_setup(tmp_path, contract(5000))
    research_work_budget.enable(pipeline.store, 'cost-window', authorization='synthetic shared allocation')
    research_work_budget.reserve_review(root, calls=1, binding='one', output_tokens=100)
    research_work_budget.reserve_review(root, calls=1, binding='two', output_tokens=100)
    root._review_work_binding = 'one'
    send(root, 'one')
    with closing(pipeline.store._connect()) as db:
        assert research_work_budget.held(db, 'cost-window') == 2400
        assert db.execute('SELECT sum(charged) FROM studio_research_costs').fetchone()[0] == 60
    research_work_budget.release(root, 'two')
    with closing(pipeline.store._connect()) as db:
        assert research_work_budget.held(db, 'cost-window') == 0


def test_work_forecast_does_not_block_after_historical_cost_ceiling(tmp_path, monkeypatch):
    pipeline, root = setup(tmp_path, monkeypatch)
    research_work_budget.enable(pipeline.store, 'journey-cost', authorization='synthetic authorization')
    with closing(pipeline.store._connect()) as db, db:
        db.execute("UPDATE studio_research_costs SET charged=999999999 WHERE call_key='0'")
    assert research_work_budget.reserve_review(root, calls=5, binding='draft', output_tokens=8192) == 66
    with closing(pipeline.store._connect()) as db:
        assert db.execute("SELECT charged FROM studio_research_costs WHERE call_key='0'").fetchone()[0] == 999999999

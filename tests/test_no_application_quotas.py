"""Request/fee accounting survives removal of app quotas. Synthetic providers only."""
import json
from contextlib import closing
from decimal import Decimal

import pytest
from test_research_budget import contract, journal, send, setup

from project_watch.model_gateway import BudgetGate, BudgetPolicy
from project_watch.studio import research_budget
from project_watch.studio.dsh_closeout import request_budget
from project_watch.studio.http import HttpError


def test_dispatch_exceeds_legacy_job_account_and_money_caps_without_erasing_usage(tmp_path):
    pipeline, execution = setup(tmp_path, contract(1), n=1)
    execution.budgets['model'] = 0
    for i in range(3):
        send(execution, str(i))
    rows = journal(pipeline.store)
    assert len(rows) == 3
    assert all(r['status'] == 'completed' and r['request_limit'] is None for r in rows)
    usage = research_budget.summary(pipeline.store, 'cost-window')
    assert usage['committed'] == 180 and usage['historical_ceiling'] == 1
    assert usage['remaining'] is None and usage['ceiling'] is None
    with closing(pipeline.store._connect()) as db:
        original = json.loads(db.execute('SELECT specification FROM studio_acceptance_batches').fetchone()[0])
    assert original['caps']['model'] == 1
    assert original['cost_policy']['ceiling'] == 1
    assert request_budget(execution)['model'] == dict(limit=None, remaining=None, used=3)
    send(execution, '0', transport=lambda *args: pytest.fail('completed request must replay'))
    assert len(journal(pipeline.store)) == 3


def test_old_unknown_is_not_resent_even_without_quotas(tmp_path):
    pipeline, execution = setup(tmp_path, contract(1), n=1)
    def lost(*args):
        raise TimeoutError('synthetic uncertain response')
    with pytest.raises(TimeoutError):
        send(execution, transport=lost)
    before = journal(pipeline.store)
    with pytest.raises(HttpError, match='unknown'):
        send(execution, transport=lambda *args: pytest.fail('must not dispatch old unknown'))
    assert journal(pipeline.store) == before
    assert before[0]['status'] == 'unknown'
    assert research_budget.summary(pipeline.store, 'cost-window')['retained'] > 0


def test_manual_pause_still_blocks_dispatch(tmp_path):
    pipeline, execution = setup(tmp_path, contract(1), n=1)
    with closing(pipeline.store._connect()) as db, db:
        db.execute("UPDATE studio_projects SET status='paused'")
    with pytest.raises(HttpError, match='project_paused'):
        send(execution, transport=lambda *args: pytest.fail('paused project must not send'))
    assert journal(pipeline.store) == []


def test_retired_quota_hold_keeps_audit_without_blocking_new_work(tmp_path):
    from project_watch.studio.dsh_closeout import active_hold
    from project_watch.studio.material_ownership import project_guard

    pipeline, execution = setup(tmp_path)
    original = {'code': 'request_budget'}
    execution.checkpoint('dsh_hold', original)
    with closing(pipeline.store._connect()) as db:
        project_guard(db, execution.job['project_id'], execution.job['id'])
    send(execution)
    assert execution.read_checkpoint('dsh_hold') == original
    assert active_hold(original) is None
    for code in ('request_outcome_unknown', 'judgment_review_invalid', 'judgment_review_limit', 'project_paused'):
        hold = {'code': code}
        assert active_hold(hold) == hold


def test_analysis_capability_remains_separate_from_quota(tmp_path):
    pipeline, execution = setup(tmp_path)
    execution.allowed_call_kinds = frozenset({'search'})
    with pytest.raises(HttpError, match='call_kind_not_permitted'):
        send(execution, transport=lambda *args: pytest.fail('wrong capability'))
    assert journal(pipeline.store) == []


def test_legacy_gateway_counts_and_reserves_beyond_historical_ceiling():
    gate = BudgetGate(BudgetPolicy('synthetic', 1, 2000, 200, Decimal('0.000001'), Decimal('1'), Decimal('10')))
    body = json.dumps(dict(model='synthetic', stream=True, max_tokens=200,
                           messages=[dict(role='user', content='synthetic')])).encode()
    for _ in range(3):
        gate.admit(body)
    assert gate.receipt()['requests_admitted'] == 3


def test_review_plan_covers_more_than_twenty_four_batches_without_dropping_fields():
    from project_watch.studio.dsh_judgment_review import review_batches
    fields = {f'field.{i}': 'x' * 6000 for i in range(30)}
    batches = review_batches(dict(fields=fields, evidence={}))
    assert len(batches) == 31
    reviewed = {key for batch in batches if batch['phase'] == 'fields' for key in batch['fields']}
    assert reviewed == set(fields)


def test_analysis_capability_restores_collection_after_success_or_failure(tmp_path):
    _, execution = setup(tmp_path)
    with pytest.raises(RuntimeError), execution.call_scope({'model'}):
        assert execution.allowed_call_kinds == frozenset({'model'})
        raise RuntimeError('synthetic section failure')
    assert execution.allowed_call_kinds is None

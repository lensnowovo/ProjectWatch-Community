"""Durable collection receipts on isolated SQLite, including lease loss."""
from contextlib import closing

import pytest
import test_studio_dsh_monitor as fixtures
from test_collection_plan import plan, success

from project_watch.studio.collection_ledger import STAGE, CollectionLedger
from project_watch.studio.execution import JobExecution


@pytest.fixture
def execution(tmp_path):
    return fixtures.loop.__wrapped__(tmp_path).execution


def test_completed_zero_survives_executor_restart(execution):
    p = plan(round_id=execution.job['id'], project_id=execution.job['project_id'])
    ledger = CollectionLedger(execution, p)
    assert ledger.coverage()['status'] == 'partial'
    ledger.record(success(p))
    restarted = CollectionLedger(JobExecution(execution.store, execution.job), p)
    assert restarted.coverage()['status'] == 'complete'
    assert restarted.receipts == [success(p)]


@pytest.mark.parametrize('outcome', ['success', 'unknown', 'failed', 'budget_deferred'])
def test_terminal_receipt_cannot_be_erased_or_retried(execution, outcome):
    p = plan(round_id=execution.job['id'], project_id=execution.job['project_id'])
    ledger = CollectionLedger(execution, p)
    receipt = success(p, outcome=outcome)
    ledger.record(receipt)
    ledger.record(receipt)  # idempotent receipt write only, not a dispatch
    with pytest.raises(ValueError, match='terminal'):
        ledger.record(success(p, outcome='running'))
    assert ledger.receipts == [receipt]


def test_interrupted_running_can_complete_from_journal_evidence(execution):
    p = plan(round_id=execution.job['id'], project_id=execution.job['project_id'])
    ledger = CollectionLedger(execution, p)
    ledger.record(success(p, outcome='running'))
    restarted = CollectionLedger(JobExecution(execution.store, execution.job), p)
    assert restarted.coverage()['status'] == 'partial'
    restarted.record(success(p))
    assert restarted.coverage()['status'] == 'complete'


def test_manifest_frozen_and_bound_to_job(execution):
    p = plan(round_id=execution.job['id'], project_id=execution.job['project_id'])
    ledger = CollectionLedger(execution, p)
    with pytest.raises(ValueError, match='changed'):
        CollectionLedger(execution, p | {'binding': 'different-scope'})
    with pytest.raises(ValueError, match='another job'):
        CollectionLedger(execution, plan())
    p['units'].clear()
    exposed = ledger.plan
    exposed['units'].clear()
    assert len(ledger.plan['units']) == 1


def test_expired_worker_cannot_persist_or_claim_completion(execution):
    p = plan(round_id=execution.job['id'], project_id=execution.job['project_id'])
    ledger = CollectionLedger(execution, p)
    before = execution.read_checkpoint(STAGE)
    with closing(execution.store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET lease_token='replacement' WHERE id=?", (execution.job['id'],))
    with pytest.raises(RuntimeError, match='lease'):
        ledger.record(success(p))
    assert execution.read_checkpoint(STAGE) == before
    assert ledger.coverage()['status'] == 'partial'


def test_invalid_receipt_does_not_change_checkpoint(execution):
    p = plan(round_id=execution.job['id'], project_id=execution.job['project_id'])
    ledger = CollectionLedger(execution, p)
    before = execution.read_checkpoint(STAGE)
    with pytest.raises(ValueError, match='provenance'):
        ledger.record(success(p, request_ids=[]))
    assert execution.read_checkpoint(STAGE) == before


def test_foreign_project_plan_cannot_be_saved_under_valid_job(execution):
    assert execution.read_checkpoint(STAGE) is None
    with pytest.raises(ValueError, match='another project'):
        CollectionLedger(execution, plan(round_id=execution.job['id'], project_id='foreign-project'))
    assert execution.read_checkpoint(STAGE) is None

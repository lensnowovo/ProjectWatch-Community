"""Cross-job material adoption never grants permission to resend unknown calls."""
import json
from contextlib import closing

import pytest
import test_collection_view as fixtures
from test_evidence_tasks import proposal, rows

from project_watch.studio import evidence_tasks
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError, Response
from project_watch.studio.material_ownership import MaterialExecution, take_over


@pytest.fixture
def owner(tmp_path):
    return fixtures.owner.__wrapped__(tmp_path)


def next_owner(execution):
    store = execution.store
    assert store.finish(execution.job['id'], lease_token=execution.job['lease_token'])
    store.enqueue(execution.job['project_id'], 'external')
    return JobExecution(store, store.claim(), budgets=execution.budgets)


def test_unsent_pending_transfers_exactly_once_and_old_lease_cannot_write(owner):
    previous, _, _, _ = fixtures.save(owner)
    task = rows(owner)[0]
    stale = MaterialExecution(previous, task)
    current = next_owner(previous)
    take_over(current, task['id'])
    take_over(current, task['id'])
    assert rows(owner)[0]['job_id'] == current.job['id']
    with closing(current.store._connect()) as db:
        assert db.execute('SELECT count(*) FROM studio_material_transfers').fetchone()[0] == 1
    with pytest.raises(RuntimeError, match='lease expired'):
        stale.checkpoint('late', {'must_not_write': True})
    evidence_tasks.run_pending(current, proposal)
    assert rows(owner)[0]['status'] == 'complete'


def test_received_response_reused_across_jobs_without_new_usage(owner, monkeypatch):
    previous, _, _, _ = fixtures.save(owner)
    task = rows(owner)[0]
    sent = []
    def extract(source, execution):
        def transport():
            sent.append(execution.job['id'])
            return Response(200, {}, json.dumps(proposal(source, execution)).encode())
        return json.loads(execution.call('model', 'material:' + source['task_id'], transport, provider='fixture').body)
    save = evidence_tasks._save
    monkeypatch.setattr(evidence_tasks, '_save', lambda *_: (_ for _ in ()).throw(RuntimeError('after response crash')))
    with pytest.raises(RuntimeError, match='after response crash'):
        evidence_tasks.run_pending(previous, extract)
    monkeypatch.setattr(evidence_tasks, '_save', save)
    current = next_owner(previous)
    take_over(current, task['id'])
    evidence_tasks.run_pending(current, extract)
    assert sent == [previous.job['id']] and rows(owner)[0]['status'] == 'complete'
    assert previous.usage()['model']['completed'] == 1 and not current.usage()


@pytest.mark.parametrize('boundary', ['unknown', 'started', 'hold'])
def test_new_job_cannot_unlock_old_unknown_or_hold(owner, boundary):
    previous, _, _, _ = fixtures.save(owner)
    task = rows(owner)[0]
    if boundary == 'hold':
        previous.checkpoint('dsh_hold', {'code': 'model_response_invalid'})
    else:
        def fail(source, execution):
            return execution.call('model', 'material:' + source['task_id'],
                lambda: (_ for _ in ()).throw(HttpError('connection_failed')), provider='fixture')
        with pytest.raises(HttpError, match='connection_failed'):
            evidence_tasks.run_pending(previous, fail)
        if boundary == 'started':
            with closing(previous.store._connect()) as db, db:
                db.execute("UPDATE studio_job_calls SET status='started' WHERE job_id=?", (previous.job['id'],))
    current = next_owner(previous)
    with pytest.raises(HttpError, match='model_response_invalid' if boundary == 'hold' else 'request_outcome_unknown'):
        take_over(current, task['id'])
    assert rows(owner)[0]['job_id'] == previous.job['id']


def test_active_owner_and_foreign_project_cannot_be_adopted(owner):
    previous, _, _, _ = fixtures.save(owner)
    task = rows(owner)[0]
    store = previous.store
    store.enqueue(previous.job['project_id'], 'brief')
    current = JobExecution(store, store.claim())
    with pytest.raises(ValueError, match='not terminal'):
        take_over(current, task['id'])
    # A foreign material ID must never become visible through an ownership move.
    with pytest.raises(ValueError, match='pending material required'):
        take_over(current, 'unknown-material')
    foreign = owner[0].repository.create_project('Foreign synthetic')
    store.upsert_project(foreign.id, {'legal_name': 'Foreign synthetic'}, consent=True)
    store.enqueue(foreign.id, 'external')
    foreign_execution = JobExecution(store, store.claim())
    with pytest.raises(ValueError, match='pending material required'):
        take_over(foreign_execution, task['id'])


def test_adopted_completed_calls_still_consume_explicit_budget(owner, monkeypatch):
    previous, _, _, _ = fixtures.save(owner)
    previous.budgets['model'] = 1
    task = rows(owner)[0]
    material = MaterialExecution(previous, task)
    material.call('model', 'first', lambda: Response(200, {}, b'{}'), provider='fixture')
    current = next_owner(previous)
    take_over(current, task['id'])
    adopted = MaterialExecution(current, rows(owner)[0])
    with pytest.raises(HttpError, match='request_budget'):
        adopted.call('model', 'second', lambda: pytest.fail('must not dispatch beyond original cap'), provider='fixture')
    assert not current.usage()


def test_saved_proposal_and_attempt_count_transfer_without_model_dispatch(owner):
    previous, _, _, _ = fixtures.save(owner)
    task = rows(owner)[0]
    stage = 'extract_result:' + task['id']
    previous.checkpoint(stage, {'proposal': {'observations': []}})
    previous.checkpoint('extract_attempt:' + task['id'], ['first', 'second'])
    current = next_owner(previous)
    take_over(current, task['id'])
    assert current.read_checkpoint(stage) == previous.read_checkpoint(stage)
    assert current.read_checkpoint('extract_attempt:' + task['id']) == ['first', 'second']
    assert not current.usage()


def test_legacy_unattributed_dispatch_does_not_masquerade_as_unsent(owner):
    previous, _, _, _ = fixtures.save(owner)
    task = rows(owner)[0]
    previous.call('model', 'legacy-unscoped', lambda: Response(200, {}, b'{}'), provider='fixture')
    current = next_owner(previous)
    with pytest.raises(ValueError, match='provenance missing'):
        take_over(current, task['id'])
    assert rows(owner)[0]['job_id'] == previous.job['id']


def test_late_old_owner_is_fenced_even_if_its_job_gets_a_fresh_lease(owner):
    previous, _, _, _ = fixtures.save(owner)
    task = rows(owner)[0]
    current = next_owner(previous)
    take_over(current, task['id'])
    with closing(previous.store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET status='running',lease_token=?,lease_until=? WHERE id=?",
                   ('new-old-lease', current.job['lease_until'], previous.job['id']))
    old_job = {**previous.job, 'lease_token': 'new-old-lease', 'lease_until': current.job['lease_until']}
    stale = MaterialExecution(JobExecution(previous.store, old_job), task)
    with pytest.raises(RuntimeError, match='material owner expired'):
        stale.checkpoint('late', {})

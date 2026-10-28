"""Read-only owner projection of current jobs, including chunked receipts."""
import json
from contextlib import closing

import pytest
import test_collection_decisions as fixtures
from test_collection_plan import candidate, plan, success

from project_watch.studio import collection_dsh
from project_watch.studio.collection_ledger import STAGE, CollectionLedger
from project_watch.studio.execution import JobExecution


@pytest.fixture
def owner(tmp_path):
    return fixtures.owner.__wrapped__(tmp_path)


def start(owner):
    pipe, _client, pid, _url = owner
    pipe.store.enqueue(pid, 'external')
    execution = JobExecution(pipe.store, pipe.store.claim())
    p = plan([candidate() | dict(source='openalex_topic', category='paper')],
             project_id=pid, round_id=execution.job['id'])
    return execution, CollectionLedger(execution, p)


def test_running_then_saved_zero_visible_through_both_apis_without_write(owner):
    pipe, client, pid, _url = owner
    execution, ledger = start(owner)
    receipt = dict(round_id=execution.job['id'], unit_id=ledger.plan['units'][0]['id'], outcome='running')
    ledger.record(receipt)
    before = pipe.store.get_project(pid)
    with closing(pipe.store._connect()) as db:
        job_before = dict(db.execute('SELECT * FROM studio_jobs WHERE id=?', (execution.job['id'],)).fetchone())
    for route in (f'/api/studio/projects/{pid}', f'/api/studio/projects/{pid}/monitoring-plan'):
        response = client.get(route)
        assert response.status_code == 200
        report = response.json()['collection_report']
        assert report['active'] and report['job_status'] == 'running'
        assert report['channels']['paper']['execution'] == 'running'
        assert report['job_id'] == execution.job['id'] and report['view_state'] == 'receipts'
    with closing(pipe.store._connect()) as db:
        assert dict(db.execute('SELECT * FROM studio_jobs WHERE id=?', (execution.job['id'],)).fetchone()) == job_before
        assert db.execute('SELECT COUNT(*) FROM studio_job_calls').fetchone()[0] == 0
    assert pipe.store.get_project(pid) == before
    ledger.record(success(ledger.plan))
    result = client.get(f'/api/studio/projects/{pid}/monitoring-plan').json()
    paper = next(r for r in result['coverage']['science'] if r['key'] == 'paper')
    assert paper['execution'] == 'empty' and paper['state'] == 'on'
    assert result['collection_report']['status'] == 'complete'
    assert pipe.store.get_plan(pid) is None  # no finish/publish or read-side report write


def test_new_round_without_manifest_never_inherits_previous_success(owner):
    pipe, client, pid, _url = owner
    execution, ledger = start(owner)
    ledger.record(success(ledger.plan))
    pipe.store.finish(execution.job['id'], lease_token=execution.job['lease_token'])
    new = pipe.store.enqueue(pid, 'external')
    result = client.get(f'/api/studio/projects/{pid}').json()['collection_report']
    assert result['job_id'] == new['id'] and result['view_state'] == 'awaiting_plan'
    assert result['channels'] == {} and result['status'] == 'partial' and not result['active']
    view = client.get(f'/api/studio/projects/{pid}/monitoring-plan').json()
    assert next(r for r in view['coverage']['science'] if r['key'] == 'paper')['execution'] == 'pending'


@pytest.mark.parametrize('boundary', ['paused', 'expired', 'failed'])
def test_saved_running_receipt_does_not_claim_an_active_request_after_stop(owner, boundary):
    pipe, _client, pid, _url = owner
    execution, ledger = start(owner)
    ledger.record(dict(round_id=execution.job['id'], unit_id=ledger.plan['units'][0]['id'], outcome='running'))
    if boundary == 'paused':
        pipe.store.set_status(pid, 'paused')
    elif boundary == 'failed':
        pipe.store.finish(execution.job['id'], lease_token=execution.job['lease_token'], error='synthetic stopped')
    else:
        with closing(pipe.store._connect()) as db, db:
            db.execute('UPDATE studio_jobs SET lease_until=? WHERE id=?', ('2000-01-01T00:00:00+00:00', execution.job['id']))
    report = collection_dsh.latest_report(pipe.store, pid)
    assert not report['active'] and report['channels']['paper']['execution'] == 'running'


def test_topic_and_other_project_do_not_replace_company_collection(owner):
    pipe, _client, pid, _url = owner
    execution, ledger = start(owner)
    ledger.record(success(ledger.plan))
    pipe.store.finish(execution.job['id'], lease_token=execution.job['lease_token'])
    topic = pipe.store.enqueue(pid, 'external')
    with closing(pipe.store._connect()) as db, db:
        db.execute("UPDATE studio_job_checkpoints SET data=? WHERE job_id=? AND stage='collection_request_v1'",
                   (json.dumps(dict(mode='topic')), topic['id']))
    assert collection_dsh.latest_report(pipe.store, pid)['job_id'] == execution.job['id']
    assert collection_dsh.latest_report(pipe.store, 'other-project') is None


def test_chunk_corruption_is_visible_and_never_replaced_with_old_success(owner):
    pipe, client, pid, _url = owner
    execution, ledger = start(owner)
    ledger.record(success(ledger.plan))
    pipe.store.put_plan(pid, {'round_id': execution.job['id'], 'collection_report': {
        'schema_version': 1, 'job_id': execution.job['id'], 'status': 'complete', 'channels': {}}})
    with closing(pipe.store._connect()) as db, db:
        manifest = json.loads(db.execute('SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage=?',
                             (execution.job['id'], STAGE)).fetchone()[0])
        db.execute('UPDATE studio_job_checkpoints SET data=? WHERE job_id=? AND stage=?',
                   ('"corrupt"', execution.job['id'], manifest['prefix'] + '0'))
    result = client.get(f'/api/studio/projects/{pid}').json()['collection_report']
    assert result['view_state'] == 'unavailable' and result['channels'] == {} and result['status'] == 'partial'
    view = client.get(f'/api/studio/projects/{pid}/monitoring-plan').json()
    assert next(r for r in view['coverage']['science'] if r['key'] == 'paper')['execution'] == 'unavailable'
    assert not execution.usage()


def test_all_checkpoint_reads_share_a_single_transaction(owner, monkeypatch):
    pipe, _client, pid, _url = owner
    start(owner)
    original = JobExecution.read_checkpoint
    snapshots = []
    def read(self, stage, *, db=None):
        assert db is not None and db.in_transaction
        snapshots.append(db)
        return original(self, stage, db=db)
    monkeypatch.setattr(JobExecution, 'read_checkpoint', read)
    collection_dsh.latest_report(pipe.store, pid)
    assert len(snapshots) > 2 and all(connection is snapshots[0] for connection in snapshots)

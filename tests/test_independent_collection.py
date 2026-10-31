"""Actual refresh -> claim -> pipeline: independent public inputs, zero model work."""
from contextlib import closing

import pytest
import test_refresh_hold_admission as fixtures
from test_refresh_hold_admission import own_kind, receipt

from project_watch.studio import collection_host, dsh_runtime, independent_collection
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.material_ownership import project_guard


@pytest.fixture
def case(tmp_path, monkeypatch):
    return fixtures.case.__wrapped__(tmp_path, monkeypatch)


def calls(store, job):
    with closing(store._connect()) as db:
        return [tuple(r) for r in db.execute('SELECT * FROM studio_job_calls WHERE job_id=? ORDER BY call_key', (job,))]


def configure(client, monkeypatch):
    pipeline = client.app.state.studio_pipeline
    sent = []

    def send(method, url, *_):
        sent.append((method, url))
        if 'cninfo.com.cn' in url:
            return Response(200, {}, b'{"announcements":[],"totalAnnouncement":0}')
        if 'openalex.org' in url:
            return Response(200, {}, b'{"results":[],"meta":{"count":0}}')
        if 'ebi.ac.uk' in url:
            return Response(200, {}, b'{"hitCount":0,"resultList":{"result":[]}}')
        pytest.fail('Unexpected provider or model request')

    pipeline.http_factory = lambda _: Http(send)
    # Independent public host must not read old extractor proposals or analysis.
    for name in ('get_plan', 'get_public_profile'):
        monkeypatch.setattr(pipeline.store, name, lambda *_: pytest.fail('dependent output read'))
    return pipeline, sent


@pytest.mark.parametrize('owner,status,http', [
    ('business', 'unknown', None), ('details', 'started', None), ('business', 'completed', 500)])
@pytest.mark.parametrize('entry', ['pipeline', 'monitor'])
def test_refresh_reaches_bounded_collection_but_never_material_or_analysis(case, monkeypatch, owner, status, http, entry):
    client, store, pid, old = case
    own_kind(store, old, owner)
    receipt(store, old, status=status, http=http)
    old_calls = calls(store, old)
    before_events = store.list_events(project_id=pid)
    pipeline, sent = configure(client, monkeypatch)
    response = client.post(f'/api/studio/projects/{pid}/refresh', json={'kind': 'external'})
    assert response.status_code == 202
    job = store.claim()
    assert job['id'] == response.json()['id']
    if entry == 'pipeline':
        pipeline.run(job)
        ex = JobExecution(store, job)
    else:
        ex = JobExecution(store, job, budgets={'open_data': 1, 'model': 0, 'search': 0, 'page': 0, 'site': 0})
        pipeline._execution_state.current = ex
        dsh_runtime.run_monitor(pipeline, ex, None)
    result = ex.read_checkpoint(independent_collection.RESULT)
    assert ex.read_checkpoint(collection_host.ADMISSION)['enabled'] is True
    assert sent and result['status'] == 'partial' and result['analysis'] == 'not_started'
    assert result['blocking_jobs'] == [old]
    assert ex.read_checkpoint(collection_host.SCOPE)['public_plan']
    assert calls(store, old) == old_calls and store.list_events(project_id=pid) == before_events
    own_calls = calls(store, job['id'])
    assert own_calls and not any(r[2] == 'model' for r in own_calls)
    if entry == 'monitor':
        assert len(sent) == 1 and any(r['outcome'] == 'budget_deferred' for r in result['collection']['rows'])
    with closing(store._connect()) as db:
        assert not db.execute('SELECT 1 FROM studio_evidence_tasks WHERE job_id=?', (job['id'],)).fetchone()
        assert not db.execute('SELECT 1 FROM studio_call_recoveries').fetchone()
        from project_watch.studio.recovery import project_request_holds
        assert [r['job_id'] for r in project_request_holds(db, pid)] == [old]
        if status != 'completed':
            with pytest.raises(HttpError, match='request_outcome_unknown'):
                project_guard(db, pid, job['id'])
    before = len(sent)
    pipeline.run(job)  # completion replay never dispatches again
    assert len(sent) == before


@pytest.mark.parametrize('change', ['paused', 'mixed_hold', 'old_recovery', 'linked_budget', 'input_drift'])
def test_changes_after_admission_stop_before_any_request(case, monkeypatch, change):
    client, store, pid, old = case
    receipt(store, old)
    pipeline, sent = configure(client, monkeypatch)
    assert client.post(f'/api/studio/projects/{pid}/refresh', json={'kind': 'external'}).status_code == 202
    job = store.claim()
    with closing(store._connect()) as db, db:
        if change == 'paused':
            db.execute("UPDATE studio_projects SET status='paused' WHERE project_id=?", (pid,))
        elif change == 'mixed_hold':
            db.execute("UPDATE studio_jobs SET kind='public_profile' WHERE id=?", (old,))
        elif change == 'old_recovery':
            db.execute('INSERT INTO studio_job_checkpoints VALUES (?,?,?)', (job['id'], 'dsh_request:old', '{}'))
        elif change == 'linked_budget':
            db.execute('INSERT INTO studio_job_budget_links VALUES (?,?,?)', (job['id'], old, 'old'))
        else:
            db.execute("UPDATE studio_job_checkpoints SET data='{}' WHERE job_id=? AND stage='detail_inputs'", (old,))
    with pytest.raises((ValueError, HttpError)):
        pipeline.run(job)
    assert not sent


@pytest.mark.parametrize('change', ['no_frozen_inputs', 'active_owner', 'source_hold'])
def test_kind_alone_cannot_admit_an_exception(case, change):
    client, store, pid, old = case
    receipt(store, old, kind='search' if change == 'source_hold' else 'model')
    with closing(store._connect()) as db, db:
        if change == 'no_frozen_inputs':
            db.execute('DELETE FROM studio_job_checkpoints WHERE job_id=?', (old,))
        elif change == 'active_owner':
            db.execute("UPDATE studio_jobs SET status='running' WHERE id=?", (old,))
    result = client.post(f'/api/studio/projects/{pid}/refresh', json={'kind': 'external'})
    assert result.status_code == 409


def test_collection_failure_keeps_receipts_and_does_not_start_analysis(case, monkeypatch):
    client, store, pid, old = case
    receipt(store, old)
    pipeline, sent = configure(client, monkeypatch)

    def fail(*args):
        sent.append(True)
        # Use the actual typed transport failure, not an invented error code.
        raise TimeoutError('synthetic public read timeout')

    pipeline.http_factory = lambda _: Http(fail)
    assert client.post(f'/api/studio/projects/{pid}/refresh', json={'kind': 'external'}).status_code == 202
    job = store.claim()
    pipeline.run(job)
    ex = JobExecution(store, job)
    assert ex.read_checkpoint(independent_collection.RESULT)['analysis'] == 'not_started'
    assert sent and calls(store, job['id'])
    assert all(r[4] == 'failed_read' for r in calls(store, job['id']))
    assert calls(store, old)[0][4] == 'unknown'

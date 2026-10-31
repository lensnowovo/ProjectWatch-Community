"""Exact legacy-owner exclusion never grants replay or mutable-input authority."""
import json
from contextlib import closing

import pytest
import test_refresh_hold_admission as fixtures
from test_refresh_hold_admission import receipt

from project_watch.studio import public_research_admission as admission
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError, Response
from project_watch.studio.material_ownership import project_guard


@pytest.fixture
def case(tmp_path, monkeypatch):
    return fixtures.case.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.parametrize('change', ['paused', 'input_drift', 'receipt_drift', 'mixed_owner',
    'material_owner', 'linked_budget', 'recovery_alias', 'own_unknown', 'root_identity', 'lost_lease'])
def test_mutation_after_admission_stops_actual_dispatch(case, monkeypatch, change):
    client, store, pid, old = case
    receipt(store, old)
    monkeypatch.setenv('PROJECT_WATCH_DSH_RUNTIME', 'synthetic-runtime')
    monkeypatch.setenv('PROJECT_WATCH_DSH_MONITOR', '1')
    assert client.post(f'/api/studio/projects/{pid}/refresh', json={'kind': 'external'}).status_code == 202
    job = store.claim()
    execution = JobExecution(store, job)
    assert admission.admit(client.app.state.studio_pipeline, execution)
    with closing(store._connect()) as db:
        assert admission.held_jobs(db, pid, job['id']) == {old}
        project_guard(db, pid, job['id'])
        before = [tuple(r) for r in db.execute('SELECT * FROM studio_job_calls WHERE job_id=?', (old,))]
    with closing(store._connect()) as db, db:
        if change == 'paused':
            db.execute("UPDATE studio_projects SET status='paused' WHERE project_id=?", (pid,))
        elif change == 'input_drift':
            db.execute("UPDATE studio_job_checkpoints SET data='{}' WHERE job_id=? AND stage='detail_inputs'", (old,))
        elif change == 'receipt_drift':
            db.execute("UPDATE studio_job_calls SET response_status=503 WHERE job_id=?", (old,))
        elif change == 'mixed_owner':
            db.execute("UPDATE studio_jobs SET kind='external' WHERE id=?", (old,))
        elif change == 'material_owner':
            db.execute('INSERT INTO studio_job_checkpoints VALUES (?,?,?)', (old, 'material_pipeline_v1', '{"owner":"materials"}'))
        elif change == 'linked_budget':
            db.execute('INSERT INTO studio_job_budget_links VALUES (?,?,?)', (job['id'], old, 'old'))
        elif change == 'recovery_alias':
            db.execute('INSERT INTO studio_job_checkpoints VALUES (?,?,?)', (job['id'], 'dsh_request:old', '{}'))
        elif change == 'own_unknown':
            db.execute('INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at) '
                       'SELECT ?,call_key,kind,provider,status,created_at FROM studio_job_calls WHERE job_id=?', (job['id'], old))
        elif change == 'root_identity':
            saved = admission.read(db, job['id'])
            saved['root_job_id'] = old
            db.execute('UPDATE studio_job_checkpoints SET data=? WHERE job_id=? AND stage=?', (json.dumps(saved), job['id'], admission.STAGE))
        else:
            db.execute("UPDATE studio_jobs SET lease_token='replaced' WHERE id=?", (job['id'],))
    dispatched = []
    with pytest.raises((HttpError, RuntimeError)):
        execution.call('model', 'brand-new-key', lambda: dispatched.append(True) or Response(200, {}, b'{}'))
    assert not dispatched
    with closing(store._connect()) as db:
        after = [tuple(r) for r in db.execute('SELECT * FROM studio_job_calls WHERE job_id=?', (old,))]
        if change != 'receipt_drift':
            assert after == before
        assert db.execute('SELECT count(*) FROM studio_job_calls WHERE job_id=?', (job['id'],)).fetchone()[0] == (change == 'own_unknown')


@pytest.mark.parametrize('kind', ['insight', 'scope', 'assessment'])
@pytest.mark.parametrize('change', ['frozen_input', 'new_root_unknown', 'old_receipt'])
def test_closeout_dispatch_rechecks_frozen_input_and_both_request_owners(case, monkeypatch, kind, change):
    client, store, pid, old = case
    receipt(store, old)
    monkeypatch.setenv('PROJECT_WATCH_DSH_RUNTIME', 'synthetic-runtime')
    monkeypatch.setenv('PROJECT_WATCH_DSH_MONITOR', '1')
    assert client.post(f'/api/studio/projects/{pid}/refresh', json={'kind': 'external'}).status_code == 202
    root = store.claim()
    assert admission.admit(client.app.state.studio_pipeline, JobExecution(store, root))
    store.finish(root['id'], lease_token=root['lease_token'])
    child = store.enqueue(pid, kind, parent_job_id=root['id'])
    payload = dict(round_root_job_id=root['id'], origin_job_id=root['id'], predecessor_job_id=root['id'],
                   **{'objects' if kind == 'insight' else 'work': [('fixed', 1)]})
    with closing(store._connect()) as db, db:
        db.execute('INSERT INTO studio_job_checkpoints VALUES (?,?,?)',
                   (child['id'], admission.WORK_STAGES[kind], json.dumps(payload)))
        # A JSON round trip changes tuples to lists without changing the input.
        admission.inherit_work(db, pid, root['id'], child, payload)
        assert admission.held_jobs(db, pid, child['id']) == {old}
    claimed = store.claim()
    assert claimed['id'] == child['id']
    with closing(store._connect()) as db, db:
        if change == 'frozen_input':
            payload['unexpected'] = 'changed after admission'
            db.execute('UPDATE studio_job_checkpoints SET data=? WHERE job_id=? AND stage=?',
                       (json.dumps(payload), child['id'], admission.WORK_STAGES[kind]))
        elif change == 'new_root_unknown':
            db.execute('INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at) '
                       'SELECT ?,call_key,kind,provider,status,created_at FROM studio_job_calls WHERE job_id=?', (root['id'], old))
        else:
            db.execute("UPDATE studio_job_calls SET response_status=503 WHERE job_id=?", (old,))
    dispatched = []
    with pytest.raises(HttpError):
        JobExecution(store, claimed).call('model', 'new-closeout', lambda: dispatched.append(True) or Response(200, {}, b'{}'))
    assert not dispatched
    with closing(store._connect()) as db:
        assert db.execute('SELECT count(*) FROM studio_job_calls WHERE job_id=?', (child['id'],)).fetchone()[0] == 0

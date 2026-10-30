"""Owner refresh checks use synthetic receipts; never dispatch or resolve them."""
import socket

import pytest
import test_studio_digest
from fastapi.testclient import TestClient

from project_watch.studio.store import now


@pytest.fixture
def case(tmp_path, monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('network prohibited')
    monkeypatch.setattr(socket, 'create_connection', deny)
    app = test_studio_digest.app.__wrapped__(tmp_path)
    project = test_studio_digest._seed(app)
    store = app.state.studio_store
    old = store.enqueue(project['id'], 'details')
    with store._connect() as db:
        db.execute("UPDATE studio_jobs SET status='failed',error='request_outcome_unknown',finished_at=? WHERE id=?",
                   (now(), old['id']))
    return TestClient(app), store, project['id'], old['id']


def receipt(store, job, *, status='unknown', kind='model', http=None, key='old'):
    with store._connect() as db:
        db.execute('INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,response_status,created_at) '
                   'VALUES(?,?,?,?,?,?,?)', (job, key, kind, 'synthetic', status, http, now()))


def count_rows(store):
    with store._connect() as db:
        return tuple(db.execute('SELECT count(*) FROM '+table).fetchone()[0]
                     for table in ('studio_jobs', 'studio_job_calls', 'studio_job_checkpoints', 'studio_call_recoveries'))


def own_kind(store, job, kind):
    with store._connect() as db:
        db.execute('UPDATE studio_jobs SET kind=? WHERE id=?', (kind, job))


@pytest.mark.parametrize('owner', ['external', 'external_followup', 'public_profile', 'insight'])
@pytest.mark.parametrize(('status', 'kind', 'http'), [
    ('unknown', 'model', None), ('started', 'model', None), ('unknown', 'search', None),
    ('unknown', 'site', None), ('completed', 'model', 500), ('completed', 'model', 503),
])
def test_prior_hold_rejects_refresh_before_queue_without_any_replay(case, status, kind, http, owner):
    client, store, pid, old = case
    own_kind(store, old, owner)
    receipt(store, old, status=status, kind=kind, http=http)
    before = count_rows(store)
    for _ in range(3):
        response = client.post(f'/api/studio/projects/{pid}/refresh', json={'kind': 'external'})
        assert response.status_code == 409
        data = response.json()
        assert data['code'] == 'refresh_blocked' and data['dispatch'] == 'not_started'
        assert data['blocking_jobs'] == [old]
        assert '本次未执行' in data['detail'] and '本次未发送外部请求' in data['detail']
    assert count_rows(store) == before
    assert len(store.list_events(project_id=pid)) == 1


@pytest.mark.parametrize('status', ['not_sent', 'completed'])
def test_known_settled_or_unsent_receipt_does_not_block(case, status):
    client, store, pid, old = case
    receipt(store, old, status=status, http=200 if status == 'completed' else None)
    response = client.post(f'/api/studio/projects/{pid}/refresh', json={'kind':'external'})
    assert response.status_code == 202 and response.json()['status'] == 'queued'


def test_consumed_recovery_and_other_project_cannot_create_false_hold(case):
    client, store, pid, old = case
    receipt(store, old)
    with store._connect() as db:
        db.execute('INSERT INTO studio_call_recoveries(job_id,base_key,previous_key,next_key,created_at,consumed_at) '
                   'VALUES(?,?,?,?,?,?)', (old, 'old', 'old', 'settled-next', now(), now()))
    other = client.post('/api/studio/projects', json={'entity':{'legal_name':'合成另一个主体'},'consent':True}).json()
    with store._connect() as db:
        other_job = db.execute('SELECT id FROM studio_jobs WHERE project_id=?', (other['id'],)).fetchone()[0]
        db.execute("UPDATE studio_jobs SET status='failed' WHERE id=?", (other_job,))
    receipt(store, other_job)
    assert client.post(f'/api/studio/projects/{pid}/refresh', json={'kind':'external'}).status_code == 202
    with store._connect() as db:
        assert db.execute("SELECT status FROM studio_job_calls WHERE job_id=? AND call_key='old'", (old,)).fetchone()[0] == 'unknown'


def test_active_external_job_is_reused_without_new_job(case):
    client, store, pid, old = case
    active = store.enqueue(pid, 'external')
    receipt(store, old)
    before = count_rows(store)
    result = client.post(f'/api/studio/projects/{pid}/refresh', json={'kind':'external'})
    assert result.status_code == 202 and result.json()['id'] == active['id']
    assert count_rows(store) == before


def test_live_prior_request_is_not_described_as_another_timeout(case):
    client, store, pid, old = case
    own_kind(store, old, 'public_profile')  # an independent extractor no longer blocks; see below
    receipt(store, old, status='started')
    with store._connect() as db:
        db.execute("UPDATE studio_jobs SET status='running',lease_until='9999-12-31' WHERE id=?", (old,))
    result = client.post(f'/api/studio/projects/{pid}/refresh', json={'kind':'external'})
    assert result.status_code == 409 and '先前任务仍在执行' in result.json()['detail']


@pytest.mark.parametrize('own_call', [False, True])
def test_task_projection_distinguishes_pre_dispatch_hold_from_own_unknown(case, own_call):
    client, store, pid, old = case
    receipt(store, old)
    legacy = store.enqueue(pid, 'external')
    with store._connect() as db:
        db.execute("UPDATE studio_jobs SET status='failed',error='request_outcome_unknown',finished_at=? WHERE id=?",
                   (now(), legacy['id']))
    if own_call:
        receipt(store, legacy['id'])
    job = next(j for j in client.get(f'/api/studio/tasks?project_id={pid}').json()['jobs'] if j['id'] == legacy['id'])
    if own_call:
        assert job['runtime']['state'] == 'needs_confirmation' and not job['runtime']['blocking_jobs']
        assert job['advice']['action'] == 'recover' and '已经发出' in job['advice']['why']
    else:
        assert job['runtime']['state'] == 'blocked' and job['runtime']['blocking_jobs'] == [old]
        assert job['advice']['action'] == 'none' and '本次未执行' in job['advice']['why']
        assert '可能已经计费' not in job['advice']['why'] and job['unknown'] == job['calls'] == 0


@pytest.mark.parametrize('owner', ['business', 'details'])
@pytest.mark.parametrize(('status', 'http'), [('unknown', None), ('started', None), ('completed', 500)])
def test_independent_extractor_hold_admits_refresh_and_keeps_its_own_hold(case, owner, status, http):
    """A frozen-input extractor's pending request is not an input to public collection."""
    from project_watch.studio.recovery import project_request_holds

    client, store, pid, old = case
    own_kind(store, old, owner)
    receipt(store, old, status=status, http=http)
    with store._connect() as db:
        before_calls = db.execute('SELECT * FROM studio_job_calls ORDER BY rowid').fetchall()
        before_recoveries = db.execute('SELECT count(*) FROM studio_call_recoveries').fetchone()[0]
    first = client.post(f'/api/studio/projects/{pid}/refresh', json={'kind': 'external'})
    assert first.status_code == 202 and first.json()['status'] == 'queued' and first.json()['kind'] == 'external'
    # A second press coalesces into the same round; it does not create more work.
    again = client.post(f'/api/studio/projects/{pid}/refresh', json={'kind': 'external'})
    assert again.json()['id'] == first.json()['id']
    with store._connect() as db:
        # Nothing was sent, settled, retried or relabelled: the old receipt is byte-identical.
        assert db.execute('SELECT * FROM studio_job_calls ORDER BY rowid').fetchall() == before_calls
        assert db.execute('SELECT count(*) FROM studio_call_recoveries').fetchone()[0] == before_recoveries
        assert db.execute('SELECT status FROM studio_jobs WHERE id=?', (old,)).fetchone()[0] == 'failed'
        # The hold is still reported for its own job and recovery dialog.
        assert [r['job_id'] for r in project_request_holds(db, pid)] == [old]


def test_independent_hold_does_not_hide_a_blocking_one(case):
    client, store, pid, old = case
    own_kind(store, old, 'business')
    receipt(store, old)
    blocking = store.enqueue(pid, 'public_profile')
    with store._connect() as db:
        db.execute("UPDATE studio_jobs SET status='failed' WHERE id=?", (blocking['id'],))
    receipt(store, blocking['id'], key='other')
    response = client.post(f'/api/studio/projects/{pid}/refresh', json={'kind': 'external'})
    assert response.status_code == 409 and response.json()['blocking_jobs'] == [blocking['id']]

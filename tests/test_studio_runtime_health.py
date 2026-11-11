"""Offline watchdog, queue fairness, restart and truthful status contracts."""

import json
import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_studio_agent_instance import make_service

from project_watch.studio.execution import JobExecution, JobYield
from project_watch.studio.http import Response
from project_watch.studio.runtime_health import projection, tick


def setup(tmp_path):
    service, pid = make_service(tmp_path / 'watch.sqlite3')
    store = service.store
    store.enqueue(pid, 'public_profile')
    job = store.claim()
    return store, pid, job


def test_saved_section_partial_is_not_reported_as_complete(tmp_path):
    store, _, job = setup(tmp_path)
    JobExecution(store, job).checkpoint_chunked('snapshot_analysis_publication_v1',
        dict(status='partial', sections={'operations': {'status': 'rejected'}}))
    assert store.finish(job['id'], lease_token=job['lease_token'])
    with closing(store._connect()) as db:
        result = projection(db, row(store, job))
    assert result['state'] == 'partial'
    assert result['label'] == '材料已保存，部分分析未完成'


def row(store, job):
    with closing(store._connect()) as db:
        return dict(db.execute('SELECT * FROM studio_jobs WHERE id=?', (job['id'],)).fetchone())


def age(store, job):
    with closing(store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET progress_at='2000-01-01T00:00:00+00:00' WHERE id=?", (job['id'],))


def expire_owner(store, job):
    with closing(store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET lease_until='2000-01-01' WHERE id=?", (job['id'],))


def test_local_processing_age_does_not_revoke_a_live_owner(tmp_path):
    store, pid, job = setup(tmp_path)
    age(store, job)
    assert store.heartbeat(job['id'], job['lease_token'])
    assert tick(store, job) == 'alive'
    assert row(store, job)['status'] == 'running'
    expire_owner(store, job)
    with pytest.raises(RuntimeError):
        JobExecution(store, job).checkpoint('late', {})
    again = store.claim()
    assert again['id'] == job['id'] and again['lease_token'] != job['lease_token']
    assert store.jobs(pid)[0]['attempts'] == 2


def test_known_receipts_reused_after_watchdog_restart(tmp_path):
    store, _, job = setup(tmp_path)
    execution = JobExecution(store, job)
    response = execution.call('model', 'one', lambda: Response(200, {}, b'{}'))
    expire_owner(store, job)
    from project_watch.studio.store import StudioStore

    restarted = StudioStore(store.db_path)
    again = restarted.claim()
    assert JobExecution(restarted, again).call('model', 'one', lambda: pytest.fail('paid replay')) == response


def test_recovery_count_does_not_permanently_stop_safe_work(tmp_path):
    store, _, job = setup(tmp_path)
    for _ in range(4):
        expire_owner(store, job)
        previous = job
        job = store.claim()
        assert job['lease_token'] != previous['lease_token']
    assert row(store, job)['attempts'] == 5


def test_live_transport_has_deadline_and_late_reply_cannot_publish(tmp_path):
    store, _, job = setup(tmp_path)
    entered, released = threading.Event(), threading.Event()
    errors = []

    def transport():
        entered.set()
        assert released.wait(10)
        return Response(200, {}, b'{"synthetic":true}')

    def call():
        try:
            JobExecution(store, job).call('model', 'slow', transport, timeout=3600)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=call)
    thread.start()
    try:
        assert entered.wait(5)
        age(store, job)
        assert tick(store, job) == 'alive'  # long but legitimate request
        with closing(store._connect()) as db, db:
            assert projection(db, row(store, job))['state'] == 'waiting_response'
            db.execute("UPDATE studio_job_calls SET deadline_at='2000-01-01' WHERE job_id=?", (job['id'],))
        assert tick(store, job) == 'held'
        assert row(store, job)['retry_after'] == '9999-12-31'
        assert store.claim() is None
    finally:
        released.set()
        thread.join(5)
    assert len(errors) == 1 and 'lease' in str(errors[0])
    with closing(store._connect()) as db:
        assert db.execute('SELECT response_body FROM studio_job_calls').fetchone()[0] is None
        assert projection(db, row(store, job))['state'] == 'failed'  # unclear, recorded, never resent


def test_unknown_search_held_not_just_model_calls(tmp_path):
    store, _, job = setup(tmp_path)
    def lost():
        raise OSError('synthetic connection lost')
    with pytest.raises(OSError):
        JobExecution(store, job).call('search', 'search', lost)
    assert store.finish(job['id'], error='request_outcome_unknown', lease_token=job['lease_token'])
    from project_watch.studio.taskboard import board
    result = board(store, worker_state='running', model_ready=True)
    assert result['jobs'][0]['advice']['action'] == 'none'  # no resend action


def test_only_changed_stage_or_checkpoint_is_progress(tmp_path):
    store, _, job = setup(tmp_path)
    execution = JobExecution(store, job)
    store.set_stage(job['id'], '读取正文')
    execution.checkpoint('phase', {'n': 1})
    age(store, job)
    store.set_stage(job['id'], '读取正文')
    execution.checkpoint('phase', {'n': 1})
    assert row(store, job)['progress_at'].startswith('2000')
    execution.checkpoint('phase', {'n': 2})
    assert not row(store, job)['progress_at'].startswith('2000')


def test_lost_owner_cannot_watchdog_new_owner(tmp_path):
    store, _, job = setup(tmp_path)
    expire_owner(store, job)
    again = store.claim()
    assert tick(store, job) == 'lost'
    assert row(store, again)['lease_token'] == again['lease_token']


def test_cooperative_yield_keeps_receipts_and_queue_fairness(tmp_path):
    store, pid, job = setup(tmp_path)
    execution = JobExecution(store, job)
    execution.checkpoint('saved_phase', True)
    other = store.enqueue(pid, 'details')
    execution.slice_seconds = 0
    with pytest.raises(JobYield):
        execution.yield_if_due()
    assert store.yield_job(job)
    chosen = store.claim()
    assert chosen['id'] == other['id']
    store.finish(chosen['id'], lease_token=chosen['lease_token'])
    resumed = store.claim()
    assert resumed['id'] == job['id'] and resumed['attempts'] == 1
    assert JobExecution(store, resumed).read_checkpoint('saved_phase') is True


def test_pause_is_not_healthy_queued_work(tmp_path):
    store, pid, job = setup(tmp_path)
    store.yield_job(job)
    store.set_status(pid, 'paused')
    with closing(store._connect()) as db:
        assert projection(db, row(store, job))['state'] == 'paused'


def test_read_health_never_dispatches_or_leaks(tmp_path):
    from project_watch.studio.app import create_studio_app

    app = create_studio_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        result = client.get('/api/studio/health').json()
        assert {k: result[k] for k in ('state', 'label', 'restart_required')} == {
            'state': 'stopped', 'label': '后台未运行', 'restart_required': False}
        assert result['parsing']['state'] == 'stopped'
        assert not result['draining'] and not result['drained']
        assert str(tmp_path) not in json.dumps(result)
        tasks = client.get('/api/studio/tasks').json()['runtime']
        assert {k: v for k, v in tasks.items() if k != 'storage'} == {
            k: v for k, v in result.items() if k != 'storage'}


def test_worker_detects_stall_and_transient_loop_error(tmp_path, monkeypatch):
    from project_watch.studio.app import create_studio_app

    worker = create_studio_app(tmp_path, start_worker=False).state.studio_worker
    count = []
    def step():
        count.append(1)
        if len(count) == 1:
            raise OSError('synthetic busy database')
        worker._stop.set()
        return True
    monkeypatch.setattr(worker, 'step', step)
    monkeypatch.setattr(worker._stop, 'wait', lambda *_: None)
    worker._run()
    assert len(count) == 2
    monkeypatch.setattr(worker, 'runtime_state', lambda: 'running')
    worker._stalled_job = 'synthetic'
    assert worker.health()['restart_required'] is True




def test_expired_lease_is_reported_as_recovery_not_running(tmp_path):
    store, _, job = setup(tmp_path)
    with closing(store._connect()) as db, db:
        db.execute('UPDATE studio_jobs SET lease_until=? WHERE id=?', ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), job['id']))
    with closing(store._connect()) as db:
        assert projection(db, row(store, job))['state'] == 'recovering'




def test_worker_heartbeat_really_fences_hung_dispatch(tmp_path, monkeypatch):
    from project_watch.studio import runtime_health
    from project_watch.studio.app import create_studio_app
    from project_watch.studio.pipeline import StudioWorker

    pipeline = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    pid = pipeline.repository.create_project('Synthetic Stall').id
    pipeline.store.upsert_project(pid, {}, consent=True)
    pipeline.store.enqueue(pid, 'public_profile')
    worker = StudioWorker(pipeline, heartbeat_seconds=.01)
    released, fenced = threading.Event(), threading.Event()
    monkeypatch.setattr(worker, 'schedule', lambda: None)
    real_tick = runtime_health.tick
    def tick_notify(*args, **kwargs):
        result = real_tick(*args, **kwargs)
        if result == 'lost':
            fenced.set()
        return result
    monkeypatch.setattr(runtime_health, 'tick', tick_notify)
    monkeypatch.setattr(pipeline, 'run', lambda _: released.wait(5))
    thread = threading.Thread(target=worker.step)
    worker._thread = thread
    thread.start()
    try:
        for _ in range(100):
            if worker._current_job:
                break
            threading.Event().wait(.01)
        active = pipeline.store.jobs(pid)[0]
        expire_owner(pipeline.store, active)
        replacement = pipeline.store.claim()
        assert fenced.wait(5)
        # Tick sets the event just before the worker records the local stall.
        for _ in range(100):
            if worker.health()['restart_required']:
                break
            threading.Event().wait(.01)
        assert worker.health()['state'] == 'stalled'
        assert pipeline.store.jobs(pid)[0]['lease_token'] == replacement['lease_token']
    finally:
        released.set()
        thread.join(5)
    assert pipeline.store.jobs(pid)[0]['status'] == 'running'  # late finish fenced
    assert not worker.health()['restart_required']


def test_reply_after_sleep_is_kept_and_replayed_not_resent(tmp_path):
    """The computer slept past the lease; on wake another lane requeued the job first."""
    store, _, job = setup(tmp_path)
    execution = JobExecution(store, job)
    owned = []

    def transport():
        owned.append(execution._owns_transport(_started_key(store, job)))
        expire_owner(store, job)
        assert store.claim('round') is None  # the other lane wakes first and requeues the expired job
        assert row(store, job)['status'] == 'queued'
        owned.append(execution._owns_transport(_started_key(store, job)))
        return Response(200, {}, b'{"usage":{"prompt_tokens":11,"completion_tokens":3}}')

    with pytest.raises(RuntimeError, match='lease'):
        execution.call('model', 'paid', transport, timeout=None)
    assert owned == [True, True]  # sleeping past the lease does not abandon the request
    with closing(store._connect()) as db:
        saved = db.execute('SELECT status,input_tokens,output_tokens FROM studio_job_calls').fetchone()
    assert tuple(saved) == ('completed', 11, 3)
    resumed = store.claim()
    assert resumed['id'] == job['id']
    replay = JobExecution(store, resumed).call('model', 'paid', lambda: pytest.fail('paid request resent'))
    assert replay.body.startswith(b'{"usage"')


def test_explicit_stop_and_watchdog_hold_still_end_the_wait(tmp_path):
    store, _, job = setup(tmp_path)
    stopped = False
    execution = JobExecution(store, job, cancel_requested=lambda: stopped)
    checks = []

    def transport():
        key = _started_key(store, job)
        checks.append(execution._owns_transport(key))
        with closing(store._connect()) as db, db:
            db.execute("UPDATE studio_job_calls SET deadline_at='2000-01-01' WHERE job_id=?", (job['id'],))
        assert tick(store, job) == 'held'
        checks.append(execution._owns_transport(key))
        return Response(200, {}, b'{}')

    with pytest.raises(RuntimeError, match='lease'):
        execution.call('model', 'late', transport, timeout=3600)
    assert checks == [True, False]
    with closing(store._connect()) as db:  # held as unknown: a late reply does not settle it
        assert db.execute('SELECT status FROM studio_job_calls').fetchone()[0] == 'started'
    (tmp_path / 'second').mkdir()
    store2, _, job2 = setup(tmp_path / 'second')
    stopper = JobExecution(store2, job2, cancel_requested=lambda: stopped)

    def stopped_transport():
        nonlocal stopped
        stopped = True
        checks.append(stopper._owns_transport(_started_key(store2, job2)))
        return Response(200, {}, b'{}')
    from project_watch.studio.http import HttpError
    with pytest.raises(HttpError, match='request_cancelled'):
        stopper.call('model', 'stop', stopped_transport, timeout=None)
    assert checks[-1] is False
    with closing(store2._connect()) as db:  # a stop prevents new work, not saving a received reply
        assert db.execute('SELECT status FROM studio_job_calls').fetchone()[0] == 'completed'


def test_heartbeat_keeps_renewing_through_a_transient_database_error(tmp_path, monkeypatch):
    from project_watch.studio import runtime_health
    from project_watch.studio.app import create_studio_app
    from project_watch.studio.pipeline import StudioWorker

    pipeline = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    pid = pipeline.repository.create_project('Synthetic Busy').id
    pipeline.store.upsert_project(pid, {}, consent=True)
    pipeline.store.enqueue(pid, 'public_profile')
    worker = StudioWorker(pipeline, heartbeat_seconds=.01)
    monkeypatch.setattr(worker, 'schedule', lambda: None)
    real_tick, calls, renewed = runtime_health.tick, [], threading.Event()

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError('synthetic database is locked')
        if len(calls) >= 3:
            renewed.set()
        return real_tick(*args, **kwargs)
    monkeypatch.setattr(runtime_health, 'tick', flaky)
    seen = {}

    def run(job):
        renewed.wait(5)
        seen.update(stalled=worker._stalled_job, lease=row(pipeline.store, job)['lease_until'],
                    claimed=job['lease_until'])
    monkeypatch.setattr(pipeline, 'run', run)
    worker.step()
    assert renewed.is_set() and seen['stalled'] is None
    assert seen['lease'] > seen['claimed']  # renewed after the failed check


def test_waiting_for_a_sent_request_after_sleep_is_not_a_stall(tmp_path, monkeypatch):
    """A supervisor restarts a stalled worker; that would discard the reply still on its way."""
    from project_watch.studio.app import create_studio_app
    from project_watch.studio.execution import JobExecution as Execution
    from project_watch.studio.pipeline import StudioWorker

    pipeline = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    pid = pipeline.repository.create_project('Synthetic Sleep').id
    pipeline.store.upsert_project(pid, {}, consent=True)
    pipeline.store.enqueue(pid, 'public_profile')
    worker = StudioWorker(pipeline, heartbeat_seconds=.01)
    monkeypatch.setattr(worker, 'schedule', lambda: None)
    seen = {}

    def run(job):
        def transport():
            expire_owner(pipeline.store, job)  # slept past the lease
            for _ in range(200):
                if seen.get('lost'):
                    break
                threading.Event().wait(.01)
            seen['stalled'] = worker._stalled_job
            return Response(200, {}, b'{}')
        Execution(pipeline.store, job).call('model', 'asleep', transport, timeout=None)
    from project_watch.studio import runtime_health
    real_tick = runtime_health.tick

    def watched(*args, **kwargs):
        outcome = real_tick(*args, **kwargs)
        seen['lost'] = seen.get('lost') or outcome == 'lost'
        return outcome
    monkeypatch.setattr(runtime_health, 'tick', watched)
    monkeypatch.setattr(pipeline, 'run', run)
    worker.step()
    assert seen['lost'] and seen['stalled'] is None


def _started_key(store, job):
    with closing(store._connect()) as db:
        return db.execute("SELECT call_key FROM studio_job_calls WHERE job_id=? AND status='started'",
                          (job['id'],)).fetchone()[0]

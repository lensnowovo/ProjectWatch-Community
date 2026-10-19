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
    store.enqueue(pid, 'external')
    job = store.claim()
    return store, pid, job


def row(store, job):
    with closing(store._connect()) as db:
        return dict(db.execute('SELECT * FROM studio_jobs WHERE id=?', (job['id'],)).fetchone())


def age(store, job):
    with closing(store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET progress_at='2000-01-01T00:00:00+00:00' WHERE id=?", (job['id'],))


def test_lease_heartbeat_cannot_hide_missing_progress(tmp_path):
    store, pid, job = setup(tmp_path)
    age(store, job)
    assert store.heartbeat(job['id'], job['lease_token'])
    assert tick(store, job) == 'recovering'
    assert row(store, job)['status'] == 'queued'
    with pytest.raises(RuntimeError):
        JobExecution(store, job).checkpoint('late', {})
    again = store.claim()
    assert again['id'] == job['id'] and again['lease_token'] != job['lease_token']
    assert store.jobs(pid)[0]['attempts'] == 2


def test_known_receipts_reused_after_watchdog_restart(tmp_path):
    store, _, job = setup(tmp_path)
    execution = JobExecution(store, job)
    response = execution.call('model', 'one', lambda: Response(200, {}, b'{}'))
    age(store, job)
    tick(store, job)
    from project_watch.studio.store import StudioStore

    restarted = StudioStore(store.db_path)
    again = restarted.claim()
    assert JobExecution(restarted, again).call('model', 'one', lambda: pytest.fail('paid replay')) == response


def test_three_stalls_hold_without_restart_storm(tmp_path):
    store, _, job = setup(tmp_path)
    for attempt in range(3):
        age(store, job)
        assert tick(store, job) == ('held' if attempt == 2 else 'recovering')
        if attempt < 2:
            job = store.claim()
    assert row(store, job)['retry_after'] == '9999-12-31'
    assert store.claim() is None


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
        assert projection(db, row(store, job))['state'] == 'needs_confirmation'


def test_unknown_search_held_not_just_model_calls(tmp_path):
    store, _, job = setup(tmp_path)
    def lost():
        raise OSError('synthetic connection lost')
    with pytest.raises(OSError):
        JobExecution(store, job).call('search', 'search', lost)
    age(store, job)
    assert tick(store, job) == 'held'
    from project_watch.studio.taskboard import board
    result = board(store, worker_state='running', model_ready=True)
    assert result['jobs'][0]['advice']['action'] == 'recover'


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
    age(store, job)
    tick(store, job)
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


def test_superseded_unknown_call_is_not_active_deadline(tmp_path):
    store, _, job = setup(tmp_path)
    with closing(store._connect()) as db, db:
        db.execute("INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at,deadline_at) VALUES (?,'old','model','hash','started','2000-01-01','2000-01-01')", (job['id'],))
        db.execute("INSERT INTO studio_call_recoveries VALUES (?,'base','old','next','2000-01-01','2000-01-02')", (job['id'],))
    assert tick(store, job) == 'alive'


def test_expired_lease_is_reported_as_recovery_not_running(tmp_path):
    store, _, job = setup(tmp_path)
    with closing(store._connect()) as db, db:
        db.execute('UPDATE studio_jobs SET lease_until=? WHERE id=?', ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), job['id']))
    with closing(store._connect()) as db:
        assert projection(db, row(store, job))['state'] == 'recovering'


def test_external_followup_yields_once_per_saved_phase(tmp_path, monkeypatch):
    from test_studio_external_round import first, followup
    from test_studio_external_round import setup as setup_external

    pipeline, _pid, calls = setup_external(tmp_path, monkeypatch)
    first(pipeline)
    job = followup(pipeline)
    monkeypatch.setattr(JobExecution, 'yield_if_due', lambda _: (_ for _ in ()).throw(JobYield()))
    for expected in ('external_collected', 'followup_bodies_done', 'followup_events_done'):
        with pytest.raises(JobYield):
            pipeline.run(job)
        assert JobExecution(pipeline.store, job).read_checkpoint(expected) is not None
        pipeline.store.yield_job(job)
        job = pipeline.store.claim()
        assert job['kind'] == 'external_followup'
    before = list(calls)
    pipeline.run(job)
    assert JobExecution(pipeline.store, job).read_checkpoint('dispatch_completed')
    assert not [c for c in calls[len(before):] if c[0] in ('bing', 'paper', 'filing', 'site')]


def test_worker_heartbeat_really_fences_hung_dispatch(tmp_path, monkeypatch):
    from project_watch.studio import runtime_health
    from project_watch.studio.app import create_studio_app
    from project_watch.studio.pipeline import StudioWorker

    pipeline = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    pid = pipeline.repository.create_project('Synthetic Stall').id
    pipeline.store.upsert_project(pid, {}, consent=True)
    pipeline.store.enqueue(pid, 'external')
    worker = StudioWorker(pipeline, heartbeat_seconds=.01)
    released, fenced = threading.Event(), threading.Event()
    monkeypatch.setattr(worker, 'schedule', lambda: None)
    monkeypatch.setattr(runtime_health, 'IDLE_SECONDS', 0)
    real_tick = runtime_health.tick
    def tick_notify(*args, **kwargs):
        result = real_tick(*args, **kwargs)
        if result == 'recovering':
            fenced.set()
        return result
    monkeypatch.setattr(runtime_health, 'tick', tick_notify)
    monkeypatch.setattr(pipeline, 'run', lambda _: released.wait(5))
    thread = threading.Thread(target=worker.step)
    worker._thread = thread
    thread.start()
    try:
        assert fenced.wait(5)
        # Tick sets the event just before the worker records the local stall.
        for _ in range(100):
            if worker.health()['restart_required']:
                break
            threading.Event().wait(.01)
        assert worker.health()['state'] == 'stalled'
        assert pipeline.store.jobs(pid)[0]['status'] == 'queued'
    finally:
        released.set()
        thread.join(5)
    assert pipeline.store.jobs(pid)[0]['status'] == 'queued'  # late finish fenced
    assert not worker.health()['restart_required']

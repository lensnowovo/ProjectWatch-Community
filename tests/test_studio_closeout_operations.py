"""Synthetic-only parser supervision, drain and storage reliability checks."""

import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from project_watch.extraction_process import _child_environment, extract_document_subprocess
from project_watch.extractors import ExtractionError, extract_document
from project_watch.parser_supervision import run_parser
from project_watch.studio.app import create_studio_app
from project_watch.studio.maintenance import disk_health, rotating_child_log


def test_progress_includes_empty_units_and_does_not_limit_content(tmp_path):
    source = tmp_path / 'example.txt'
    source.write_text('one\n\nthree', encoding='utf-8')
    ticks = []
    result = extract_document(source, progress=lambda: ticks.append(True))
    assert len(ticks) == 3
    assert [f.text for f in result.fragments] == ['one', 'three']
    assert extract_document_subprocess(source, suffix='.txt').fragments == result.fragments


def test_healthy_progress_outlives_idle_window(tmp_path):
    progress = tmp_path / 'progress'
    code = ('import time;from pathlib import Path;'
            f'p=Path({str(progress)!r});'
            '\nfor _ in range(12):\n p.touch();time.sleep(.15)')
    ticks = []
    before = time.monotonic()
    run_parser([sys.executable, '-c', code], env=_child_environment(), progress_path=progress,
               idle_seconds=.8, poll_seconds=.05, on_progress=lambda: ticks.append(True))
    assert time.monotonic() - before > 1.5
    assert len(ticks) >= 5


@pytest.mark.parametrize('mode', ['stalled', 'cancelled', 'crashed'])
def test_parser_owned_process_is_stopped_on_failure(tmp_path, mode):
    cancel = threading.Event()
    timer = threading.Timer(.5, cancel.set) if mode == 'cancelled' else None
    if timer:
        timer.start()
    try:
        with pytest.raises(ExtractionError) as caught:
            run_parser([sys.executable, '-c', 'raise SystemExit(3)' if mode == 'crashed' else
                        'import time;time.sleep(60)'], env=_child_environment(),
                       progress_path=tmp_path / 'progress', cancel=cancel,
                       idle_seconds=1, poll_seconds=.05)
        assert caught.value.error_code == 'parser_' + mode
    finally:
        if timer:
            timer.cancel()


def test_cancel_before_launch_never_creates_child(tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ExtractionError, match='parser_cancelled'):
        run_parser(['nonexistent'], env={}, progress_path=tmp_path / 'p', cancel=cancel)


def test_parser_environment_does_not_forward_keys(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'synthetic-secret')
    monkeypatch.setenv('PROJECT_WATCH_HOME', 'synthetic-private')
    assert 'ANTHROPIC_API_KEY' not in _child_environment()
    assert 'PROJECT_WATCH_HOME' not in _child_environment()


def test_cancelled_intake_keeps_original_and_can_retry(tmp_path, monkeypatch):
    app = create_studio_app(tmp_path, start_worker=False)
    worker = app.state.intake_worker
    def cancel(*args, **kwargs):
        assert kwargs['cancel'] is worker._stop
        kwargs['on_progress']()
        raise ExtractionError('parser_cancelled')
    monkeypatch.setattr('project_watch.intake_worker.extract_document_subprocess', cancel)
    with TestClient(app) as client:
        project = client.post('/api/projects', json={'name': 'Synthetic'}).json()
        uploaded = client.post(f"/api/projects/{project['id']}/files",
                               files={'file': ('intro.txt', b'synthetic', 'text/plain')}).json()
        job = worker.process_one()
        assert job.status == 'failed' and job.error_code == 'parser_cancelled'
        source = worker.repository.get_source(job.source_id)
        assert worker.blob_store.path_for_hash(source.content_sha256).read_bytes() == b'synthetic'
        assert worker.repository.retry_failed_intake(job.id).status == 'queued'
        assert uploaded['source']['id'] == source.id
        worker.request_stop()
        assert worker.process_one() is None


def test_drain_blocks_writes_keeps_reads_and_stops_all_workers(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        state = client.post('/api/studio/runtime/drain').json()
        assert state['draining'] and state['drained']
        assert not state['restart_required']
        assert client.post('/api/studio/resolve', json={'name': 'Synthetic'}).status_code == 503
        assert client.post('/api/projects', json={'name': 'Synthetic'}).status_code == 503
        assert client.get('/api/studio/projects').status_code == 200
        assert client.post('/api/studio/runtime/drain').json()['drained']
        for worker in (app.state.studio_worker, app.state.intake_worker,
                       app.state.reading_worker, app.state.monitoring_scheduler):
            assert worker._stop.is_set()


def test_drain_does_not_claim_done_with_live_writer_or_worker(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    @app.post('/api/synthetic-slow')
    def slow():
        entered.set()
        release.wait(5)
        return {}
    entered, release = threading.Event(), threading.Event()
    with TestClient(app) as client:
        request = threading.Thread(target=lambda: client.post('/api/synthetic-slow'))
        request.start()
        assert entered.wait(3)
        try:
            state = client.post('/api/studio/runtime/drain').json()
            assert not state['drained'] and state['active_writes'] == 1
        finally:
            release.set()
            request.join(5)
        assert client.get('/api/studio/health').json()['drained']


def test_shutdown_keeps_home_lock_until_writer_thread_exits(tmp_path):
    from project_watch.studio.host import instance_lock
    app = create_studio_app(tmp_path, start_worker=False)
    entered, exit_context, release, exited = (threading.Event() for _ in range(4))
    writer = threading.Thread(target=lambda: release.wait(5))
    app.state.studio_worker._thread = writer
    writer.start()
    def lifespan():
        with TestClient(app):
            entered.set()
            exit_context.wait(5)
        exited.set()
    lifetime = threading.Thread(target=lifespan)
    lifetime.start()
    try:
        assert entered.wait(3)
        exit_context.set()
        assert not exited.wait(.3)
        with pytest.raises(RuntimeError), instance_lock(tmp_path):
            pytest.fail('live writer lost the directory lock')
    finally:
        release.set()
        writer.join(5)
        lifetime.join(5)
    assert exited.is_set()
    with instance_lock(tmp_path):
        pass


def test_health_exposes_intake_stall_and_disk_warning_without_private_paths(tmp_path, monkeypatch):
    app = create_studio_app(tmp_path, start_worker=False)
    worker = app.state.intake_worker
    worker._active = True
    worker._phase = 'indexing'
    worker._progress_at = time.monotonic() - 901
    with TestClient(app) as client:
        state = client.get('/api/studio/health').json()
        assert state['restart_required'] and state['state'] == 'stalled'
        assert state['parsing']['phase'] == 'indexing'
        assert str(tmp_path) not in str(state)
        worker._active = False
    monkeypatch.setattr('shutil.disk_usage', lambda _: SimpleNamespace(total=100, free=1))
    assert disk_health(tmp_path)['state'] == 'low'
    def unavailable(_):
        raise OSError('synthetic')
    monkeypatch.setattr('shutil.disk_usage', unavailable)
    assert disk_health(tmp_path)['state'] == 'unknown'


def test_owned_log_rotates_without_touching_legacy_logs(tmp_path):
    legacy = tmp_path / 'run-old.log'
    legacy.write_text('preserved')
    with rotating_child_log(tmp_path / 'runtime.log', max_bytes=8192, backups=2) as (stream, state):
        for _ in range(30):
            stream.write(b'x' * 4096)
    assert not state['write_failed']
    assert legacy.read_text() == 'preserved'
    files = list(tmp_path.glob('runtime.log*'))
    assert len(files) == 3
    assert sum(f.stat().st_size for f in files) <= 3 * 8192


def test_log_disk_failure_still_drains_pipe(tmp_path, monkeypatch):
    def failed(*args):
        raise OSError('disk full')
    monkeypatch.setattr('logging.handlers.RotatingFileHandler.shouldRollover', failed)
    with rotating_child_log(tmp_path / 'runtime.log') as (stream, state):
        stream.write(b'x' * 100_000)
    assert state['write_failed']


@pytest.mark.skipif(os.name != 'nt', reason='Windows owned-tree integration')
def test_rotating_pipe_accepts_windows_owned_child(tmp_path):
    from project_watch.studio.windows_child import WindowsChild
    with rotating_child_log(tmp_path / 'runtime.log') as (stream, state):
        child = WindowsChild([sys.executable, '-c', 'print("synthetic-child")'],
                             env=_child_environment(), log=stream)
        try:
            assert child.wait(10) == 0
        finally:
            child.close()
    assert 'synthetic-child' in (tmp_path / 'runtime.log').read_text()
    assert not state['write_failed']


def test_supervisor_respects_explicit_drain_without_restarting(tmp_path, monkeypatch):
    from project_watch.studio import host
    calls = []
    class Child:
        def poll(self):
            return None
        def terminate(self):
            calls.append('stop')
        def wait(self, timeout):
            return 0
    monkeypatch.setattr(host, 'launch_child', lambda *args: calls.append('start') or Child())
    monkeypatch.setattr(host.time, 'sleep', lambda _: None)
    monkeypatch.setattr(host, 'health', lambda _: {'state': 'stopped', 'draining': True, 'drained': True})
    host.supervise(tmp_path, 0)
    assert calls == ['start', 'stop']


def test_drain_rejects_cross_origin_request(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        response = client.post('/api/studio/runtime/drain', headers={'Origin': 'https://untrusted.example'})
        assert response.status_code == 403
        assert not client.get('/api/studio/health').json()['draining']


def test_supervisor_drain_grace_does_not_restart_hung_child(tmp_path, monkeypatch):
    from project_watch.studio import host
    clock, calls = [100.0], []
    class Child:
        def poll(self):
            return None
        def terminate(self):
            calls.append('stop')
        def wait(self, timeout):
            return 0
    def sleep(_):
        clock[0] += 35
    monkeypatch.setattr(host, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    monkeypatch.setattr(host, 'launch_child', lambda *args: calls.append('start') or Child())
    monkeypatch.setattr(host, 'health', lambda _: {'state': 'stopping', 'draining': True, 'drained': False})
    host.supervise(tmp_path, 0)
    assert calls == ['start', 'stop']
    assert clock[0] >= 255


@pytest.mark.skipif(os.name != 'nt', reason='Windows watchdog tree integration')
def test_watchdog_reaps_parser_descendant(tmp_path):
    import ctypes
    from ctypes import wintypes as wt
    marker = tmp_path / 'child.pid'
    code = ('import subprocess,sys,time;from pathlib import Path;'
            'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]);'
            f'Path({str(marker)!r}).write_text(str(p.pid));time.sleep(60)')
    with pytest.raises(ExtractionError, match='parser_stalled'):
        run_parser([sys.executable, '-c', code], env=_child_environment(),
                   progress_path=tmp_path / 'progress', idle_seconds=2, poll_seconds=.05)
    assert marker.exists()
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel.OpenProcess.restype = wt.HANDLE
    kernel.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    handle = kernel.OpenProcess(0x100000, False, int(marker.read_text()))
    if handle:
        try:
            assert kernel.WaitForSingleObject(handle, 5000) == 0
        finally:
            kernel.CloseHandle(handle)

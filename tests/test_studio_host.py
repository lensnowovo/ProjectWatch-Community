import json
import os
import sqlite3
import sys
import time
from contextlib import closing

import pytest

from project_watch.studio.host import (
    instance_lock,
    restore_snapshot,
    snapshot,
    stop_child,
    verify_snapshot,
)


def home(tmp_path):
    root = tmp_path / 'home'
    root.mkdir()
    with closing(sqlite3.connect(root / 'projectwatch.sqlite3')) as db:
        db.execute('CREATE TABLE sample(value TEXT)')
        db.execute("INSERT INTO sample VALUES ('synthetic')")
        db.commit()
    (root / 'settings.json').write_text('{"synthetic": true}', encoding='utf-8')
    (root / 'objects').mkdir()
    (root / 'objects' / 'synthetic').write_bytes(b'original')
    return root


def test_home_lock_excludes_second_instance_and_backup(tmp_path):
    root = home(tmp_path)
    with instance_lock(root):
        with pytest.raises(RuntimeError), instance_lock(root):
            pytest.fail('double owner')
        with pytest.raises(RuntimeError):
            snapshot(root, tmp_path / 'backup')
    with instance_lock(root):
        pass


def test_full_stopped_snapshot_roundtrip(tmp_path):
    root = home(tmp_path)
    backup = tmp_path / 'backup'
    manifest = snapshot(root, backup)
    assert set(manifest['files']) == {'projectwatch.sqlite3', 'settings.json', 'objects/synthetic'}
    restored = tmp_path / 'restored'
    restore_snapshot(backup, restored)
    assert (restored / 'settings.json').read_bytes() == (root / 'settings.json').read_bytes()
    with closing(sqlite3.connect(restored / 'projectwatch.sqlite3')) as db:
        assert db.execute('SELECT value FROM sample').fetchone()[0] == 'synthetic'
    with pytest.raises(ValueError):
        restore_snapshot(backup, restored)
    with pytest.raises(ValueError):
        snapshot(root, root / 'backup')


def test_corruption_and_traversal_rejected(tmp_path):
    root = home(tmp_path)
    backup = tmp_path / 'backup'
    snapshot(root, backup)
    (backup / 'settings.json').write_text('changed')
    with pytest.raises(ValueError):
        verify_snapshot(backup)
    (backup / 'studio-manifest.json').write_text(json.dumps({'format': 'studio-home-v1', 'files': {'../escape': 'x'}}))
    with pytest.raises(ValueError):
        restore_snapshot(backup, tmp_path / 'new')
    assert not (tmp_path / 'new').exists()


def test_supervisor_stops_only_own_child():
    class Child:
        def __init__(self):
            self.actions = []
        def poll(self):
            return None
        def terminate(self):
            self.actions.append('terminate')
        def wait(self, timeout):
            self.actions.append('wait')
    child = Child()
    stop_child(child)
    assert child.actions == ['terminate', 'wait']


def test_supervisor_restart_storm_is_bounded(tmp_path, monkeypatch):
    from project_watch.studio import host

    created = []
    class Exited:
        def poll(self):
            return 1
    def spawn(command, env, log):
        created.append(command)
        assert env['PROJECT_WATCH_HOME'] == str((tmp_path / 'home').resolve())
        return Exited()
    monkeypatch.setattr(host, 'launch_child', spawn)
    monkeypatch.setattr(host.time, 'sleep', lambda _: None)
    with pytest.raises(RuntimeError, match='多次恢复失败'):
        host.supervise(tmp_path / 'home', 0)
    assert len(created) == 4
    assert all('--strict-port' in cmd and '--no-browser' in cmd for cmd in created)


def test_app_lifespan_locks_even_without_background_worker(tmp_path):
    from fastapi.testclient import TestClient

    from project_watch.studio.app import create_studio_app

    app = create_studio_app(tmp_path, start_worker=False)
    with TestClient(app):
        with pytest.raises(RuntimeError), TestClient(create_studio_app(tmp_path, start_worker=False)):
            pytest.fail('second app acquired the same directory')
        with pytest.raises(RuntimeError):
            snapshot(tmp_path, tmp_path.parent / (tmp_path.name + '-backup'))


@pytest.mark.skipif(os.name != 'nt', reason='Windows process tree only')
def test_windows_job_object_kills_venv_descendants(tmp_path):
    import ctypes
    from ctypes import wintypes as wt

    from project_watch.studio.windows_child import WindowsChild

    pid_path = tmp_path / 'descendant.pid'
    code = ('import subprocess,sys,time;from pathlib import Path;'
            'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]);'
            f'Path({str(pid_path)!r}).write_text(str(p.pid));time.sleep(60)')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel.OpenProcess.restype = wt.HANDLE
    kernel.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    kernel.WaitForSingleObject.restype = wt.DWORD
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    descendant = None
    with (tmp_path / 'child.log').open('wb') as log:
        process = WindowsChild([sys.executable, '-c', code], env=dict(os.environ), log=log)
        try:
            deadline = time.monotonic() + 10
            while not pid_path.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            assert pid_path.exists(), (tmp_path / 'child.log').read_text()
            descendant = kernel.OpenProcess(0x100000, False, int(pid_path.read_text()))
            assert descendant
            assert process.poll() is None
            # Closing the ownership handle, including unexpected supervisor exit,
            # kills the entire tree, not only CPython's venv launcher.
            process.close()
            assert kernel.WaitForSingleObject(descendant, 10000) == 0
        finally:
            process.close()
            if descendant:
                kernel.CloseHandle(descendant)

"""Desktop-only cooperative exit; all homes and requests are synthetic loopback."""
import asyncio
import json
import sys
import threading
import time
import urllib.request
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from project_watch.studio import desktop
from project_watch.studio.app import create_studio_app
from project_watch.studio.host import instance_lock


def controls(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    server = SimpleNamespace(should_exit=False, force_exit=False)
    stop = desktop.ExitRequest(server, app.state.request_desktop_drain)
    desktop.attach_controls(app, 'a'*32, 'classic', stop)
    return app, server, stop


def test_exit_checks_identity_then_drains_once_without_changing_pause(tmp_path):
    app, server, stop = controls(tmp_path)
    app.state.studio_store.set_state('monitor_paused', 'true')
    with TestClient(app) as client:
        assert client.get('/api/desktop/identity').json()['exit_available']
        assert client.get('/api/desktop/exit').status_code == 405
        assert client.post('/api/desktop/exit', json={'instance': 'wrong'}).status_code == 409
        assert not server.should_exit
        for _ in range(2):
            response = client.post('/api/desktop/exit', json={'instance': 'a'*32})
            assert response.status_code == 202
            assert response.json()['status'] == 'exit_requested'
        assert stop.requested.is_set() and server.should_exit and not server.force_exit
        assert client.get('/api/studio/health').json()['draining']
        assert client.put('/api/studio/settings', json={}).status_code == 503
        assert app.state.studio_store.get_state('monitor_paused') == 'true'


@pytest.mark.parametrize('headers', [{'Origin': 'https://attacker.invalid'}, {'Sec-Fetch-Site': 'cross-site'},
                                    {'Host': 'attacker.invalid'}])
def test_cross_site_or_wrong_host_cannot_exit(tmp_path, headers):
    app, server, stop = controls(tmp_path)
    with TestClient(app) as client:
        response = client.post('/api/desktop/exit', json={'instance': 'a'*32}, headers=headers)
        assert response.status_code in (400, 403)
        assert not stop.requested.is_set() and not server.should_exit


def test_plain_form_and_non_desktop_mode_have_no_exit(tmp_path):
    app, _, stop = controls(tmp_path/'desktop')
    with TestClient(app) as client:
        assert client.post('/api/desktop/exit', data={'instance': 'a'*32}).status_code == 422
        assert not stop.requested.is_set()
    with TestClient(create_studio_app(tmp_path/'server', start_worker=False)) as client:
        assert client.post('/api/desktop/exit', json={'instance': 'a'*32}).status_code == 404


def test_request_exit_does_not_launch_or_fallback_on_missing_or_old_instance(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, 'run_desktop', lambda *_a, **_k: pytest.fail('must not launch'))
    with pytest.raises(RuntimeError, match='未找到'):
        desktop.request_exit(tmp_path)
    marker = tmp_path/'desktop-session/endpoint.json'
    marker.parent.mkdir()
    marker.write_text(json.dumps({'port': 12345, 'instance': 'a'*32}))
    monkeypatch.setattr(desktop, 'local_identity', lambda _: {'instance': 'a'*32})
    monkeypatch.setattr(desktop, 'local_request', lambda *_a: pytest.fail('unsupported version must not receive POST'))
    with pytest.raises(RuntimeError, match='不支持'):
        desktop.request_exit(tmp_path)


def test_request_exit_rechecks_instance_and_never_follows_marker_url(tmp_path, monkeypatch):
    marker = tmp_path/'desktop-session/endpoint.json'
    marker.parent.mkdir()
    marker.write_text(json.dumps({'port': 12345, 'instance': 'a'*32, 'url': 'https://invalid.test'}))
    identities = iter([{'instance': 'a'*32}, {'instance': 'b'*32, 'exit_available': True}])
    monkeypatch.setattr(desktop, 'local_identity', lambda _: next(identities))
    monkeypatch.setattr(desktop, 'local_request', lambda *_a: pytest.fail('changed instance must not receive POST'))
    with pytest.raises(RuntimeError, match='实例已变化'):
        desktop.request_exit(tmp_path)


@pytest.mark.parametrize('with_tray', [False, True])
def test_actual_desktop_waits_for_inflight_write_then_releases_both_locks(tmp_path, monkeypatch, with_tray):
    import project_watch.studio.app as app_module
    home = tmp_path/'home'
    entered, release = threading.Event(), threading.Event()
    saved = home/'completed.txt'
    original = app_module.create_studio_app

    def create(*args, **kwargs):
        app = original(*args, **kwargs)

        @app.post('/api/studio/synthetic-write')
        async def slow_write():
            entered.set()
            await asyncio.to_thread(release.wait)
            saved.write_text('finished')
            return {'done': True}
        return app

    monkeypatch.setattr(app_module, 'create_studio_app', create)
    monkeypatch.setattr(desktop, 'configure_runtime', lambda *_a: 'classic')
    errors = []
    tray_stopped = threading.Event()
    if with_tray:
        class Icon:
            def __init__(self, *args, **kwargs):
                pass

            def run(self, setup):
                watcher = threading.Thread(target=setup, args=(self,))
                watcher.start()
                assert tray_stopped.wait(20)
                watcher.join(5)

            def stop(self):
                tray_stopped.set()
        monkeypatch.setitem(sys.modules, 'pystray', SimpleNamespace(Icon=Icon,
            Menu=lambda *a: a, MenuItem=lambda *a, **k: a))
        monkeypatch.setattr(desktop, 'tray_image', lambda: None)

    def run():
        try:
            desktop.run_desktop(home, browser=False, tray=with_tray, smoke_seconds=30)
        except Exception as exc:
            errors.append(type(exc).__name__)
    runner = threading.Thread(target=run)
    runner.start()
    writer = None
    try:
        marker = home/'desktop-session/endpoint.json'
        until = time.monotonic()+15
        while not marker.exists() and time.monotonic()<until:
            time.sleep(.05)
        assert marker.exists()
        endpoint = json.loads(marker.read_text())
        url = f"http://127.0.0.1:{endpoint['port']}/"

        def write():
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(urllib.request.Request(url+'api/studio/synthetic-write', data=b'{}',
                    headers={'Content-Type': 'application/json'}), timeout=15) as response:
                    assert response.status == 200
            except Exception as exc:
                errors.append(type(exc).__name__)
        writer = threading.Thread(target=write)
        writer.start()
        assert entered.wait(5)
        assert desktop.request_exit(home)['status'] == 'exit_requested'
        time.sleep(.2)
        assert runner.is_alive() and marker.exists() and not saved.exists()
        assert not tray_stopped.is_set()
        with pytest.raises(RuntimeError), instance_lock(home/'desktop-session'):
            pass
        with pytest.raises(RuntimeError), instance_lock(home):
            pass
    finally:
        release.set()
        if writer:
            writer.join(15)
        runner.join(15)
    assert not runner.is_alive() and not errors
    assert saved.read_text() == 'finished' and not marker.exists()
    if with_tray:
        assert tray_stopped.is_set()
    with instance_lock(home), instance_lock(home/'desktop-session'):
        pass

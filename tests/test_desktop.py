"""Synthetic desktop lifecycle; never opens a browser or an existing home."""
import json
import threading
from pathlib import Path

import pytest

from project_watch.studio import desktop


def test_default_home_does_not_reuse_developer_home(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop.sys, 'platform', 'win32')
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.setenv('PROJECT_WATCH_HOME', str(tmp_path/'existing'))
    assert desktop.desktop_home() == tmp_path/'ProjectWatch-Desktop'
    assert not (tmp_path/'existing').exists()


@pytest.mark.parametrize('port', [0, -1, 65536, True, '8933'])
def test_marker_rejects_invalid_port(tmp_path, port):
    marker = tmp_path/'endpoint.json'
    marker.write_text(json.dumps({'port': port, 'instance': 'a'*32}))
    assert desktop.existing_url(marker) is None


def test_existing_instance_requires_matching_identity(monkeypatch, tmp_path):
    marker = tmp_path/'endpoint.json'
    marker.write_text(json.dumps({'port': 30000, 'instance': 'a'*32}))
    calls = []
    def probe(url):
        calls.append(url)
        return {'instance': 'wrong'}
    monkeypatch.setattr(desktop, 'local_identity', probe)
    assert desktop.existing_url(marker) is None
    monkeypatch.setattr(desktop, 'local_identity', lambda url: {'instance': 'a'*32})
    assert desktop.existing_url(marker) == 'http://127.0.0.1:30000/'
    assert calls == ['http://127.0.0.1:30000/api/desktop/identity']


def test_marker_url_is_not_used(monkeypatch, tmp_path):
    marker = tmp_path/'endpoint.json'
    marker.write_text(json.dumps({'port': 30000, 'instance': 'a'*32, 'url': 'https://invalid.test'}))
    monkeypatch.setattr(desktop, 'local_identity', lambda url: {'instance': 'a'*32})
    assert desktop.existing_url(marker) == 'http://127.0.0.1:30000/'


def test_stop_requests_graceful_exit_once():
    class Server:
        should_exit = False
        force_exit = False
    server = Server()
    controller = desktop.ExitRequest(server)
    assert controller.request() is True
    assert controller.request() is False
    assert server.should_exit and not server.force_exit


def test_ready_wait_observes_server_failure():
    class Server:
        started = False
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    with pytest.raises(RuntimeError, match='启动失败'):
        desktop.wait_ready(Server(), dead, timeout=.1)


def test_stale_marker_is_not_a_success(tmp_path):
    marker = tmp_path/'endpoint.json'
    marker.write_text('not-json')
    assert desktop.existing_url(marker) is None


def test_launcher_has_no_shell_or_global_environment_install():
    source = (Path(__file__).parents[1]/'scripts/windows/Launcher.cs').read_text(encoding='utf-8')
    assert 'UseShellExecute = false' in source
    assert 'CreateNoWindow = true' in source
    assert 'pythonw.exe' in source
    assert 'pip install' not in source and 'winget' not in source


def test_old_version_cannot_start_after_upgrade(tmp_path, monkeypatch):
    runtime = tmp_path/'program/versions/old/runtime'
    runtime.mkdir(parents=True)
    home = tmp_path/'data'
    (tmp_path/'program/install-state.json').write_text(json.dumps({'active': 'new', 'home': str(home)}))
    monkeypatch.setattr(desktop.sys, 'executable', str(runtime/'pythonw.exe'))
    with pytest.raises(RuntimeError, match='版本已切换'):
        desktop.check_installed_version(home.resolve())

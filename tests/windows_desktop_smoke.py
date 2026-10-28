"""Run with bundled python -I -B, explicit NEW scratch home, no external providers.

Starts the real pythonw entry point with a minimal PATH, verifies a second launch
reuses the existing service, and lets the owned smoke process drain itself.
Never terminate/kill a timed-out process: retain receipts and report failure.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from project_watch.studio.app import create_studio_app


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--seconds', type=int, default=15)
    parser.add_argument('--tray', action='store_true', help='Exercise native tray with automatic exit, no browser')
    args = parser.parse_args()
    home = args.home.resolve()
    if home.exists():
        raise ValueError('Use a new synthetic home only')
    runtime = Path(sys.executable).resolve().parent
    app = create_studio_app(home, start_worker=False)
    project = app.state.studio_pipeline.repository.create_project('Synthetic desktop retention')
    app.state.studio_store.upsert_project(project.id, {'legal_name': project.name}, consent=False)
    app.state.studio_store.set_status(project.id, 'paused')
    app.state.studio_pipeline.settings_store.update({'refresh_hours': 72})
    original = home/'synthetic-original.txt'
    original.write_text('Synthetic local original, no personal data.', encoding='utf-8')
    original_hash, config_hash = sha(original), sha(home/'settings.json')
    env = {k: v for k, v in os.environ.items() if not k.startswith(('PROJECT_WATCH_', 'PYTHON', 'PIP_'))}
    env['PATH'] = str(Path(os.environ['SYSTEMROOT'])/'System32')
    # Deliberately invalid global paths must not be used by isolated embedded Python.
    env['PYTHONHOME'] = str(home/'nonexistent-global-python')
    env['PYTHONPATH'] = str(home/'nonexistent-modules')
    command = [str(runtime/'pythonw.exe'), '-I', '-B', '-X', 'utf8', '-m', 'project_watch.studio.desktop',
               '--home', str(home), '--headless', '--smoke-seconds', str(args.seconds)]
    first_command = command
    if args.tray:
        code = ("import sys,os; from pathlib import Path; "
                "sys.stdout=open(os.devnull,'w'); sys.stderr=open(os.devnull,'w'); "
                "from project_watch.studio.desktop import run_desktop; "
                f"run_desktop(Path({str(home)!r}),browser=False,tray=True,smoke_seconds={args.seconds})")
        first_command = [str(runtime/'pythonw.exe'), '-I', '-B', '-X', 'utf8', '-c', code]
    first = subprocess.Popen(first_command, env=env, cwd=home,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    marker = home/'desktop-session/endpoint.json'
    deadline = time.monotonic()+45
    while not marker.exists():
        if first.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError(f'Windowless startup failed: code={first.poll()}; process not killed')
        time.sleep(.1)
    endpoint = json.loads(marker.read_text())
    base = f"http://127.0.0.1:{endpoint['port']}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def read(path):
        with opener.open(base+path, timeout=5) as response:
            return response.read()
    assert json.loads(read('/api/desktop/identity'))['instance'] == endpoint['instance']
    assert b'assets/app.js' in read('/setup')
    assert json.loads(read('/api/studio/projects'))[0]['status'] == 'paused'
    second = subprocess.run(command, env=env, cwd=home, timeout=20,
                            creationflags=subprocess.CREATE_NO_WINDOW, check=True)
    assert second.returncode == 0 and first.poll() is None
    assert json.loads(read('/api/desktop/identity'))['instance'] == endpoint['instance']
    assert first.wait(timeout=args.seconds+30) == 0
    assert not marker.exists()
    assert sha(original) == original_hash and sha(home/'settings.json') == config_hash
    reopened = create_studio_app(home, start_worker=False)
    assert reopened.state.studio_store.get_project(project.id)['status'] == 'paused'
    assert reopened.state.studio_pipeline.settings_store.load().refresh_hours == 72
    assert not reopened.state.studio_store.jobs(project.id)
    # PE subsystem=2 means Windows GUI, not a console launcher.
    exe = (runtime.parent/'ProjectWatch.exe').read_bytes()
    pe = struct.unpack_from('<I', exe, 0x3c)[0]
    assert struct.unpack_from('<H', exe, pe+24+68)[0] == 2
    print(json.dumps({'windowless_start': 'passed', 'duplicate_reopen': 'passed',
        'graceful_exit': 'passed', 'original_config_pause_retained': True,
        'minimal_path': True, 'live_calls': 0, 'workers_started': False,
        'gui_launcher_subsystem': True, 'native_tray': args.tray}))


if __name__ == '__main__':
    main()

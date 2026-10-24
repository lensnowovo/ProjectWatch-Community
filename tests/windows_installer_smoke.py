"""Actual NSIS lifecycle in compile-time sealed paths; synthetic data, no providers.

Run as a developer with --first-installer from build_windows_installer --test-root.
No process termination on timeout. No production registry/shortcut destinations.
"""
import argparse
import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from scripts.build_windows_installer import build  # noqa: E402
from scripts.windows.maintain import digest  # noqa: E402


class Checks(list):
    def append(self, item):
        super().append(item)
        print('PASS '+item, flush=True)


def run(command, *, env=None, timeout=240):
    command = [str(s) for s in command]
    if command[-1].startswith(('_?=', '/D=')):
        # NSIS requires this final switch UNQUOTED, even for paths with spaces.
        # Passing a Python argv list would quote it, then only wait for the stub.
        if any(c in command[-1] for c in '\"\r\n'):
            raise ValueError('Unsafe NSIS test destination')
        command = subprocess.list2cmdline(command[:-1])+' '+command[-1]
    child = subprocess.Popen(command, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    # wait() deliberately never kills an installer/app on timeout.
    return child.wait(timeout=timeout)


def wait_ready(home, child):
    until = time.monotonic()+60
    while time.monotonic() < until:
        marker = home/'desktop-session/endpoint.json'
        if marker.exists():
            return
        if child.poll() is not None:
            raise RuntimeError('Desktop exited before readiness')
        time.sleep(.2)
    raise RuntimeError('Readiness timeout; not terminating app')


def fingerprint(home):
    result = {}
    for path in home.rglob('*'):
        if not path.is_file() or path.name == '.studio.lock' or path.name.endswith(('-wal', '-shm')):
            continue
        name = path.relative_to(home).as_posix()
        if path.suffix == '.sqlite3':
            with contextlib.closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as db:
                # SQLite checkpoints may change physical bytes without changing a
                # record. Compare ALL logical content; never ignore the database.
                result[name] = hashlib.sha256('\n'.join(db.iterdump()).encode()).hexdigest()
        else:
            result[name] = digest(path)
    return result


def main(args):
    target = args.test_root.resolve()
    if target.exists():
        raise ValueError('Require new test destination')
    # Build a second complete synthetic edition, before any test destination exists.
    second_bundle = args.out/'second-bundle'
    second_out = args.out/'second-installer'
    if args.second_installer:
        args.out.mkdir()
    elif not args.reuse_build:
        args.out.mkdir()
        shutil.copytree(args.bundle, second_bundle)
        value = json.loads((second_bundle/'bundle-manifest.json').read_text())
        value['version'] = '0.2.0a1+synthetic.upgrade'
        (second_bundle/'synthetic-upgrade.txt').write_text('SYNTHETIC INSTALLER TEST ONLY')
        value['files']['synthetic-upgrade.txt'] = digest(second_bundle/'synthetic-upgrade.txt')
        (second_bundle/'bundle-manifest.json').write_text(json.dumps(value))
        build(SimpleNamespace(bundle=second_bundle, nsis=args.nsis, out=second_out, test_root=target))
    first, second = args.first_installer, args.second_installer or second_out/'ProjectWatch-Setup.exe'
    for exe in (first, second):
        receipt = json.loads((exe.parent/'installer-build.json').read_text())
        assert receipt['isolated_test_build'] and digest(exe) == receipt['sha256']
    program_dir = Path(args.program_dir)
    if program_dir.is_absolute() or '..' in program_dir.parts:
        raise ValueError('Program directory must stay within test root')
    home, root = target/'data', target/program_dir
    checks = Checks()
    escape = target/'..'/(target.name+'-must-not-exist')
    assert not escape.exists()
    assert run([first, '/S', '/D='+str(escape)]) != 0
    assert not escape.exists()
    checks.append('test_installer_rejects_outside_destination')
    assert run([first, '/S', '/D='+str(root)]) == 0
    checks.append('silent_first_install_custom_directory')
    assert (target/'desktop/ProjectWatch.lnk').is_file()
    assert (target/'start-menu/ProjectWatch.lnk').is_file()
    checks.append('desktop_and_start_menu_shortcuts')
    wrong_root = target/'different-program'
    assert run([second, '/S', '/D='+str(wrong_root)]) != 0
    assert not wrong_root.exists()
    checks.append('registered_install_blocks_accidental_second_location')
    one = json.loads((root/'install-state.json').read_text())['active']
    env = {k: v for k, v in os.environ.items() if not k.startswith(('PROJECT_WATCH_', 'PYTHON'))}
    env['PATH'] = str(Path(os.environ['SYSTEMROOT'])/'System32')
    child = subprocess.Popen([str(root/'ProjectWatch.exe'), '--smoke-seconds', '180'], env=env,
        creationflags=subprocess.CREATE_NO_WINDOW)
    wait_ready(home, child)
    before = (root/'install-state.json').read_bytes()
    assert run([second, '/S']) != 0
    assert (root/'install-state.json').read_bytes() == before
    checks.append('active_instance_blocks_upgrade')
    # NSIS normal uninstaller spawns a copy; _?= requests synchronous in-place mode.
    assert run([root/'Uninstall.exe', '/S', '_?='+str(root)]) != 0
    assert (root/'install-state.json').read_bytes() == before
    checks.append('active_instance_blocks_uninstall')
    assert child.wait(timeout=220) == 0
    with contextlib.closing(sqlite3.connect(home/'projectwatch.sqlite3')) as db:
        db.execute('CREATE TABLE synthetic_install_test (paused integer)')
        db.execute('INSERT INTO synthetic_install_test VALUES (1)')
        db.commit()
    (home/'synthetic-config.json').write_text('{"key":"FAKE-NO-LIVE-KEY"}')
    (home/'originals').mkdir(exist_ok=True)
    (home/'originals/原件 BP.txt').write_text('SYNTHETIC ORIGINAL')
    originals = fingerprint(home)
    assert run([second, '/S']) == 0
    state = json.loads((root/'install-state.json').read_text())
    assert one != state['active'] and (root/'versions'/one).is_dir()
    assert fingerprint(home) == originals
    checks.append('upgrade_retains_old_version_pause_data_config')
    # Verify new runtime and installed-package restore using its own full libraries.
    new_python = root/'versions'/state['active']/'runtime/python.exe'
    restored = args.out/'restored-home'
    code = ('from project_watch.studio.host import restore_snapshot; '
            'import sys; restore_snapshot(sys.argv[1], sys.argv[2])')
    assert run([new_python, '-I', '-B', '-c', code, state['backup'], restored], env=env) == 0
    assert (restored/'originals/原件 BP.txt').read_text() == 'SYNTHETIC ORIGINAL'
    with contextlib.closing(sqlite3.connect(restored/'projectwatch.sqlite3')) as db:
        assert db.execute('SELECT paused FROM synthetic_install_test').fetchone() == (1,)
    checks.append('upgrade_backup_restores_to_new_directory')
    assert run([new_python, '-I', '-B', '-m', 'project_watch.studio.desktop', '--home', home,
                '--headless', '--smoke-seconds', '2'], env=env) == 0
    checks.append('upgraded_desktop_reopens_without_system_python')
    before_uninstall = fingerprint(home)
    assert run([root/'Uninstall.exe', '/S', '/REMOVEFILES', '_?='+str(root)]) == 0
    assert fingerprint(home) == before_uninstall
    assert not (target/'desktop/ProjectWatch.lnk').exists()
    assert not (target/'start-menu/ProjectWatch.lnk').exists()
    checks.append('uninstall_preserves_home_and_backups_removes_shortcuts')
    # In-place test uninstall cannot delete its own exe on Windows. Normal uninstall
    # self-copies. Remove only this exact test executable after it has exited.
    if (root/'Uninstall.exe').exists():
        (root/'Uninstall.exe').unlink()
    if root.exists():
        root.rmdir()
    assert run([second, '/S', '/D='+str(root)]) == 0
    assert fingerprint(home) == before_uninstall
    checks.append('reinstall_reuses_preserved_home')
    import winreg
    token = hashlib.sha256(str(target).encode()).hexdigest()[:16]
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Software\\ProjectWatchInstallerTests\\'+token) as key:
        assert winreg.QueryValueEx(key, 'InstallLocation')[0] == str(root)
    checks.append('isolated_registry_registration')
    receipt = {'checks': checks, 'live_calls': 0, 'isolated': True, 'publication_authorized': False}
    (args.out/'result.json').write_text(json.dumps(receipt, indent=2))
    print(json.dumps(receipt))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('bundle', 'nsis', 'out', 'first-installer', 'test-root'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--reuse-build', action='store_true', help='Reuse explicitly supplied isolated test installers')
    parser.add_argument('--program-dir', default='program', help='Relative custom directory within the isolated root')
    parser.add_argument('--second-installer', type=Path, help='Prebuilt isolated upgrade installer for the same test root')
    main(parser.parse_args())

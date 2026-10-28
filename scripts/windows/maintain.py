"""Offline installer transaction, run by a minimal bundled Python from a temporary directory.

No providers, network, migrations or application workers. Runtime and home locks
remain held through backup and pointer publication. Files not in a manifest survive.

Every new file is hashed exactly once, then moved (not copied) into place when the
payload sits on the same volume: on Windows each first read of a newly written file
is scanned by antivirus, so re-reading copies doubled install time. Large shared
runtimes (DSH, Node) live in ``runtimes/<content id>`` and are reused untouched by
later versions that carry the same content.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path


def digest(path):
    with safe(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def safe(path):
    path = Path(os.path.abspath(path))
    # CPython's stat/is_file can silently report missing for >260-character
    # paths on Windows. A staging directory may fit while its hash-named final
    # directory does not. Use extended-length paths for ALL maintenance I/O.
    if os.name == 'nt' and not str(path).startswith('\\\\?\\'):
        if str(path).startswith('\\\\'):
            raise ValueError('Use a local disk, not a network share')
        path = Path('\\\\?\\'+str(path))
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError('Linked path')
    return path


def display(path):
    value = str(path)
    return value[4:] if value.startswith('\\\\?\\') else value


def child(root, name):
    if (not isinstance(name, str) or not name or '\\' in name or ':' in name
            or any(p in ('', '.', '..') for p in name.split('/'))
            or name.startswith('/') or any(p.endswith(('.', ' ')) for p in name.split('/'))):
        raise ValueError('Unsafe manifest path')
    return safe(root/name)


@contextlib.contextmanager
def lock(path):
    path = safe(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def atomic(path, value):
    path = safe(path)
    temp = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    with temp.open('x', encoding='utf-8') as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def check_files(root, files):
    seen = set()
    for name, expected in files.items():
        child(root, name)
        if name.casefold() in seen or not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
            raise ValueError('Invalid file hash')
        seen.add(name.casefold())


def runtime_digest(files):
    """Content identity of a shared runtime: its exact file inventory."""
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def manifest(root):
    value = json.loads(safe(root/'bundle-manifest.json').read_text(encoding='utf-8'))
    if value.get('format') != 'projectwatch-windows-bundle-v1' or not isinstance(value.get('files'), dict):
        raise ValueError('Invalid bundle')
    check_files(root, value['files'])
    runtimes = value.get('runtimes', {})
    if not isinstance(runtimes, dict):
        raise ValueError('Invalid runtimes')
    for rid, entry in runtimes.items():
        if (not isinstance(rid, str) or not re.fullmatch(r'(dsh|node)-[A-Za-z0-9.-]{1,32}-[0-9a-f]{16}', rid)
                or not isinstance(entry, dict) or not isinstance(entry.get('files'), dict)
                or not rid.endswith(runtime_digest(entry['files'])[:16])):
            raise ValueError('Invalid runtime inventory')
        check_files(root/'runtimes'/rid, entry['files'])
    return value


def verify(root, value):
    for name, expected in value['files'].items():
        if digest(child(root, name)) != expected:
            raise ValueError('Bundle hash mismatch')
    for rid, entry in value.get('runtimes', {}).items():
        for name, expected in entry['files'].items():
            if digest(child(root/'runtimes'/rid, name)) != expected:
                raise ValueError('Bundle hash mismatch')


def place(source, destination, files, *, move):
    """Hash each source file once, then move it into place (copy only across volumes).

    The hash is taken from the very bytes that are moved; a same-volume move does
    not rewrite them, so the destination is not hashed again.
    """
    for name, expected in files.items():
        src, dst = child(source, name), child(destination, name)
        if digest(src) != expected:
            raise ValueError('Bundle hash mismatch')
        dst.parent.mkdir(parents=True, exist_ok=True)
        if move:
            try:
                os.replace(src, dst)
                continue
            except OSError:
                pass  # Different volume: fall back to a copy of the verified file.
        shutil.copy2(src, dst)


def snapshot(home, destination):
    """Stopped-home snapshot; SQLite backup includes committed WAL transactions."""
    destination = safe(destination)
    destination.mkdir(parents=True, exist_ok=False)
    files = {}
    for source in sorted(home.rglob('*')):
        safe(source)
        if not source.is_file() or source.name in ('.studio.lock', '.supervisor.lock', 'endpoint.json', 'studio-manifest.json'):
            continue
        if source.name.endswith(('-wal', '-shm')):
            continue
        name = source.relative_to(home).as_posix()
        target = child(destination, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix == '.sqlite3':
            with (contextlib.closing(sqlite3.connect(Path(display(source)).as_uri()+'?mode=ro', uri=True)) as db,
                  contextlib.closing(sqlite3.connect(target)) as copy):
                db.backup(copy)
                if copy.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('Backup integrity check failed')
        else:
            shutil.copy2(source, target)
            if digest(source) != digest(target):
                raise ValueError('Backup copy mismatch')
        files[name] = digest(target)
    atomic(destination/'desktop-backup.json', json.dumps({'format': 'desktop-stopped-home-v1', 'files': files}))
    if 'projectwatch.sqlite3' in files:
        # Compatible with the existing verified restore-to-new-directory command.
        atomic(destination/'studio-manifest.json', json.dumps({'format': 'studio-home-v1',
               'created_at': datetime.now(UTC).isoformat(), 'files': files}))
    return display(destination)


def validate_roots(root, home):
    root, home = safe(root), safe(home)
    if len(root.parts) < 3 or len(home.parts) < 3 or root.is_relative_to(home) or home.is_relative_to(root):
        raise ValueError('Program and data directories must be separate')
    if root.exists() and not (root/'install-state.json').exists():
        owner = safe(root/'install-owner.json')
        if owner.exists():
            value = json.loads(owner.read_text(encoding='utf-8'))
            if value != {'format': 'projectwatch-desktop-owner-v1', 'root': display(root), 'home': display(home)}:
                raise ValueError('Invalid installation owner')
        elif any(p.name not in ('.studio.lock', '.payload') for p in root.iterdir()):
            raise ValueError('Refusing unowned installation directory')
    return root, home


def state_at(root):
    path = root/'install-state.json'
    if not path.exists():
        return None
    value = json.loads(safe(path).read_text(encoding='utf-8'))
    if value.get('format') != 'projectwatch-desktop-install-v1':
        raise ValueError('Not an owned installation')
    if not isinstance(value.get('versions'), list) or not value['versions']:
        raise ValueError('Invalid version inventory')
    for version in value['versions']:
        if not re.fullmatch('[0-9a-f]{64}', version):
            raise ValueError('Invalid version identity')
    if value.get('active') not in value['versions']:
        raise ValueError('Invalid active version')
    return value


def prepare(root, home):
    """Validate paths before NSIS extracts anything; never delete old staging."""
    root, home = validate_roots(root, home)
    with lock(root/'.studio.lock'), lock(home/'desktop-session/.studio.lock'), lock(home/'.studio.lock'):
        previous = state_at(root)
        if previous and previous['home'] != display(home):
            raise ValueError('Data home mismatch')
        staging = safe(root/'.payload')
        if staging.exists():
            raise ValueError('Existing staging directory; preserve it for inspection')
        atomic(root/'install-owner.json', json.dumps({'format': 'projectwatch-desktop-owner-v1',
                                                      'root': display(root), 'home': display(home)}))
        staging.mkdir()
    return {'status': 'prepared'}


def install(payload, root, home, *, move=False):
    site = Path(os.path.abspath(payload))/'runtime/Lib/site-packages'
    if site.is_dir() and str(site) not in sys.path:
        sys.path.append(str(site))  # The installer runs a minimal Python without site-packages.
    root, home, payload = *validate_roots(root, home), safe(payload)
    value = manifest(payload)
    version = digest(payload/'bundle-manifest.json')
    with lock(root/'.studio.lock'), lock(home/'desktop-session/.studio.lock'), lock(home/'.studio.lock'):
        return install_locked(payload, root, home, value, version, move=move)


def install_locked(payload, root, home, value, version, *, move=False):
    """Internal transaction: caller MUST hold all three installation/data locks."""
    from packaging.version import Version

    previous = state_at(root)
    if previous and previous['home'] != display(home):
        raise ValueError('Data home mismatch')
    if previous:
        previous_manifest = manifest(child(root, 'versions/'+previous['active']))
        previous_version = previous.get('version') or previous_manifest['version']
        if Version(value['version']) < Version(previous_version):
            raise ValueError('Downgrade requires a verified matching data restore')
    # Keep the prior implicit executor on upgrade without rewriting the data
    # home. Explicit desktop-runtime.json choices take precedence at launch.
    executor_default = 'dsh' if value.get('dsh') else 'classic'
    if previous:
        executor_default = previous.get('public_executor_default') or (
            'dsh' if previous_manifest.get('dsh') else 'classic')
        if executor_default not in ('dsh', 'classic'):
            raise ValueError('Invalid previous executor default')
    atomic(root/'install-owner.json', json.dumps({'format': 'projectwatch-desktop-owner-v1',
                                                  'root': display(root), 'home': display(home)}))
    # The bootstrap is stable for this installer generation. Its binary may
    # differ between compiler invocations, but source changes need an explicit
    # installer migration, not a two-file non-atomic activation.
    launcher = safe(root/'ProjectWatch.exe')
    if launcher.exists() and digest(launcher) != (previous or {}).get('launcher_sha256', digest(payload/'ProjectWatch.exe')):
        raise ValueError('Customized launcher; Agent assistance required')
    if previous and previous.get('launcher_source_sha256') != value.get('launcher_source_sha256'):
        raise ValueError('Bootstrap changed; explicit installer migration required')
    backup = snapshot(home, home.parent/'ProjectWatch-Desktop-backups'/uuid.uuid4().hex)
    # Shared runtimes first: a version is only published once everything it uses exists.
    for rid, entry in value.get('runtimes', {}).items():
        install_runtime(payload/'runtimes'/rid, root, rid, entry['files'], move=move)
    target = child(root, 'versions/'+version)
    if target.exists():
        verify_app(target, value)
    else:
        stage = child(root, 'versions/staging-'+uuid.uuid4().hex)
        stage.mkdir(parents=True)
        place(payload, stage, value['files'], move=move)
        shutil.copy2(payload/'bundle-manifest.json', stage/'bundle-manifest.json')
        stage.rename(target)
    if not launcher.exists():
        launcher_stage = child(root, 'launcher-'+uuid.uuid4().hex+'.tmp')
        shutil.copy2(target/'ProjectWatch.exe', launcher_stage)  # The payload copy may have been moved.
        if digest(launcher_stage) != value['files']['ProjectWatch.exe']:
            raise ValueError('Launcher copy mismatch')
        os.replace(launcher_stage, launcher)
    versions = list(previous['versions']) if previous else []
    if version not in versions:
        versions.append(version)
    state = {'format': 'projectwatch-desktop-install-v1', 'active': version, 'versions': versions,
             'version': value['version'],
             'public_executor_default': executor_default,
             'home': display(home), 'backup': backup, 'launcher_sha256': digest(launcher),
             'launcher_source_sha256': value.get('launcher_source_sha256')}
    # One atomic file is the only launch pointer. Failed staging keeps old state.
    atomic(root/'install-state.json', json.dumps(state))
    return {'status': 'installed', 'active': version, 'backup_created': True}


def verify_app(root, value):
    verify(root, {'files': value['files']})


def install_runtime(source, root, rid, files, *, move):
    """Reuse a complete shared runtime untouched; otherwise stage it, mark it, publish it."""
    target = child(root, 'runtimes/'+rid)
    marker = runtime_digest(files)
    done = target/'.complete'
    if done.is_file() and done.read_text(encoding='utf-8') == marker:
        return 'reused'
    if target.exists():
        # An earlier interrupted publish: keep it aside rather than delete unknown content.
        target.rename(child(root, 'runtimes/abandoned-'+uuid.uuid4().hex))
    stage = child(root, 'runtimes/staging-'+uuid.uuid4().hex)
    stage.mkdir(parents=True)
    place(source, stage, files, move=move)
    atomic(stage/'.complete', marker)
    stage.rename(target)
    return 'installed'


def remove_listed(folder, files):
    """Delete exactly the listed files without reading them; keep anything else."""
    for name in files:
        path = child(folder, name)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
    for directory in sorted((p for p in folder.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        safe(directory)
        with contextlib.suppress(OSError):
            directory.rmdir()


def uninstall(root, home, *, keep_program=False):
    root, home = validate_roots(root, home)
    with lock(root/'.studio.lock'), lock(home/'desktop-session/.studio.lock'), lock(home/'.studio.lock'):
        state = state_at(root)
        if not state or state['home'] != display(home):
            raise ValueError('Installation ownership mismatch')
        if keep_program:
            # Remove registration/shortcuts in NSIS only. Keep the entire program
            # tree, including its launch pointer, for manual recovery/reinstall.
            return {'status': 'uninstalled', 'data_preserved': True, 'program_preserved': True}
        # Validate all inventory paths before deleting any file. Listed files are removed
        # by name without reading their content; files that were never listed survive.
        inventories = [(child(root, 'versions/'+v), manifest(child(root, 'versions/'+v))) for v in state['versions']]
        retained = 0
        runtimes = {}
        for folder, value in inventories:
            runtimes.update({rid: entry['files'] for rid, entry in value.get('runtimes', {}).items()})
            remove_listed(folder, value['files'])
            remaining = [p for p in folder.rglob('*') if p.is_file() and p.name != 'bundle-manifest.json']
            retained += len(remaining)
            if not remaining:
                (folder/'bundle-manifest.json').unlink()
                folder.rmdir()
        for rid, files in runtimes.items():
            folder = child(root, 'runtimes/'+rid)
            if not folder.exists():
                continue
            remove_listed(folder, files)
            with contextlib.suppress(FileNotFoundError):
                (folder/'.complete').unlink()
            remaining = [p for p in folder.rglob('*') if p.is_file()]
            retained += len(remaining)
            if not remaining:
                with contextlib.suppress(OSError):
                    folder.rmdir()
        with contextlib.suppress(OSError):
            (root/'runtimes').rmdir()
        launcher = safe(root/'ProjectWatch.exe')
        if launcher.is_file() and digest(launcher) == state['launcher_sha256']:
            launcher.unlink()
        elif launcher.exists():
            retained += 1
        # Preserve an audit receipt, but remove the launch pointer even if custom files survive.
        atomic(root/'uninstalled.json', json.dumps({'status': 'uninstalled', 'data_preserved': True,
                                                   'retained_files': retained}))
        (root/'install-state.json').unlink()
        return {'status': 'uninstalled', 'data_preserved': True, 'retained_files': retained}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['prepare', 'install', 'uninstall'])
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--payload', type=Path)
    parser.add_argument('--keep-program', action='store_true')
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    try:
        # The installer extracts the payload inside the program directory, so files can be moved.
        if args.operation == 'prepare':
            result = prepare(args.root, args.home)
        elif args.operation == 'install':
            result = install(args.payload, args.root, args.home, move=True)
        else:
            result = uninstall(args.root, args.home, keep_program=args.keep_program)
        atomic(args.receipt, json.dumps(result))
        return 0
    except Exception as exc:
        atomic(args.receipt, json.dumps({'status': 'failed', 'error_type': type(exc).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

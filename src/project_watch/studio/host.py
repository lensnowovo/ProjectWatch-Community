"""Opt-in local process supervision and stopped Studio-home snapshots.

Never manages an existing process. Run as the normal owner, not administrator.
Backups contain private settings and originals: keep outside Git and cloud shares.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path


@contextmanager
def instance_lock(home):
    home = Path(home).resolve()
    home.mkdir(parents=True, exist_ok=True)
    with (home / '.studio.lock').open('a+b') as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('此数据目录已有实例运行，请勿重复启动或同时备份') from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_snapshot(root):
    root = Path(root).resolve()
    manifest = json.loads((root / 'studio-manifest.json').read_text(encoding='utf-8'))
    if (not isinstance(manifest, dict) or manifest.get('format') != 'studio-home-v1'
            or not isinstance(manifest.get('files'), dict) or 'projectwatch.sqlite3' not in manifest['files']):
        raise ValueError('无效备份清单')
    for name, expected in manifest['files'].items():
        relative = Path(name)
        if (relative.is_absolute() or '..' in relative.parts or ':' in name or '\\' in name
                or relative.name in ('.studio.lock', '.supervisor.lock', 'studio-manifest.json')
                or relative.name.endswith(('-wal', '-shm'))):
            raise ValueError('无效备份路径')
        path = root / relative
        if not path.resolve().is_relative_to(root) or any(
                p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()) for p in [path, *path.parents]):
            raise ValueError('备份包含链接')
        if not path.is_file() or digest(path) != expected:
            raise ValueError('备份校验失败')
        if path.suffix == '.sqlite3':
            with closing(sqlite3.connect(f'{path.as_uri()}?mode=ro&immutable=1', uri=True)) as db:
                if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('数据库校验失败')
    return manifest


def snapshot(home, destination):
    home, destination = Path(home).resolve(), Path(destination).resolve()
    if not (home / 'projectwatch.sqlite3').is_file():
        raise ValueError('未找到 Studio 数据库')
    if destination.exists() or destination.is_relative_to(home):
        raise ValueError('备份须使用数据目录外的新目录')
    with instance_lock(home):
        return snapshot_locked(home, destination)


def snapshot_locked(home, destination):
    """Internal maintenance primitive; caller MUST hold instance_lock(home)."""
    home, destination = Path(home).resolve(), Path(destination).resolve()
    if not (home / 'projectwatch.sqlite3').is_file():
        raise ValueError('未找到 Studio 数据库')
    if destination.exists() or destination.is_relative_to(home):
        raise ValueError('备份须使用数据目录外的新目录')
    destination.mkdir(parents=True)
    files = {}
    for source in sorted(home.rglob('*')):
        if source.is_symlink() or (hasattr(source, 'is_junction') and source.is_junction()):
            raise ValueError('数据目录包含链接，未完成备份')
        if not source.is_file() or source.name in ('.studio.lock', '.supervisor.lock') or source.name.endswith(('-wal', '-shm')):
            continue
        relative = source.relative_to(home)
        if relative.as_posix() == 'studio-manifest.json':
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix == '.sqlite3':
            with closing(sqlite3.connect(f'{source.as_uri()}?mode=ro', uri=True)) as db, closing(sqlite3.connect(target)) as copy:
                db.backup(copy)
        else:
            shutil.copy2(source, target)
        files[relative.as_posix()] = digest(target)
    manifest = {'format': 'studio-home-v1', 'created_at': datetime.now(UTC).isoformat(), 'files': files}
    (destination / 'studio-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    verify_snapshot(destination)
    return manifest


def restore_snapshot(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    manifest = verify_snapshot(source)
    if destination.exists() or destination.is_relative_to(source):
        raise ValueError('只能恢复到全新的目录')
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Publish only after every copy has been checked. A failed attempt remains
    # visibly separate, never a partially restored home at the requested path.
    staging = Path(tempfile.mkdtemp(prefix=f'.{destination.name}-restore-', dir=destination.parent))
    for name, expected in manifest['files'].items():
        target = staging / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, target)
        if digest(target) != expected:
            raise ValueError('恢复校验失败，保留原备份')
    (staging / 'studio-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    verify_snapshot(staging)
    if destination.exists():
        raise ValueError('目标目录已存在，未覆盖；已校验副本保留在临时恢复目录')
    staging.rename(destination)


def health(port):
    # Ignore system proxy for a strictly loopback health check.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f'http://127.0.0.1:{port}/api/studio/health', timeout=5) as response:
        return json.loads(response.read(16384))


def stop_child(child):
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=15)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=15)


def launch_child(command, env, log):
    if os.name == 'nt':
        from project_watch.studio.windows_child import WindowsChild

        return WindowsChild(command, env=env, log=log)
    return subprocess.Popen(command, env=env, stdout=log, stderr=log)


def supervise(home, port, *, max_restarts=3):
    """Three restarts/hour maximum, no provider retries or scheduled paid calls here.

    The child uses normal project pause/consent and request-journal rules. Exiting
    this supervisor stops only its child. Run at user logon via Task Scheduler.
    """
    home = Path(home).resolve()
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', port))  # Fail rather than silently switch ports.
    # Separate process lock, while the child owns .studio.lock.
    with instance_lock(home / 'host'):
        env = dict(os.environ, PROJECT_WATCH_HOME=str(home))
        restarts = []
        while True:
            from project_watch.studio.maintenance import rotating_child_log

            log_path = home / 'host' / 'runtime.log'
            with rotating_child_log(log_path) as (log, log_state):
                child = launch_child([sys.executable, '-m', 'project_watch.studio', '--port', str(port),
                                      '--strict-port', '--no-browser'], env, log)
                failures, born, draining_at = 0, time.monotonic(), None
                try:
                    while child.poll() is None:
                        time.sleep(10)
                        state = {}
                        if log_state['write_failed']:
                            raise RuntimeError('托管日志写入失败，已停止托管；请检查磁盘空间与权限')
                        try:
                            state = health(port)
                            failures = failures + 1 if state.get('restart_required') or state.get('state') == 'stopped' else 0
                        except Exception:
                            failures += int(time.monotonic() - born > 120)
                        if state.get('draining'):
                            # Explicit owner stop must not become a restart loop.
                            draining_at = draining_at or time.monotonic()
                            if state.get('drained') or time.monotonic() - draining_at >= 120:
                                return
                            failures = 0
                        if failures >= 3:
                            break
                finally:
                    try:
                        stop_child(child)
                    finally:
                        if hasattr(child, 'close'):
                            child.close()
            now = time.monotonic()
            restarts = [when for when in restarts if now - when < 3600]
            if len(restarts) >= max_restarts:
                raise RuntimeError('一小时内多次恢复失败，已停止托管；请检查 host 日志')
            restarts.append(now)
            time.sleep(min(60, 5 * 2 ** (len(restarts) - 1)))


def main():
    parser = argparse.ArgumentParser(description='ProjectWatch 托管及停机备份（含密钥，请妥善保管）')
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run')
    run.add_argument('--home', required=True, type=Path)
    run.add_argument('--port', required=True, type=int)
    backup = commands.add_parser('backup')
    backup.add_argument('--home', required=True, type=Path)
    backup.add_argument('--dest', required=True, type=Path)
    restore = commands.add_parser('restore')
    restore.add_argument('--backup', required=True, type=Path)
    restore.add_argument('--dest', required=True, type=Path)
    verify = commands.add_parser('verify')
    verify.add_argument('--backup', required=True, type=Path)
    args = parser.parse_args()
    if args.command == 'run':
        supervise(args.home, args.port)
    elif args.command == 'backup':
        snapshot(args.home, args.dest)
    elif args.command == 'restore':
        restore_snapshot(args.backup, args.dest)
    else:
        verify_snapshot(args.backup)
    print('完成')


if __name__ == '__main__':
    main()

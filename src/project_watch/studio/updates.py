"""Opt-in source updates. Never update from the web process or overwrite customization.

Release source is an owner-configured HTTPS GitHub repository, not a guessed fork.
Checks fetch Git objects only. Applying requires stopped home + explicit pinned hashes.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
import urllib.request
import uuid
from contextlib import closing, contextmanager, suppress
from pathlib import Path

from project_watch import __version__
from project_watch.studio.host import instance_lock, restore_snapshot, snapshot_locked

TAG = re.compile(r'v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$')
SHA = re.compile(r'[0-9a-f]{40,64}$')
_check_lock = threading.Lock()
_cached: tuple[float, dict] | None = None


class UpdateError(RuntimeError):
    pass


def validate_source(source: str) -> str:
    if not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?', source):
        raise UpdateError('更新源须为不含凭据、参数的 HTTPS GitHub 仓库地址')
    return source


def _git(repo, *args, allowed=(0,), timeout=90, binary=False, input_data=None):
    # Ignore inherited GIT_DIR/INDEX/WORK_TREE from a caller or test harness.
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='Never', GIT_OPTIONAL_LOCKS='0')
    try:
        result = subprocess.run(['git', '--no-replace-objects', '-c', f'core.hooksPath={os.devnull}',
                                 *args], cwd=repo, env=env, capture_output=True, timeout=timeout, input=input_data)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError('Git 不可用、连接超时或操作未完成；请检查后重新检查更新') from exc
    if result.returncode not in allowed:
        # Git output may include private paths or authenticated URLs. Never echo it.
        raise UpdateError('Git 检查或更新失败；源码未强制覆盖，请让 Agent 检查本地状态')
    return (result.stdout if binary else result.stdout.decode('utf-8', errors='replace').strip()), result.returncode


def _linked(path):
    return any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction())
               for p in (path, *path.parents))


def _root(repo):
    repo = Path(repo).absolute()
    if _linked(repo):
        raise UpdateError('链接目录不支持自动升级，请让 Agent 检查')
    top, _ = _git(repo, 'rev-parse', '--show-toplevel')
    if Path(top).resolve() != repo.resolve():
        raise UpdateError('必须从源码仓库根目录检查更新')
    return repo.resolve()


def _local(repo):
    head = _git(repo, 'rev-parse', '--verify', 'HEAD')[0]
    branch, _ = _git(repo, 'symbolic-ref', '--quiet', '--short', 'HEAD', allowed=(0, 1))
    dirty = bool(_git(repo, 'status', '--porcelain=v1', '--untracked-files=all')[0])
    index = _git(repo, 'ls-files', '-v')[0].splitlines()
    special = any(row and (row[0].islower() or row[0] == 'S') for row in index)
    shallow = _git(repo, 'rev-parse', '--is-shallow-repository')[0] == 'true'
    sparse = _git(repo, 'config', '--bool', 'core.sparseCheckout', allowed=(0, 1))[0] == 'true'
    merging = any((repo / _git(repo, 'rev-parse', '--git-path', name)[0]).exists()
                  for name in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'rebase-merge', 'rebase-apply', 'BISECT_LOG'))
    submodule = any(row.startswith('160000 ') for row in _git(repo, 'ls-files', '--stage')[0].splitlines())
    return head, bool(dirty or special or shallow or sparse or merging or submodule or not branch)


def _result(state, message, head='', target='', tag=''):
    return {'state': state, 'message': message, 'running_version': __version__,
            'current_commit': head, 'target_commit': target, 'target_version': tag,
            'handoff': (f'ProjectWatch 更新检查：{message}\n当前提交：{head or "未知"}\n'
                        f'目标版本：{tag or "未确定"}；提交：{target or "未确定"}\n'
                        '请检查并保留本地定制；不要 reset --hard、强制覆盖或自动重试未知付费请求。'
                        '先备份数据与配置，在副本验证依赖、迁移及启动，再请求确认切换。')}


def check(repo: Path, source: str) -> dict:
    if not source:
        return _result('not_configured', '尚未配置正式更新源；不会使用当前私有远端')
    source = validate_source(source)
    try:
        repo = _root(repo)
    except UpdateError:
        return _result('manual_install', '非标准 Git 源码安装或 Git 不可用，请使用安装包升级指南')
    try:
        head, custom = _local(repo)
        if custom:
            return _result('agent_required', '检测到本地修改或特殊 Git 状态，请由 Agent 协助升级', head)
        refs, _ = _git(repo, 'ls-remote', '--tags', source)
        releases = {}
        for row in refs.splitlines():
            sha, name = row.split('\t', 1)
            name = name.removeprefix('refs/tags/').removesuffix('^{}')
            match = TAG.fullmatch(name)
            if match and SHA.fullmatch(sha):
                releases[tuple(map(int, match.groups()))] = name
        if not releases:
            return _result('no_release', '更新源尚无稳定发布版本（v数字.数字.数字）', head)
        tag = releases[max(releases)]
        # Do not overwrite local tags or remote tracking branches.
        ref = 'refs/projectwatch-updates/' + uuid.uuid4().hex
        try:
            _git(repo, 'fetch', '--no-tags', '--no-write-fetch-head', '--no-recurse-submodules', source, f'refs/tags/{tag}:{ref}')
            target = _git(repo, 'rev-parse', '--verify', ref + '^{commit}')[0]
        finally:
            _git(repo, 'update-ref', '-d', ref)
        if head == target:
            return _result('up_to_date', '已是当前稳定版本', head, target, tag)
        _, code = _git(repo, 'merge-base', '--is-ancestor', head, target, allowed=(0, 1))
        if code:
            return _result('agent_required', '存在本地独有提交或历史分叉，请由 Agent 合并', head, target, tag)
        return _result('available', '发现新版；可在停止实例后执行安全升级', head, target, tag)
    except (UpdateError, ValueError):
        return _result('check_failed', '无法确认最新版本，请检查网络或更新源；没有执行升级')


def configured_check(*, refresh=False):
    """Web callers cannot supply a filesystem path, remote URL or Git arguments."""
    global _cached
    if not refresh:
        return _cached[1] if _cached else _result('unchecked', '点击检查更新；不会自动联网或安装')
    with _check_lock:
        if _cached and time.monotonic() - _cached[0] < 60:
            return _cached[1]
        try:
            result = check(Path(__file__).resolve().parents[3], os.environ.get('PROJECT_WATCH_UPDATE_SOURCE', ''))
        except UpdateError:
            result = _result('check_failed', '更新源配置无效；请由本机管理员检查')
        _cached = time.monotonic(), result
        return result


@contextmanager
def source_runtime_lock():
    """Shared source lease for editable/source installs; wheels have no source tree."""
    repo = Path(__file__).resolve().parents[3]
    if not (repo / '.git').exists():
        yield
        return
    with _source_lock(repo, exclusive=False):
        yield


@contextmanager
def _source_lock(repo, *, exclusive):
    location = Path(_git(repo, 'rev-parse', '--git-common-dir')[0])
    if not location.is_absolute():
        location = repo / location
    with (location / 'projectwatch-source.lock').open('a+b') as stream:
        if os.name == 'nt':
            import ctypes
            import msvcrt
            from ctypes import wintypes

            class Overlapped(ctypes.Structure):
                _fields_ = [('internal', ctypes.c_size_t), ('high', ctypes.c_size_t),
                            ('offset', wintypes.DWORD), ('offset_high', wintypes.DWORD),
                            ('event', wintypes.HANDLE)]
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                         wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
            kernel.LockFileEx.restype = wintypes.BOOL
            kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                           wintypes.DWORD, ctypes.POINTER(Overlapped)]
            kernel.UnlockFileEx.restype = wintypes.BOOL
            handle, overlapped = msvcrt.get_osfhandle(stream.fileno()), Overlapped()
            if not kernel.LockFileEx(handle, 1 | (2 if exclusive else 0), 0, 1, 0, ctypes.byref(overlapped)):
                raise UpdateError('同一源码仍有实例运行或正在升级，请关闭相关实例后操作')
            try:
                yield
            finally:
                kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped))
        else:
            import fcntl

            try:
                fcntl.flock(stream, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except OSError as exc:
                raise UpdateError('同一源码仍有实例运行或正在升级，请关闭相关实例后操作') from exc
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


@contextmanager
def _repo_lock(repo):
    location = Path(_git(repo, 'rev-parse', '--git-common-dir')[0])
    if not location.is_absolute():
        location = repo / location
    lock = location / 'projectwatch-update.lock'
    try:
        stream = lock.open('x', encoding='utf-8')
    except FileExistsError as exc:
        raise UpdateError('已有升级或上次升级未结束，请由 Agent 检查维护锁，不要强行清理') from exc
    try:
        with stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        lock.unlink()


def _export(repo, commit, destination):
    destination.mkdir()
    rows, _ = _git(repo, '-c', 'core.quotepath=false', 'ls-tree', '-r', commit)
    entries = []
    for row in rows.splitlines():
        meta, name = row.split('\t', 1)
        mode, kind, blob = meta.split()
        relative = Path(name)
        if (kind != 'blob' or mode not in {'100644', '100755'} or relative.is_absolute()
                or any(p in {'..', '.git'} for p in relative.parts) or '\\' in name or ':' in name
                or name.startswith('"')):
            raise UpdateError('新版包含不支持的文件路径、子模块或链接，请由 Agent 升级')
        entries.append((relative, mode, blob))
    blobs = list(dict.fromkeys(blob for _, _, blob in entries))
    data = _git(repo, 'cat-file', '--batch', binary=True,
                input_data=('\n'.join(blobs) + '\n').encode())[0]
    contents, offset = {}, 0
    for blob in blobs:
        end = data.find(b'\n', offset)
        parts = data[offset:end].decode('ascii').split()
        if len(parts) != 3 or parts[:2] != [blob, 'blob'] or not parts[2].isdigit():
            raise UpdateError('新版源码对象读取失败')
        size = int(parts[2])
        start = end + 1
        if data[start + size:start + size + 1] != b'\n':
            raise UpdateError('新版源码对象不完整')
        contents[blob] = data[start:start + size]
        offset = start + size + 1
    for relative, mode, blob in entries:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as output:
            output.write(contents[blob])
        if mode == '100755' and os.name != 'nt':
            target.chmod(0o755)


def _preserved_state(home):
    """Compare pause states and held unknown requests without exporting their values."""
    from project_watch.studio.host import digest

    result = {}
    with closing(sqlite3.connect(f'{(home / "projectwatch.sqlite3").as_uri()}?mode=ro', uri=True)) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'studio_projects' in tables:
            result['pause'] = db.execute('SELECT project_id,status FROM studio_projects ORDER BY project_id').fetchall()
        for table in ('studio_job_calls', 'studio_direct_calls'):
            if table in tables:
                # Frozen existing columns; a future schema change requires manual review.
                db.row_factory = sqlite3.Row
                result[table] = sorted((dict(r) for r in db.execute(
                    f"SELECT * FROM {table} WHERE status IN ('unknown','started')")),
                    key=lambda r: (r.get('job_id', r.get('operation_id', '')), r['call_key']))
    result['settings'] = {p.relative_to(home).as_posix(): digest(p) for p in home.rglob('*settings*.json') if p.is_file()}
    return result


def _spawn(command, cwd, stream, env=None):
    if os.name == 'nt':
        from project_watch.studio.windows_child import WindowsChild

        # The wrapper supplies cwd and supports npm.cmd without putting user paths
        # in a shell command. Every descendant belongs to the existing Job Object.
        wrapper = ('import os,sys,json,subprocess; os.chdir(sys.argv[1]); '
                   'sys.exit(subprocess.run(json.loads(sys.argv[2])).returncode)')
        return WindowsChild([sys.executable, '-I', '-c', wrapper, str(cwd), json.dumps(command)],
                            env=env or dict(os.environ), log=stream)
    return subprocess.Popen(command, cwd=cwd, env=env, stdout=stream,
                            stderr=subprocess.STDOUT, start_new_session=True)


def _close_child(child):
    if hasattr(child, 'close'):
        # Windows: kill-on-close also covers the venv Python redirector's children.
        child.close()
    else:
        import signal

        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=10)


def _run(command, cwd, log, *, env=None, timeout=900):
    with log.open('ab') as stream:
        child = None
        try:
            child = _spawn(command, cwd, stream, env)
            code = child.wait(timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UpdateError('候选构建/验证未完成；原环境保留，请查看私有日志') from exc
        finally:
            if child is not None:
                _close_child(child)
    if code:
        raise UpdateError('候选构建/验证失败；原环境保留，请查看私有日志')


def validate_candidate(stage: Path, backup: Path) -> dict:
    """Build a separate installed environment, migrate ONLY a copy, no workers."""
    source, environment, log = stage / 'source', stage / 'environment', stage / 'build.private.log'
    version = tomllib.loads((source / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    release = json.loads((stage / 'receipt.json').read_text(encoding='utf-8'))['target_version']
    if version != release.removeprefix('v'):
        raise UpdateError('发布标签与安装包版本不一致，未更新')
    _run([sys.executable, '-m', 'venv', str(environment)], stage, log)
    python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    _run([str(python), '-m', 'pip', 'install', '--upgrade', 'pip>=26.2', 'build'], stage, log)
    npm = shutil.which('npm.cmd' if os.name == 'nt' else 'npm')
    if not npm:
        raise UpdateError('未找到 Node/npm；未更新原源码')
    _run([npm, 'ci'], source / 'frontend', log)
    _run([npm, 'run', 'build'], source / 'frontend', log)
    _run([str(python), '-m', 'build', '--wheel', '--outdir', str(stage / 'wheel')], source, log)
    wheels = list((stage / 'wheel').glob('*.whl'))
    if len(wheels) != 1:
        raise UpdateError('新版安装包数量异常')
    # Preserve installed optional capability sets, not just the minimal dependency set.
    extras = []
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as installed_version
    for extra, package in [('ocr', 'rapidocr'), ('mcp', 'mcp'), ('team', 'argon2-cffi'), ('postgres', 'psycopg')]:
        try:
            installed_version(package)
            extras.append(extra)
        except PackageNotFoundError:
            pass
    spec = str(wheels[0]) + ('[' + ','.join(extras) + ']' if extras else '')
    _run([str(python), '-m', 'pip', 'install', spec], stage, log)
    _run([str(python), '-m', 'pip', 'check'], stage, log)
    copied = stage / 'validation-home'
    restore_snapshot(backup, copied)
    _probe_candidate(python, copied, log, version)
    return {'validated': True, 'python': str(python), 'extras': extras}


def _probe_candidate(python, copied, log, version):
    stage = copied.parent
    preserved = _preserved_state(copied)
    # Explicit start_worker=False; do not run the normal automatic scheduler.
    nonce = uuid.uuid4().hex
    script = ('import sys,uvicorn\nfrom project_watch import __version__\n'
              'assert __version__ == sys.argv[3]\nfrom project_watch.studio.app import create_studio_app\n'
              'app=create_studio_app(data_dir=sys.argv[1],start_worker=False)\n'
              '@app.middleware("http")\n'
              'async def stamp(request,call_next):\n'
              '    response=await call_next(request)\n'
              '    response.headers["X-ProjectWatch-Update-Probe"]=sys.argv[4]\n'
              '    return response\n'
              'uvicorn.run(app,host="127.0.0.1",port=int(sys.argv[2]),log_level="error")')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if k not in {'PYTHONPATH', 'PYTHONHOME'}
           and not k.startswith('PROJECT_WATCH_')}
    env['PROJECT_WATCH_HOME'] = str(copied)
    with log.open('ab') as stream:
        child = _spawn([str(python), '-I', '-c', script, str(copied), str(port), version, nonce], stage, stream, env)
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(60):
                if child.poll() is not None:
                    raise UpdateError('新版副本启动失败，原数据未迁移')
                try:
                    with opener.open(f'http://127.0.0.1:{port}/api/studio/health', timeout=2) as response:
                        if response.headers.get('X-ProjectWatch-Update-Probe') != nonce:
                            raise UpdateError('验证端口被其他实例占用，未更新原源码')
                        health = json.loads(response.read(16384))
                    if health.get('restart_required') or not isinstance(health.get('parsing'), dict):
                        raise UpdateError('新版健康验证未通过')
                    break
                except OSError:
                    time.sleep(.5)
            else:
                raise UpdateError('新版副本启动超时')
        finally:
            _close_child(child)
    if _preserved_state(copied) != preserved:
        raise UpdateError('副本中的暂停状态、配置或未知请求发生变化；需 Agent 检查迁移')


def upgrade(repo, source, home, stage, expected_current, expected_target):
    repo = _root(repo)
    home, stage = Path(home).absolute(), Path(stage).absolute()
    if any(_linked(p) for p in (home, stage)):
        raise UpdateError('数据和暂存路径不得包含链接')
    home, stage = home.resolve(), stage.resolve()
    paths = [repo, home, stage]
    if any(a.is_relative_to(b) for a in paths for b in paths if a != b) or len(set(paths)) != 3:
        raise UpdateError('源码、数据和升级目录必须相互独立')
    if stage.exists() or not (home / 'projectwatch.sqlite3').is_file():
        raise UpdateError('升级目录须全新，且必须指定现有 Studio 数据目录')
    if not SHA.fullmatch(expected_current) or not SHA.fullmatch(expected_target):
        raise UpdateError('须提供检查时确认的完整旧/新提交')
    with _repo_lock(repo), _source_lock(repo, exclusive=True), instance_lock(home / 'host'), instance_lock(home):
        report = check(repo, source)
        if (report['state'] != 'available' or report['current_commit'] != expected_current
                or report['target_commit'] != expected_target):
            raise UpdateError('检查结果已变化或存在定制；未更新，请重新检查或交给 Agent')
        stage.mkdir(parents=True)
        receipt = {'phase': 'preparing', 'before': expected_current, 'target': expected_target,
                   'target_version': report['target_version'], 'home': str(home), 'backup': str(stage / 'backup')}
        def save(phase):
            receipt['phase'] = phase
            temp = stage / 'receipt.tmp'
            temp.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
            temp.replace(stage / 'receipt.json')
        save('backing_up')
        try:
            required = sum(p.stat().st_size for p in home.rglob('*') if p.is_file()) * 3 + 1024 ** 3
            if shutil.disk_usage(stage).free < required:
                raise UpdateError('升级盘可用空间不足，尚未备份或更新')
            snapshot_locked(home, stage / 'backup')
            save('validating')
            _export(repo, expected_target, stage / 'source')
            receipt['candidate'] = validate_candidate(stage, stage / 'backup')
            head, custom = _local(repo)
            if custom or head != expected_current:
                raise UpdateError('验证期间源码发生变化，请由 Agent 合并；候选与备份已保留')
            save('applying')
            _git(repo, 'merge', '--ff-only', '--no-overwrite-ignore', expected_target)
            if _git(repo, 'rev-parse', 'HEAD')[0] != expected_target:
                raise UpdateError('源码更新结果不明，请检查回执；不自动回滚')
            save('source_updated')
            return receipt
        except Exception:
            receipt['failed_during'] = receipt['phase']
            save('failed')
            raise


def main():
    parser = argparse.ArgumentParser(description='源码更新：显式检查；停机、备份和验证后快进，不强制覆盖')
    parser.add_argument('action', choices=['check', 'apply'])
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--home', type=Path)
    parser.add_argument('--stage', type=Path)
    parser.add_argument('--current')
    parser.add_argument('--target')
    parser.add_argument('--confirm', action='store_true', help='允许下载构建依赖并执行已确认新版代码')
    args = parser.parse_args()
    source = os.environ.get('PROJECT_WATCH_UPDATE_SOURCE', '')
    try:
        if args.action == 'check':
            print(json.dumps(check(args.repo, source), ensure_ascii=False, indent=2))
        else:
            if not all([args.confirm, args.home, args.stage, args.current, args.target, source]):
                parser.error('apply 需要 --confirm --home --stage --current --target 和已配置的更新源')
            receipt = upgrade(args.repo, source, args.home, args.stage, args.current, args.target)
            print(json.dumps(receipt, ensure_ascii=False, indent=2))
            print('源码已更新，尚未启动。使用回执中 candidate.python 启动；原环境/备份保留。')
    except (UpdateError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()

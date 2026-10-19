"""Offline Git remotes and synthetic homes only; never use user configuration."""
import json
import sqlite3
import subprocess
from contextlib import closing

import pytest

from project_watch.studio import updates
from project_watch.studio.host import instance_lock


def git(root, *args):
    return subprocess.check_output(['git', '-c', 'user.name=Test', '-c',
        'user.email=test@localhost', *args], cwd=root).decode().strip()


@pytest.fixture
def repos(tmp_path, monkeypatch):
    remote = tmp_path / 'remote'
    remote.mkdir()
    git(remote, 'init', '-b', 'main')
    (remote / 'README.md').write_text('v1')
    git(remote, 'add', '.')
    git(remote, 'commit', '-m', 'v1')
    git(remote, 'tag', 'v1.0.0')
    local = tmp_path / 'local'
    git(tmp_path, 'clone', str(remote), str(local))
    (remote / 'README.md').write_text('v2')
    git(remote, 'commit', '-am', 'v2')
    git(remote, 'tag', 'v1.1.0')
    # Production accepts HTTPS GitHub only. Tests explicitly replace this boundary.
    monkeypatch.setattr(updates, 'validate_source', lambda value: str(remote))
    return local, str(remote)


def test_clean_fast_forward(repos):
    repo, source = repos
    before = git(repo, 'rev-parse', 'HEAD')
    result = updates.check(repo, source)
    assert result['state'] == 'available'
    assert result['current_commit'] == before
    assert result['target_version'] == 'v1.1.0'
    assert git(repo, 'rev-parse', 'HEAD') == before
    assert 'README.md' not in result['handoff']  # No paths or business content exported.


@pytest.mark.parametrize('kind', ['dirty', 'untracked', 'staged', 'ahead', 'detached', 'skip'])
def test_customizations_need_agent(repos, kind):
    repo, source = repos
    if kind in {'dirty', 'staged', 'ahead'}:
        (repo / 'README.md').write_text('custom')
    if kind == 'untracked':
        (repo / 'private.txt').write_text('do not disclose')
    if kind == 'staged':
        git(repo, 'add', '.')
    if kind == 'ahead':
        git(repo, 'commit', '-am', 'custom')
    if kind == 'detached':
        git(repo, 'checkout', '--detach')
    if kind == 'skip':
        git(repo, 'update-index', '--skip-worktree', 'README.md')
    result = updates.check(repo, source)
    assert result['state'] == 'agent_required'
    assert 'do not disclose' not in json.dumps(result)


def test_source_config_and_non_git(tmp_path):
    assert updates.check(tmp_path, '')['state'] == 'not_configured'
    assert updates.check(tmp_path, 'https://github.com/example/project.git')['state'] == 'manual_install'
    for value in ['file:///tmp/a', 'https://name:secret@github.com/a/b',
                  'https://github.com.evil.test/a/b', 'ssh://github.com/a/b',
                  'https://github.com/a/b?token=secret']:
        with pytest.raises(updates.UpdateError):
            updates.validate_source(value)


def make_home(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    with closing(sqlite3.connect(home / 'projectwatch.sqlite3')) as db:
        db.execute('create table projects(status text)')
        db.execute("insert into projects values ('paused')")
        db.execute('create table calls(status text)')
        db.execute("insert into calls values ('unknown')")
        db.commit()
    (home / 'settings.json').write_text('{"synthetic":true}')
    (home / 'original.txt').write_text('synthetic original')
    return home


def prepare(repos, tmp_path):
    repo, source = repos
    report = updates.check(repo, source)
    home = make_home(tmp_path)
    return repo, source, report, home


def fake_validate(stage, backup):
    assert (stage / 'source' / 'README.md').read_text() == 'v2'
    assert (backup / 'original.txt').read_text() == 'synthetic original'
    return {'validated': True, 'python': 'synthetic-only'}


def test_upgrade_preserves_home_and_ff_only(repos, tmp_path, monkeypatch):
    repo, source, report, home = prepare(repos, tmp_path)
    monkeypatch.setattr(updates, 'validate_candidate', fake_validate)
    before = {p.name: p.read_bytes() for p in home.iterdir()}
    result = updates.upgrade(repo, source, home, tmp_path / 'upgrade',
                             report['current_commit'], report['target_commit'])
    assert result['phase'] == 'source_updated'
    assert git(repo, 'rev-parse', 'HEAD') == report['target_commit']
    for name, content in before.items():
        assert (home / name).read_bytes() == content
    assert (tmp_path / 'upgrade' / 'backup' / 'studio-manifest.json').exists()


def test_validation_failure_does_not_change_source_or_data(repos, tmp_path, monkeypatch):
    repo, source, report, home = prepare(repos, tmp_path)
    def fail(*_):
        raise updates.UpdateError('synthetic build failure')
    monkeypatch.setattr(updates, 'validate_candidate', fail)
    with pytest.raises(updates.UpdateError):
        updates.upgrade(repo, source, home, tmp_path / 'upgrade',
                        report['current_commit'], report['target_commit'])
    assert git(repo, 'rev-parse', 'HEAD') == report['current_commit']
    assert json.loads((tmp_path / 'upgrade' / 'receipt.json').read_text())['phase'] == 'failed'
    assert (home / 'original.txt').read_text() == 'synthetic original'


def test_running_home_blocks_update(repos, tmp_path, monkeypatch):
    repo, source, report, home = prepare(repos, tmp_path)
    monkeypatch.setattr(updates, 'validate_candidate', lambda *_: pytest.fail('must not build'))
    with instance_lock(home), pytest.raises(RuntimeError):
        updates.upgrade(repo, source, home, tmp_path / 'upgrade',
                        report['current_commit'], report['target_commit'])
    assert git(repo, 'rev-parse', 'HEAD') == report['current_commit']


def test_stale_approval_and_dirty_rejected(repos, tmp_path):
    repo, source, report, home = prepare(repos, tmp_path)
    with pytest.raises(updates.UpdateError):
        updates.upgrade(repo, source, home, tmp_path / 'upgrade', '0' * 40, report['target_commit'])
    (repo / 'README.md').write_text('keep me')
    with pytest.raises(updates.UpdateError):
        updates.upgrade(repo, source, home, tmp_path / 'upgrade2',
                        report['current_commit'], report['target_commit'])
    assert (repo / 'README.md').read_text() == 'keep me'


def test_changes_during_validation_block_apply(repos, tmp_path, monkeypatch):
    repo, source, report, home = prepare(repos, tmp_path)
    def edited(stage, backup):
        (repo / 'README.md').write_text('new local edits')
        return fake_validate(stage, backup)
    monkeypatch.setattr(updates, 'validate_candidate', edited)
    with pytest.raises(updates.UpdateError):
        updates.upgrade(repo, source, home, tmp_path / 'upgrade',
                        report['current_commit'], report['target_commit'])
    assert git(repo, 'rev-parse', 'HEAD') == report['current_commit']
    assert (repo / 'README.md').read_text() == 'new local edits'


def test_ignored_file_collision_is_not_overwritten(repos, tmp_path, monkeypatch):
    repo, source = repos
    remote = tmp_path / 'remote'
    (remote / 'local-only.txt').write_text('official')
    git(remote, 'add', '.')
    git(remote, 'commit', '-m', 'new file')
    git(remote, 'tag', 'v1.2.0')
    (repo / '.git' / 'info' / 'exclude').write_text('local-only.txt\n')
    (repo / 'local-only.txt').write_text('keep ignored content')
    report = updates.check(repo, source)
    home = make_home(tmp_path)
    monkeypatch.setattr(updates, 'validate_candidate', fake_validate)
    with pytest.raises(updates.UpdateError):
        updates.upgrade(repo, source, home, tmp_path / 'upgrade',
                        report['current_commit'], report['target_commit'])
    assert (repo / 'local-only.txt').read_text() == 'keep ignored content'
    assert git(repo, 'rev-parse', 'HEAD') == report['current_commit']


def test_new_remote_target_requires_new_confirmation(repos, tmp_path, monkeypatch):
    repo, source, report, home = prepare(repos, tmp_path)
    remote = tmp_path / 'remote'
    (remote / 'README.md').write_text('v3')
    git(remote, 'commit', '-am', 'v3')
    git(remote, 'tag', 'v1.2.0')
    monkeypatch.setattr(updates, 'validate_candidate', lambda *_: pytest.fail('not authorized'))
    with pytest.raises(updates.UpdateError):
        updates.upgrade(repo, source, home, tmp_path / 'upgrade',
                        report['current_commit'], report['target_commit'])


def test_failure_to_fetch_is_not_up_to_date(repos, monkeypatch):
    repo, source = repos
    original = updates._git
    def fail(path, *args, **kwargs):
        if 'ls-remote' in args:
            raise updates.UpdateError('do not disclose credentials')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(updates, '_git', fail)
    result = updates.check(repo, source)
    assert result['state'] == 'check_failed'
    assert 'credentials' not in json.dumps(result)


def test_no_automatic_network_and_cache(monkeypatch):
    monkeypatch.setattr(updates, '_cached', None)
    calls = []
    monkeypatch.setattr(updates, 'check', lambda *_: calls.append(1) or updates._result('no_release', 'none'))
    assert updates.configured_check()['state'] == 'unchecked'
    assert calls == []
    updates.configured_check(refresh=True)
    updates.configured_check(refresh=True)
    assert calls == [1]


def test_lock_and_overlap_rejected(repos, tmp_path, monkeypatch):
    repo, source, report, home = prepare(repos, tmp_path)
    monkeypatch.setattr(updates, 'validate_candidate', lambda *_: pytest.fail('must not build'))
    with updates._repo_lock(repo), pytest.raises(updates.UpdateError, match='维护锁'):
        updates.upgrade(repo, source, home, tmp_path / 'upgrade',
                        report['current_commit'], report['target_commit'])
    with pytest.raises(updates.UpdateError, match='相互独立'):
        updates.upgrade(repo, source, home, repo / 'upgrade',
                        report['current_commit'], report['target_commit'])


def test_backup_failure_does_not_apply(repos, tmp_path, monkeypatch):
    repo, source, report, home = prepare(repos, tmp_path)
    def fail(*_):
        raise OSError('synthetic disk error')
    monkeypatch.setattr(updates, 'snapshot_locked', fail)
    monkeypatch.setattr(updates, 'validate_candidate', lambda *_: pytest.fail('must not build'))
    with pytest.raises(OSError):
        updates.upgrade(repo, source, home, tmp_path / 'upgrade',
                        report['current_commit'], report['target_commit'])
    assert git(repo, 'rev-parse', 'HEAD') == report['current_commit']


def test_update_api_is_explicit_and_has_no_apply(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from project_watch.studio.app import create_studio_app
    calls = []
    monkeypatch.setattr(updates, 'configured_check', lambda refresh=False:
                        calls.append(refresh) or updates._result('unchecked', 'synthetic'))
    with TestClient(create_studio_app(tmp_path / 'app-home', start_worker=False)) as client:
        assert client.get('/api/studio/updates').status_code == 200
        assert calls == [False]
        assert client.post('/api/studio/updates/check').status_code == 200
        assert calls == [False, True]
        assert client.post('/api/studio/updates/apply').status_code in {404, 405}
        assert client.post('/api/studio/updates/check', headers={'Origin': 'https://evil.test'}).status_code == 403


def test_shared_source_instances_exclude_upgrade(repos):
    repo, _ = repos
    with (updates._source_lock(repo, exclusive=False), updates._source_lock(repo, exclusive=False),
          pytest.raises(updates.UpdateError), updates._source_lock(repo, exclusive=True)):
        pytest.fail('active source updated')
    with (updates._source_lock(repo, exclusive=True), pytest.raises(updates.UpdateError),
          updates._source_lock(repo, exclusive=False)):
        pytest.fail('started during upgrade')


def test_preserved_state_accepts_binary_unknown_receipts(tmp_path):
    home = make_home(tmp_path)
    with closing(sqlite3.connect(home / 'projectwatch.sqlite3')) as db:
        db.execute('create table studio_job_calls(job_id text,call_key text,status text,response_body blob)')
        db.execute("insert into studio_job_calls values ('synthetic','synthetic','unknown',?)", (b'\xff\x00',))
        db.commit()
    state = updates._preserved_state(home)
    assert state['studio_job_calls'][0]['response_body'] == b'\xff\x00'


def test_candidate_version_mismatch_blocks_before_install(tmp_path, monkeypatch):
    (tmp_path / 'source').mkdir()
    (tmp_path / 'source' / 'pyproject.toml').write_text('[project]\nversion="1.0.0"')
    (tmp_path / 'receipt.json').write_text('{"target_version":"v2.0.0"}')
    monkeypatch.setattr(updates, '_run', lambda *_: pytest.fail('must not install'))
    with pytest.raises(updates.UpdateError, match='版本不一致'):
        updates.validate_candidate(tmp_path, tmp_path / 'backup')


def test_owned_build_commands_keep_cwd_and_fail_closed(tmp_path):
    import sys

    updates._run([sys.executable, '-c', 'from pathlib import Path; Path("owned").write_text("synthetic")'],
                 tmp_path, tmp_path / 'build.log')
    assert (tmp_path / 'owned').read_text() == 'synthetic'
    with pytest.raises(updates.UpdateError, match='验证失败'):
        updates._run([sys.executable, '-c', 'raise SystemExit(7)'], tmp_path, tmp_path / 'build.log')
    with pytest.raises(updates.UpdateError, match='未完成'):
        updates._run([sys.executable, '-c', 'import time; time.sleep(30)'], tmp_path,
                     tmp_path / 'build.log', timeout=.5)


def test_invalid_backup_does_not_create_a_home(tmp_path):
    from project_watch.studio.host import snapshot

    with pytest.raises(ValueError):
        snapshot(tmp_path / 'missing', tmp_path / 'backup')
    assert not (tmp_path / 'missing').exists()


def test_health_from_another_instance_cannot_validate_candidate(tmp_path, monkeypatch):
    home = make_home(tmp_path)
    closed = []
    class Child:
        def poll(self):
            return None
    class Response:
        def __init__(self):
            self.headers = {'X-ProjectWatch-Update-Probe': 'another-instance'}
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
    class Opener:
        def open(self, *_args, **_kwargs):
            return Response()
    monkeypatch.setattr(updates, '_spawn', lambda *_: Child())
    monkeypatch.setattr(updates, '_close_child', lambda child: closed.append(child))
    monkeypatch.setattr(updates.urllib.request, 'build_opener', lambda *_: Opener())
    with pytest.raises(updates.UpdateError, match='其他实例占用'):
        updates._probe_candidate('synthetic-python', home, tmp_path / 'probe.log', '1.0.0')
    assert len(closed) == 1


def test_source_lock_is_cross_process(repos):
    import sys
    from pathlib import Path

    repo, _ = repos
    script = ('import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); '
              'from project_watch.studio.updates import _source_lock\n'
              'with _source_lock(Path(sys.argv[2]),exclusive=True): pass')
    command = [sys.executable, '-I', '-c', script, str(Path(updates.__file__).parents[2]), str(repo)]
    with updates._source_lock(repo, exclusive=False):
        assert subprocess.run(command, capture_output=True, timeout=15).returncode != 0
    assert subprocess.run(command, capture_output=True, timeout=15).returncode == 0

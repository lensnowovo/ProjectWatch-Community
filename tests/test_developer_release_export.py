"""Synthetic fixed-tree export: unclassified data and missing analysis must fail."""
import subprocess

import pytest

from scripts.developer_release_export import ExportError, digest, export, safe_path


@pytest.fixture
def case(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    files = {'pyproject.toml': b'[project]\nname="projectwatch-local"\n',
             'README.md': b'Synthetic developer guide', 'LICENSE': b'Synthetic test license',
             'src/project_watch/__init__.py': b'# synthetic\n',
             'src/project_watch/studio/analysis.py': b'# synthetic analysis\n',
             'docs/handoff/old.md': b'Synthetic private history'}
    for name, data in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    for args in (['init'], ['config', 'core.autocrlf', 'false'], ['add', '.'],
                 ['-c', 'user.name=synthetic', '-c', 'user.email=test@example.test', 'commit', '-m', 'synthetic']):
        subprocess.run(['git', *args], cwd=repo, capture_output=True, check=True)
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo).decode().strip()
    manifest = {'schema_version': 1, 'publication_authorized': False, 'commit': commit,
                'files': [{'path': name, 'sha256': digest(data), 'reason': 'synthetic review',
                           'disposition': 'exclude' if name.startswith('docs/') else 'include'}
                          for name, data in files.items()]}
    return repo, manifest, tmp_path / 'clean'


def test_export_uses_reviewed_commit_not_worktree_or_history(case):
    repo, manifest, dest = case
    (repo / 'README.md').write_text('Unreviewed change')
    (repo / '.env').write_text('Synthetic untracked secret')
    result = export(repo, manifest, dest)
    assert result['exported_files'] == 5
    assert result['publication_authorized'] is False
    assert (dest / 'README.md').read_text() == 'Synthetic developer guide'
    assert not (dest / '.git').exists() and not (dest / '.env').exists()
    assert not (dest / 'docs').exists()
    assert all(digest((dest / p).read_bytes()) == h for p, h in result['files_sha256'].items())


@pytest.mark.parametrize('fault', ['missing', 'hash', 'duplicate', 'history', 'analysis', 'traversal', 'exists', 'inside', 'publication'])
def test_rejects_unreviewed_or_incomplete_exports(case, fault):
    repo, manifest, dest = case
    if fault == 'missing':
        manifest['files'].pop()
    elif fault == 'hash':
        manifest['files'][0]['sha256'] = '0' * 64
    elif fault == 'duplicate':
        manifest['files'].append(manifest['files'][0])
    elif fault == 'history':
        manifest['files'][-1]['disposition'] = 'include'
    elif fault == 'analysis':
        manifest['files'][-2]['disposition'] = 'exclude'
    elif fault == 'traversal':
        manifest['files'][0]['path'] = '../escape'
    elif fault == 'exists':
        dest.mkdir()
    elif fault == 'inside':
        dest = repo / 'clean'
    else:
        manifest['publication_authorized'] = True
    with pytest.raises(ExportError):
        export(repo, manifest, dest)
    if fault != 'exists':
        assert not dest.exists()


@pytest.mark.parametrize('path', ['../a', '/a', '.git/config', '.GIT/objects', 'x\\y', 'x:y', 'a//b', 'CON', 'aux.txt', 'a/../b', 'a.'])
def test_unsafe_windows_paths(path):
    assert not safe_path(path)


@pytest.mark.parametrize('path,data,disposition', [
    ('.env', b'Synthetic configuration', 'include'),
    ('private.sqlite3', b'Synthetic database', 'include'),
    ('example.py', ('token="ghp_' + 'A' * 36 + '"').encode(), 'include'),
    ('frontend/src/Analysis.tsx', b'// synthetic analysis UI', 'exclude'),
])
def test_sensitive_content_and_missing_ui_are_rejected(case, path, data, disposition):
    repo, manifest, dest = case
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    subprocess.run(['git', 'add', '--force', '--', path], cwd=repo, capture_output=True, check=True)
    subprocess.run(['git', '-c', 'user.name=synthetic', '-c', 'user.email=test@example.test',
                    'commit', '-m', 'synthetic input'], cwd=repo, capture_output=True, check=True)
    manifest['commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo).decode().strip()
    manifest['files'].append({'path': path, 'sha256': digest(data), 'disposition': disposition, 'reason': 'synthetic'})
    with pytest.raises(ExportError):
        export(repo, manifest, dest)
    assert not dest.exists()


def test_git_symlink_is_rejected_even_when_excluded(case):
    repo, manifest, dest = case
    data = b'../outside'
    blob = subprocess.check_output(['git', 'hash-object', '-w', '--stdin'], input=data, cwd=repo).decode().strip()
    subprocess.run(['git', 'update-index', '--add', '--cacheinfo', f'120000,{blob},linked'],
                   cwd=repo, capture_output=True, check=True)
    subprocess.run(['git', '-c', 'user.name=synthetic', '-c', 'user.email=test@example.test',
                    'commit', '-m', 'synthetic symlink'], cwd=repo, capture_output=True, check=True)
    manifest['commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo).decode().strip()
    manifest['files'].append({'path': 'linked', 'sha256': digest(data), 'disposition': 'exclude', 'reason': 'synthetic'})
    with pytest.raises(ExportError, match='mode'):
        export(repo, manifest, dest)
    assert not dest.exists()

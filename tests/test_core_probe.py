"""Synthetic-only staging and fail-closed private probe packaging."""
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.build_core_probe import inventory, stage, verify_wheel


def fixture(tmp_path):
    repo = tmp_path / 'repo'
    (repo / 'src/project_watch/studio').mkdir(parents=True)
    (repo / 'scripts').mkdir()
    (repo / 'pyproject.toml').write_text('[project]\nname = "greenpine-project-watch"\nversion = "0.2.0a0"\n')
    (repo / 'README.md').write_text('Synthetic candidate')
    modules = {'project_watch': 'Core', 'project_watch.studio': 'Core',
               'project_watch.studio.core': 'Core', 'project_watch.studio.secret': 'Insight'}
    for name in ('__init__.py', 'studio/__init__.py', 'studio/core.py', 'studio/secret.py'):
        (repo / 'src/project_watch' / name).write_text('# synthetic\n')
    (repo / 'scripts/layer_map.json').write_text(json.dumps({'modules': modules, 'symbols': {}}))
    (repo / 'settings.json').write_text('{"synthetic-secret": true}')
    return repo


def test_stage_excludes_registered_private_modules_and_marks_probe(tmp_path):
    repo = fixture(tmp_path)
    manifest = stage(repo, tmp_path / 'candidate')
    assert manifest['publication_authorized'] is False
    assert manifest['excluded_modules'] == ['project_watch.studio.secret']
    assert manifest['version'] == '0.2.0a0+coreprobe'
    source = tmp_path / 'candidate/source'
    assert (source / 'src/project_watch/studio/core.py').is_file()
    assert not (source / 'src/project_watch/studio/secret.py').exists()
    assert not (source / 'settings.json').exists()
    assert manifest['public_core_ready'] is False
    assert manifest['blockers']
    assert (repo / 'src/project_watch/studio/secret.py').exists()


@pytest.mark.parametrize('relative', ['unregistered.py', 'web/assets/settings.json', 'private.sqlite3',
                                     'studio/unregistered.mjs', 'static/unknown.png'])
def test_unknown_runtime_files_fail_before_staging(tmp_path, relative):
    repo = fixture(tmp_path)
    path = repo / 'src/project_watch' / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('synthetic')
    with pytest.raises(ValueError):
        stage(repo, tmp_path / 'candidate')
    assert not (tmp_path / 'candidate').exists()


def test_exact_brand_and_dsh_assets_are_included_without_a_suffix_wildcard(tmp_path):
    from scripts.build_core_probe import RUNTIME_ASSETS
    repo = fixture(tmp_path)
    for relative in RUNTIME_ASSETS:
        path = repo / 'src' / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'synthetic runtime asset')
    assert set(inventory(repo)['files']) >= RUNTIME_ASSETS


def test_existing_or_in_repo_destinations_are_never_modified(tmp_path):
    repo = fixture(tmp_path)
    dest = tmp_path / 'existing'
    dest.mkdir()
    (dest / 'keep.txt').write_text('synthetic')
    for target in (dest, repo / 'candidate', Path('relative-candidate')):
        with pytest.raises(ValueError):
            stage(repo, target)
    assert (dest / 'keep.txt').read_text() == 'synthetic'


def test_stale_registration_is_not_silently_omitted(tmp_path):
    repo = fixture(tmp_path)
    path = repo / 'scripts/layer_map.json'
    data = json.loads(path.read_text())
    data['modules']['project_watch.missing'] = 'Core'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        inventory(repo)


def test_text_secrets_are_rejected_without_echoing_value(tmp_path):
    repo = fixture(tmp_path)
    token = 'ghp_' + 'A' * 36
    (repo / 'src/project_watch/studio/core.py').write_text(f'TOKEN = "{token}"')
    with pytest.raises(ValueError) as caught:
        stage(repo, tmp_path / 'candidate')
    assert token not in str(caught.value)


def test_registered_probe_set_matches_independent_runtime_guards():
    from test_core_without_insight import INSIGHT_MODULES
    repo = Path(__file__).resolve().parents[1]
    excluded = inventory(repo)['excluded_modules']
    assert excluded == sorted('project_watch.studio.' + name for name in INSIGHT_MODULES)
    assert 'project_watch.studio.insight_generation' in excluded
    assert 'project_watch.studio.brief_analysis' in excluded
    assert 'project_watch.studio.crosscheck_analysis' in excluded
    assert 'project_watch.studio.pipeline:Pipeline.run_brief' not in inventory(repo)['mixed_private_symbols']
    assert 'project_watch/studio/insight.py' in inventory(repo)['files']


@pytest.mark.parametrize('change', ['missing', 'extra', 'modified'])
def test_wheel_inventory_and_hashes_are_checked(tmp_path, change):
    repo = fixture(tmp_path)
    manifest = stage(repo, tmp_path / 'candidate')
    wheel = tmp_path / 'synthetic.whl'
    with ZipFile(wheel, 'w') as archive:
        for relative in manifest['files']:
            if change == 'missing' and relative.endswith('core.py'):
                continue
            data = (tmp_path / 'candidate/source/src' / relative).read_bytes()
            archive.writestr(relative, b'# changed' if change == 'modified' else data)
        if change == 'extra':
            archive.writestr('project_watch/studio/secret.py', '# synthetic private module')
    with pytest.raises(ValueError):
        verify_wheel(wheel, manifest)


def test_runtime_junction_is_not_followed(tmp_path):
    repo = fixture(tmp_path)
    link = repo / 'src/project_watch/private_link'
    target = tmp_path / 'outside'
    target.mkdir()
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip('symlink privileges unavailable')
    with pytest.raises(ValueError):
        stage(repo, tmp_path / 'candidate')


def test_declared_license_is_staged_and_hash_checked_in_wheel(tmp_path):
    repo = fixture(tmp_path)
    path = repo / 'pyproject.toml'
    path.write_text(path.read_text() + 'license = "Apache-2.0"\nlicense-files = ["LICENSE"]\n')
    payload = b'Apache License\nSynthetic license fixture\n'
    (repo / 'LICENSE').write_bytes(payload)
    manifest = stage(repo, tmp_path / 'candidate')
    assert (tmp_path / 'candidate/source/LICENSE').read_bytes() == payload
    assert 'LICENSE' in manifest['staged_sha256']
    wheel = tmp_path / 'synthetic.whl'
    for variant in ('correct', 'missing', 'changed'):
        with ZipFile(wheel, 'w') as archive:
            for relative in manifest['files']:
                archive.write(tmp_path / 'candidate/source/src' / relative, relative)
            if variant != 'missing':
                archive.writestr('synthetic.dist-info/licenses/LICENSE', payload if variant == 'correct' else b'changed')
        if variant == 'correct':
            verify_wheel(wheel, manifest)
        else:
            with pytest.raises(ValueError, match='license'):
                verify_wheel(wheel, manifest)


@pytest.mark.parametrize('declared', [['../LICENSE'], ['*'], ['settings.json'], ['LICENSE', 'LICENSE']])
def test_license_inputs_are_explicit_and_safe(tmp_path, declared):
    repo = fixture(tmp_path)
    path = repo / 'pyproject.toml'
    path.write_text(path.read_text() + f'license-files = {json.dumps(declared)}\n')
    with pytest.raises(ValueError, match='license'):
        stage(repo, tmp_path / 'candidate')
    assert not (tmp_path / 'candidate').exists()


def test_missing_declared_license_fails_before_staging(tmp_path):
    repo = fixture(tmp_path)
    path = repo / 'pyproject.toml'
    path.write_text(path.read_text() + 'license-files = ["LICENSE"]\n')
    with pytest.raises(ValueError, match='license'):
        stage(repo, tmp_path / 'candidate')
    assert not (tmp_path / 'candidate').exists()

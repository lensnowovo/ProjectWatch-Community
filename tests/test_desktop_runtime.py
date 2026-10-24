"""Local runtime selection only; never executes Node, providers or the real home."""
import json
import os

import pytest

from project_watch.studio import public_executor
from project_watch.studio.desktop import configure_runtime

DSH_ENV = ('PROJECT_WATCH_DSH_RUNTIME', 'PROJECT_WATCH_DSH_MONITOR')


@pytest.fixture(autouse=True)
def clean_executor_env(monkeypatch):
    # configure_runtime writes os.environ directly. Record every key it may touch
    # (setenv first, so even absent keys are restored) to keep later tests clean.
    monkeypatch.setenv('PATH', os.environ.get('PATH', ''))
    for key in (public_executor.EXECUTOR, public_executor.SOURCE, public_executor.UNAVAILABLE,
                'PROJECT_WATCH_NODE_VERSION', *DSH_ENV):
        monkeypatch.setenv(key, 'recorded')
        monkeypatch.delenv(key)


def make_runtime(root, *, version='0.2.1-rc.2', entry=True):
    package = root/'node_modules/@deepseek-ai/dsh'
    (package/'lib').mkdir(parents=True)
    (package/'package.json').write_text(json.dumps({'version': version}))
    if entry:
        (package/'lib/bin.js').write_text('// synthetic')
    return root


def make_bundle(root, *, dsh=True, node=True, **runtime):
    root.mkdir(parents=True)
    manifest = {'format': 'projectwatch-windows-bundle-v1'}
    if dsh:
        manifest['dsh'] = {'version': '0.2.1-rc.2', 'node_version': 'v24.13.1'}
        make_runtime(root/'dsh', **runtime)
    if node:
        (root/'node').mkdir()
        (root/'node/node.exe').write_bytes(b'synthetic never executed')
    (root/'bundle-manifest.json').write_text(json.dumps(manifest))
    return root


def test_absent_keeps_existing_opt_in_for_source_runs(tmp_path, monkeypatch):
    monkeypatch.setenv('PROJECT_WATCH_DSH_RUNTIME', 'inherited')
    monkeypatch.setenv('PROJECT_WATCH_DSH_MONITOR', '1')
    assert configure_runtime(tmp_path, bundle=None) == 'dsh'
    assert os.environ['PROJECT_WATCH_DSH_RUNTIME'] == 'inherited'
    assert os.environ[public_executor.SOURCE] == 'environment'


def test_source_run_without_opt_in_is_classic(tmp_path, monkeypatch):
    for key in DSH_ENV:
        monkeypatch.delenv(key, raising=False)
    assert configure_runtime(tmp_path, bundle=None) == 'classic'


def test_explicit_disabled_overrides_inherited_and_bundle(tmp_path, monkeypatch):
    monkeypatch.setenv('PROJECT_WATCH_DSH_RUNTIME', 'inherited')
    monkeypatch.setenv('PROJECT_WATCH_DSH_MONITOR', '1')
    (tmp_path/'desktop-runtime.json').write_text('{"dsh_enabled":false}')
    assert configure_runtime(tmp_path, bundle=make_bundle(tmp_path/'bundle')) == 'classic'
    assert not any(key in os.environ for key in DSH_ENV)
    assert public_executor.unavailable_reason() is None


def test_bundled_runtime_is_enabled_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv('PATH', 'original')
    bundle = make_bundle(tmp_path/'versions/1')
    assert configure_runtime(tmp_path/'home', bundle=bundle) == 'dsh'
    assert os.environ['PROJECT_WATCH_DSH_RUNTIME'] == str((bundle/'dsh').resolve())
    assert os.environ['PROJECT_WATCH_DSH_MONITOR'] == '1'
    assert os.environ['PATH'].split(os.pathsep)[0] == str((bundle/'node').resolve())
    view = public_executor.current(tmp_path/'home')
    assert view['executor'] == 'dsh' and view['source'] == 'bundled' and view['configurable']
    assert view['node_version'] == 'v24.13.1'


def test_enabled_without_paths_uses_bundle(tmp_path):
    (tmp_path/'desktop-runtime.json').write_text('{"dsh_enabled":true}')
    assert configure_runtime(tmp_path, bundle=make_bundle(tmp_path/'bundle')) == 'dsh'
    assert os.environ[public_executor.SOURCE] == 'bundled'


def test_older_bundle_without_dsh_stays_classic(tmp_path, monkeypatch):
    monkeypatch.setenv('PROJECT_WATCH_DSH_RUNTIME', 'inherited')
    monkeypatch.setenv('PROJECT_WATCH_DSH_MONITOR', '1')
    assert configure_runtime(tmp_path, bundle=make_bundle(tmp_path/'bundle', dsh=False)) == 'classic'
    assert not any(key in os.environ for key in DSH_ENV)


@pytest.mark.parametrize(('fault', 'reason'), [
    ('node', 'node_missing'), ('version', 'version_mismatch'), ('entry', 'bundled_missing'),
])
def test_damaged_bundle_degrades_and_still_starts(tmp_path, monkeypatch, fault, reason):
    monkeypatch.setenv('PROJECT_WATCH_DSH_RUNTIME', 'inherited')
    monkeypatch.setenv('PROJECT_WATCH_DSH_MONITOR', '1')
    monkeypatch.setenv('PATH', 'original')
    bundle = make_bundle(tmp_path/'bundle', node=fault != 'node',
                         version='wrong' if fault == 'version' else '0.2.1-rc.2', entry=fault != 'entry')
    assert configure_runtime(tmp_path, bundle=bundle) == 'unavailable'
    # No legacy route is left armed and nothing is added to PATH.
    assert not any(key in os.environ for key in DSH_ENV)
    assert os.environ['PATH'] == 'original'
    assert public_executor.unavailable_reason() == reason
    with pytest.raises(ValueError, match='dsh_unavailable'):
        public_executor.require_available()
    assert public_executor.current(tmp_path)['reason_text']


def test_missing_dsh_directory_degrades(tmp_path):
    bundle = make_bundle(tmp_path/'bundle', dsh=False)
    manifest = json.loads((bundle/'bundle-manifest.json').read_text())
    (bundle/'bundle-manifest.json').write_text(json.dumps({**manifest, 'dsh': {'version': '0.2.1-rc.2'}}))
    assert configure_runtime(tmp_path, bundle=bundle) == 'unavailable'
    assert public_executor.unavailable_reason() == 'bundled_missing'


@pytest.mark.parametrize('fault', [None, 'version', 'entry', 'node', 'relative', 'type', 'malformed'])
def test_custom_paths_validate_before_mutating(tmp_path, monkeypatch, fault):
    monkeypatch.setenv('PROJECT_WATCH_DSH_RUNTIME', 'inherited')
    monkeypatch.setenv('PROJECT_WATCH_DSH_MONITOR', '0')
    monkeypatch.setenv('PATH', 'original')
    root = make_runtime(tmp_path/'runtime with spaces', version='wrong' if fault == 'version' else '0.2.1-rc.2',
                        entry=fault != 'entry')
    node = tmp_path/'node.exe'
    if fault != 'node':
        node.write_bytes(b'synthetic never executed')
    config = {'dsh_enabled': 'yes' if fault == 'type' else True,
              'dsh_runtime': 'relative' if fault == 'relative' else str(root), 'node_executable': str(node)}
    (tmp_path/'desktop-runtime.json').write_text('{' if fault == 'malformed' else json.dumps(config))
    if fault:
        assert configure_runtime(tmp_path, bundle=None) == 'unavailable'
        assert not any(key in os.environ for key in DSH_ENV)
        assert os.environ['PATH'] == 'original'
        assert public_executor.unavailable_reason()
    else:
        assert configure_runtime(tmp_path, bundle=None) == 'dsh'
        assert os.environ['PROJECT_WATCH_DSH_RUNTIME'] == str(root.resolve())
        assert os.environ['PROJECT_WATCH_DSH_MONITOR'] == '1'
        assert os.environ['PATH'].split(os.pathsep)[0] == str(tmp_path.resolve())
        assert os.environ[public_executor.SOURCE] == 'custom'


def test_switch_keeps_custom_paths(tmp_path):
    (tmp_path/'desktop-runtime.json').write_text(json.dumps(
        {'dsh_enabled': True, 'dsh_runtime': 'C:/custom', 'node_executable': 'C:/node.exe', 'extra': 1}))
    public_executor.write_choice(tmp_path, False)
    saved = json.loads((tmp_path/'desktop-runtime.json').read_text())
    assert saved == {'dsh_enabled': False, 'dsh_runtime': 'C:/custom', 'node_executable': 'C:/node.exe'}
    public_executor.write_choice(tmp_path, True)
    assert json.loads((tmp_path/'desktop-runtime.json').read_text())['dsh_enabled'] is True


def shared_layout(root, *, installed):
    """Shared runtimes beside the bundle, or under the install root once installed."""
    version = root/'versions/abc' if installed else root/'bundle'
    version.mkdir(parents=True)
    base = (root if installed else version)/'runtimes'
    make_runtime(base/'dsh-0.2.1-1111111111111111')
    (base/'node-24.13.1-2222222222222222').mkdir(parents=True)
    (base/'node-24.13.1-2222222222222222/node.exe').write_bytes(b'synthetic never executed')
    (version/'bundle-manifest.json').write_text(json.dumps({'dsh': {
        'version': '0.2.1-rc.2', 'node_version': 'v24.13.1',
        'dsh_runtime_id': 'dsh-0.2.1-1111111111111111', 'node_runtime_id': 'node-24.13.1-2222222222222222'}}))
    return version, base


@pytest.mark.parametrize('installed', [False, True])
def test_shared_runtimes_resolve_before_and_after_install(tmp_path, installed):
    version, base = shared_layout(tmp_path, installed=installed)
    assert configure_runtime(tmp_path/'home', bundle=version) == 'dsh'
    assert os.environ['PROJECT_WATCH_DSH_RUNTIME'] == str((base/'dsh-0.2.1-1111111111111111').resolve())
    assert os.environ['PATH'].split(os.pathsep)[0] == str((base/'node-24.13.1-2222222222222222').resolve())

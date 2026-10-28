"""Synthetic macOS layout checks, not Darwin execution or paid-provider tests."""
import json

import pytest

from project_watch.studio import dsh_runtime, public_executor


@pytest.fixture
def mac_bundle(tmp_path, monkeypatch):
    root = tmp_path / '含 空格' / 'ProjectWatch.app/Contents/Resources'
    python = root / 'runtime/bin/python3.12'
    python.parent.mkdir(parents=True)
    python.write_bytes(b'synthetic interpreter')
    monkeypatch.setattr(public_executor.sys, 'platform', 'darwin')
    monkeypatch.setattr(public_executor.sys, 'executable', str(python))
    (root / 'bundle-manifest.json').write_text(json.dumps({
        'format': 'projectwatch-macos-bundle-v1',
        'dsh': {'version': dsh_runtime.DSH_VERSION, 'node_version': 'v24.13.1'},
    }), encoding='utf-8')
    node = root / 'node/bin/node'
    node.parent.mkdir(parents=True)
    node.write_bytes(b'synthetic node')
    node.chmod(0o755)
    package = root / 'dsh/node_modules/@deepseek-ai/dsh'
    (package / 'lib').mkdir(parents=True)
    (package / 'package.json').write_text(json.dumps({'version': dsh_runtime.DSH_VERSION}), encoding='utf-8')
    (package / 'lib/bin.js').write_text('// synthetic only', encoding='utf-8')
    return root


def test_macos_bundled_executor_uses_resources_not_system_node(mac_bundle, tmp_path):
    home = tmp_path / 'isolated-home'
    assert public_executor.bundle_root() == mac_bundle
    state = public_executor.resolve(home, public_executor.bundle_root())
    assert state['executor'] == 'dsh'
    assert state['node'] == str((mac_bundle / 'node/bin/node').resolve())
    assert state['runtime'] == str((mac_bundle / 'dsh').resolve())
    assert not home.exists()


@pytest.mark.parametrize('missing,reason', [
    ('node/bin/node', 'node_missing'),
    ('dsh/node_modules/@deepseek-ai/dsh/lib/bin.js', 'bundled_missing'),
    ('bundle-manifest.json', 'bundled_missing'),
])
def test_damaged_mac_bundle_never_silently_runs_classic(mac_bundle, tmp_path, missing, reason):
    (mac_bundle / missing).unlink()
    assert public_executor.bundle_root() == mac_bundle
    state = public_executor.resolve(tmp_path / 'home', public_executor.bundle_root())
    assert state['executor'] == 'unavailable'
    assert state['reason'] == reason


def test_mac_saved_disabled_choice_preserved(mac_bundle, tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    config = home / 'desktop-runtime.json'
    original = b'{"dsh_enabled":false}'
    config.write_bytes(original)
    assert public_executor.resolve(home, mac_bundle) == {'executor': 'classic', 'source': 'setting'}
    assert config.read_bytes() == original


def test_legacy_mac_without_dsh_metadata_stays_classic(mac_bundle, tmp_path):
    (mac_bundle / 'bundle-manifest.json').write_text(
        '{"format":"projectwatch-macos-bundle-v1"}', encoding='utf-8')
    assert public_executor.resolve(tmp_path / 'home', mac_bundle)['executor'] == 'classic'


def test_mac_node_without_execute_permission_is_unavailable(mac_bundle, tmp_path, monkeypatch):
    monkeypatch.setattr(public_executor.os, 'access', lambda *_: False)
    state = public_executor.resolve(tmp_path / 'home', mac_bundle)
    assert state['executor'] == 'unavailable' and state['reason'] == 'node_missing'


def test_developer_python_not_misidentified_as_mac_bundle(tmp_path, monkeypatch):
    python = tmp_path / 'venv/bin/python'
    python.parent.mkdir(parents=True)
    python.write_bytes(b'synthetic')
    monkeypatch.setattr(public_executor.sys, 'platform', 'darwin')
    monkeypatch.setattr(public_executor.sys, 'executable', str(python))
    assert public_executor.bundle_root() is None

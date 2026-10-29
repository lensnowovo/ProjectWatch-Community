"""Exact-base, offline update transactions with synthetic data only."""
import json
import shutil
import sqlite3
import zipfile
from pathlib import Path

import pytest
from test_windows_install import hashes, home_at, with_runtime

from scripts.build_windows_update import build
from scripts.windows import maintain as m
from scripts.windows.apply_update import apply, validate_manifest


def prepared(tmp_path, *, changed_runtime=False):
    before, rid = with_runtime(tmp_path)
    after, new_rid = with_runtime(tmp_path, 'two', content=b'new dsh' if changed_runtime else b'synthetic dsh')
    # Exercise added dependencies and removed files without any product call.
    old = before/'obsolete.py'
    old.write_bytes(b'old only')
    new = after/'pdf.py'
    new.write_bytes(b'new dependency')
    for folder, extra in ((before, old), (after, new)):
        p = folder/'bundle-manifest.json'
        v = json.loads(p.read_text(encoding='utf-8'))
        v['files'][extra.name] = m.digest(extra)
        p.write_text(json.dumps(v), encoding='utf-8')
    home, root = home_at(tmp_path), tmp_path/'program'
    with sqlite3.connect(home/'projectwatch.sqlite3') as db:
        db.execute('create table requests (id text, status text)')
        db.execute("insert into requests values ('old-unknown', 'unknown')")
    state = m.install(before, root, home)
    output, package = tmp_path/'update.zip', tmp_path/'unpacked'
    receipt = build(before, after, output)
    with zipfile.ZipFile(output) as z:
        z.extractall(package)  # Trusted ZIP generated above, never a downloaded input.
    return root, home, package, receipt, state, rid, new_rid


@pytest.mark.parametrize('changed_runtime', [False, True])
def test_manual_update_preserves_data_old_version_and_unknown(tmp_path, changed_runtime):
    root, home, package, receipt, state, rid, new_rid = prepared(tmp_path, changed_runtime=changed_runtime)
    before = hashes(home)
    shared = root/'runtimes'/rid/'LICENSE'
    stamp = shared.stat().st_mtime_ns
    result = apply(package, root, confirm=True)
    current = m.state_at(root)
    assert result['active'] == current['active'] == receipt['target']
    assert current['public_executor_default'] == 'dsh'
    assert hashes(home) == before
    assert (root/'versions'/state['active']/'obsolete.py').read_bytes() == b'old only'
    active = root/'versions'/result['active']
    assert not (active/'obsolete.py').exists()
    assert (active/'pdf.py').read_bytes() == b'new dependency'
    assert shared.stat().st_mtime_ns == stamp
    assert (root/'runtimes'/new_rid/'.complete').exists()
    m.verify_app(active, m.manifest(package))
    assert Path(current['backup']).is_dir()
    with sqlite3.connect(Path(current['backup'])/'projectwatch.sqlite3') as db:
        assert db.execute('select paused from projects').fetchone() == (1,)
        assert db.execute('select status from requests').fetchone() == ('unknown',)
    # New-version edits must not modify old version files through hard links.
    (active/'ProjectWatch.exe').write_bytes(b'custom')
    assert (root/'versions'/state['active']/'ProjectWatch.exe').read_bytes() == b'synthetic fixed launcher'


@pytest.mark.parametrize('damage', ['base', 'extra', 'modified', 'missing', 'launcher', 'runtime',
                                    'runtime-extra', 'payload', 'support', 'support-extra', 'package-extra',
                                    'manifest', 'existing-target', 'new-runtime'])
def test_invalid_or_custom_install_stops_without_changing_data_or_pointer(tmp_path, damage):
    root, home, package, receipt, state, rid, new_rid = prepared(tmp_path, changed_runtime=True)
    old = root/'versions'/state['active']
    if damage in ('base', 'support-extra'):
        p = package/'update.json'
        value = json.loads(p.read_text(encoding='utf-8'))
        if damage == 'base':
            value['base'] = '0'*64
        else:
            value['support']['extra.py'] = '0'*64
        p.write_text(json.dumps(value), encoding='utf-8')
    elif damage == 'extra':
        (old/'custom.py').write_bytes(b'preserve')
    elif damage == 'modified':
        (old/'runtime/pythonw.exe').write_bytes(b'custom')
    elif damage == 'missing':
        (old/'runtime/pythonw.exe').unlink()
    elif damage == 'launcher':
        (root/'ProjectWatch.exe').write_bytes(b'custom')
    elif damage == 'runtime':
        (root/'runtimes'/rid/'LICENSE').write_bytes(b'custom')
    elif damage == 'runtime-extra':
        (root/'runtimes'/rid/'custom.js').write_bytes(b'custom')
    elif damage == 'payload':
        (package/'payload/pdf.py').write_bytes(b'broken')
    elif damage == 'support':
        (package/'maintain.py').write_bytes(b'broken')
    elif damage == 'package-extra':
        (package/'extra.py').write_bytes(b'broken')
    elif damage == 'manifest':
        (package/'bundle-manifest.json').write_text('{}', encoding='utf-8')
    elif damage == 'existing-target':
        target = root/'versions'/receipt['target']
        target.mkdir()
        (target/'custom.py').write_bytes(b'preserve')
    else:
        # A forged completion marker must not make an unrelated runtime trusted.
        target = root/'runtimes'/new_rid
        shutil.copytree(package/'payload/runtimes'/new_rid, target)
        files = m.manifest(package)['runtimes'][new_rid]['files']
        (target/'.complete').write_text(m.runtime_digest(files), encoding='utf-8')
        (target/'LICENSE').write_bytes(b'corrupt')
    pointer, data = (root/'install-state.json').read_bytes(), hashes(home)
    with pytest.raises((ValueError, OSError)):
        apply(package, root, confirm=True)
    assert (root/'install-state.json').read_bytes() == pointer
    assert hashes(home) == data


@pytest.mark.parametrize('held', ['program', 'desktop', 'home'])
def test_running_instance_blocks_update(tmp_path, held):
    root, home, package, *_ = prepared(tmp_path)
    pointer = (root/'install-state.json').read_bytes()
    target = {'program': root, 'desktop': home/'desktop-session', 'home': home}[held]
    with m.lock(target/'.studio.lock'), pytest.raises(OSError):
        apply(package, root, confirm=True)
    assert (root/'install-state.json').read_bytes() == pointer


@pytest.mark.parametrize('failure', ['backup', 'copy', 'activation'])
def test_interrupted_transaction_keeps_old_active_and_data(tmp_path, monkeypatch, failure):
    root, home, package, *_ = prepared(tmp_path)
    pointer, data = (root/'install-state.json').read_bytes(), hashes(home)
    def fail(*a, **kw):
        raise OSError('synthetic disk failure')
    if failure == 'backup':
        monkeypatch.setattr(m, 'snapshot', fail)
    elif failure == 'copy':
        monkeypatch.setattr(shutil, 'copy2', fail)
    else:
        real = m.atomic
        monkeypatch.setattr(m, 'atomic', lambda p, v: fail() if p.name == 'install-state.json' else real(p, v))
    with pytest.raises(OSError):
        apply(package, root, confirm=True)
    assert (root/'install-state.json').read_bytes() == pointer
    assert hashes(home) == data


@pytest.mark.parametrize('name', ['../x', 'runtime/CON.txt', 'runtime/NUL', 'runtime/x:y',
                                  'runtime/a?', 'runtime/A.', 'ProjectWatch.exe/x',
                                  'bundle-manifest.json', 'runtimes/x', 'RUNTIMES/x', 'runtimes'])
def test_unsafe_target_paths_rejected(tmp_path, name):
    with pytest.raises(ValueError):
        validate_manifest({'files': {'ProjectWatch.exe': '0'*64, name: '1'*64}}, tmp_path)


@pytest.mark.parametrize('change', ['python', 'launcher_source_sha256', 'version'])
def test_builder_requires_full_installer_for_incompatible_updates(tmp_path, change):
    one, _ = with_runtime(tmp_path)
    two, _ = with_runtime(tmp_path, 'two')
    p = two/'bundle-manifest.json'
    value = m.manifest(two)
    value[change] = '0.1.0' if change == 'version' else 'changed'
    p.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError):
        build(one, two, tmp_path/'bad.zip')
    assert not (tmp_path/'bad.zip').exists()


def test_no_confirmation_cannot_mutate_or_start_anything(tmp_path):
    with pytest.raises(ValueError, match='confirmation'):
        apply(tmp_path/'package', tmp_path/'program')
    assert not list(tmp_path.iterdir())


def test_build_needs_only_manifests_and_changed_bytes(tmp_path):
    root, _home, _package, receipt, state, _rid, _ = prepared(tmp_path)
    # Old bundle bytes and unchanged target runtime bytes are not transmitted.
    # The installed old version is the source of truth for reconstructing them.
    shutil.rmtree(tmp_path/'one/runtime')
    shutil.rmtree(tmp_path/'one/runtimes')
    shutil.rmtree(tmp_path/'two/runtimes')
    output = tmp_path/'sparse-update.zip'
    again = build(tmp_path/'one', tmp_path/'two', output)
    assert again['base'] == state['active'] and again['target'] == receipt['target']
    sparse = tmp_path/'sparse'
    with zipfile.ZipFile(output) as z:
        z.extractall(sparse)
        assert not any(n.startswith('payload/runtimes/') for n in z.namelist())
    assert apply(sparse, root, confirm=True)['active'] == receipt['target']


def test_changed_payload_corruption_is_rejected_during_packaging(tmp_path):
    one, _ = with_runtime(tmp_path)
    two, _ = with_runtime(tmp_path, 'two')
    (two/'runtime/pythonw.exe').write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='Input changed'):
        build(one, two, tmp_path/'bad.zip')

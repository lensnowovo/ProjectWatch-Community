import json

import pytest

from scripts.audit_windows_distribution import audit
from scripts.windows.maintain import digest, runtime_digest


def fixture(tmp_path):
    info = tmp_path/'runtime/Lib/site-packages/example-1.0.dist-info'
    info.mkdir(parents=True)
    (info/'METADATA').write_text('Name: example\nVersion: 1.0\nLicense-Expression: MIT\n')
    (info/'LICENSE').write_text('SYNTHETIC LICENSE')
    (info/'RECORD').write_text('example-1.0.dist-info/LICENSE,,\n')
    files = {p.relative_to(tmp_path).as_posix(): digest(p) for p in tmp_path.rglob('*') if p.is_file()}
    (tmp_path/'bundle-manifest.json').write_text(json.dumps({
        'format': 'projectwatch-windows-bundle-v1', 'version': 'test', 'python': 'test', 'files': files}))
    return info


def test_inventory_uses_bundle_not_installed_environment(tmp_path):
    fixture(tmp_path)
    result = audit(tmp_path)
    assert [p['name'] for p in result['packages']] == ['example']
    assert result['packages'][0]['license_files']
    assert not result['publication_authorized']


def test_changed_payload_rejected(tmp_path):
    info = fixture(tmp_path)
    (info/'LICENSE').write_text('changed')
    with pytest.raises(ValueError, match='hash mismatch'):
        audit(tmp_path)


def test_unlisted_private_residue_rejected(tmp_path):
    fixture(tmp_path)
    (tmp_path/'settings.json').write_text('SYNTHETIC')
    with pytest.raises(ValueError, match='Unlisted'):
        audit(tmp_path)


@pytest.mark.parametrize('fault', [None, 'changed', 'extra', 'missing'])
def test_shared_runtime_inventory_is_verified_and_counted(tmp_path, fault):
    fixture(tmp_path)
    files = {'node.exe': __import__('hashlib').sha256(b'synthetic node').hexdigest()}
    rid = 'node-24.13.1-' + runtime_digest(files)[:16]
    folder = tmp_path / 'runtimes' / rid
    folder.mkdir(parents=True)
    (folder / 'node.exe').write_bytes(b'synthetic node')
    manifest_path = tmp_path / 'bundle-manifest.json'
    value = json.loads(manifest_path.read_text())
    value['runtimes'] = {rid: {'files': files}}
    manifest_path.write_text(json.dumps(value))
    if fault == 'changed':
        (folder / 'node.exe').write_bytes(b'changed')
    elif fault == 'extra':
        (folder / 'private.txt').write_bytes(b'synthetic residue')
    elif fault == 'missing':
        (folder / 'node.exe').unlink()
    if fault:
        with pytest.raises((ValueError, FileNotFoundError)):
            audit(tmp_path)
    else:
        result = audit(tmp_path)
        assert result['files_verified'] == len(value['files']) + 1
        assert result['shared_runtimes'] == {rid: {'files_verified': 1}}

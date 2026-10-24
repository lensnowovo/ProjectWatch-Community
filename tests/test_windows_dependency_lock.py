import json
from zipfile import ZipFile

import pytest

from scripts.freeze_windows_dependencies import inventory, requirements, verify


def wheel(folder, name, version='1.0', tag='py3-none-any', body=b'synthetic'):
    path = folder / f'{name}-{version}-{tag}.whl'
    with ZipFile(path, 'w') as archive:
        archive.writestr(f'{name}-{version}.dist-info/METADATA',
                         f'Name: {name}\nVersion: {version}\n')
        archive.writestr(f'{name}/__init__.py', body)
    return path


def setup(tmp_path):
    house = tmp_path / 'wheels'
    house.mkdir()
    product = wheel(house, 'projectwatch_local')
    wheel(house, 'synthetic_native', tag='cp313-cp313-win_amd64')
    lock = tmp_path / 'lock.json'
    lock.write_text(json.dumps(inventory(house, product)))
    return house, product, lock


def test_target_not_host_controls_wheel_compatibility(tmp_path):
    house, product, lock = setup(tmp_path)
    value = verify(lock, house, product)
    assert value['target'] == 'cp313-win_amd64'
    text = requirements(value)
    assert 'projectwatch-local[desktop,ocr,mcp,team,postgres]==1.0 --hash=sha256:' in text
    assert text.count('--hash=sha256:') == 2


def test_windows_builder_installs_only_hash_locked_offline_requirements(tmp_path):
    from scripts.build_windows_bundle import dependency_install_command
    command = dependency_install_command(tmp_path / 'wheels', tmp_path / 'requirements.lock', tmp_path / 'site')
    assert '--isolated' in command and '--no-index' in command and '--require-hashes' in command
    assert command[command.index('--python-version') + 1] == '3.13'
    assert command[command.index('--platform') + 1] == 'win_amd64'
    assert command[command.index('-r') + 1] == str(tmp_path / 'requirements.lock')


@pytest.mark.parametrize('fault', [None, 'version', 'missing', 'extra'])
def test_installed_dependency_closure_must_match_lock(tmp_path, fault):
    from scripts.freeze_windows_dependencies import verify_installed
    site = tmp_path / 'site'
    site.mkdir()
    if fault != 'missing':
        folder = site / 'synthetic-1.0.dist-info'
        folder.mkdir()
        version = '2.0' if fault == 'version' else '1.0'
        (folder / 'METADATA').write_text(f'Name: synthetic\nVersion: {version}\n')
    if fault == 'extra':
        folder = site / 'extra-1.0.dist-info'
        folder.mkdir()
        (folder / 'METADATA').write_text('Name: extra\nVersion: 1.0\n')
    value = {'wheels': [{'name': 'synthetic', 'version': '1.0'}]}
    if fault:
        with pytest.raises(ValueError, match='differ'):
            verify_installed(value, site)
    else:
        verify_installed(value, site)


@pytest.mark.parametrize('fault', ['changed', 'extra', 'missing', 'duplicate', 'wrong_platform', 'metadata', 'product'])
def test_changed_or_ambiguous_dependency_inputs_fail_closed(tmp_path, fault):
    house, product, lock = setup(tmp_path)
    if fault == 'changed':
        wheel(house, 'synthetic_native', tag='cp313-cp313-win_amd64', body=b'changed')
    elif fault == 'extra':
        wheel(house, 'extra')
    elif fault == 'missing':
        (house / 'synthetic_native-1.0-cp313-cp313-win_amd64.whl').unlink()
    elif fault == 'duplicate':
        wheel(house, 'synthetic_native', version='2.0')
    elif fault == 'wrong_platform':
        wheel(house, 'foreign', tag='cp313-cp313-macosx_13_0_arm64')
    elif fault == 'metadata':
        product.rename(house / 'projectwatch_local-2.0-py3-none-any.whl')
    else:
        product = wheel(tmp_path, 'projectwatch_local', body=b'another build')
    with pytest.raises(ValueError):
        verify(lock, house, product)

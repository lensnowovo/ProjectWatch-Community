"""Synthetic build input validation; no downloads or compiler invocation."""
import hashlib
from zipfile import ZipFile

import pytest

from scripts.build_windows_bundle import extract_runtime, validate_output, verify_product


def test_existing_output_is_not_overwritten(tmp_path):
    dest = tmp_path/'existing'
    dest.mkdir()
    (dest/'keep').write_text('synthetic')
    with pytest.raises(ValueError):
        validate_output(dest, tmp_path/'source')
    assert (dest/'keep').read_text() == 'synthetic'


def test_output_cannot_be_in_source(tmp_path):
    with pytest.raises(ValueError):
        validate_output(tmp_path/'source'/'bundle', tmp_path/'source')


def test_installed_same_version_wrong_product_is_rejected(tmp_path):
    wheel = tmp_path/'synthetic.whl'
    with ZipFile(wheel, 'w') as z:
        z.writestr('project_watch/__init__.py', b'expected')
    site = tmp_path/'site'
    (site/'project_watch').mkdir(parents=True)
    installed = site/'project_watch/__init__.py'
    installed.write_bytes(b'other build')
    with pytest.raises(ValueError, match='differs'):
        verify_product(wheel, site)
    installed.write_bytes(b'expected')
    verify_product(wheel, site)


def test_runtime_hash_checked_before_extraction(tmp_path):
    archive = tmp_path/'runtime.zip'
    with ZipFile(archive, 'w') as z:
        z.writestr('python.exe', b'synthetic')
    with pytest.raises(ValueError, match='hash'):
        extract_runtime(archive, tmp_path/'runtime', '0'*64)
    assert not (tmp_path/'runtime').exists()


@pytest.mark.parametrize('name', ['../escape', '/escape', 'C:/escape', 'foo\\escape', 'foo:bar'])
def test_runtime_paths_are_validated_before_writes(tmp_path, name):
    archive = tmp_path/'runtime.zip'
    with ZipFile(archive, 'w') as z:
        # ZipInfo preserves malformed separators; Windows ZipInfo(name) normalizes them.
        from zipfile import ZipInfo
        member = ZipInfo('placeholder')
        member.filename = name
        z.writestr(member, b'synthetic')
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match='path'):
        extract_runtime(archive, tmp_path/'runtime', digest)
    assert not (tmp_path/'runtime').exists()

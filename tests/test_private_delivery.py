"""Private delivery must bind bytes to a clean source commit, not a label."""
import subprocess
from collections import namedtuple
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from scripts.private_delivery import assemble, wheel_record


def make_wheel(folder, name='greenpine_project_watch', version='0.1.0', body=b'# synthetic\n'):
    path = folder / f'{name}-{version}-py3-none-any.whl'
    with ZipFile(path, 'w') as z:
        z.writestr(f'{name}-{version}.dist-info/METADATA',
                   f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n')
        z.writestr('project_watch/__init__.py' if name == 'greenpine_project_watch'
                   else name + '/__init__.py', body)
    return path


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    # This legacy profile intentionally targets 3.12; universal synthetic wheels
    # exercise its inventory logic on any CI interpreter without changing the
    # real interpreter or relaxing the production platform/version guard.
    from scripts import private_delivery
    version = namedtuple('VersionInfo', 'major minor micro releaselevel serial')
    monkeypatch.setattr(private_delivery, 'sys', SimpleNamespace(version_info=version(3, 12, 0, 'final', 0)))
    monkeypatch.setattr(private_delivery.sysconfig, 'get_platform', lambda: 'win-amd64')
    repo = tmp_path / 'repo'
    repo.mkdir()
    package = repo / 'src/project_watch'
    package.mkdir(parents=True)
    (package / '__init__.py').write_bytes(b'# synthetic\n')
    (repo / 'pyproject.toml').write_text('[project]\nname="greenpine-project-watch"\nversion="0.1.0"\n')
    for args in [('init',), ('add', '.'), ('-c', 'user.name=synthetic', '-c',
                                       'user.email=test@example.test', 'commit', '-m', 'synthetic')]:
        subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True)
    wheels = tmp_path / 'wheels'
    wheels.mkdir()
    product = make_wheel(wheels)
    make_wheel(wheels, 'synthetic_dependency', '1.2.3')
    return repo, wheels, product, tmp_path / 'delivery'


def test_assemble_pins_files_and_preserves_private_boundary(fixture):
    repo, wheels, product, dest = fixture
    result = assemble(repo, product, wheels, dest, profile='full')
    assert result['publication_authorized'] is False
    assert result['independent_windows_accepted'] is False
    assert len(result['commit']) == 40
    assert len(result['wheels']) == 2
    lock = (dest / 'requirements.lock').read_text(encoding='utf-8')
    assert 'greenpine-project-watch==0.1.0 --hash=sha256:' in lock
    assert 'synthetic-dependency==1.2.3 --hash=sha256:' in lock
    assert '--no-index' in (dest / 'INSTALL.md').read_text(encoding='utf-8')


@pytest.mark.parametrize('fault', ['dirty', 'wrong_bytes', 'missing_module', 'non_wheel', 'duplicate', 'exists'])
def test_rejects_ambiguous_or_drifted_delivery(fixture, fault):
    repo, wheels, product, dest = fixture
    if fault == 'dirty':
        (repo / 'unreviewed.txt').write_text('synthetic')
    elif fault == 'wrong_bytes':
        make_wheel(wheels, body=b'# different\n')
    elif fault == 'missing_module':
        with ZipFile(product, 'w') as z:
            z.writestr('greenpine_project_watch-0.1.0.dist-info/METADATA',
                       'Name: greenpine-project-watch\nVersion: 0.1.0\n')
    elif fault == 'non_wheel':
        (wheels / 'secret.env').write_text('synthetic')
    elif fault == 'duplicate':
        make_wheel(wheels, 'synthetic_dependency', '2.0')
    else:
        dest.mkdir()
    with pytest.raises(ValueError):
        assemble(repo, product, wheels, dest, profile='full')
    if fault != 'exists':
        assert not dest.exists()


def test_rejects_metadata_filename_mismatch(tmp_path):
    path = tmp_path / 'other-1.0-py3-none-any.whl'
    with ZipFile(path, 'w') as z:
        z.writestr('other-1.0.dist-info/METADATA', 'Name: synthetic\nVersion: 1.0\n')
    with pytest.raises(ValueError):
        wheel_record(path)


def test_rejects_old_and_new_product_distributions_together(fixture):
    repo, wheels, product, dest = fixture
    make_wheel(wheels, 'projectwatch_local')
    with pytest.raises(ValueError, match='co-installed'):
        assemble(repo, product, wheels, dest, profile='full')
    assert not dest.exists()


def test_rejects_destination_inside_repository(fixture):
    repo, wheels, product, _ = fixture
    with pytest.raises(ValueError):
        assemble(repo, product, wheels, repo / 'delivery', profile='full')


def test_rejects_other_platform_before_writing(fixture, monkeypatch):
    repo, wheels, product, dest = fixture
    monkeypatch.setattr('scripts.private_delivery.sysconfig.get_platform', lambda: 'linux-x86_64')
    with pytest.raises(ValueError, match=r'Windows x64 Python 3\.12'):
        assemble(repo, product, wheels, dest, profile='full')
    assert not dest.exists()


@pytest.mark.parametrize('minor', [11, 13])
def test_legacy_profile_rejects_other_python_before_writing(fixture, monkeypatch, minor):
    from scripts import private_delivery
    repo, wheels, product, dest = fixture
    monkeypatch.setattr(private_delivery.sys, 'version_info', private_delivery.sys.version_info._replace(minor=minor))
    with pytest.raises(ValueError, match=r'Windows x64 Python 3\.12'):
        assemble(repo, product, wheels, dest, profile='full')
    assert not dest.exists()


def test_rejects_source_drift_during_validation(fixture, monkeypatch):
    from scripts import private_delivery
    repo, wheels, product, dest = fixture
    original = private_delivery.git
    reads = 0

    def drift(repo, *args):
        nonlocal reads
        if args[0] == 'status':
            reads += 1
            if reads == 2:
                return b'?? concurrent-edit.txt\n'
        return original(repo, *args)

    monkeypatch.setattr(private_delivery, 'git', drift)
    with pytest.raises(ValueError, match='Source changed'):
        assemble(repo, product, wheels, dest, profile='full')
    assert not dest.exists()

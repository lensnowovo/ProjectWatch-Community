from zipfile import ZipFile

import pytest

from scripts.build_manifest import verify_source_correspondence

DIST_INFO = 'projectwatch_local-1.0.dist-info'
METADATA = '''Metadata-Version: 2.4
Name: projectwatch-local
Version: 1.0
Requires-Python: >=3.11
Provides-Extra: desktop
Requires-Dist: example<7,>=6.2
Requires-Dist: widget; python_version >= "3.11" and extra == "desktop"

Example project.
'''


def write_distribution(tmp_path, archive, metadata=METADATA):
    (tmp_path / 'pyproject.toml').write_text('''[project]
name = "projectwatch-local"
version = "1.0"
readme = "README.md"
requires-python = ">=3.11"
dependencies = ["example>=6.2,<7"]
license-files = ["NOTICE"]
[project.optional-dependencies]
desktop = ["widget; python_version >= '3.11'"]
''', encoding='utf-8')
    (tmp_path / 'README.md').write_text('Example project.\n', encoding='utf-8')
    (tmp_path / 'NOTICE').write_bytes(b'Synthetic license notice')
    archive.writestr(f'{DIST_INFO}/licenses/NOTICE', b'Synthetic license notice')
    archive.writestr(f'{DIST_INFO}/METADATA', metadata)
    archive.writestr(f'{DIST_INFO}/WHEEL', 'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
    archive.writestr(f'{DIST_INFO}/RECORD', '')


@pytest.mark.parametrize('pollution', ['extra', 'missing', 'changed', None])
def test_wheel_matches_frozen_source_not_stale_build(tmp_path, pollution):
    source = tmp_path / 'src/project_watch'
    source.mkdir(parents=True)
    (source / 'app.py').write_bytes(b'# current')
    stale = tmp_path / 'build/lib/project_watch'
    stale.mkdir(parents=True)
    (stale / 'deleted.py').write_bytes(b'# obsolete')
    wheel = tmp_path / 'projectwatch_local-1.0-py3-none-any.whl'
    with ZipFile(wheel, 'w') as z:
        write_distribution(tmp_path, z)
        if pollution != 'missing':
            z.writestr('project_watch/app.py', b'# old' if pollution == 'changed' else b'# current')
        if pollution == 'extra':
            z.writestr('project_watch/deleted.py', b'# obsolete')
    if pollution:
        with pytest.raises(ValueError, match='correspondence failed'):
            verify_source_correspondence(wheel, tmp_path)
    else:
        assert verify_source_correspondence(wheel, tmp_path)['product_files'] == 1


@pytest.mark.parametrize(('before', 'after'), [
    ('>=6.2', '>=6.1'), (' and extra == "desktop"', ''), ('Provides-Extra: desktop', 'Provides-Extra: other'),
    ('Requires-Python: >=3.11', 'Requires-Python: >=3.9'), ('Version: 1.0', 'Version: 0.9'),
    ('Example project.', 'Old project description.'),
])
def test_unchanged_source_rejects_stale_distribution_metadata(tmp_path, before, after):
    wheel = tmp_path / 'projectwatch_local-1.0-py3-none-any.whl'
    source = tmp_path / 'src/project_watch/app.py'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'# current')
    with ZipFile(wheel, 'w') as z:
        write_distribution(tmp_path, z, METADATA.replace(before, after))
        z.write(source, 'project_watch/app.py')
    with pytest.raises(ValueError, match='metadata'):
        verify_source_correspondence(wheel, tmp_path)


@pytest.mark.parametrize('extra', [
    'projectwatch_local-1.0.data/purelib/project_watch/deleted_module.py',
    'startup.pth', 'other.dist-info/METADATA', f'{DIST_INFO}/entry_points.txt',
])
def test_full_wheel_rejects_additional_install_paths(tmp_path, extra):
    wheel = tmp_path / 'projectwatch_local-1.0-py3-none-any.whl'
    source = tmp_path / 'src/project_watch/app.py'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'# current')
    with ZipFile(wheel, 'w') as z:
        write_distribution(tmp_path, z)
        z.write(source, 'project_watch/app.py')
        z.writestr(extra, '# extra')
    with pytest.raises(ValueError, match='inventory'):
        verify_source_correspondence(wheel, tmp_path)

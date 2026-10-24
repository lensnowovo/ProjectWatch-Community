"""Complete developer builds must retain analysis and identify the renamed package."""
import importlib
import json
import tomllib
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from zipfile import ZipFile

import pytest
from packaging.requirements import Requirement

from project_watch.studio import runtime_manifest
from scripts.private_delivery import wheel_record
from scripts.release_audit import product_scope

ROOT = Path(__file__).resolve().parents[1]
NAME = 'projectwatch-local'


def test_developer_package_has_neutral_distribution_name():
    metadata = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    assert metadata['project']['name'] == NAME
    assert metadata['tool']['setuptools']['packages']['find']['where'] == ['src']
    assert 'exclude' not in metadata['tool']['setuptools']['packages']['find']


def test_developer_test_runner_excludes_known_vulnerable_versions():
    metadata = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    requirements = [Requirement(value) for value in metadata['project']['optional-dependencies']['dev']]
    runner = next(value for value in requirements if value.name == 'pytest')
    assert '8.4.2' not in runner.specifier
    assert '9.0.2' not in runner.specifier
    assert '9.0.3' in runner.specifier


def test_developer_setup_updates_package_installer_before_installing():
    for name in ('README.md', 'docs/developer-quickstart.md'):
        guide = (ROOT / name).read_text(encoding='utf-8')
        assert guide.index('pip install --upgrade "pip>=26.2.2"') < guide.index('pip install -e')
    script = (ROOT / 'scripts/start-projectwatch.ps1').read_text(encoding='utf-8')
    upgrade = script.index('--upgrade "pip>=26.2.2"')
    install = script.index('-q -e "$repo[ocr]"')
    assert 'if ($LASTEXITCODE -ne 0)' in script[upgrade:install]


def test_all_existing_insight_modules_remain_importable():
    modules = json.loads((ROOT / 'scripts/layer_map.json').read_text(encoding='utf-8'))['modules']
    analysis = [name for name, layer in modules.items() if layer == 'Insight']
    assert len(analysis) == 15
    for name in analysis:
        assert importlib.import_module(name).__file__


@pytest.mark.parametrize('name', [NAME, 'greenpine-project-watch'])
def test_audit_recognizes_new_and_legacy_product(tmp_path, name):
    (tmp_path / 'pyproject.toml').write_text(f'[project]\nname="{name}"\n')
    assert product_scope(tmp_path) == '.'


def test_runtime_prefers_new_metadata(monkeypatch, tmp_path):
    calls = []
    def lookup(name):
        calls.append(name)
        return '0.2.0a1' if name == NAME else '0.2.0a0'
    monkeypatch.setattr(runtime_manifest, 'version', lookup)
    assert runtime_manifest.snapshot(tmp_path)['package_version'] == '0.2.0a1'
    assert calls == [NAME]


def test_runtime_supports_old_install_metadata(monkeypatch, tmp_path):
    def lookup(name):
        if name == NAME:
            raise PackageNotFoundError(name)
        return '0.2.0a0'
    monkeypatch.setattr(runtime_manifest, 'version', lookup)
    assert runtime_manifest.snapshot(tmp_path)['package_version'] == '0.2.0a0'


@pytest.mark.parametrize('name', ['projectwatch_local', 'greenpine_project_watch'])
def test_delivery_accepts_both_product_names(tmp_path, name):
    wheel = tmp_path / f'{name}-0.2.0a1-py3-none-any.whl'
    with ZipFile(wheel, 'w') as archive:
        archive.writestr(f'{name}-0.2.0a1.dist-info/METADATA',
                         f'Name: {name}\nVersion: 0.2.0a1\n')
        archive.writestr('project_watch/__init__.py', '# synthetic\n')
    assert wheel_record(wheel)['name'] == name.replace('_', '-')


def test_launcher_uses_separate_renamed_environment():
    script = (ROOT / 'scripts/start-projectwatch.ps1').read_text(encoding='utf-8')
    assert 'venv-projectwatch-local' in script
    assert 'installed-projectwatch-local.txt' in script
    assert "version('projectwatch-local')" in script

"""Security floors for release inputs; entirely offline, no registry queries."""
import json
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]


def test_parser_and_desktop_install_exclude_unpatched_image_decoder():
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']
    dependencies = {r.name.lower(): r for r in map(Requirement, project['dependencies'])}
    # PDF/Office parsing may load Pillow even without the desktop extra.
    pillow = dependencies['pillow'].specifier
    assert '12.3.0' not in pillow
    assert '13.0.0' in pillow
    assert '14.0.0' not in pillow
    desktop = [Requirement(r) for r in project['optional-dependencies']['desktop']]
    for requirement in desktop:
        if requirement.name.lower() == 'pillow':
            assert '13.0.0' in requirement.specifier


def test_certificate_bundle_floor_matches_reviewed_release():
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']
    dependencies = {r.name.lower(): r for r in map(Requirement, project['dependencies'])}
    assert '2026.10.19' in dependencies['certifi'].specifier
    assert '2026.10.18' not in dependencies['certifi'].specifier


def test_pdf_parser_floor_and_container_lock_exclude_known_dos():
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']
    pdf = next(r for r in map(Requirement, project['dependencies']) if r.name == 'pypdf')
    assert '6.21.1' not in pdf.specifier and '6.21.2' in pdf.specifier
    assert 'pypdf==6.21.2' in (ROOT / 'requirements-parser.lock').read_text()


def test_pinned_dsh_lock_has_redirect_and_zip_security_fixes():
    package = json.loads((ROOT / 'packaging/dsh/package.json').read_text())
    lock = json.loads((ROOT / 'packaging/dsh/package-lock.json').read_text())
    assert package['dependencies']['@deepseek-ai/dsh'] == '0.2.1-rc.2'
    assert package['overrides']['@modelcontextprotocol/client'] == '2.3.0'
    assert lock['packages']['node_modules/@modelcontextprotocol/client']['version'] == '2.3.0'
    assert lock['packages']['node_modules/@modelcontextprotocol/core']['version'] == '2.3.0'
    for path, entry in lock['packages'].items():
        if path.endswith('/fflate'):
            assert entry['version'] == '0.8.3'
        if path.endswith('/undici'):
            assert entry['version'] == '8.12.2'
        if path.endswith('/zod'):
            assert entry['version'] == '4.6.7'
    assert package['overrides']['undici'] == '8.12.2'
    assert package['overrides']['zod'] == '4.6.7'


def test_vite_lock_matches_reviewed_fixed_dev_server():
    package = json.loads((ROOT / 'frontend/package.json').read_text())
    lock = json.loads((ROOT / 'frontend/package-lock.json').read_text())
    assert package['devDependencies']['vite'] == '6.4.5'
    assert lock['packages']['node_modules/vite']['version'] == '6.4.5'


def test_windows_runtime_security_pins_match_ci():
    from scripts.build_windows_bundle import NODE_VERSION, PYTHON_VERSION

    assert NODE_VERSION == 'v24.22.1'
    assert PYTHON_VERSION == '3.13.17'
    ci = json.loads((ROOT / '.github/workflows/ci.yml').read_text())
    for name in ('python', 'frontend', 'native', 'installed'):
        setup = next(s for s in ci['jobs'][name]['steps'] if s.get('name') == 'Node')
        assert setup['with']['node-version'] == NODE_VERSION.removeprefix('v')

"""Offline CI contracts; no GitHub dispatch or real provider requests."""
import ast
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from zipfile import ZipFile

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('extended', [False, True])
def test_installed_journey_checks_real_runtime_containment(tmp_path, extended):
    if extended and sys.platform != 'win32':
        pytest.skip('Windows extended-length path identity')
    spec = importlib.util.spec_from_file_location('journey_contract', ROOT / 'tests/installed_wheel_smoke.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    runtime = tmp_path / 'runtime'
    package = runtime / 'Lib/site-packages/project_watch'
    package.mkdir(parents=True)
    installed = package / '__init__.py'
    installed.write_text('# synthetic')
    source = tmp_path / 'runtime-other/project_watch/__init__.py'
    source.parent.mkdir(parents=True)
    source.write_text('# synthetic outside runtime')
    tested = Path('\\\\?\\' + str(installed)) if extended else installed
    assert module.within_runtime(tested, runtime)
    assert not module.within_runtime(source, runtime)
    assert not module.within_runtime(runtime / 'missing.py', runtime)
    assert not module.within_runtime(installed, tmp_path / 'missing-runtime')


def test_native_desktop_smoke_does_not_write_into_frozen_runtime():
    tree = ast.parse((ROOT / 'tests/windows_desktop_smoke.py').read_text(encoding='utf-8'))
    commands = [node for node in ast.walk(tree) if isinstance(node, ast.List)
                and any(isinstance(item, ast.Constant) and item.value == '-I' for item in node.elts)]
    assert len(commands) == 2  # Headless/second launch and native-tray launch.
    for command in commands:
        assert any(isinstance(item, ast.Constant) and item.value == '-B' for item in command.elts)


def test_release_versions_match_python_frontend_and_lock():
    from packaging.version import Version

    from project_watch import __version__
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']
    frontend = json.loads((ROOT / 'frontend/package.json').read_text())
    lock = json.loads((ROOT / 'frontend/package-lock.json').read_text())
    assert Version(project['version']) == Version(__version__) == Version(frontend['version'])
    assert lock['version'] == lock['packages']['']['version'] == frontend['version']


def test_ci_has_read_only_pinned_windows_job_with_mac_deferred():
    flow = json.loads((ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8'))
    assert set(flow['on']) == {'pull_request', 'push', 'workflow_dispatch', 'workflow_call'}
    assert flow['permissions'] == {'contents': 'read'}
    assert flow['on']['push']['branches'] == ['main']
    matrix = flow['jobs']['offline']['strategy']['matrix']['include']
    assert {(row['os'], row['arch']) for row in matrix} == {
        ('windows-2022', 'x64'),
    }
    steps = flow['jobs']['offline']['steps']
    for step in steps:
        if 'uses' in step:
            _, pin = step['uses'].rsplit('@', 1)
            assert len(pin) == 40 and all(c in '0123456789abcdef' for c in pin)
        if step.get('uses', '').startswith('actions/checkout@'):
            assert step['with']['persist-credentials'] is False
    text = json.dumps(flow)
    for forbidden in ('pull_request_target', 'secrets.', 'contents: write', 'gh release', 'git push'):
        assert forbidden not in text
    commands = '\n'.join(s.get('run', '') for s in steps)
    for required in ('run_offline_tests.py', '--dsh-runtime', 'check_layer_boundaries.py',
                     'typecheck', 'frontend run build', 'build --wheel', 'installed_wheel_smoke.py', '--full-journey'):
        assert required in commands
    assert not any(s.get('continue-on-error') for s in steps)


def test_candidate_gate_requires_exact_commit_and_never_publishes():
    flow = json.loads((ROOT / '.github/workflows/release-candidate.yml').read_text(encoding='utf-8'))
    assert set(flow['on']) == {'workflow_dispatch'}
    assert flow['on']['workflow_dispatch']['inputs']['expected_sha']['required']
    assert flow['permissions'] == {'contents': 'read'}
    verify = flow['jobs']['verify']
    assert verify['environment'] == 'release-candidate-review'
    checkout = verify['steps'][0]
    assert checkout['with']['persist-credentials'] is False
    assert len(checkout['uses'].split('@')[1]) == 40
    check = verify['steps'][1]
    assert check['env']['EXPECTED_SHA'] == '${{ inputs.expected_sha }}'
    assert 'inputs.' not in check['run']  # Untrusted strings only enter through env.
    assert 'git rev-parse HEAD' in check['run'] and 'throw' in check['run']
    regression = flow['jobs']['regression']
    assert regression == {'needs': 'verify', 'uses': './.github/workflows/ci.yml'}
    assert flow['jobs']['review']['needs'] == ['verify', 'regression']
    raw = json.dumps(flow)
    for forbidden in ('pull_request_target', 'secrets.', 'secrets":', 'contents: write',
                      'gh release', 'git push', 'softprops/action-gh-release', 'continue-on-error'):
        assert forbidden not in raw


@pytest.mark.parametrize('case', ['matched', 'stale', 'injection', 'wrong_trigger'])
def test_candidate_gate_runs_and_rejects_mismatches(case):
    shell = shutil.which('pwsh')
    if not shell or not shutil.which('git'):
        pytest.skip('PowerShell and Git required to exercise workflow gate')
    flow = json.loads((ROOT / '.github/workflows/release-candidate.yml').read_text(encoding='utf-8'))
    command = flow['jobs']['verify']['steps'][1]['run']
    actual = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    env = dict(os.environ, EXPECTED_SHA=actual, GITHUB_SHA=actual)
    expected = (b'Verified candidate source' if case == 'matched'
                else b'Selected commit differs from the reviewed SHA')
    if case == 'stale':
        env['EXPECTED_SHA'] = '0' * 40
    elif case == 'injection':
        env['EXPECTED_SHA'] = "';Write-Output INJECTED_SENTINEL;#"
        expected = b'A full lowercase commit SHA is required'
    elif case == 'wrong_trigger':
        env['GITHUB_SHA'] = '0' * 40
    result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-Command', command],
                            cwd=ROOT, env=env, capture_output=True, timeout=30)
    # PowerShell diagnostics can use the Windows code page rather than UTF-8.
    # Match the ASCII contract in raw bytes; a decoder failure is not a rejection.
    output = re.sub(rb'\x1b\[[0-?]*[ -/]*[@-~]', b'', result.stdout + result.stderr)
    assert (result.returncode == 0) == (case == 'matched'), result.stderr
    assert expected in output, output
    assert b'INJECTED_SENTINEL' not in result.stdout


def test_offline_environment_removes_providers_and_preserves_inputs(tmp_path):
    from scripts.run_offline_tests import clean_environment
    supplied = {'PATH': 'test-path', 'OPENAI_API_KEY': 'synthetic-only',
                'PW_DSH_TEST_RUNTIME': '/old', 'PROJECT_WATCH_HOME': '/existing',
                'AWS_SECRET_ACCESS_KEY': 'synthetic', 'GITHUB_TOKEN': 'synthetic'}
    clean = clean_environment(supplied, tmp_path)
    assert supplied['PROJECT_WATCH_HOME'] == '/existing'
    assert clean['PATH'] == 'test-path'
    assert clean['PROJECT_WATCH_HOME'] == str(tmp_path / 'data')
    for key in ('OPENAI_API_KEY', 'PW_DSH_TEST_RUNTIME', 'AWS_SECRET_ACCESS_KEY', 'GITHUB_TOKEN'):
        assert key not in clean


@pytest.mark.parametrize('host', ['example.com', '8.8.8.8', '192.168.1.1', '100.64.1.2'])
def test_offline_network_guard_blocks_external_hosts_without_connecting(host):
    from scripts.run_offline_tests import network_guard
    with pytest.raises(RuntimeError, match='non-loopback'):
        network_guard('socket.connect', (None, (host, 443)))
    with pytest.raises(RuntimeError, match='non-loopback'):
        network_guard('socket.getaddrinfo', (host, 443, 0, 0, 0))


@pytest.mark.parametrize('host', ['127.0.0.1', '::1', 'localhost'])
def test_offline_network_guard_allows_synthetic_local_servers(host):
    from scripts.run_offline_tests import network_guard
    network_guard('socket.connect', (None, (host, 12345)))
    network_guard('socket.getaddrinfo', (host, 12345, 0, 0, 0))


def test_offline_runner_rejects_existing_output_before_mutating(tmp_path):
    sentinel = tmp_path / 'keep.txt'
    sentinel.write_text('untouched')
    result = subprocess.run([sys.executable, '-I', str(ROOT / 'scripts/run_offline_tests.py'),
                             '--output', str(tmp_path)], capture_output=True, text=True)
    assert result.returncode != 0
    assert 'Output must be new and outside source' in result.stderr
    assert sentinel.read_text() == 'untouched'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['keep.txt']


@pytest.mark.parametrize('fault', [None, 'changed', 'missing', 'extra'])
def test_installed_smoke_verifies_exact_product_bytes(tmp_path, fault):
    from scripts.installed_wheel_smoke import verify_installed_product
    wheel = tmp_path / 'synthetic.whl'
    with ZipFile(wheel, 'w') as archive:
        archive.writestr('project_watch/__init__.py', 'synthetic')
    package = tmp_path / 'project_watch'
    package.mkdir()
    if fault != 'missing':
        (package / '__init__.py').write_text('changed' if fault == 'changed' else 'synthetic')
    if fault == 'extra':
        (package / 'stale.py').write_text('stale')
    if fault is None:
        assert verify_installed_product(wheel, package) == 1
    else:
        with pytest.raises(ValueError, match='differs'):
            verify_installed_product(wheel, package)

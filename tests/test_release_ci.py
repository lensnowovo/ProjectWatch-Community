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
    from scripts.ci_scope import DOMAINS
    flow = json.loads((ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8'))
    assert set(flow['on']) == {'pull_request', 'push', 'workflow_dispatch', 'workflow_call'}
    assert flow['permissions'] == {'contents': 'read'}
    assert flow['on']['push']['branches'] == ['main']
    assert flow['on']['workflow_call']['inputs']['force_full']['default'] is True
    assert flow['on']['workflow_dispatch']['inputs']['force_full']['default'] is True
    jobs = flow['jobs']
    assert set(jobs) == {'plan', *DOMAINS, 'release_checks'}
    for name, job in jobs.items():
        assert job['runs-on'] == 'windows-2022'
        assert job.get('permissions', flow['permissions']) == (
            {'contents': 'read', 'actions': 'read', 'pull-requests': 'read'}
            if name == 'plan' else {'contents': 'read'})
        assert not job.get('continue-on-error')
        for step in job['steps']:
            if 'uses' in step:
                _, pin = step['uses'].rsplit('@', 1)
                assert len(pin) == 40 and all(c in '0123456789abcdef' for c in pin)
            if step.get('uses', '').startswith('actions/checkout@'):
                assert step['with'] == {'persist-credentials': False, 'ref': '${{ github.sha }}'}
            assert not step.get('continue-on-error')
            if 'GITHUB_TOKEN' in step.get('env', {}):
                assert name == 'plan' and step['id'] == 'select'
    text = json.dumps(flow)
    for forbidden in ('pull_request_target', 'secrets.', 'contents: write', 'gh release', 'git push'):
        assert forbidden not in text


def test_ci_independent_budgets_preserve_every_required_gate_and_summary():
    from scripts.ci_scope import CONTRACT_TESTS, DOMAINS, FRONTEND_TESTS, python_command
    flow = json.loads((ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8'))
    jobs = flow['jobs']
    assert jobs['python']['timeout-minutes'] == 180
    assert 'native' not in jobs  # the native DSH domain was retired with the DSH executor
    for domain in DOMAINS:
        assert jobs[domain]['needs'] == 'plan'
        assert jobs[domain]['if'] == "needs.plan.outputs." + domain + " == 'true'"
        assert all('if' not in step for step in jobs[domain]['steps'] if 'run' in step)
    phases = {d: {s['name']: s for s in jobs[d]['steps']} for d in DOMAINS}
    assert phases['python']['Python domain tests']['run'] == python_command('python')
    assert phases['frontend']['Frontend Python contracts']['run'] == python_command('frontend')
    assert not set(CONTRACT_TESTS) & set(FRONTEND_TESTS)
    quick = next(s['run'] for s in jobs['plan']['steps'] if s['name'] == 'CI and document contracts')
    assert all(path in quick for path in CONTRACT_TESTS)
    assert phases['python']['Lint']['run'] == 'python -m ruff check .'
    assert phases['python']['Architecture boundaries']['run'] == 'python scripts/check_layer_boundaries.py'
    for name, command in {'Frontend tests': 'npm --prefix frontend test',
                          'Frontend types': 'npm --prefix frontend run typecheck',
                          'Frontend build': 'npm --prefix frontend run build',
                          'Node regression': 'node --test tests/*.test.mjs'}.items():
        assert phases['frontend'][name]['run'] == command
    installed = phases['installed']
    names = list(installed)
    assert names.index('Frontend build') < names.index('Build full wheel') < names.index('Fresh installed-wheel smoke') < names.index('Full installed synthetic journey')
    assert '$wheel.Count -ne 1' in installed['Fresh installed-wheel smoke']['run']
    assert 'python -m venv' in installed['Fresh installed-wheel smoke']['run']
    assert '--full-journey --wheel $wheel.FullName' in installed['Full installed synthetic journey']['run']
    artifacts = [step for d in DOMAINS for step in jobs[d]['steps'] if step.get('id') in {'ci_upload', 'ci_proof_upload'}]
    assert len({step['with']['name'] for step in artifacts}) == 6
    assert all(step['if'] == 'always()' and step['with']['retention-days'] == 7 for step in artifacts)
    assert {p for step in artifacts if step['id'] == 'ci_upload' for p in step['with']['path'].splitlines()} == {
        '${{ runner.temp }}/offline/pytest*.xml', '${{ runner.temp }}/frontend-python/pytest.xml',
        '${{ runner.temp }}/installed-smoke/receipt.json',
        '${{ runner.temp }}/installed-journey/receipt.json',
    }
    rollup = jobs['release_checks']
    assert rollup['name'] == 'windows-2022 / Python 3.13'
    assert rollup['needs'] == ['plan', *DOMAINS] and rollup['if'] == 'always()'
    assert rollup['steps'][-1]['run'] == 'python -I -X utf8 scripts/ci_scope.py summary'


def test_installed_round_harness_runs_collect_analyze_report(tmp_path):
    import runpy
    support = runpy.run_path(str(ROOT / 'tests/spine_installed_support.py'))
    result = support['run'](tmp_path)
    assert result['jobs'] == ['collect', 'analyze', 'report'] and result['sections']
    assert result['materials']['paper'] >= 1 and result['materials']['patent'] >= 1


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
    assert regression == {'needs': 'verify', 'uses': './.github/workflows/ci.yml',
                          'with': {'force_full': True},
                          'permissions': {'contents': 'read', 'actions': 'read', 'pull-requests': 'read'}}
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


def test_parallel_offline_runner_guards_every_worker_and_runs_serial_tests_alone(tmp_path):
    probe = tmp_path / 'probe'
    probe.mkdir()
    # Its own config: rootdir stays inside the probe and the marker is registered.
    (probe / 'pytest.ini').write_text('[pytest]\nmarkers =\n    serial: timing test\n')
    (probe / 'test_probe.py').write_text('''import os
import socket
import sys

import pytest


@pytest.mark.parametrize('attempt', range(4))
def test_a_worker_cannot_reach_the_network(attempt):
    assert os.environ.get('PYTEST_XDIST_WORKER')  # really inside a worker process
    assert os.environ.get('PYTHONNOUSERSITE') == '1' and sys.flags.utf8_mode == 1
    connection = socket.socket()
    connection.settimeout(1)
    with pytest.raises(RuntimeError, match='non-loopback'):
        connection.connect(('192.0.2.1', 9))  # TEST-NET-1: never routable


@pytest.mark.serial
def test_serial_tests_run_outside_the_workers():
    assert 'PYTEST_XDIST_WORKER' not in os.environ
''')
    output = tmp_path / 'out'
    result = subprocess.run([sys.executable, '-I', str(ROOT / 'scripts/run_offline_tests.py'), '--output', str(output),
                             '--workers', '2', str(probe)], capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '4 passed' in result.stdout and '1 passed' in result.stdout and 'Unknown pytest.mark' not in result.stdout
    assert (output / 'pytest.xml').is_file() and (output / 'pytest-serial.xml').is_file()


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

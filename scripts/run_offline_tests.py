"""Isolated release tests; no provider keys, existing data, or paid calls.

The Python audit hook blocks non-loopback sockets/DNS in this process. It is not
an OS sandbox for arbitrary test subprocesses. Use an ephemeral CI runner or the
reviewed synthetic tests; never interpret this wrapper as permission to run an
untrusted checkout with real credentials.
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROVIDER_PREFIXES = ('PROJECT_WATCH_', 'PW_', 'OPENAI_', 'ANTHROPIC_', 'DEEPSEEK_',
                     'ZHIPU_', 'GLM_', 'BOCHA_', 'TAVILY_', 'XINIU_', 'PHARMCUBE_',
                     'AWS_', 'AZURE_', 'GOOGLE_')


def clean_environment(original, output):
    clean = {key: value for key, value in original.items()
             if not key.upper().startswith(PROVIDER_PREFIXES)
             and not key.upper().endswith(('_API_KEY', '_ACCESS_TOKEN'))
             and key.upper() not in {'GITHUB_TOKEN', 'GH_TOKEN', 'PYTHONPATH', 'PYTHONHOME'}}
    clean.update(PROJECT_WATCH_HOME=str(output / 'data'),
                 LOCALAPPDATA=str(output / 'localappdata'), APPDATA=str(output / 'appdata'),
                 PYTHONDONTWRITEBYTECODE='1')
    return clean


def network_guard(event, args):
    if event == 'socket.connect':
        address = args[1]
        if not isinstance(address, tuple):
            return  # Local Unix-domain socket.
        host = address[0]
    elif event in {'socket.getaddrinfo', 'socket.gethostbyname'}:
        host = args[0]
    else:
        return
    if host in {'localhost', b'localhost'}:
        return
    try:
        allowed = ipaddress.ip_address(host).is_loopback
    except ValueError:
        allowed = False
    if not allowed:
        raise RuntimeError('Offline release tests block non-loopback connections and DNS')


WORKER_GUARD = 'PROJECTWATCH_OFFLINE_WORKER_GUARD'
NO_TESTS = 5  # pytest.ExitCode.NO_TESTS_COLLECTED


def install_guard():
    if not getattr(sys, '_projectwatch_offline_guard', False):
        sys.addaudithook(network_guard)
        sys._projectwatch_offline_guard = True


# Loaded with -p in every xdist worker: workers are separate processes and do not inherit
# the controller's audit hook, so each installs the same guard as soon as it imports this.
if os.environ.get(WORKER_GUARD) == '1':
    install_guard()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New directory outside source')
    parser.add_argument('--workers', default='', help="Parallel workers ('auto' or a number); tests marked serial run afterwards")
    parser.add_argument('tests', nargs='*', default=['tests'])
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() or output.is_relative_to(ROOT) or ROOT.is_relative_to(output):
        raise ValueError('Output must be new and outside source')
    output.mkdir(parents=True)
    clean = clean_environment(os.environ, output)
    if args.workers:
        # Workers start without -I and -X utf8: keep user site-packages out and UTF-8 mode on.
        clean.update({WORKER_GUARD: '1', 'PYTHONNOUSERSITE': '1', 'PYTHONUTF8': '1'})
    os.environ.clear()
    os.environ.update(clean)
    install_guard()
    sys.path[:0] = [str(ROOT), str(ROOT / 'src')]
    os.chdir(ROOT)
    import pytest

    base = ['-q', '--tb=short', '-ra']
    if not args.workers:
        return pytest.main([*base, '--junitxml=' + str(output / 'pytest.xml'), *args.tests])
    parallel = pytest.main([*base, '-p', 'scripts.run_offline_tests', '-n', args.workers, '-m', 'not serial',
                            '--junitxml=' + str(output / 'pytest.xml'), *args.tests])
    if int(parallel) in (2, 3, 4):  # interrupted, internal or usage error: do not start the serial pass
        return int(parallel)
    serial = pytest.main([*base, '-m', 'serial', '--junitxml=' + str(output / 'pytest-serial.xml'), *args.tests])
    codes = {int(parallel), int(serial)}
    if codes == {NO_TESTS}:
        return NO_TESTS
    return max(codes - {NO_TESTS}, default=0)


if __name__ == '__main__':
    raise SystemExit(main())

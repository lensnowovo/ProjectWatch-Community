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
import platform
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New directory outside source')
    parser.add_argument('--dsh-runtime', type=Path, help='Explicit prepared synthetic-test runtime')
    parser.add_argument('tests', nargs='*', default=['tests'])
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() or output.is_relative_to(ROOT) or ROOT.is_relative_to(output):
        raise ValueError('Output must be new and outside source')
    dsh = None
    if args.dsh_runtime:
        sys.path.insert(0, str(ROOT))
        from scripts.prepare_dsh_runtime import verify_prepared

        target = ('win32-x64' if sys.platform == 'win32' else
                  'darwin-arm64' if sys.platform == 'darwin' and platform.machine() == 'arm64' else None)
        if target is None:
            raise ValueError('Official DSH release testing requires a supported native host')
        dsh = args.dsh_runtime.resolve()
        verify_prepared(dsh, target=target)
    output.mkdir(parents=True)
    clean = clean_environment(os.environ, output)
    os.environ.clear()
    os.environ.update(clean)
    if dsh:
        os.environ['PW_DSH_TEST_RUNTIME'] = str(dsh)
    sys.addaudithook(network_guard)
    sys.path[:0] = [str(ROOT), str(ROOT / 'src')]
    os.chdir(ROOT)
    import pytest

    return pytest.main(['-q', '--tb=short', '-ra', '--junitxml=' + str(output / 'pytest.xml'),
                        *args.tests])


if __name__ == '__main__':
    raise SystemExit(main())

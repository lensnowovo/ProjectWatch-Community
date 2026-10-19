"""Windows launch preflight with mocked commands, never installs or starts apps."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which(os.environ.get('PW_TEST_POWERSHELL', 'pwsh')) or shutil.which('powershell')


def run_functions(body):
    if not POWERSHELL:
        pytest.skip('PowerShell not installed; Windows launcher runtime tests require PowerShell')
    path = str(ROOT / 'scripts/start-projectwatch.ps1').replace("'", "''")
    command = f"""
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$ErrorActionPreference = 'Stop'
$tokens = $null; $parseErrors = $null
$tree = [System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) {{ throw 'Launcher parse failure' }}
$tree.FindAll({{ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }}, $true) |
    ForEach-Object {{ Invoke-Expression $_.Extent.Text }}
{body}
"""
    result = subprocess.run([POWERSHELL, '-NoProfile', '-NonInteractive', '-Command', command],
                            capture_output=True, encoding='utf-8', check=False)
    assert result.returncode == 0, result.stderr + result.stdout
    return result.stdout


def test_missing_python_does_not_run_an_installer_in_preflight():
    output = run_functions('''
function py { $global:LASTEXITCODE = 1 }
function python { $global:LASTEXITCODE = 1 }
if ($null -ne (Find-Python)) { throw 'Unexpected Python detection' }
Write-Output 'missing-python-detected'
''')
    assert 'missing-python-detected' in output
    source = (ROOT / 'scripts/start-projectwatch.ps1').read_text(encoding='utf-8-sig')
    assert 'https://www.python.org/downloads/' in source and 'Add to PATH' in source


def test_missing_dependency_prints_repair_command():
    output = run_functions('''
function SyntheticPython { $global:LASTEXITCODE = 1 }
if (Test-Dependencies 'SyntheticPython' 'D:\\synthetic-repo') { throw 'Broken dependency accepted' }
''')
    assert '依赖不完整' in output and '-m pip install' in output


@pytest.mark.parametrize('all_busy', [False, True])
def test_busy_ports_give_action_without_stopping_existing_services(all_busy):
    condition = '$false' if all_busy else '$Port -ne 8796'
    expected = '$false' if all_busy else '$true'
    output = run_functions(f'''
function Test-PortAvailable {{ param([int]$Port) return {condition} }}
if ((Test-StartupPort 8796) -ne {expected}) {{ throw 'Wrong port preflight result' }}
''')
    if all_busy:
        assert '--port' in output and '不要停止' in output
    else:
        assert '8797' in output and '占用' in output


def test_launcher_does_not_require_wsl_docker_or_database():
    source = (ROOT / 'scripts/start-projectwatch.ps1').read_text(encoding='utf-8-sig').lower()
    assert 'wsl.exe' not in source and 'docker.exe' not in source and 'postgres' not in source
    cmd = (ROOT / 'Start-ProjectWatch.cmd').read_text(encoding='utf-8')
    assert '%~dp0scripts\\start-projectwatch.ps1' in cmd
    assert 'if errorlevel 1 pause' in cmd
    assert os.name in ('nt', 'posix')  # Source checks also run on Linux CI.

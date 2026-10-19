"""Assemble an offline PRIVATE Windows candidate; never installs or authorizes release."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import sysconfig
import tomllib
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

from packaging.tags import sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

from scripts.release_audit import audit_wheel


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def linked(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(['git', *args], cwd=repo)


def wheel_record(path: Path) -> dict:
    if linked(path) or not path.is_file() or path.suffix != '.whl':
        raise ValueError('Only regular wheel files are accepted')
    name, version, _, tags = parse_wheel_filename(path.name)
    if not tags.intersection(sys_tags()):
        raise ValueError('Wheel is incompatible with the build interpreter/platform')
    with ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or any(
            n.startswith('/') or '..' in PurePosixPath(n).parts or '\\' in n or ':' in n
            for n in names
        ):
            raise ValueError('Unsafe or duplicate wheel members')
        metadata = [n for n in names if n.endswith('.dist-info/METADATA')]
        if len(metadata) != 1 or archive.getinfo(metadata[0]).file_size > 2 * 1024 * 1024:
            raise ValueError('Missing or oversized wheel metadata')
        info = BytesParser().parsebytes(archive.read(metadata[0]))
        if canonicalize_name(info.get('Name', '')) != name or Version(info.get('Version', '0')) != version:
            raise ValueError('Wheel metadata and filename disagree')
        if name not in {'projectwatch-local', 'greenpine-project-watch'} and any(n.startswith('project_watch/') for n in names):
            raise ValueError('Dependency must not overwrite product files')
    return {'name': name, 'version': str(version), 'file': path.name,
            'sha256': sha(path.read_bytes()), 'bytes': path.stat().st_size}


def assemble(repo: Path, product: Path, wheelhouse: Path, dest: Path, *, profile: str) -> dict:
    if sys.version_info[:2] != (3, 12) or sysconfig.get_platform() != 'win-amd64':
        raise ValueError('This delivery profile requires Windows x64 Python 3.12')
    if profile not in {'full', 'studio'}:
        raise ValueError('Unknown profile')
    if any(linked(p) for p in (repo, product, wheelhouse, dest)):
        raise ValueError('Linked delivery inputs are not accepted')
    repo, product, wheelhouse = repo.resolve(), product.resolve(), wheelhouse.resolve()
    if not dest.is_absolute() or dest.exists() or not dest.parent.is_dir():
        raise ValueError('Destination must be new, absolute and have an existing parent')
    dest = dest.resolve()
    if dest.is_relative_to(repo) or repo.is_relative_to(dest) or dest.is_relative_to(wheelhouse):
        raise ValueError('Destination must be separate from source and wheelhouse')
    if git(repo, 'status', '--porcelain=v1', '--untracked-files=all').strip():
        raise ValueError('Source checkout must be clean')
    commit = git(repo, 'rev-parse', 'HEAD').decode().strip()
    metadata = tomllib.loads(git(repo, 'show', f'{commit}:pyproject.toml').decode())['project']
    record = wheel_record(product)
    expected_version = metadata['version'] + ('+studioprobe' if profile == 'studio' else '')
    if record['name'] != canonicalize_name(metadata['name']) or record['version'] != expected_version:
        raise ValueError('Product version does not match source/profile')
    _, findings = audit_wheel(product)
    if findings:
        raise ValueError('Product wheel audit failed')
    tracked = git(repo, 'ls-tree', '-r', '--name-only', commit, '--', 'src/project_watch').decode().splitlines()
    selected = {name.removeprefix('src/') for name in tracked}
    if profile == 'studio':
        from scripts.build_core_probe import inventory
        selected = set(inventory(repo, profile='studio')['files'])
    source_hashes = {}
    with ZipFile(product) as archive:
        actual = {n for n in archive.namelist() if n.startswith('project_watch/') and not n.endswith('/')}
        if actual != selected:
            raise ValueError('Product module inventory differs from the committed source/profile')
        for name in sorted(selected):
            content = git(repo, 'show', f'{commit}:src/{name}')
            if archive.read(name) != content:
                raise ValueError('Product bytes differ from Git source: ' + name)
            source_hashes[name] = sha(content)
        for name in metadata.get('license-files', []):
            matches = [n for n in archive.namelist() if n.endswith('.dist-info/licenses/' + name)]
            if len(matches) != 1 or archive.read(matches[0]) != git(repo, 'show', f'{commit}:{name}'):
                raise ValueError('Product license differs from committed source')
    wheels = sorted(wheelhouse.iterdir())
    records = [wheel_record(path) for path in wheels]
    if len({r['name'] for r in records}) != len(records):
        raise ValueError('Duplicate dependency versions are not accepted')
    if sum(r['name'] in {'projectwatch-local', 'greenpine-project-watch'} for r in records) != 1:
        raise ValueError('New and legacy product distributions must not be co-installed')
    if record not in records:
        raise ValueError('Wheelhouse must contain the exact product wheel')
    result = {'schema_version': 1, 'scope': 'private_candidate_pending_acceptance',
              'publication_authorized': False, 'independent_windows_accepted': False,
              'commit': commit, 'profile': profile,
              'python': f'{sys.version_info.major}.{sys.version_info.minor}',
              'platform': sysconfig.get_platform(), 'wheels': records,
              'product_files_sha256': source_hashes,
              'limits': ['Not a signature or public license clearance',
                         'No user credentials, data, OCR/team/MCP/Postgres extras or Python installer',
                         'Dependency closure must pass the recorded offline installation test']}
    lock = ''.join(f"{r['name']}=={r['version']} --hash=sha256:{r['sha256']}\n" for r in records)
    instructions = '''# Private ProjectWatch candidate — not a public release

Requires Windows x64 and Python 3.12. Use a new installation directory.
This bundle contains software only; configure your own services after installation.
No model/search/commercial calls or monitoring are started by these commands.

From the bundle folder in PowerShell, replace the destination with a NEW absolute path:

```powershell
$install = 'D:\\ProjectWatch-private-test'
if (Test-Path -LiteralPath $install) { throw 'Choose a new directory' }
py -3.12 -m venv "$install/venv"
if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 venv failed' }
$python = "$install/venv/Scripts/python.exe"
& $python -m pip --isolated install --no-index --only-binary=:all: --find-links ./wheelhouse --require-hashes -r ./requirements.lock
if ($LASTEXITCODE -ne 0) { throw 'Installation failed; do not start' }
& $python -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency check failed; do not start' }
```

Only after verifying the bundle SHA-256 against the separate handoff receipt,
and when ready to configure your own model/search services, start it explicitly:

```powershell
$env:PROJECT_WATCH_HOME = "$install/data"
& $python -I -m project_watch.studio --no-browser
```

Open the printed localhost URL. Do not reuse any running instance's data directory.
Close with Ctrl+C; closing the browser does not stop monitoring. Reopen with the same
interpreter and data directory. Keep unknown paid requests on hold; do not delete receipts.
Follow the independent Windows checklist. Fresh installation on the build computer
does not constitute independent-user acceptance. Do not publish this bundle.
'''
    # Recheck the source after inventory reads so an in-flight edit cannot silently
    # turn a clean-commit receipt into a mixed worktree receipt.
    if git(repo, 'rev-parse', 'HEAD').decode().strip() != commit or git(
        repo, 'status', '--porcelain=v1', '--untracked-files=all'
    ).strip():
        raise ValueError('Source changed during validation')
    # Validate all inputs before writing; no overwrite, recursive removal or whole-home copy.
    dest.mkdir()
    (dest / 'wheelhouse').mkdir()
    for path, entry in zip(wheels, records, strict=True):
        target = dest / 'wheelhouse' / path.name
        shutil.copyfile(path, target)
        if sha(target.read_bytes()) != entry['sha256']:
            raise ValueError('Input changed during copying; discard this incomplete candidate')
    (dest / 'requirements.lock').write_text(lock, encoding='utf-8', newline='\n')
    (dest / 'INSTALL.md').write_text(instructions, encoding='utf-8', newline='\n')
    result['support_files_sha256'] = {n: sha((dest / n).read_bytes()) for n in ('requirements.lock', 'INSTALL.md')}
    (dest / 'manifest.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--product', type=Path, required=True)
    parser.add_argument('--wheelhouse', type=Path, required=True)
    parser.add_argument('--dest', type=Path, required=True)
    parser.add_argument('--profile', choices=('full', 'studio'), required=True)
    args = parser.parse_args()
    result = assemble(args.repo, args.product, args.wheelhouse, args.dest, profile=args.profile)
    print(json.dumps({k: result[k] for k in ('commit', 'profile', 'python', 'platform', 'publication_authorized')}))


if __name__ == '__main__':
    main()

"""Build a PRIVATE registered-Insight-free runtime probe, not a public Core release.

Run as python -m scripts.build_core_probe --dest <new absolute directory>.
Never changes the checkout or an installed application. No dependency downloads.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

from scripts.check_layer_boundaries import LAYERS, audit
from scripts.release_audit import _secret_findings, audit_wheel
from scripts.studio_runtime_boundary import legacy_closure

FONTS = {'newsreader-latin-wght-normal.woff2', 'noto-serif-sc-chinese-simplified-600-normal.woff2'}


def _linked(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def inventory(repo: Path, *, profile: str = 'compat') -> dict:
    if profile not in {'compat', 'studio'}:
        raise ValueError('Unknown probe profile')
    package = repo / 'src/project_watch'
    config = json.loads((repo / 'scripts/layer_map.json').read_text(encoding='utf-8-sig'))
    modules = config['modules']
    if set(modules.values()) - LAYERS:
        raise ValueError('Invalid layer registration')
    boundary = legacy_closure(repo, config) if profile == 'studio' else {
        'retained_legacy_modules': sorted(k for k, v in modules.items() if v == 'Legacy'),
        'excluded_legacy_modules': [], 'retained_legacy_paths': {},
    }
    seen, selected = set(), []
    for path in sorted(package.rglob('*')):
        if _linked(path):
            raise ValueError('Linked runtime file/directory is not allowed')
        if not path.is_file() or '__pycache__' in path.parts:
            continue
        relative = path.relative_to(repo / 'src').as_posix()
        parts = path.relative_to(package).parts
        if path.suffix == '.py':
            module = relative[:-3].replace('/', '.').removesuffix('.__init__')
            if module not in modules:
                raise ValueError(f'Unclassified module: {module}')
            seen.add(module)
            if modules[module] == 'Insight' or module in boundary['excluded_legacy_modules']:
                continue
        elif not (
            (len(parts) == 2 and parts[0] == 'static' and path.suffix in {'.html', '.css', '.js', '.mjs', '.svg'})
            or (len(parts) == 2 and parts[0] == 'migrations' and path.suffix == '.sql')
            or relative == 'project_watch/web/index.html'
            or (len(parts) == 3 and parts[:2] == ('web', 'assets') and
                (path.suffix in {'.js', '.css', '.svg'} or path.name in FONTS | {'THIRD-PARTY-LICENSES.txt'}))
        ):
            raise ValueError(f'Unexpected runtime asset: {relative}')
        selected.append(relative)
    if seen != modules.keys():
        raise ValueError('Stale module registration or missing package')
    return {'files': selected, 'profile': profile, **boundary,
            'excluded_modules': sorted(k for k, v in modules.items() if v == 'Insight'),
            'mixed_private_symbols': sorted(f'{module}:{symbol}'
                for module, symbols in config.get('symbols', {}).items()
                if modules.get(module) != 'Insight' for symbol, layer in symbols.items() if layer == 'Insight')}


def stage(repo: Path, dest: Path, *, profile: str = 'compat') -> dict:
    repo = repo.resolve()
    if not dest.is_absolute() or dest.exists() or dest.is_symlink():
        raise ValueError('Destination must be a new absolute directory')
    dest = dest.resolve()
    if dest.is_relative_to(repo) or repo.is_relative_to(dest) or not dest.parent.is_dir():
        raise ValueError('Destination must be outside the checkout with an existing parent')
    for relative in ('src', 'src/project_watch', 'scripts', 'scripts/layer_map.json', 'pyproject.toml', 'README.md'):
        if _linked(repo / relative):
            raise ValueError('Linked build input is not allowed')
    manifest = inventory(repo, profile=profile)
    metadata = (repo / 'pyproject.toml').read_text(encoding='utf-8')
    project = tomllib.loads(metadata)['project']
    old_version = project['version']
    license_files = project.get('license-files', [])
    if (not isinstance(license_files, list)
            or any(not isinstance(name, str) or name not in {'LICENSE', 'NOTICE'} for name in license_files)
            or len(set(license_files)) != len(license_files)):
        raise ValueError('Unsupported license input; use explicit root LICENSE/NOTICE files')
    if '+' in old_version:
        raise ValueError('Base version must not already have a local suffix')
    version = old_version + ('+studioprobe' if profile == 'studio' else '+coreprobe')
    metadata, count = re.subn(r'(?m)^version = "' + re.escape(old_version) + r'"$', f'version = "{version}"', metadata)
    if count != 1:
        raise ValueError('Unsupported version declaration')
    payloads = {'pyproject.toml': metadata.encode(), 'README.md': (
        b'# Private runtime probe\n\nNot an open-source Core release. Registered Insight plugins are absent; '
        b'mixed analysis, Legacy and frontend rights remain under review. Do not distribute.\n')}
    for name in license_files:
        path = repo / name
        if _linked(path) or not path.is_file():
            raise ValueError('Missing or linked declared license input')
        data = path.read_bytes()
        if not data.strip() or _secret_findings(data, name):
            raise ValueError('Invalid license input')
        payloads[name] = data
    for relative in manifest['files']:
        path = repo / 'src' / relative
        data = path.read_bytes()
        if path.suffix != '.woff2' and _secret_findings(data, relative):
            raise ValueError(f'Credential-shaped text in build input: {relative}')
        payloads['src/' + relative] = data
    if _secret_findings(payloads['pyproject.toml'], 'pyproject.toml'):
        raise ValueError('Credential-shaped metadata')
    # Validate everything before creating the destination; never copy a whole repo/home.
    dest.mkdir()
    source = dest / 'source'
    for relative, data in payloads.items():
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    manifest.update(version=version, license_files=license_files, kind='registered-plugin-free-private-probe',
        publication_authorized=False, public_core_ready=False,
        blockers=(['mixed investment analysis symbols remain in Core-registered modules (see mixed_private_symbols)']
                  if manifest['mixed_private_symbols'] else []) + [
                  'remaining Core and Legacy content requires method review; split crosscheck policy is not full clearance',
                  'Legacy and compiled frontend content/rights require review',
                  'license and clean-machine acceptance not complete'],
        staged_sha256={name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()})
    (dest / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest


def verify_wheel(wheel: Path, manifest: dict) -> None:
    _, findings = audit_wheel(wheel)
    if findings:
        raise ValueError('Wheel content audit failed; do not distribute')
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        actual = {name for name in names if name.startswith('project_watch/')}
        if actual != set(manifest['files']) or len(names) != len(set(names)):
            raise ValueError('Wheel file inventory mismatch')
        for name in actual:
            if hashlib.sha256(archive.read(name)).hexdigest() != manifest['staged_sha256']['src/' + name]:
                raise ValueError('Wheel source hash mismatch')
        for relative in manifest.get('license_files', []):
            matches = [name for name in names if name.endswith('.dist-info/licenses/' + relative)]
            if (len(matches) != 1 or hashlib.sha256(archive.read(matches[0])).hexdigest()
                    != manifest['staged_sha256'][relative]):
                raise ValueError('Wheel license missing or hash mismatch')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dest', type=Path, required=True)
    parser.add_argument('--profile', choices=('compat', 'studio'), default='compat')
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    config = json.loads((repo / 'scripts/layer_map.json').read_text(encoding='utf-8-sig'))
    result = audit(repo, config)
    if not result.ok:
        raise SystemExit(result.summary())
    manifest = stage(repo, args.dest, profile=args.profile)
    # Build an allowlisted snapshot, never an editable install or a modified wheel.
    subprocess.run([sys.executable, '-m', 'pip', 'wheel', str(args.dest / 'source'), '--no-deps',
                    '--no-build-isolation', '--no-index', '--wheel-dir', str(args.dest / 'dist')], check=True)
    wheels = list((args.dest / 'dist').glob('*.whl'))
    if len(wheels) != 1:
        raise RuntimeError('Expected exactly one wheel')
    verify_wheel(wheels[0], manifest)
    # Verify staging did not drift during build; artifacts only, not repository files.
    for relative, digest in manifest['staged_sha256'].items():
        if hashlib.sha256((args.dest / 'source' / relative).read_bytes()).hexdigest() != digest:
            raise RuntimeError('Staged input changed during build')
    shutil.copy2(repo / 'scripts/layer_map.json', args.dest / 'layer-map.snapshot.json')
    manifest['wheel_sha256'] = hashlib.sha256(wheels[0].read_bytes()).hexdigest()
    manifest['wheel'] = wheels[0].name
    (args.dest / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'wheel': str(wheels[0]), 'excluded_modules': len(manifest['excluded_modules']),
                      'sha256': manifest['wheel_sha256'], 'public_core_ready': False}))


if __name__ == '__main__':
    main()

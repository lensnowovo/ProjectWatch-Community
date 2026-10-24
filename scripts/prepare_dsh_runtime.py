"""Prepare the DSH runtime bundled into desktop builds, reproducibly.

Installs exactly ``packaging/dsh/package-lock.json`` with ``npm ci --ignore-scripts``
for an explicit target (this step downloads from the npm registry),
prunes the parts ProjectWatch never loads, checks every
installed package against the lock, writes third-party notices and a hash manifest.
Optional dependencies are not omitted wholesale: DSH needs some platform-native
ones at start; unused native packages are named in ``prune.json``.

``--installed`` adopts an existing ``npm ci`` result instead of downloading, for
offline verification. Output must be new and outside the repository.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKAGING = REPO/'packaging/dsh'
MANIFEST = 'dsh-runtime-manifest.json'
NOTICES = 'THIRD-PARTY-NOTICES.txt'
REVIEW = 'third-party-review.json'

# Declared licenses accepted for redistribution with attribution. Anything else is
# listed for human review and the build refuses to continue.
PERMISSIVE = {'MIT', 'ISC', 'BSD-2-CLAUSE', 'BSD-3-CLAUSE', 'APACHE-2.0', '0BSD', 'UNLICENSE',
              'CC0-1.0', 'PYTHON-2.0', 'BLUEOAK-1.0.0', 'ZLIB'}
LICENSE_PREFIXES = ('license', 'licence', 'copying', 'notice')
TARGETS = ('win32-x64', 'darwin-arm64', 'darwin-x64')


def target_parts(target: str) -> tuple[str, str]:
    if target not in TARGETS:
        raise ValueError(f'Unsupported DSH target: {target}')
    return tuple(target.split('-', 1))


def target_rules(target: str) -> dict:
    platform, _ = target_parts(target)
    rules = json.loads((PACKAGING/'prune.json').read_text(encoding='utf-8'))
    if platform == 'darwin':
        extra = json.loads((PACKAGING/'prune-darwin.json').read_text(encoding='utf-8'))
        rules['remove_packages'] += extra['remove_packages']
    return rules


def target_prune_digest(target: str) -> str | None:
    platform, _ = target_parts(target)
    if platform != 'darwin':
        return None
    # New target rules use canonical LF, independent of checkout line endings.
    return config_digest(PACKAGING/'prune-darwin.json')


def required_native_packages(target: str) -> tuple[str, ...]:
    target_parts(target)
    # The system addon implements POSIX primitives and has no Windows child.
    # Windows uses Koffi and the explicitly MSVC-suffixed builtin loader.
    if target == 'win32-x64':
        return ('@koromix/koffi-win32-x64', 'node-addon-require-builtin-win32-x64-msvc')
    return (f'@deepseek-ai/node-addon-system-{target}', f'@koromix/koffi-{target}',
            f'node-addon-require-builtin-{target}')


def verify_target_packages(modules: Path, target: str) -> None:
    platform, cpu = target_parts(target)
    for name in required_native_packages(target):
        folder = modules/name
        if not (folder/'package.json').is_file() or not any(p.is_file() for p in folder.rglob('*.node')):
            raise ValueError(f'Required native package missing for {target}: {name}')
    for rel in installed_packages(modules):
        data = json.loads((modules/rel/'package.json').read_text(encoding='utf-8'))
        for field, expected in (('os', platform), ('cpu', cpu)):
            values = data.get(field, [])
            values = [values] if isinstance(values, str) else values
            positive = [v for v in values if not v.startswith('!')]
            if ('!' + expected in values or
                    (positive and expected not in positive and 'any' not in positive)):
                raise ValueError(f'Package target mismatch: {rel} ({field})')


def install_command(npm: str, target: str) -> list[str]:
    platform, cpu = target_parts(target)
    return [npm, 'ci', '--ignore-scripts', '--no-audit', '--no-fund', '--bin-links=false',
            '--include=optional', f'--os={platform}', f'--cpu={cpu}',
            '--registry=https://registry.npmjs.org/']


def reject_links(root: Path) -> None:
    for path in (root, *root.rglob('*')):
        if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
            raise ValueError(f'Runtime must not contain symlinks or junctions: {path.name}')


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def text_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def config_digest(path: Path) -> str:
    """Canonicalize checkout newlines only, not whitespace/JSON values."""
    return hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()


def legacy_config_hashes(path: Path) -> set[str]:
    """Old manifests used raw bytes from either LF or CRLF worktrees."""
    lf = path.read_bytes().replace(b'\r\n', b'\n')
    return {hashlib.sha256(data).hexdigest() for data in (lf, lf.replace(b'\n', b'\r\n'))}


def dsh_version() -> str:
    sys.path.insert(0, str(REPO/'src'))
    from project_watch.studio.dsh_runtime import DSH_VERSION
    return DSH_VERSION


def lock_entries(lock: dict) -> dict[str, dict]:
    """Lock paths relative to node_modules, e.g. ``a/node_modules/@b/c``."""
    return {key[len('node_modules/'):]: value for key, value in lock['packages'].items()
            if key.startswith('node_modules/') and not value.get('link')}


def installed_packages(modules: Path) -> dict[str, str]:
    found = {}
    for package_json in modules.rglob('package.json'):
        rel = package_json.parent.relative_to(modules).as_posix()
        parts = rel.split('/')
        # A package directory is <name> or @scope/<name>, possibly nested under node_modules.
        chunks, i, ok = [], 0, True
        while i < len(parts):
            if parts[i].startswith('@'):
                if i + 1 >= len(parts):
                    ok = False
                    break
                chunks.append(parts[i] + '/' + parts[i + 1])
                i += 2
            else:
                chunks.append(parts[i])
                i += 1
            if i < len(parts):
                if parts[i] != 'node_modules':
                    ok = False
                    break
                i += 1
        if not ok:
            continue
        try:
            data = json.loads(package_json.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get('name') and data.get('version'):
            found['/node_modules/'.join(chunks)] = data['version']
    return found


def prune(modules: Path, rules: dict) -> None:
    for name in rules['remove_packages']:
        for path in [modules/name, *modules.glob('**/node_modules/' + name)]:
            shutil.rmtree(path, ignore_errors=True)
    for rel in rules['remove_paths']:
        shutil.rmtree(modules/rel, ignore_errors=True)
    suffixes = tuple(rules['remove_suffixes'])
    keep = tuple(rules['keep_prefixes'])
    for path in list(modules.rglob('*')):
        if path.is_file() and path.name.lower().endswith(suffixes) and not path.name.lower().startswith(keep):
            path.unlink()


def verify_against_lock(modules: Path, lock: dict, rules: dict) -> dict[str, str]:
    """Every installed package is a non-optional lock entry of the exact version."""
    entries = lock_entries(lock)
    installed = installed_packages(modules)
    removed = set(rules['remove_packages'])
    for rel, version in installed.items():
        entry = entries.get(rel)
        if entry is None or entry.get('version') != version:
            raise ValueError(f'Installed package does not match the lock: {rel}@{version}')
    for rel, entry in entries.items():
        last = rel.split('/node_modules/')[-1]
        # Optional entries for other platforms are legitimately absent.
        if entry.get('optional') or last in removed or any(rel.startswith(n + '/node_modules/') for n in removed):
            continue
        if rel not in installed and entry.get('dev') is not True:
            raise ValueError(f'Lock package missing after install: {rel}')
    return installed


def is_package_dir(path: Path) -> bool:
    """``node_modules/<name>`` or ``node_modules/@scope/<name>``; not folders inside a package."""
    parent = path.parent
    if parent.name == 'node_modules':
        return not path.name.startswith('@')
    return parent.name.startswith('@') and parent.parent.name == 'node_modules'


def spdx_terms(value) -> list[str]:
    if isinstance(value, dict):
        value = value.get('type')
    if isinstance(value, list):
        value = ' OR '.join(str(v.get('type') if isinstance(v, dict) else v) for v in value)
    text = str(value or '').upper().replace('(', ' ').replace(')', ' ')
    return [t for t in text.split() if t not in ('OR', 'AND', 'WITH')]


def upstream_texts(folder: Path = PACKAGING/'upstream-licenses') -> dict:
    """Texts collected from upstream for packages that ship none, with provenance."""
    index = folder/'index.json'
    if not index.is_file():
        return {}
    entries = json.loads(index.read_text(encoding='utf-8'))
    for key, entry in entries.items():
        if text_digest(folder/entry['file']) != entry['sha256']:
            raise ValueError(f'Upstream license text changed: {key}')
        entry['text'] = (folder/entry['file']).read_text(encoding='utf-8')
    return entries


def notices(modules: Path, out: Path, *, extra: list[tuple[str, str]] = (), upstream: dict | None = None) -> dict:
    """Write license texts for every package; list anything needing human review.

    A text supplied from upstream is used but stays on the review list until a
    maintainer approves it; it is never presented as shipped by the package itself.
    """
    upstream = upstream_texts() if upstream is None else upstream
    blocks, review = [], []
    seen = set()
    for package_json in sorted(modules.rglob('package.json')):
        try:
            data = json.loads(package_json.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not data.get('name') or not data.get('version'):
            continue
        if not is_package_dir(package_json.parent):
            continue
        key = f"{data['name']}@{data['version']}"
        if key in seen:
            continue
        seen.add(key)
        declared = data.get('license') or data.get('licenses') or 'UNKNOWN'
        terms = spdx_terms(declared)
        files = sorted(p for p in package_json.parent.iterdir()
                       if p.is_file() and p.name.lower().startswith(LICENSE_PREFIXES))
        texts = [f.read_text(encoding='utf-8', errors='replace').strip() for f in files]
        if not terms or not any(t in PERMISSIVE for t in terms):
            review.append({'package': key, 'license': str(declared), 'reason': 'not_on_permissive_list'})
        supplied = upstream.get(key) if not texts else None
        if supplied:
            review.append({'package': key, 'license': str(declared), 'reason': supplied['kind'],
                           'source': supplied['source'], 'note': supplied.get('note', '')})
            texts = [f"(Not shipped in the package. Collected from {supplied['source']}"
                     + (f"; {supplied['note']}" if supplied.get('note') else '') + ')\n\n' + supplied['text'].strip()]
        elif not texts:
            review.append({'package': key, 'license': str(declared), 'reason': 'no_license_text'})
        body = '\n\n'.join(texts) if texts else (
            f'No license text is shipped in this package. Declared license: {declared}. '
            f'Author: {data.get("author") or "not stated"}. Standard text of each license is in LICENSES/.')
        blocks.append(f'{key}\nLicense: {declared}\n\n{body}')
    for name, text in extra:
        blocks.append(f'{name}\n\n{text.strip()}')
    separator = '\n\n' + '-' * 72 + '\n\n'
    out.write_text('Third-party software bundled with the ProjectWatch DSH runtime.\n'
                   'ProjectWatch\'s own license does not apply to these components.' + separator
                   + separator.join(blocks) + '\n', encoding='utf-8')
    return {'packages': len(seen), 'review': review}


def file_manifest(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): digest(p) for p in sorted(root.rglob('*')) if p.is_file()}


def prepare(out: Path, *, installed: Path | None, npm: str | None,
            approved_resolutions: set[str], target: str = 'win32-x64') -> dict:
    out = out.resolve()
    if out.exists() or out.is_relative_to(REPO) or REPO.is_relative_to(out):
        raise ValueError('Output must be new and outside source')
    lock = json.loads((PACKAGING/'package-lock.json').read_text(encoding='utf-8'))
    rules = target_rules(target)
    out.mkdir(parents=True)
    for name in ('package.json', 'package-lock.json'):
        shutil.copyfile(PACKAGING/name, out/name)
    modules = out/'node_modules'
    if installed:
        source = installed.resolve()/'node_modules'
        if config_digest(installed.resolve()/'package-lock.json') != config_digest(PACKAGING/'package-lock.json'):
            raise ValueError('Adopted install was made from a different lock')
        reject_links(source)
        shutil.copytree(source, modules, symlinks=False)
    else:
        if not npm:
            raise ValueError('npm is required to install the pinned runtime')
        env = {k: v for k, v in os.environ.items() if not k.lower().startswith('npm_config_')}
        subprocess.run(install_command(npm, target), cwd=out, env=env, check=True)
    reject_links(modules)
    prune(modules, rules)
    installed_versions = verify_against_lock(modules, lock, rules)
    verify_target_packages(modules, target)
    version = json.loads((modules/'@deepseek-ai/dsh/package.json').read_text(encoding='utf-8'))['version']
    if version != dsh_version():
        raise ValueError(f'DSH {version} does not match the version ProjectWatch pins ({dsh_version()})')
    # Standard license texts referenced by packages that ship none of their own.
    shutil.copytree(PACKAGING/'licenses', out/'LICENSES')
    report = notices(modules, out/NOTICES)
    unresolved = [r for r in report['review'] if r['package'] not in approved_resolutions]
    (out/REVIEW).write_text(json.dumps(report['review'], ensure_ascii=False, indent=1), encoding='utf-8')
    files = file_manifest(out)
    manifest = {'format': 'projectwatch-dsh-runtime-v1', 'dsh_version': version,
                'target': target, 'target_prune_sha256': target_prune_digest(target),
                'lock_sha256': config_digest(PACKAGING/'package-lock.json'),
                'prune_sha256': config_digest(PACKAGING/'prune.json'),
                'packages': len(installed_versions), 'third_party_review_open': len(unresolved), 'files': files}
    (out/MANIFEST).write_text(json.dumps(manifest, indent=1), encoding='utf-8')
    return {'files': len(files), 'packages': len(installed_versions), 'review_open': len(unresolved),
            'bytes': sum((out/p).stat().st_size for p in files)}


def verify_prepared(root: Path, *, target: str | None = None) -> dict:
    """Used by the bundle build: the prepared tree is exactly what its manifest lists."""
    reject_links(root)
    manifest = json.loads((root/MANIFEST).read_text(encoding='utf-8'))
    if manifest.get('format') != 'projectwatch-dsh-runtime-v1':
        raise ValueError('Not a prepared DSH runtime')
    actual_target = manifest.get('target', 'win32-x64')
    target_parts(actual_target)
    if target is not None and actual_target != target:
        raise ValueError(f'Prepared DSH target mismatch: {actual_target}, expected {target}')
    if manifest.get('target_prune_sha256') != target_prune_digest(actual_target):
        raise ValueError('Prepared DSH target prune list mismatch')
    if (manifest['lock_sha256'] not in legacy_config_hashes(PACKAGING/'package-lock.json') or
            manifest['prune_sha256'] not in legacy_config_hashes(PACKAGING/'prune.json')):
        raise ValueError('Prepared DSH runtime is from a different lock or prune list')
    if manifest['dsh_version'] != dsh_version():
        raise ValueError('Prepared DSH runtime version mismatch')
    if manifest.get('third_party_review_open') != 0:
        raise ValueError('Third-party licenses still need review')
    actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()} - {MANIFEST}
    if actual != set(manifest['files']):
        raise ValueError('Prepared DSH runtime has unlisted or missing files')
    for rel, expected in manifest['files'].items():
        if digest(root/rel) != expected:
            raise ValueError(f'Prepared DSH runtime file changed: {rel}')
    if 'target' in manifest:
        verify_target_packages(root/'node_modules', actual_target)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--installed', type=Path, help='adopt an existing npm ci result (offline verification)')
    parser.add_argument('--npm', default=shutil.which('npm'))
    parser.add_argument('--approved', type=Path, help='JSON list of name@version reviewed by a maintainer')
    parser.add_argument('--target', choices=TARGETS, default='win32-x64')
    args = parser.parse_args()
    approved = set(json.loads(args.approved.read_text(encoding='utf-8'))) if args.approved else set()
    print(json.dumps(prepare(args.out, installed=args.installed, npm=args.npm,
                             approved_resolutions=approved, target=args.target)))


if __name__ == '__main__':
    main()

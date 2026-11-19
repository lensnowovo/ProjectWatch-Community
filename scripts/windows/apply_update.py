"""Manual, offline exact-base update. No workers, network, migrations or retries."""
import argparse
import json
import re
import shutil
import sys
import uuid
from pathlib import Path

if __package__:
    from . import maintain as m
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import maintain as m


def paths(root):
    root = m.safe(root)
    result = set()
    for p in root.rglob('*'):
        m.safe(p)
        if p.is_file():
            result.add(p.relative_to(root).as_posix())
    return result


def validate_manifest(value, root):
    m.check_files(root, value['files'])
    names = set(value['files'])
    for name in names:
        if name.casefold() in ('bundle-manifest.json', 'runtimes') or name.casefold().startswith('runtimes/'):
            raise ValueError('Reserved application path')
    for entry in value.get('runtimes', {}).values():
        if any(n.casefold() == '.complete' for n in entry['files']):
            raise ValueError('Reserved runtime marker')
    # Also reject case-insensitive file/directory collisions and Win32 device paths.
    all_names = names | {f'runtimes/{rid}/{n}' for rid, e in value.get('runtimes', {}).items() for n in e['files']}
    folded = {n.casefold() for n in all_names}
    for name in all_names:
        parts = name.split('/')
        if any('/'.join(parts[:i]).casefold() in folded for i in range(1, len(parts))):
            raise ValueError('Conflicting paths')
        if any(re.fullmatch(r'(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?', p, re.I)
               or any(ord(c) < 32 for c in p) or any(c in p for c in '<>"|?*') for p in parts):
            raise ValueError('Unsafe Windows path')


def changed_files(base, target):
    changed = {n: h for n, h in target['files'].items() if base['files'].get(n) != h}
    for rid, entry in target.get('runtimes', {}).items():
        if base.get('runtimes', {}).get(rid) != entry:
            changed.update({f'runtimes/{rid}/{n}': h for n, h in entry['files'].items()})
    return changed


def verify_runtime(root, rid, entry):
    runtime = m.child(root, 'runtimes/' + rid)
    if paths(runtime) != set(entry['files']) | {'.complete'}:
        raise ValueError('Local runtime files changed')
    if (runtime/'.complete').read_text(encoding='utf-8') != m.runtime_digest(entry['files']):
        raise ValueError('Incomplete runtime')
    m.verify_app(runtime, entry)


def verify_existing(root, state, base):
    folder = m.child(root, 'versions/' + state['active'])
    if m.digest(folder/'bundle-manifest.json') != state['active']:
        raise ValueError('Modified installed manifest')
    if paths(folder) != set(base['files']) | {'bundle-manifest.json'}:
        raise ValueError('Local added or missing program files; preserve customizations')
    m.verify_app(folder, base)
    if m.digest(root/'ProjectWatch.exe') != state['launcher_sha256']:
        raise ValueError('Customized launcher')
    for rid, entry in base.get('runtimes', {}).items():
        verify_runtime(root, rid, entry)
    return folder


def apply(package, root, *, confirm=False):
    if not confirm:
        raise ValueError('Explicit manual confirmation required')
    package, root = m.safe(package), m.safe(root)
    state = m.state_at(root)
    if not state:
        raise ValueError('Existing full installation required')
    root, home = m.validate_roots(root, Path(state['home']))
    if any(package.is_relative_to(p) or p.is_relative_to(package) for p in (root, home)):
        raise ValueError('Extract update outside program and data directories')
    info = json.loads(m.safe(package/'update.json').read_text(encoding='utf-8'))
    if info.get('format') != 'projectwatch-windows-update-v1':
        raise ValueError('Invalid update format')
    if set(info.get('support', {})) != {'apply_update.py', 'maintain.py', 'Apply-Update.cmd', 'README.txt'}:
        raise ValueError('Unexpected update support inventory')
    for field in ('base', 'target'):
        if not re.fullmatch('[0-9a-f]{64}', info[field]):
            raise ValueError('Invalid update identity')
    target = m.manifest(package)
    validate_manifest(target, package)
    if m.digest(package/'bundle-manifest.json') != info['target']:
        raise ValueError('Target manifest hash mismatch')
    with m.lock(root/'.studio.lock'), m.lock(home/'desktop-session/.studio.lock'), m.lock(home/'.studio.lock'):
        state = m.state_at(root)
        if state['active'] != info['base'] or state['home'] != m.display(home):
            raise ValueError('Wrong base installation; use the matching update or full installer')
        old = m.child(root, 'versions/' + state['active'])
        base = m.manifest(old)
        validate_manifest(base, old)
        if base.get('python') != target.get('python'):
            raise ValueError('Python runtime change requires full installer')
        if base.get('launcher_source_sha256') != target.get('launcher_source_sha256'):
            raise ValueError('Bootstrap change requires full installer')
        changes = changed_files(base, target)
        if info['files'] != changes:
            raise ValueError('Update file inventory mismatch')
        expected_package = {'update.json', 'bundle-manifest.json'} | set(info['support']) | {'payload/' + n for n in changes}
        if paths(package) != expected_package:
            raise ValueError('Unexpected update package files')
        m.check_files(package, info['support'])
        for name, expected in info['support'].items():
            if m.digest(m.child(package, name)) != expected:
                raise ValueError('Update support file hash mismatch')
        m.verify_app(package/'payload', {'files': changes})
        verify_existing(root, state, base)
        # Do not trust a previous target's .complete marker or unlisted files.
        for rid, entry in target.get('runtimes', {}).items():
            if rid not in base.get('runtimes', {}) and m.child(root, 'runtimes/' + rid).exists():
                verify_runtime(root, rid, entry)
        existing = m.child(root, 'versions/' + info['target'])
        if existing.exists():
            if paths(existing) != set(target['files']) | {'bundle-manifest.json'}:
                raise ValueError('Existing target contains local changes')
            if m.digest(existing/'bundle-manifest.json') != info['target']:
                raise ValueError('Existing target manifest changed')
            m.verify_app(existing, target)
        from packaging.version import Version
        if Version(target['version']) < Version(base['version']):
            raise ValueError('Downgrade requires a verified matching data restore')
        stage = m.child(root, 'updates/staging-' + uuid.uuid4().hex)
        stage.mkdir(parents=True)
        # Copy instead of hard-linking: later local edits must not mutate old versions.
        for name in target['files']:
            src = m.child(package/'payload' if name in changes else old, name)
            dst = m.child(stage, name)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        for name in changes:
            if name.startswith('runtimes/'):
                dst = m.child(stage, name)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(m.child(package/'payload', name), dst)
        shutil.copy2(package/'bundle-manifest.json', stage/'bundle-manifest.json')
        m.verify_app(stage, target)
        result = m.install_locked(stage, root, home, target, info['target'], move=True)
        return {**result, 'base': info['base'], 'changed_files': len(changes), 'manual_update': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--package', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--confirm', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(apply(args.package, args.root, confirm=args.confirm)))
        return 0
    except Exception as exc:
        # Never log settings, database content or raw subprocess arguments.
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__, 'message': str(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

"""Build an offline exact-base update; no application or provider execution."""
import argparse
import json
import zipfile
from pathlib import Path

from scripts.build_windows_bundle import validate_output
from scripts.windows import maintain as m
from scripts.windows.apply_update import changed_files, validate_manifest


def build(base, target, output):
    repo = Path(__file__).resolve().parents[1]
    base, target, output = m.safe(base), m.safe(target), Path(m.display(m.safe(output)))
    validate_output(output, repo)
    if output.is_relative_to(Path(m.display(base))) or output.is_relative_to(Path(m.display(target))):
        raise ValueError('Output must be outside input bundles')
    before, after = m.manifest(base), m.manifest(target)
    for folder, value in ((base, before), (target, after)):
        validate_manifest(value, folder)
    # Unchanged bytes come from the verified installed base, never these source
    # trees. Read/hash only transmitted files below. The installer verifies the
    # whole base and reconstructed target; release validation still checks the
    # full candidate independently. Do not reread two unchanged DSH copies here.
    if before.get('python') != after.get('python') or before.get('launcher_source_sha256') != after.get('launcher_source_sha256'):
        raise ValueError('Runtime/bootstrap migration requires full installer')
    from packaging.version import Version
    if Version(after['version']) < Version(before['version']):
        raise ValueError('Cannot build downgrade update')
    changes = changed_files(before, after)
    support = {name: (repo/'scripts/windows'/name).read_bytes()
               for name in ('apply_update.py', 'maintain.py', 'Apply-Update.cmd')}
    support['README.txt'] = (
        b'ProjectWatch manual Windows update\n'
        b'Download from the official release and verify the ZIP SHA-256 there. Hashes are not signatures.\n'
        b'Extract to a new folder OUTSIDE program/data directories. Exit ProjectWatch from the tray.\n'
        b'Double-click Apply-Update.cmd, check the displayed installation path, and type UPDATE.\n'
        b'Only the exact declared base is supported. Modified or added installed files stop the update.\n'
        b'Backups/data/configuration/paused state/unknown records are retained; no provider calls occur.\n'
        b'On failure, keep staging and the console result; the old active version remains. Use the matching\n'
        b'full installer or maintainer assistance if this update cannot apply. Do not delete user data.\n'
        b'Old versions remain, but reverting after running a schema migration requires a verified\n'
        b'matching data restore. No automatic downgrade or automatic update is performed.\n'
    )
    import hashlib
    info = {'format': 'projectwatch-windows-update-v1',
            'base': m.digest(base/'bundle-manifest.json'),
            'target': m.digest(target/'bundle-manifest.json'), 'files': changes,
            'support': {n: hashlib.sha256(b).hexdigest() for n, b in support.items()}}
    if info['base'] == info['target']:
        raise ValueError('No target change')
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED) as z:
        z.write(target/'bundle-manifest.json', 'bundle-manifest.json')
        z.writestr('update.json', json.dumps(info, indent=2))
        for name, data in support.items():
            z.writestr(name, data)
        for name, expected in changes.items():
            source = m.child(target, name)
            data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError('Input changed while packaging')
            z.writestr('payload/' + name, data)
    return {'base': info['base'], 'target': info['target'], 'changed_files': len(changes),
            'bytes': output.stat().st_size, 'sha256': m.digest(output)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('base', 'target', 'out'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.base, args.target, args.out)))

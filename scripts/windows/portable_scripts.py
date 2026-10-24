"""Relocate pip-generated dependency commands without stripping their payloads.

distlib's Windows launcher supports <launcher_dir> in its interpreter shebang.
Only rewrite a recognized launcher pointing at this build interpreter; preserve
its executable prefix and ZIP exactly. Validate every RECORD before any writes.
"""
import base64
import csv
import hashlib
import io
import sys
import zipfile
from pathlib import Path, PurePosixPath

RELATIVE_SHEBANG = b'#!<launcher_dir>\\..\\..\\..\\python.exe\n'


def _hash(data):
    return 'sha256='+base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()


def make_portable(site: Path) -> list[str]:
    folder = site/'bin'
    if not folder.exists():
        return []
    if folder.is_symlink() or (hasattr(folder, 'is_junction') and folder.is_junction()):
        raise ValueError('Linked dependency launcher directory')
    expected = {b'#!'+sys.executable.encode()+b'\n', b'#!"'+sys.executable.encode()+b'"\n'}
    changes = {}
    for path in sorted(folder.iterdir()):
        if path.is_symlink() or not path.is_file():
            raise ValueError('Unexpected dependency launcher entry')
        before = path.read_bytes()
        if path.suffix.lower() == '.exe':
            try:
                with zipfile.ZipFile(io.BytesIO(before)) as archive:
                    entries = archive.infolist()
                    if len(entries) != 1 or entries[0].filename != '__main__.py':
                        raise ValueError('Unexpected launcher archive')
                    start = entries[0].header_offset
            except zipfile.BadZipFile as exc:
                raise ValueError('Unrecognized dependency launcher') from exc
            marker = before.rfind(b'#!', 0, start)
            if (marker < 0 or not before.startswith(b'MZ')
                    or '<launcher_dir>\\'.encode('utf-16le') not in before[:marker]):
                raise ValueError('Unsupported dependency launcher')
            shebang = before[marker:start]
            if shebang == RELATIVE_SHEBANG:
                continue
            if shebang not in expected:
                raise ValueError('Unexpected launcher interpreter')
            after = before[:marker]+RELATIVE_SHEBANG+before[start:]
        elif path.suffix.lower() in {'.py', '.pyw'}:
            first, separator, rest = before.partition(b'\n')
            if first.removesuffix(b'\r')+b'\n' not in expected:
                continue  # Upstream script without a generated build shebang.
            ending = b'\r\n' if first.endswith(b'\r') and separator else separator
            after = b'#!/usr/bin/env python'+ending+rest
        else:
            raise ValueError('Unexpected dependency launcher type')
        changes[path.name] = (path, before, after)
    records = []
    owners = {name: 0 for name in changes}
    for record in sorted(site.glob('*.dist-info/RECORD')):
        if record.is_symlink() or not record.resolve().is_relative_to(site.resolve()):
            raise ValueError('Linked dependency RECORD')
        with record.open(encoding='utf-8', newline='') as stream:
            rows = list(csv.reader(stream))
        changed = False
        for row in rows:
            if len(row) != 3:
                raise ValueError('Malformed dependency RECORD')
            parts = PurePosixPath(row[0]).parts
            if len(parts) < 2 or parts[-2] != 'bin' or parts[-1] not in changes:
                continue
            name = parts[-1]
            _, before, after = changes[name]
            if row[1:] != [_hash(before), str(len(before))]:
                raise ValueError('Dependency launcher RECORD mismatch')
            owners[name] += 1
            row[:] = ['bin/'+name, _hash(after), str(len(after))]
            changed = True
        if changed:
            records.append((record, rows))
    if any(count != 1 for count in owners.values()):
        raise ValueError('Dependency launcher RECORD ownership mismatch')
    for path, _, after in changes.values():
        path.write_bytes(after)
    for record, rows in records:
        with record.open('w', encoding='utf-8', newline='') as stream:
            csv.writer(stream).writerows(rows)
    return sorted(changes)

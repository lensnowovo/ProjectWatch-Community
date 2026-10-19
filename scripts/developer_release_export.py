"""Export an explicitly reviewed fixed tree, without copying Git history or local data.

The manifest is private review input, not publication approval. All tracked paths
must have a disposition and content hash. This does not grant third-party rights.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath

from scripts.release_audit import _secret_findings


class ExportError(ValueError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(['git', *args], cwd=repo, capture_output=True)
    if result.returncode:
        raise ExportError('Git read failed: ' + args[0])
    return result.stdout


def read_blobs(repo: Path, blob_ids) -> dict[str, bytes]:
    ids = sorted(set(blob_ids))
    if not all(re.fullmatch('[0-9a-f]{40}', value) for value in ids):
        raise ExportError('Invalid blob identity')
    result = subprocess.run(['git', 'cat-file', '--batch'], cwd=repo,
                            input=('\n'.join(ids) + '\n').encode(), capture_output=True)
    if result.returncode:
        raise ExportError('Git blob read failed')
    data, offset, blobs = result.stdout, 0, {}
    for expected in ids:
        end = data.find(b'\n', offset)
        header = data[offset:end].decode('ascii').split()
        if end < 0 or len(header) != 3 or header[:2] != [expected, 'blob'] or not header[2].isdigit():
            raise ExportError('Invalid Git blob response')
        start, size = end + 1, int(header[2])
        if start + size >= len(data) or data[start + size:start + size + 1] != b'\n':
            raise ExportError('Truncated Git blob response')
        blobs[expected] = data[start:start + size]
        offset = start + size + 1
    if offset != len(data):
        raise ExportError('Unexpected Git blob output')
    return blobs


def safe_path(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not any((
        value.startswith('/'), '\\' in value, ':' in value, '\x00' in value,
        path.as_posix() != value,
        any(part.casefold() in {'.', '..', '.git'} or part.endswith((' ', '.')) for part in path.parts),
        any(part.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL',
            *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
            for part in path.parts),
    ))


def linked(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def export(repo: Path, manifest: dict, dest: Path) -> dict:
    if not dest.is_absolute() or dest.exists() or not dest.parent.is_dir():
        raise ExportError('Destination must be new, absolute, with an existing parent')
    if any(linked(p) for p in (repo, *repo.parents, dest, *dest.parents)):
        raise ExportError('Linked paths are not supported')
    repo, dest = repo.resolve(strict=True), dest.resolve()
    if dest.is_relative_to(repo) or repo.is_relative_to(dest):
        raise ExportError('Destination must be separate from source')
    commit = manifest.get('commit', '')
    if not isinstance(commit, str) or not re.fullmatch('[0-9a-f]{40}', commit):
        raise ExportError('Manifest must pin a full commit')
    if manifest.get('schema_version') != 1 or manifest.get('publication_authorized') is not False:
        raise ExportError('Only review-only schema 1 manifests accepted')
    if git(repo, 'rev-parse', '--verify', commit + '^{commit}').decode().strip() != commit:
        raise ExportError('Commit identity mismatch')
    entries = {}
    for row in git(repo, 'ls-tree', '-rz', '--full-tree', commit).split(b'\0'):
        if not row:
            continue
        meta, raw_path = row.split(b'\t', 1)
        mode, kind, blob = meta.decode().split()
        path = raw_path.decode('utf-8')
        if kind != 'blob' or mode not in {'100644', '100755'} or not safe_path(path):
            raise ExportError('Unsupported tracked path or mode')
        entries[path] = blob
    rows = manifest.get('files')
    if not isinstance(rows, list):
        raise ExportError('Manifest files must be a list')
    blobs = read_blobs(repo, entries.values())
    selected, reviewed, folded = {}, set(), set()
    for row in rows:
        if not isinstance(row, dict):
            raise ExportError('Invalid manifest row')
        path = row.get('path')
        if not isinstance(path, str) or not safe_path(path) or path not in entries:
            raise ExportError('Untracked or unsafe manifest path')
        if path in reviewed or path.casefold() in folded:
            raise ExportError('Duplicate or case-colliding path')
        reviewed.add(path)
        folded.add(path.casefold())
        disposition = row.get('disposition')
        if disposition not in {'include', 'exclude'} or not str(row.get('reason', '')).strip():
            raise ExportError('Each file requires an explicit disposition and reason')
        data = blobs[entries[path]]
        if row.get('sha256') != digest(data):
            raise ExportError('Reviewed content hash mismatch: ' + path)
        if disposition == 'exclude':
            if path.startswith('src/project_watch/') or (path.startswith('frontend/')
                    and not path.startswith('frontend/prototype/')):
                raise ExportError('Complete distribution cannot exclude runtime/analysis files')
            continue
        parts = PurePosixPath(path).parts
        if (any(p.casefold() in {'.venv', 'venv', 'node_modules', '__pycache__', '.env'} for p in parts)
                or path.lower().endswith(('.sqlite3', '.sqlite', '.db', '.log', '.pem', '.key', '.pyc'))
                or PurePosixPath(path).name.casefold().startswith('.env.')
                or path.startswith(('docs/acceptance/', 'docs/handoff/', 'docs/plans/', 'docs/reviews/', 'frontend/prototype/'))
                or path == 'CURRENT_SYSTEM.md'):
            raise ExportError('Private/history/generated artifact cannot be exported: ' + path)
        if _secret_findings(data, path):
            raise ExportError('Credential-shaped content requires review: ' + path)
        selected[path] = data
    if reviewed != set(entries):
        raise ExportError('Unclassified tracked files')
    required = {'pyproject.toml', 'README.md', 'LICENSE', 'src/project_watch/__init__.py'}
    if not required.issubset(selected):
        raise ExportError('Required developer files missing')
    # Preflight writes nothing. An I/O failure leaves no success receipt; callers
    # must retain/review that partial directory, never silently reuse it.
    dest.mkdir()
    hashes = {}
    for path, data in sorted(selected.items()):
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as output:
            output.write(data)
        hashes[path] = digest(data)
    return {'schema_version': 1, 'publication_authorized': False, 'source_commit': commit,
            'exported_files': len(hashes), 'files_sha256': hashes,
            'git_history_copied': False, 'content_and_rights_review_still_required': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--dest', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    if args.receipt.exists() or not args.receipt.parent.is_dir():
        raise ExportError('Receipt must be new')
    if args.receipt.resolve().is_relative_to(args.dest.resolve()):
        raise ExportError('Private receipt must stay outside export')
    result = export(args.repo, json.loads(args.manifest.read_text(encoding='utf-8-sig')), args.dest)
    with args.receipt.open('x', encoding='utf-8') as output:
        json.dump(result, output, indent=2)
    print(json.dumps({'exported_files': result['exported_files'], 'publication_authorized': False}))


if __name__ == '__main__':
    main()

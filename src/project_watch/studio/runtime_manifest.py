"""Startup package snapshot: distinguish installed files from repository HEAD.

This reports file hashes captured at application construction, not an attestation
of every dynamically imported instruction or a historical build signature.
No data-directory paths, model settings, project names or credentials are read.
"""

import hashlib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def snapshot(root=None):
    root = Path(root) if root else Path(__file__).resolve().parent.parent
    package_version = 'source-uninstalled'
    for distribution in ('projectwatch-local', 'greenpine-project-watch'):
        try:
            package_version = version(distribution)
            break
        except PackageNotFoundError:
            continue
    backend = hashlib.sha256()
    for path in sorted((root / 'studio').glob('*.py')):
        backend.update(path.name.encode() + b'\0' + path.read_bytes() + b'\0')
    frontend = hashlib.sha256()
    for name in ('index.html', 'assets/app.js', 'assets/index.css'):
        path = root / 'web' / name
        frontend.update(name.encode() + b'\0' + (path.read_bytes() if path.is_file() else b'missing') + b'\0')
    return {'package_version': package_version, 'backend_digest': backend.hexdigest(),
            'frontend_digest': frontend.hexdigest(), 'basis': 'package_files_at_application_start'}

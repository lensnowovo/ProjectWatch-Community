"""Freeze an explicitly resolved Windows wheelhouse; no downloads or installation."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path

from packaging.tags import compatible_tags, cpython_tags
from packaging.utils import canonicalize_name

from scripts.private_delivery import linked, wheel_record

TARGET = 'cp313-win_amd64'
EXTRAS = 'desktop,ocr,mcp,team,postgres'


def inventory(wheelhouse, product):
    if linked(wheelhouse) or linked(product):
        raise ValueError('Linked inputs are not accepted')
    tags = set(cpython_tags((3, 13), platforms=['win_amd64']))
    tags.update(compatible_tags((3, 13), interpreter='cp313', platforms=['win_amd64']))
    wheels = sorted(wheelhouse.iterdir())
    records = [wheel_record(path, compatible_tags=tags) for path in wheels]
    if len({r['name'] for r in records}) != len(records):
        raise ValueError('Duplicate dependency versions')
    chosen = wheel_record(product, compatible_tags=tags)
    if chosen['name'] != 'projectwatch-local' or chosen not in records:
        raise ValueError('Wheelhouse must contain the exact product wheel')
    if any(r['name'] == 'greenpine-project-watch' for r in records):
        raise ValueError('Legacy product must not be included')
    return {'format': 'projectwatch-windows-dependencies-v1', 'target': TARGET,
            'extras': EXTRAS, 'wheels': records, 'product': chosen,
            'publication_authorized': False}


def verify(lock, wheelhouse, product):
    expected = json.loads(lock.read_text(encoding='utf-8'))
    actual = inventory(wheelhouse, product)
    if expected != actual:
        raise ValueError('Dependency lock differs from supplied wheels')
    return actual


def requirements(value):
    # Records have already been validated against wheel filenames and metadata.
    return ''.join(f"{r['name']}"
                   + (f'[{EXTRAS}]' if r['name'] == 'projectwatch-local' else '')
                   + f"=={r['version']} --hash=sha256:{r['sha256']}\n"
                   for r in value['wheels'])


def verify_installed(value, site):
    expected = {r['name']: r['version'] for r in value['wheels']}
    actual = {}
    for dist in importlib.metadata.distributions(path=[str(site)]):
        name = canonicalize_name(dist.metadata['Name'])
        if name in actual:
            raise ValueError('Duplicate installed dependency metadata')
        actual[name] = dist.version
    if actual != expected:
        raise ValueError('Installed dependencies differ from frozen lock')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('wheelhouse', 'product', 'out'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise ValueError('Lock output already exists')
    result = inventory(args.wheelhouse, args.product)
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps({'wheels': len(result['wheels']), 'target': TARGET,
                      'publication_authorized': False}))


if __name__ == '__main__':
    main()

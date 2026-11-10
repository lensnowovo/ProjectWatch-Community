"""Write a synthetic-only receipt for one already-built Project Watch wheel."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path


def _verify_distribution(archive, wheel, source_root):
    """Match the full bundle's install paths and resolver inputs to the static project metadata."""
    from packaging.markers import Marker
    from packaging.requirements import Requirement
    from packaging.specifiers import SpecifierSet
    from packaging.utils import canonicalize_name, parse_wheel_filename
    from packaging.version import Version

    project = tomllib.loads((source_root / 'pyproject.toml').read_text(encoding='utf-8'))['project']
    name, version, _, _ = parse_wheel_filename(wheel.name)
    if (name, version) != (canonicalize_name(project['name']), Version(project['version'])):
        raise ValueError('Wheel metadata identity differs from source')
    dist_info = '-'.join(wheel.name.split('-')[:2]) + '.dist-info'
    licenses = {p.relative_to(source_root).as_posix(): p for pattern in project.get('license-files', [])
                for p in source_root.glob(pattern) if p.is_file()}
    required = {f'{dist_info}/{key}' for key in ('METADATA', 'WHEEL', 'RECORD')}
    required.update(f'{dist_info}/licenses/{path}' for path in licenses)
    allowed = required | {f'{dist_info}/top_level.txt'}
    files = [n for n in archive.namelist() if not n.endswith('/')]
    outside_product = {n for n in files if not n.startswith('project_watch/')}
    if len(files) != len(set(files)) or not required <= outside_product <= allowed:
        raise ValueError('Unexpected wheel installation inventory')
    for relative, source in licenses.items():
        if archive.read(f'{dist_info}/licenses/{relative}') != source.read_bytes():
            raise ValueError('Wheel license differs from source: ' + relative)

    metadata = BytesParser().parsebytes(archive.read(f'{dist_info}/METADATA'))
    for key, value in [('Name', project['name']), ('Version', project['version'])]:
        if metadata.get_all(key) != [value]:
            raise ValueError('Wheel metadata differs from source: ' + key)
    python = metadata.get_all('Requires-Python', [])
    if len(python) != 1 or SpecifierSet(python[0]) != SpecifierSet(project['requires-python']):
        raise ValueError('Wheel metadata differs from source: Requires-Python')

    expected = {Requirement(value) for value in project.get('dependencies', [])}
    extras = project.get('optional-dependencies', {})
    for group, values in extras.items():
        extra = f'extra == "{canonicalize_name(group)}"'
        for value in values:
            dependency = Requirement(value)
            dependency.marker = Marker(f'({dependency.marker}) and {extra}' if dependency.marker else extra)
            expected.add(dependency)
    if {canonicalize_name(e) for e in metadata.get_all('Provides-Extra', [])} != {canonicalize_name(e) for e in extras}:
        raise ValueError('Wheel metadata differs from source: Provides-Extra')
    if {Requirement(value) for value in metadata.get_all('Requires-Dist', [])} != expected:
        raise ValueError('Wheel metadata differs from source: Requires-Dist')
    readme = (source_root / project['readme']).read_text(encoding='utf-8')
    description = metadata.get_payload(decode=True).decode('utf-8').replace('\r\n', '\n')
    if description.rstrip('\n') != readme.rstrip('\n'):
        raise ValueError('Wheel metadata differs from source: README')


def verify_source_correspondence(wheel: Path, source_root: Path) -> dict:
    """Reject deleted/stale/missing product files, including stale build-directory modules."""
    product = source_root / 'src' / 'project_watch'
    expected = {p.relative_to(source_root / 'src').as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in product.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}
    if not expected:
        raise ValueError('empty source product inventory')
    with zipfile.ZipFile(wheel) as archive:
        names = [n for n in archive.namelist() if n.startswith('project_watch/') and not n.endswith('/')]
        actual = {n: hashlib.sha256(archive.read(n)).hexdigest() for n in names}
        if len(names) != len(actual) or actual != expected:
            raise ValueError('wheel/source correspondence failed: extra=' + str(len(actual.keys()-expected.keys()))
                             + ', missing=' + str(len(expected.keys()-actual.keys()))
                             + ', changed=' + str(sum(actual[k] != expected[k] for k in actual.keys() & expected.keys())))
        _verify_distribution(archive, wheel, source_root)
    return dict(product_files=len(expected), source_sha256=hashlib.sha256(
        json.dumps(expected, sort_keys=True).encode()).hexdigest())


def build_manifest(wheel: Path, commit: str) -> dict[str, str | int | bool]:
    from scripts.release_audit import audit_wheel

    if wheel.suffix != ".whl" or not wheel.is_file():
        raise ValueError("wheel must be an existing .whl file")
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("commit must be a full lowercase Git SHA")
    count, findings = audit_wheel(wheel)
    if count == 0 or findings:
        raise ValueError("wheel inventory rejected")
    with wheel.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    return {
        "schema_version": 1,
        "commit": commit,
        "wheel": wheel.name,
        "sha256": digest,
        "size_bytes": wheel.stat().st_size,
        "scope": "synthetic_only",
        "publication_authorized": False,
    }


def write_manifest(wheel: Path, commit: str, output: Path) -> None:
    receipt = build_manifest(wheel, commit)
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    write_manifest(args.wheel, args.commit, args.output)


if __name__ == "__main__":
    main()

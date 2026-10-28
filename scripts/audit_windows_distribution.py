"""Inventory the actual Windows bundle, never the developer environment.

Offline evidence, not legal clearance, vulnerability clearance or publication
authority. Only file paths relative to the bundle are recorded.
"""
import argparse
import importlib.metadata
import json
from pathlib import Path

from scripts.windows.maintain import digest, manifest, verify


def audit(bundle):
    bundle = Path(bundle).resolve()
    value = manifest(bundle)
    verify(bundle, value)
    actual = {p.relative_to(bundle).as_posix() for p in bundle.rglob('*') if p.is_file()}
    shared = {f'runtimes/{rid}/{name}' for rid, entry in value.get('runtimes', {}).items()
              for name in entry['files']}
    listed = set(value['files']) | shared
    if actual != listed | {'bundle-manifest.json'}:
        raise ValueError('Unlisted or missing bundle files')
    site = bundle/'runtime/Lib/site-packages'
    packages = []
    for dist in importlib.metadata.distributions(path=[str(site)]):
        licenses = {}
        for entry in dist.files or []:
            name = str(entry).replace('\\', '/')
            if any(word in Path(name).name.lower() for word in ('license', 'licence', 'copying', 'notice', 'copyright')):
                path = Path(dist.locate_file(entry)).resolve()
                if path.is_relative_to(bundle) and path.is_file():
                    licenses[path.relative_to(bundle).as_posix()] = digest(path)
        meta = dist.metadata
        declaration = meta.get('License-Expression') or meta.get('License') or ''
        classifiers = [c for c in meta.get_all('Classifier', []) if c.startswith('License ::')]
        packages.append({'name': meta['Name'], 'version': dist.version,
                         'license_declaration': declaration, 'license_classifiers': classifiers,
                         'license_files': licenses, 'manual_review_required': not bool(licenses)})
    packages.sort(key=lambda p: p['name'].lower())
    models = [name for name in sorted(actual) if Path(name).suffix.lower() in ('.onnx', '.pt', '.safetensors')]
    return {'format': 'windows-distribution-inventory-v1', 'version': value['version'],
            'bundle_manifest_sha256': digest(bundle/'bundle-manifest.json'),
            'files_verified': len(listed), 'python_runtime': value['python'],
            'shared_runtimes': {rid: {'files_verified': len(entry['files'])}
                                for rid, entry in value.get('runtimes', {}).items()},
            'packages': packages, 'model_files': models,
            'missing_license_file_packages': [p['name'] for p in packages if p['manual_review_required']],
            'publication_authorized': False,
            'limitations': ['License metadata is not legal clearance.',
                           'Native bundled libraries and downloaded OCR models need separate review.',
                           'No vulnerability or malware scan is performed by this inventory.']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.bundle)
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False)
    print(json.dumps({'packages': len(result['packages']), 'files_verified': result['files_verified'],
                      'missing_license_files': result['missing_license_file_packages'],
                      'model_files': len(result['model_files']), 'publication_authorized': False}))

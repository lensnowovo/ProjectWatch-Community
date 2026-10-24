"""No-network smoke in a fresh environment installed from a full wheel.

Run with that environment's python -I, never an editable install. It verifies the
installed bytes against the wheel and tests synthetic local persistence. This is
not desktop lifecycle, OCR model, native DSH, or real monitoring acceptance.
The basic smoke has no worker; --full-journey also runs the synthetic worker flow.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import io
import json
import os
import runpy
import sys
import zipfile
from pathlib import Path


def verify_installed_product(wheel, package):
    expected = set()
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if not name.startswith('project_watch/') or name.endswith('/'):
                continue
            relative = name.removeprefix('project_watch/')
            if relative in expected or '..' in Path(relative).parts:
                raise ValueError('Invalid product wheel inventory')
            expected.add(relative)
            installed = package / relative
            if not installed.is_file() or hashlib.sha256(installed.read_bytes()).digest() != hashlib.sha256(archive.read(name)).digest():
                raise ValueError('Installed product differs from wheel: ' + name)
    actual = {p.relative_to(package).as_posix() for p in package.rglob('*') if p.is_file()
              and '__pycache__' not in p.relative_to(package).parts}
    if not expected or actual != expected:
        raise ValueError('Installed product inventory differs from wheel')
    return len(expected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--full-journey', action='store_true', help='Also run the existing synthetic install journey')
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    if output.exists() or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError('Smoke output must be new and outside source')
    # This imports only the safety helpers, not source product modules.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from run_offline_tests import clean_environment, network_guard

    clean = clean_environment(os.environ, output)
    os.environ.clear()
    os.environ.update(clean)
    sys.addaudithook(network_guard)
    distribution = importlib.metadata.distribution('projectwatch-local')
    import project_watch

    package = Path(project_watch.__file__).resolve().parent
    if package.is_relative_to(source) or package.parent != Path(distribution.locate_file('')).resolve():
        raise ValueError('Smoke must use the installed wheel, not source/editable files')
    count = verify_installed_product(args.wheel, package)
    # Import the shipped optional capabilities without invoking a model/download.
    modules = ['onnxruntime', 'cv2', 'pypdfium2', 'rapidocr', 'argon2', 'mcp', 'psycopg']
    if sys.platform == 'darwin':
        modules.extend(['objc', 'AppKit'])
    for name in modules:
        importlib.import_module(name)
    from fastapi.testclient import TestClient
    from pypdf import PdfReader, PdfWriter

    from project_watch.studio.app import create_studio_app

    pdf = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(pdf)
    assert len(PdfReader(io.BytesIO(pdf.getvalue())).pages) == 1
    output.mkdir(parents=True)
    data = output / 'data'
    for round_number in range(2):
        app = create_studio_app(data, start_worker=False)
        with TestClient(app) as client:
            assert client.get('/').status_code == 200
            assert client.get('/assets/app.js').status_code == 200
            if round_number == 0:
                response = client.post('/api/studio/projects', json={
                    'entity': {'legal_name': 'Synthetic CI Optics', 'short_name': 'Synthetic CI'},
                    'consent': True, 'execution_mode': 'agent',
                })
                assert response.status_code == 201, response.status_code
                project_id = response.json()['id']
                assert response.json()['status'] == 'paused'
            rows = client.get('/api/studio/projects').json()
            assert len(rows) == 1 and rows[0]['id'] == project_id and rows[0]['status'] == 'paused'
    if args.full_journey:
        # Retain the established name/BP/analysis/compatibility acceptance. The
        # product has already been imported from the verified installed wheel.
        journey = source / 'tests/installed_wheel_smoke.py'
        saved_argv = sys.argv
        try:
            sys.argv = [str(journey)]
            runpy.run_path(str(journey), run_name='__main__')
        finally:
            sys.argv = saved_argv
    receipt = {'version': distribution.version, 'product_files_verified': count,
               'native_imports': modules, 'synthetic_persistence_reopened': True,
               'synthetic_installed_journey': args.full_journey,
               'real_provider_calls': 0, 'desktop_acceptance': False,
               'publication_authorized': False}
    (output / 'receipt.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    print(json.dumps(receipt))


if __name__ == '__main__':
    main()

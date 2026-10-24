"""Browser assets must not inherit broken MIME mappings from a Windows host."""
import mimetypes

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from project_watch.api import create_app
from project_watch.static_files import NoStoreStaticFiles
from project_watch.studio.app import create_studio_app


@pytest.fixture
def misleading_host_mime(monkeypatch):
    mimetypes.init()
    for suffix in ('.js', '.mjs', '.css', '.html', '.svg', '.woff2'):
        monkeypatch.setitem(mimetypes.types_map, suffix, 'text/plain')


@pytest.mark.parametrize(('suffix', 'expected'), [
    ('.js', 'text/javascript'), ('.mjs', 'text/javascript'),
    ('.css', 'text/css'), ('.html', 'text/html'),
    ('.svg', 'image/svg+xml'), ('.woff2', 'font/woff2'),
])
def test_static_types_ignore_host_mapping(tmp_path, misleading_host_mime, suffix, expected):
    (tmp_path / ('asset' + suffix)).write_bytes(b'synthetic browser asset')
    app = FastAPI()
    app.mount('/assets', NoStoreStaticFiles(directory=tmp_path))
    with TestClient(app) as client:
        result = client.get('/assets/asset' + suffix)
        assert result.status_code == 200
        assert result.headers['content-type'].split(';')[0] == expected
        assert result.headers['cache-control'] == 'no-store'
        head = client.head('/assets/asset' + suffix)
        assert head.status_code == 200 and not head.content
        assert head.headers['content-type'] == result.headers['content-type']
        partial = client.get('/assets/asset' + suffix, headers={'Range': 'bytes=0-8'})
        assert partial.status_code == 206 and partial.content == b'synthetic'
        assert partial.headers['content-type'] == result.headers['content-type']
        cached = client.get('/assets/asset' + suffix, headers={'If-None-Match': result.headers['etag']})
        assert cached.status_code == 304 and not cached.content
        assert 'content-type' not in cached.headers


def test_classic_shell_and_modules_ignore_host_mapping(tmp_path, misleading_host_mime):
    with TestClient(create_app(tmp_path / 'classic.sqlite3')) as client:
        assert client.get('/').headers['content-type'].split(';')[0] == 'text/html'
        for path in ('/static/app.js', '/static/upload_batch.mjs'):
            assert client.get(path).headers['content-type'].split(';')[0] == 'text/javascript'
        assert client.get('/static/styles.css').headers['content-type'].split(';')[0] == 'text/css'
        assert client.get('/static/../../classic.sqlite3').status_code == 404


def test_studio_shell_and_assets_ignore_host_mapping(tmp_path, misleading_host_mime, monkeypatch):
    from project_watch.studio import app as studio_app

    web = tmp_path / 'web'
    assets = web / 'assets'
    assets.mkdir(parents=True)
    (web / 'index.html').write_text('<html>synthetic studio</html>', encoding='utf-8')
    (assets / 'app.js').write_text('export const synthetic = true;', encoding='utf-8')
    (assets / 'app.css').write_text('body { color: black; }', encoding='utf-8')
    monkeypatch.setattr(studio_app, 'WEB_ROOT', web)
    with TestClient(create_studio_app(tmp_path / 'data', start_worker=False)) as client:
        for path in ('/', '/projects/synthetic', '/settings', '/classic'):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers['content-type'].split(';')[0] == 'text/html'
        for path, expected in (('/assets/app.js', 'text/javascript'), ('/assets/app.css', 'text/css')):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers['content-type'].split(';')[0] == expected
        assert client.get('/assets/../../data/projectwatch.sqlite3').status_code == 404

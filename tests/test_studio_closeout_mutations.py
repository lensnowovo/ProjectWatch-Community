"""Synthetic only: interactive commits and populated stopped-home recovery."""

import json
from contextlib import closing

import pytest
from distribution_journey_support import COMPANY, configure, make_app
from fastapi.testclient import TestClient

from project_watch.studio import host
from project_watch.studio.direct_operations import OperationBlocked


def project_body():
    return {'entity': {'legal_name': COMPANY, 'short_name': '合成光学'},
            'consent': True, 'watchpoints': ['新产品进展']}


def test_create_replays_one_project_watch_and_job(tmp_path):
    app, transport = make_app(tmp_path)
    with TestClient(app) as client:
        configure(client)
        first = client.post('/api/studio/projects', json=project_body())
        assert first.status_code == 201, first.text
        count = len(transport.calls)
        second = client.post('/api/studio/projects', json=project_body())
        assert second.json()['id'] == first.json()['id']
        assert second.headers['X-ProjectWatch-Replayed'] == 'true'
        assert len(transport.calls) == count
        assert len(client.get('/api/studio/projects').json()) == 1
        pipeline = app.state.studio_pipeline
        assert len(pipeline.monitor.watchpoints(first.json()['id'])) == 1
        with closing(pipeline.store._connect()) as db:
            assert db.execute('SELECT count(*) FROM studio_jobs').fetchone()[0] == 1
        denied = {**project_body(), 'consent': False}
        assert client.post('/api/studio/projects', json=denied).status_code == 422


def test_create_unknown_does_not_publish_and_restart_does_not_retry(tmp_path):
    app, transport = make_app(tmp_path)
    with TestClient(app) as client:
        configure(client)
        transport.model_failure = 'offline'
        first = client.post('/api/studio/projects', json=project_body())
        assert first.status_code == 409, first.text
        assert client.get('/api/studio/projects').json() == []
        count = len(transport.calls)
        assert client.post('/api/studio/projects', json=project_body()).status_code == 409
        assert len(transport.calls) == count
    app, transport = make_app(tmp_path)
    with TestClient(app) as client:
        assert client.post('/api/studio/projects', json=project_body()).status_code == 409
        assert not transport.calls
        operation = first.json()['operation_id']
        assert client.post(f'/api/studio/operations/{operation}/retry',
                           json={'accept_duplicate_cost': True}).status_code == 200
        assert client.post('/api/studio/projects', json=project_body()).status_code == 201
        assert len(client.get('/api/studio/projects').json()) == 1


def test_commit_failure_rolls_back_and_reuses_successful_paid_preparation(tmp_path, monkeypatch):
    app, transport = make_app(tmp_path)
    pipeline = app.state.studio_pipeline
    original = pipeline.store.enqueue
    with TestClient(app) as client:
        configure(client)
        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise ValueError('synthetic local failure')
        monkeypatch.setattr(pipeline.store, 'enqueue', fail)
        assert client.post('/api/studio/projects', json=project_body()).status_code == 422
        with closing(pipeline.store._connect()) as db:
            for table in ('projects', 'studio_projects', 'studio_watchpoints', 'studio_jobs'):
                assert db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
        count = len(transport.calls)
        operation = client.get('/api/studio/operations').json()[0]
        assert operation['status'] == 'failed'
        client.post(f"/api/studio/operations/{operation['id']}/retry", json={'accept_duplicate_cost': True})
        monkeypatch.setattr(pipeline.store, 'enqueue', original)
        assert client.post('/api/studio/projects', json=project_body()).status_code == 201
        assert len(transport.calls) == count


def test_watchpoint_unknown_replay_and_deleted_result(tmp_path):
    app, transport = make_app(tmp_path)
    with TestClient(app) as client:
        configure(client)
        pid = client.post('/api/studio/projects', json={**project_body(), 'watchpoints': []}).json()['id']
        endpoint = f'/api/studio/projects/{pid}/watchpoints'
        transport.model_failure = 'offline'
        assert client.post(endpoint, json={'text': '融资进度'}).status_code == 409
        assert app.state.studio_pipeline.monitor.watchpoints(pid) == []
        transport.model_failure = ''
        first = client.post(endpoint, json={'text': '产品进度'})
        assert first.status_code == 201, first.text
        count = len(transport.calls)
        assert client.post(endpoint, json={'text': '产品进度'}).json()['id'] == first.json()['id']
        assert len(transport.calls) == count
        client.delete(f"/api/studio/watchpoints/{first.json()['id']}")
        assert client.post(endpoint, json={'text': '产品进度'}).status_code == 404
        assert app.state.studio_pipeline.monitor.watchpoints(pid) == []


def test_populated_backup_restores_originals_settings_jobs_and_held_receipts(tmp_path):
    home, backup, restored = (tmp_path / name for name in ('home', 'backup', 'restored'))
    app, transport = make_app(home)
    with TestClient(app) as client:
        configure(client)
        draft = client.post('/api/studio/drafts', files={'file': ('synthetic.txt', b'Synthetic original', 'text/plain')})
        assert draft.status_code == 200, draft.text
        body = {**project_body(), 'draft_ids': [draft.json()['draft_id']]}
        created = client.post('/api/studio/projects', json=body)
        assert created.status_code == 201, created.text
        pid = created.json()['id']
        transport.model_failure = 'offline'
        held = client.post('/api/studio/resolve', json={'name': '另一个合成公司'})
        assert held.status_code == 409
        store = app.state.studio_pipeline.store
        with closing(store._connect()) as db:
            tables = ('projects', 'studio_projects', 'sources', 'intake_jobs', 'studio_watchpoints',
                      'studio_jobs', 'studio_direct_operations', 'studio_direct_calls')
            before = {t: [tuple(r) for r in db.execute(f'SELECT * FROM {t} ORDER BY rowid')] for t in tables}
    manifest = host.snapshot(home, backup)
    host.restore_snapshot(backup, restored)
    assert host.verify_snapshot(restored) == manifest
    for name, expected in manifest['files'].items():
        assert host.digest(restored / name) == expected
    reopened, fresh_transport = make_app(restored)
    with TestClient(reopened) as client:
        assert client.get(f'/api/studio/projects/{pid}').status_code == 200
        assert client.post('/api/studio/resolve', json={'name': '另一个合成公司'}).status_code == 409
        assert not fresh_transport.calls
        with closing(reopened.state.studio_pipeline.store._connect()) as db:
            after = {t: [tuple(r) for r in db.execute(f'SELECT * FROM {t} ORDER BY rowid')] for t in tables}
        assert before == after


def test_interrupted_restore_never_publishes_target(tmp_path, monkeypatch):
    home, backup, restored = (tmp_path / name for name in ('home', 'backup', 'restored'))
    app, _ = make_app(home)
    with TestClient(app):
        pass
    host.snapshot(home, backup)
    original = host.shutil.copy2
    def broken_copy(source, target):
        original(source, target)
        raise OSError('synthetic disk full')
    monkeypatch.setattr(host.shutil, 'copy2', broken_copy)
    with pytest.raises(OSError):
        host.restore_snapshot(backup, restored)
    assert not restored.exists()
    assert list(tmp_path.glob('.restored-restore-*'))
    host.verify_snapshot(backup)


@pytest.mark.parametrize('name', ['.studio.lock', 'nested/data.sqlite3-wal', 'studio-manifest.json'])
def test_restore_rejects_runtime_reserved_paths(tmp_path, name):
    manifest = {'format': 'studio-home-v1', 'files': {name: 'unused', 'projectwatch.sqlite3': 'unused'}}
    (tmp_path / 'studio-manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='路径'):
        host.verify_snapshot(tmp_path)


def test_unserializable_commit_rolls_back_business_changes(tmp_path):
    app, _ = make_app(tmp_path)
    pipeline = app.state.studio_pipeline
    operations = pipeline.direct_operations
    def commit(_, db):
        db.execute("INSERT INTO studio_state(key,value) VALUES ('synthetic-atomic','test')")
        return object()
    with pytest.raises(TypeError):
        operations.run('synthetic', {}, pipeline.settings_store.load(), lambda: {}, commit=commit)
    with closing(pipeline.store._connect()) as db:
        assert db.execute("SELECT value FROM studio_state WHERE key='synthetic-atomic'").fetchone() is None
    with pytest.raises(OperationBlocked):
        operations.run('synthetic', {}, pipeline.settings_store.load(), lambda: {})


def test_missing_draft_does_not_charge_or_silently_create_without_document(tmp_path):
    app, transport = make_app(tmp_path)
    with TestClient(app) as client:
        configure(client)
        body = {**project_body(), 'draft_ids': ['missing']}
        assert client.post('/api/studio/projects', json=body).status_code == 404
        assert not transport.calls
        assert not client.get('/api/studio/projects').json()


def test_watchpoint_does_not_resume_paused_project(tmp_path):
    app, transport = make_app(tmp_path)
    with TestClient(app) as client:
        configure(client)
        pid = client.post('/api/studio/projects', json={**project_body(), 'watchpoints': []}).json()['id']
        store = app.state.studio_pipeline.store
        with closing(store._connect()) as db, db:
            db.execute("UPDATE studio_projects SET status='paused' WHERE project_id=?", (pid,))
            db.execute("UPDATE studio_jobs SET status='done' WHERE project_id=?", (pid,))
        endpoint = f'/api/studio/projects/{pid}/watchpoints'
        assert client.post(endpoint, json={'text': '新产品'}).status_code == 201
        with closing(store._connect()) as db:
            assert db.execute("SELECT count(*) FROM studio_jobs WHERE status='queued'").fetchone()[0] == 0
        store.remove_project(pid)
        count = len(transport.calls)
        assert client.post(endpoint, json={'text': '另一个产品'}).status_code == 404
        assert len(transport.calls) == count


def test_restore_detects_copy_corruption_without_publishing(tmp_path, monkeypatch):
    home, backup, restored = (tmp_path / name for name in ('home', 'backup', 'restored'))
    app, _ = make_app(home)
    with TestClient(app):
        pass
    host.snapshot(home, backup)
    original = host.shutil.copy2
    def corrupt_copy(source, target):
        original(source, target)
        with target.open('ab') as stream:
            stream.write(b'corrupt')
    monkeypatch.setattr(host.shutil, 'copy2', corrupt_copy)
    with pytest.raises(ValueError, match='恢复校验失败'):
        host.restore_snapshot(backup, restored)
    assert not restored.exists()
    host.verify_snapshot(backup)

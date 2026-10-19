from test_studio_agent_instance import make_service

from project_watch.studio.runtime_manifest import snapshot


def test_package_snapshot_hashes_files_not_data_paths(tmp_path):
    (tmp_path / 'studio').mkdir()
    (tmp_path / 'web/assets').mkdir(parents=True)
    (tmp_path / 'studio/synthetic.py').write_text('synthetic=1', encoding='utf-8')
    (tmp_path / 'web/assets/app.js').write_text('synthetic', encoding='utf-8')
    initial = snapshot(tmp_path)
    (tmp_path / 'studio/synthetic.py').write_text('synthetic=2', encoding='utf-8')
    changed = snapshot(tmp_path)
    assert initial['backend_digest'] != changed['backend_digest']
    assert initial['frontend_digest'] == changed['frontend_digest']
    assert str(tmp_path) not in str(initial)


def test_manifest_snapshot_is_retained_and_defensive(tmp_path, monkeypatch):
    service, _ = make_service(tmp_path / 'manifest.sqlite3')
    initial = service.manifest()
    monkeypatch.setattr('project_watch.studio.runtime_manifest.snapshot', lambda: {'changed': True})
    initial['runtime']['backend_digest'] = 'caller-mutated'
    assert service.manifest()['runtime']['backend_digest'] != 'caller-mutated'
    assert 'changed' not in service.manifest()['runtime']

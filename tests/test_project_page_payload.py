"""The project page receives only the relationship fields it shows."""
from project_watch.studio.app import _RELATIONSHIP_PAGE_FIELDS, _relationships_for_page


def test_relationship_rows_keep_page_fields_and_drop_internal_detail():
    row = {field: 'x' for field in _RELATIONSHIP_PAGE_FIELDS}
    row.update(matching={'detail': 'y' * 500}, object_versions={'o': 1}, fact_ids=['f'], evidence_ids=['e'],
               assertion_basis='z', project_id='p')
    view = {'schema_version': 1, 'items': [row], 'exclusions': [dict(row)]}
    shown = _relationships_for_page(view)
    assert shown['schema_version'] == 1
    for key in ('items', 'exclusions'):
        assert set(shown[key][0]) == _RELATIONSHIP_PAGE_FIELDS
    assert 'matching' in view['items'][0]  # the stored view itself is not changed


def test_the_revision_changes_when_a_job_moves_and_stays_put_otherwise(tmp_path):
    from contextlib import closing

    from fastapi.testclient import TestClient

    from project_watch.studio.app import create_studio_app
    app = create_studio_app(tmp_path, start_worker=False)
    web = TestClient(app)
    pid = web.post('/api/studio/projects', json={'entity': {'legal_name': 'Synthetic Revision Co'}, 'consent': True}).json()['id']
    store = app.state.studio_store
    job = store.enqueue(pid, 'brief', input_version='manual:revision')
    first = web.get(f'/api/studio/projects/{pid}/revision').json()['revision']
    assert web.get(f'/api/studio/projects/{pid}/revision').json()['revision'] == first
    with closing(store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET status='running',stage='合成步骤' WHERE id=?", (job['id'],))
    assert web.get(f'/api/studio/projects/{pid}/revision').json()['revision'] != first
    assert web.get('/api/studio/projects/missing/revision').status_code == 404


def test_the_revision_changes_when_a_job_starts_waiting(tmp_path):
    from contextlib import closing

    from fastapi.testclient import TestClient

    from project_watch.studio.app import create_studio_app
    app = create_studio_app(tmp_path, start_worker=False)
    web = TestClient(app)
    pid = web.post('/api/studio/projects', json={'entity': {'legal_name': 'Synthetic Wait Co'}, 'consent': True}).json()['id']
    job = app.state.studio_store.enqueue(pid, 'brief', input_version='manual:wait')
    first = web.get(f'/api/studio/projects/{pid}/revision').json()['revision']
    with closing(app.state.studio_store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET retry_after='2999-01-01T00:00:00+00:00' WHERE id=?", (job['id'],))
    assert web.get(f'/api/studio/projects/{pid}/revision').json()['revision'] != first

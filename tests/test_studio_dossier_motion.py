"""Offline saved revision contract; no providers, real data or workers."""
from project_watch.studio.dossier_activity import saved_revisions


def test_revisions_are_content_based_not_task_or_clock_based():
    relation = {'id': 'r', 'scope_kind': 'relation', 'relation_target': 'Synthetic route',
                'reason': 'Same route', 'status': 'active', 'updated_at': 'old', 'hits': 1}
    analysis = {'headline': 'Synthetic judgment', 'position': {'verdict': 'unclear'}}
    before = saved_revisions([relation], {'analysis': analysis, 'generated_at': 'old'})
    after = saved_revisions([{**relation, 'updated_at': 'new', 'hits': 9}],
                            {'analysis': analysis, 'generated_at': 'new'})
    assert before == after
    assert before['relations']['count'] == before['judgment']['count'] == 1
    changed = saved_revisions([{**relation, 'reason': 'Changed reason'}],
                              {'analysis': {**analysis, 'headline': 'Changed judgment'}})
    assert changed['relations']['revision'] != before['relations']['revision']
    assert changed['judgment']['revision'] != before['judgment']['revision']
    assert 'Synthetic' not in str(before)


def test_questions_retired_objects_and_empty_judgments_are_not_saved_relations():
    result = saved_revisions([{'scope_kind': 'question'}, {'scope_kind': 'relation', 'status': 'retired'}], None)
    assert result['relations']['count'] == result['judgment']['count'] == 0
    assert saved_revisions([], {'analysis': {}}) == result


def test_project_get_is_read_only_and_reflects_persisted_scope(tmp_path):
    import json
    from contextlib import closing

    from fastapi.testclient import TestClient

    from project_watch.repository import ProjectRepository
    from project_watch.studio.app import create_studio_app
    app = create_studio_app(tmp_path, start_worker=False)
    store = app.state.studio_store
    pid = ProjectRepository(store.db_path).create_project('Synthetic Dossier').id
    store.upsert_project(pid, {'legal_name': 'Synthetic Dossier'}, consent=False)
    with TestClient(app) as client:
        first = client.get(f'/api/studio/projects/{pid}').json()
        assert first['dossier_saved']['relations']['count'] == 0
        with closing(store._connect()) as db, db:
            db.execute("INSERT INTO studio_watchpoints (id,project_id,text,label,queries,criteria,alert,created_at,scope_kind,relation_target,reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       ('synthetic-watch',pid,'Synthetic route','Route','[]','',1,'2026-10-22','relation','Route','Same route'))
        store.put_public_profile(pid, {'facts': [{'kind': 'product', 'label': 'Synthetic device',
                                                 'text': 'Research use only', 'date': '', 'refs': []}]})
        store.set_state(f'insight:{pid}', json.dumps({'analysis': {'headline': 'Synthetic saved judgment'}}))
        with closing(store._connect()) as db:
            before = list(db.iterdump())
        second = client.get(f'/api/studio/projects/{pid}').json()
        third = client.get(f'/api/studio/projects/{pid}').json()
        assert second['dossier_saved']['relations']['count'] == 1
        assert second['dossier_saved']['judgment']['count'] == 1
        assert second['public_profile']['facts'][0]['text'] == 'Research use only'
        assert second['dossier_saved'] == third['dossier_saved']
        with closing(store._connect()) as db:
            assert before == list(db.iterdump())

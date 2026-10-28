import json
import sqlite3
from contextlib import closing

import pytest
import test_collection_view as fixtures
from test_studio_event_context import _event


@pytest.fixture
def owner(tmp_path):
    return fixtures.owner.__wrapped__(tmp_path)


def head(store, event):
    with closing(store._connect()) as db:
        return db.execute('SELECT revision_id FROM studio_event_revision_heads WHERE event_id=?', (event,)).fetchone()[0]


def pair(owner):
    pipe, _, pid, _ = owner
    execution, _, _, _ = fixtures.save(owner)
    a_item, a = _event(pipe.store, pid, 'SyntheticA', 'Synthetic source assertion A')
    b_item, b = _event(pipe.store, pid, 'SyntheticB', 'Synthetic source assertion B')
    return execution, a, b, a_item, b_item


def test_merge_replay_and_undo_preserve_sources_and_history(owner):
    execution, a, b, ai, bi = pair(owner)
    store, pid = owner[0].store, owner[2]
    store.set_event_status(b, 'read')
    before = {event: head(store, event) for event in (a, b)}
    receipt = store.event_revisions.merge(execution, a, before)
    assert store.event_revisions.merge(execution, a, before) == receipt
    visible = store.list_events(project_id=pid)
    assert len(visible) == 1 and {i['id'] for i in visible[0]['sources']} == {ai, bi}
    assert visible[0]['title'] == 'SyntheticA'  # no invented merged interpretation
    store.event_revisions.undo_merge(execution, receipt)
    store.event_revisions.undo_merge(execution, receipt)
    events = {event['id']: event for event in store.list_events(project_id=pid)}
    assert {i['id'] for i in events[a]['sources']} == {ai}
    assert {i['id'] for i in events[b]['sources']} == {bi}
    assert events[b]['status'] == 'read'
    assert head(store, a) != before[a]  # restoring content adds history, never rewinds it
    with closing(store._connect()) as db:
        assert db.execute('SELECT count(*) FROM studio_event_revisions WHERE event_id=?', (a,)).fetchone()[0] == 3
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            db.execute("UPDATE studio_event_revisions SET record='{}' WHERE id=?", (before[a],))
    with pytest.raises(ValueError, match='undone'):
        store.event_revisions.merge(execution, a, before)


def test_event_change_invalidates_only_bound_analysis(owner):
    execution, a, b, _, _ = pair(owner)
    store, pid = owner[0].store, owner[2]
    dep = store.evidence.dependencies
    dep.track(pid, 'bound', 'brief', [])
    dep.track(pid, 'unrelated', 'brief', [])
    before = {event: head(store, event) for event in (a, b)}
    with closing(store._connect()) as db, db:
        store.event_revisions.bind(pid, 'bound', {a: before[a]}, db=db)
    store.event_revisions.merge(execution, a, before)
    assert dep.get(pid, 'bound')['stale_reason'] == 'event_revised'
    assert dep.get(pid, 'unrelated')['status'] == 'current'
    with closing(store._connect()) as db:
        assert db.execute("SELECT count(*) FROM studio_rebuild_requests WHERE artifact_key='bound'").fetchone()[0] == 1


def test_late_undo_rejects_intervening_human_edit(owner):
    execution, a, b, _, _ = pair(owner)
    store = owner[0].store
    receipt = store.event_revisions.merge(execution, a, {event: head(store, event) for event in (a, b)})
    store.attach_to_event(a, [], importance='major', summary='new accepted summary')
    with pytest.raises(ValueError, match='changed since review'):
        store.event_revisions.undo_merge(execution, receipt)
    assert store.list_events(project_id=owner[2])[0]['summary'] == 'new accepted summary'


def test_cross_project_and_lost_lease_cannot_merge(owner):
    execution, a, b, _, _ = pair(owner)
    store = owner[0].store
    foreign = owner[0].repository.create_project('Foreign synthetic')
    _, other = _event(store, foreign.id, 'Other', 'Other synthetic source')
    with pytest.raises(ValueError, match='changed since review'):
        store.event_revisions.merge(execution, a, {a: head(store, a), other: head(store, other)})
    with closing(store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET lease_token='new-owner' WHERE id=?", (execution.job['id'],))
    with pytest.raises(RuntimeError, match='lease expired'):
        store.event_revisions.merge(execution, a, {a: head(store, a), b: head(store, b)})


def test_membership_move_records_both_event_heads(owner):
    _, a, b, _, bi = pair(owner)
    store = owner[0].store
    old_b = head(store, b)
    store.attach_to_event(a, [bi], importance='routine')
    assert head(store, b) != old_b
    with closing(store._connect()) as db:
        row = db.execute('SELECT record FROM studio_event_revisions WHERE id=?', (old_b,)).fetchone()
        assert json.loads(row[0])['members'] == [bi]


def test_body_update_retains_old_evidence_and_invalidates_bound_analysis(owner):
    _, a, _, ai, _ = pair(owner)
    store, pid = owner[0].store, owner[2]
    store.save_body(ai, 'partial_body', 'Original synthetic source text.')
    old = head(store, a)
    store.evidence.dependencies.track(pid, 'bound', 'brief', [])
    with closing(store._connect()) as db, db:
        store.event_revisions.bind(pid, 'bound', {a: old}, db=db)
    store.save_body(ai, 'partial_body', 'Corrected synthetic source text.')
    assert head(store, a) != old
    assert store.evidence.dependencies.get(pid, 'bound')['stale_reason'] == 'event_revised'
    with closing(store._connect()) as db:
        saved = json.loads(db.execute('SELECT record FROM studio_event_revisions WHERE id=?', (old,)).fetchone()[0])
        assert saved['sources'][0]['body'] == 'Original synthetic source text.'


def test_user_read_and_star_state_survive_merge_undo_without_semantic_revision(owner):
    execution, a, b, _, _ = pair(owner)
    store = owner[0].store
    before = {event: head(store, event) for event in (a, b)}
    receipt = store.event_revisions.merge(execution, a, before)
    merged = head(store, a)
    store.set_event_status(a, 'starred')
    assert head(store, a) == merged
    store.event_revisions.undo_merge(execution, receipt)
    assert next(e for e in store.list_events(project_id=owner[2]) if e['id'] == a)['status'] == 'starred'


def test_new_event_on_saved_source_fences_old_compiled_analysis(owner):
    execution, _, _, ref = fixtures.save(owner)
    store, pid = execution.store, owner[2]
    before = store.evidence.dependencies.generation(pid)
    assert store.list_events(project_id=pid) == []
    store.create_event(pid, dict(title='New synthetic event', category='company', importance='routine'), [ref['item_id']])
    result = store.put_compiled_brief(pid, {'headline': 'Old empty-event analysis'},
                                      [ref['evidence_version_id']], expected_generation=before)
    assert result['status'] == 'stale' and store.get_brief(pid) is None


def test_agent_update_after_merge_versions_actual_owner_and_prevents_undo(tmp_path):
    import copy

    from test_studio_agent_results import payload, route
    from test_studio_application_parity import headers, setup_app

    from project_watch.studio.execution import JobExecution

    app, web, pid, grant = setup_app(tmp_path)
    store = app.state.studio_store
    body = payload(web.get(f'/api/studio/projects/{pid}').json()['project']['config_version'])
    body['sources'].append({**body['sources'][0], 'id': 'N2', 'url': 'https://company.example/news/2'})
    body['news'].append({**body['news'][0], 'key': 'second', 'title': 'Second event', 'refs': ['N2']})
    reply = web.post(route(pid), headers=headers(grant), json=body)
    assert reply.status_code == 201
    events = {e['title']: e['id'] for e in store.list_events(project_id=pid)}
    a, b = events['Factory opened'], events['Second event']
    store.enqueue(pid, 'external')
    execution = JobExecution(store, store.claim())
    receipt = store.event_revisions.merge(execution, b, {e: head(store, e) for e in (a, b)})
    merged = head(store, b)
    store.evidence.dependencies.track(pid, 'bound', 'brief', [])
    with closing(store._connect()) as db, db:
        store.event_revisions.bind(pid, 'bound', {b: merged}, db=db)
    update = copy.deepcopy(body)
    update.update(expected_version=reply.json()['version'], idempotency_key='source-correction')
    update['news'] = update['news'][:1]
    update['sources'][0]['text'] = 'Corrected synthetic source text, same URL.'
    assert web.post(route(pid), headers=headers(grant), json=update).status_code == 201
    assert head(store, b) != merged
    assert store.evidence.dependencies.get(pid, 'bound')['status'] == 'stale'
    with closing(store._connect()) as db:
        old = json.loads(db.execute('SELECT record FROM studio_event_revisions WHERE id=?', (merged,)).fetchone()[0])
        assert {s['body'] for s in old['sources']} == {'Factory opened in July.'}
    with pytest.raises(ValueError, match='changed since review'):
        store.event_revisions.undo_merge(execution, receipt)

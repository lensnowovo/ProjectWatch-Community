"""Real material publication and authenticated-route correction, synthetic only."""
from contextlib import closing

import pytest
import test_collection_host as fixtures
from fastapi.testclient import TestClient
from test_event_revisions import head
from test_question_assessment import install
from test_studio_event_context import _event

from project_watch.studio import insight, question_work
from project_watch.studio.pipeline import StudioWorker


def setup(tmp_path, monkeypatch):
    original, apps = fixtures.create_studio_app, []
    def capture(*args, **kwargs):
        app = original(*args, **kwargs)
        apps.append(app)
        return app
    monkeypatch.setattr(fixtures, 'create_studio_app', capture)
    pipeline, execution, watch, seen = install(tmp_path, monkeypatch)
    pid = execution.job['project_id']
    item, event = _event(pipeline.store, pid, 'Synthetic public source', 'Earlier admitted source')
    before = pipeline.store.list_events(project_id=pid)[0]
    pipeline.run(execution.job)
    web = TestClient(apps[0])
    response = web.get(f'/api/studio/projects/{pid}/events/{event}/merges')
    assert response.status_code == 200 and len(response.json()) == 1, response.text
    receipt = response.json()[0]
    assert receipt['kind'] == 'attachment'
    return pipeline, execution, watch, seen, web, item, event, before, receipt


def test_default_publication_correction_preserves_sources_and_invalidates_consumers(tmp_path, monkeypatch):
    pipeline, execution, watch, seen, web, item, event, before, receipt = setup(tmp_path, monkeypatch)
    pid, store = execution.job['project_id'], pipeline.store
    paid = len(seen['models'])
    url = f'/api/studio/projects/{pid}/event-merges/{receipt["id"]}/undo'
    assert web.post(url, headers={'Origin': 'https://untrusted.example.invalid'}).status_code == 403
    pipeline.run(execution.job)  # Publication committed, worker completion not yet recorded.
    assert len(seen['models']) == paid
    assert len(web.get(f'/api/studio/projects/{pid}/events/{event}/merges').json()) == 1
    store.finish(execution.job['id'], lease_token=execution.job['lease_token'])
    worker = StudioWorker(pipeline)
    monkeypatch.setattr(worker, 'schedule', lambda: None)
    assert worker.step()
    old_answer = question_work.current(pipeline, pid, watch['id'])
    assert old_answer['knowledge_state'] == 'unresolved'
    response = web.post(f'/api/studio/projects/{pid}/refresh', json={'kind': 'insight'})
    assert response.status_code == 202, response.text
    assert worker.step()
    old_insight = insight.load(store, pid)
    assert old_insight and old_insight['status'] == 'current'
    paid = len(seen['models']), len(seen['assess']), len(seen['insight'])
    merged_revision = head(store, event)
    assert web.post(url).status_code == 200
    restored = head(store, event)
    assert restored != merged_revision
    assert web.post(url).status_code == 200 and head(store, event) == restored
    events = {e['id']: e for e in store.list_events(project_id=pid)}
    assert len(events) == 2 and events[event]['sources'][0]['id'] == item
    assert events[event]['title'] == before['title'] and events[event]['summary'] == before['summary']
    separated = next(e for key, e in events.items() if key != event)
    assert len(separated['sources']) == 1 and separated['sources'][0]['id'] != item
    assert insight.load(store, pid)['status'] == 'stale'
    assert question_work.current(pipeline, pid, watch['id'])['knowledge_state'] == 'needs_recheck'
    assert question_work.current(pipeline, pid, watch['id'])['text'] == old_answer['text']
    assert insight.load(store, pid)['analysis'] == old_insight['analysis']
    for _ in range(2):
        worker.dispatch_rebuilds(model_ready=True)
    with closing(store._connect()) as db:
        kinds = [r[0] for r in db.execute("SELECT kind FROM studio_jobs WHERE status='queued'")]
    assert sorted(kinds) == ['assessment', 'insight']
    assert paid == (len(seen['models']), len(seen['assess']), len(seen['insight']))
    assert worker.step() and worker.step()
    answer = question_work.current(pipeline, pid, watch['id'])
    analysis = insight.load(store, pid)
    assert answer['knowledge_state'] == 'unresolved' and answer['dependency']['status'] == 'current'
    assert analysis['status'] == 'current'
    assert separated['id'] in answer['work']['event_versions']
    assert separated['id'] in analysis['input']['event_versions']


@pytest.mark.parametrize('change', ['text', 'source', 'running', 'foreign'])
def test_correction_refuses_stale_busy_or_cross_project_receipts(tmp_path, monkeypatch, change):
    pipeline, execution, _watch, seen, web, _item, event, _before, receipt = setup(tmp_path, monkeypatch)
    store, pid = pipeline.store, execution.job['project_id']
    if change != 'running':
        store.finish(execution.job['id'], lease_token=execution.job['lease_token'])
    if change == 'text':
        with closing(store._connect()) as db, db:
            db.execute("UPDATE studio_events SET title='User corrected title' WHERE id=?", (event,))
    elif change == 'source':
        source = store.list_events(project_id=pid)[0]['sources'][-1]
        store.save_body(source['id'], 'partial_body', 'User corrected evidence')
    elif change == 'foreign':
        other = pipeline.repository.create_project('Other synthetic project')
        store.upsert_project(other.id, {'legal_name': 'Other synthetic project'}, consent=True)
        pid = other.id
    paid = len(seen['models'])
    response = web.post(f'/api/studio/projects/{pid}/event-merges/{receipt["id"]}/undo')
    assert response.status_code == (404 if change == 'foreign' else 409), response.text
    assert len(seen['models']) == paid
    if change == 'text':
        assert store.list_events(project_id=pid)[0]['title'] == 'User corrected title'


def test_automatic_attach_requires_current_lease_and_does_not_rewrite_old_receipt(tmp_path, monkeypatch):
    pipeline, execution, _watch, _seen, _web, item, event, _before, _receipt = setup(tmp_path, monkeypatch)
    with closing(pipeline.store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET lease_token='replaced' WHERE id=?", (execution.job['id'],))
    with pytest.raises(RuntimeError, match='lease expired'):
        pipeline.store.attach_to_event(event, [item], importance='major', execution=execution)

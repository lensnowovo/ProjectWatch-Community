"""Synthetic stored events only; no worker, source read, or model request."""
from datetime import UTC, datetime, timedelta, timezone

import pytest
import test_studio_digest
from fastapi.testclient import TestClient
from test_studio_digest import _seed


@pytest.fixture
def app(tmp_path):
    return test_studio_digest.app.__wrapped__(tmp_path)


def set_date(store, pid, date):
    with store._connect() as db:
        db.execute('UPDATE studio_events SET event_at=? WHERE project_id=?', (date, pid))


@pytest.mark.parametrize('date', [None, '', 'not-a-date', '2026-02-30', '2001-01-01', '2099-12-30'])
def test_unknown_invalid_old_and_future_are_not_recent_or_reported(app, date):
    p = _seed(app)
    store = app.state.studio_store
    set_date(store, p['id'], date)
    client = TestClient(app)
    assert client.get('/api/studio/events?days=7').json() == []
    digest = client.get('/api/studio/digest?days=7').json()
    assert digest['totals']['major'] == 0
    assert digest['projects'] == []
    assert digest['window_summary']['counts']['total'] == 1
    assert digest['window_summary']['counts']['in_period'] == 0
    assert '光启视界完成3亿元' not in client.get('/api/studio/digest.md?days=7').text
    # Reader filters do not delete the stored event or replace its date.
    assert len(store.list_events(project_id=p['id'])) == 1


def test_offset_date_compares_instants_not_strings(app):
    p = _seed(app)
    now = datetime.now(UTC)
    # Future in UTC, previous date/time when represented in a negative offset.
    future = (now + timedelta(hours=1)).astimezone(timezone(-timedelta(hours=12)))
    set_date(app.state.studio_store, p['id'], future.isoformat())
    assert TestClient(app).get('/api/studio/events?days=7').json() == []
    recent = (now - timedelta(hours=1)).astimezone(timezone(timedelta(hours=14)))
    set_date(app.state.studio_store, p['id'], recent.isoformat())
    client = TestClient(app)
    assert len(client.get('/api/studio/events?days=7').json()) == 1
    report = client.get('/api/studio/digest').json()
    assert report['projects'][0]['events'][0]['when'] == recent.isoformat()


def test_dated_feed_limit_keeps_newest_instant_across_offsets(app):
    recent = _seed(app, name='合成较新')
    older = _seed(app, name='合成较旧')
    now = datetime.now(UTC)
    set_date(app.state.studio_store, recent['id'],
             (now - timedelta(hours=1)).astimezone(timezone(-timedelta(hours=12))).isoformat())
    set_date(app.state.studio_store, older['id'],
             (now - timedelta(hours=2)).astimezone(timezone(timedelta(hours=14))).isoformat())
    [event] = TestClient(app).get('/api/studio/events?days=7&limit=1').json()
    assert event['project_id'] == recent['id']


def test_routine_pending_and_failed_are_explained_without_new_jobs(app):
    a = _seed(app, importance='routine')
    b = _seed(app, name='合成待评', importance='routine')
    store = app.state.studio_store
    with store._connect() as db:
        db.execute('UPDATE studio_items SET extra=? WHERE project_id=?', ('{"dsh_claims":[{}]}', b['id']))
        db.execute("INSERT INTO studio_jobs(id,project_id,kind,status,created_at) VALUES('failure',?,'external','failed',?)",
                   (a['id'], datetime.now(UTC).isoformat()))
        before = db.execute('SELECT count(*) FROM studio_jobs').fetchone()[0]
    client = TestClient(app)
    for _ in range(2):
        data = client.get('/api/studio/events/summary?days=7').json()
        assert data['counts']['in_period'] == 2
        assert data['counts']['routine'] == data['counts']['pending'] == 1
        assert any(p['state'] == 'failed' for p in data['progress'])
        report = client.get('/api/studio/digest').json()
        assert report['projects'] == []
        assert '一般 1 条，待评级 1 条' in report['markdown']
        assert report['window_note'] in client.get('/api/studio/digest.md').text
    with store._connect() as db:
        assert db.execute('SELECT count(*) FROM studio_jobs').fetchone()[0] == before
    assert not app.state.bot.posts


def test_agent_date_is_shared_by_feed_and_report_not_acquisition(app):
    p = _seed(app)
    store = app.state.studio_store
    published = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    set_date(store, p['id'], None)
    with store._connect() as db:
        db.execute("UPDATE studio_items SET key='agent:synthetic',published_at=? WHERE project_id=?", (published, p['id']))
    client = TestClient(app)
    [event] = client.get('/api/studio/events?days=7').json()
    assert event['event_at'] is None and event['news_date'] == published
    report = client.get('/api/studio/digest').json()
    assert report['projects'][0]['events'][0]['when'] == published


def test_filtered_summary_and_removed_projects(app):
    a = _seed(app, importance='routine')
    b = _seed(app, name='合成移出')
    app.state.studio_store.remove_project(b['id'])
    client = TestClient(app)
    summary = client.get('/api/studio/events/summary?days=7&importance=major').json()
    assert summary['counts']['in_period'] == 1 and summary['counts']['matching'] == 0
    assert [p['id'] for p in summary['progress']] == [a['id']]
    assert client.get('/api/studio/events/summary?days=3').status_code == 400

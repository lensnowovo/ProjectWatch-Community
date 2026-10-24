"""Stored monitoring outcomes must not turn missing results into business silence."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from project_watch.studio import digest
from project_watch.studio.app import create_studio_app


@pytest.fixture
def app(tmp_path):
    return create_studio_app(tmp_path, start_worker=False)


def seed(app, name='Synthetic Monitor'):
    pipeline = app.state.studio_pipeline
    pid = pipeline.repository.create_project(name).id
    pipeline.store.upsert_project(pid, {}, consent=True)
    return pipeline.store, pid


def complete(store, pid, report=None):
    store.enqueue(pid, 'external')
    job = store.claim()
    store.put_plan(pid, {'last_report': report or {'company': {'checked': 1, 'added': 0, 'errors': []}}})
    store.touch(pid, 'last_external_at')
    store.finish(job['id'], lease_token=job['lease_token'])


@pytest.mark.parametrize('status', ['queued', 'running', 'failed'])
def test_unfinished_external_is_not_quiet(app, status):
    store, pid = seed(app)
    store.enqueue(pid, 'external')
    if status != 'queued':
        job = store.claim()
        if status == 'failed':
            store.finish(job['id'], error='synthetic failure', lease_token=job['lease_token'])
    data = digest.build_digest(store)
    assert data['quiet'] == []
    assert data['incomplete'][0]['state'] == status
    text = digest.digest_markdown(data)
    assert '没有重大或值得关注的变化' not in text and '无明显变化' not in text
    progress = TestClient(app).get(f'/api/studio/projects/{pid}').json()['monitoring_progress']
    assert progress['state'] == status and '无变化' not in progress['empty_text']


@pytest.mark.parametrize('variant,expected', [
    ('no_run', 'unobserved'), ('no_checks', 'unobserved'), ('error', 'partial'),
    ('untriaged', 'partial'), ('old', 'stale'), ('paused', 'paused'), ('inactive', 'inactive'),
])
def test_missing_partial_or_stale_checks_do_not_enter_quiet(app, variant, expected):
    store, pid = seed(app)
    if variant not in ('no_run', 'inactive'):
        complete(store, pid, {'company': {'checked': 0, 'added': 0, 'errors': []}} if variant == 'no_checks'
                 else {'company': {'checked': 1, 'added': 0, 'errors': ['synthetic provider unavailable']}} if variant == 'error'
                 else None)
    if variant == 'untriaged':
        store.add_items(pid, 'company', [{'key': 'pending', 'title': 'Synthetic update', 'url': 'https://example.invalid/a',
                                        'source': 'Synthetic', 'snippet': '', 'published_at': None}])
    if variant == 'old':
        with store._connect() as db:
            db.execute('UPDATE studio_projects SET last_external_at=? WHERE project_id=?',
                       ((datetime.now(UTC) - timedelta(days=90)).isoformat(), pid))
    if variant == 'paused':
        store.set_status(pid, 'paused')
    if variant == 'inactive':
        with store._connect() as db:
            db.execute('UPDATE studio_projects SET consent_at=NULL WHERE project_id=?', (pid,))
    data = digest.build_digest(store)
    assert data['quiet'] == []
    if variant == 'paused':
        assert data['incomplete'] == []
    else:
        assert data['incomplete'][0]['state'] == expected


def test_completed_observed_checks_allow_bounded_quiet_statement(app):
    store, pid = seed(app)
    complete(store, pid)
    data = digest.build_digest(store)
    assert data['quiet'] == ['Synthetic Monitor'] and data['incomplete'] == []
    text = digest.digest_markdown(data)
    assert '已收录信息未见重要变化' in text and '无明显变化' not in text
    assert TestClient(app).get(f'/api/studio/projects/{pid}').json()['monitoring_progress']['state'] == 'observed'


def test_failed_refresh_retains_saved_events_and_is_visible_in_digest(app):
    store, pid = seed(app)
    complete(store, pid)
    store.create_event(pid, {'title': 'Synthetic financing', 'category': 'company', 'importance': 'major',
                            'event_at': datetime.now(UTC).isoformat()}, [])
    store.enqueue(pid, 'external')
    job = store.claim()
    store.finish(job['id'], error='synthetic failed refresh', lease_token=job['lease_token'])
    data = digest.build_digest(store)
    assert data['projects'][0]['events'][0]['title'] == 'Synthetic financing'
    assert data['projects'][0]['monitoring_progress']['state'] == 'failed'
    assert data['incomplete'][0]['state'] == 'failed'


def test_unrelated_done_job_never_masks_external_failure_and_reads_do_not_schedule(app, monkeypatch):
    store, pid = seed(app)
    store.enqueue(pid, 'external')
    job = store.claim()
    store.finish(job['id'], error='synthetic failed refresh', lease_token=job['lease_token'])
    store.enqueue(pid, 'details')
    detail_job = store.claim()
    store.finish(detail_job['id'], lease_token=detail_job['lease_token'])
    before = store.jobs(pid)
    monkeypatch.setattr(store, 'enqueue', lambda *_a, **_k: pytest.fail('GET must not enqueue'))
    monkeypatch.setattr(app.state.studio_pipeline, 'tools', lambda: pytest.fail('GET must not call providers'))
    client = TestClient(app)
    for _ in range(2):
        assert client.get(f'/api/studio/projects/{pid}').json()['monitoring_progress']['state'] == 'failed'
        assert client.get('/api/studio/digest').json()['incomplete'][0]['state'] == 'failed'
    assert store.jobs(pid) == before


def test_round_number_counts_external_rounds_not_other_work(app):
    from project_watch.studio import progress

    store, pid = seed(app)
    assert progress.read(store, pid)['round'] == 0
    complete(store, pid)
    store.enqueue(pid, 'brief')
    assert progress.read(store, pid)['round'] == 1
    complete(store, pid)
    assert progress.read(store, pid)['round'] == 2
    # A follow-up continues its round; asking for another external round meanwhile joins it.
    store.enqueue(pid, 'external_followup')
    store.enqueue(pid, 'external')
    assert progress.read(store, pid)['round'] == 2
    assert progress.read(store, 'missing-project')['round'] == 0

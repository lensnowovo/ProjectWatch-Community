from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from project_watch.api import create_app
from project_watch.external_focus import ExternalFocusStore, external_reading
from project_watch.monitoring import FeedItem, ManualSnapshotAdapter, MonitoringRunner
from project_watch.public_search import gdelt_rss_target
from project_watch.repository import ProjectRepository, ReviewConflictError
from project_watch.team_auth import TeamAuthStore


def test_bing_scope_disclosure_and_persistence(tmp_path):
    from project_watch.bing_news import bing_news_target
    repo = ProjectRepository(tmp_path / 'watch.sqlite3')
    project = repo.create_project('Synthetic Company')
    client = TestClient(create_app(repo.db_path))
    route = f'/api/projects/{project.id}/monitoring/focus'
    payload = dict(category='sector', term='Synthetic connector', provider='bing')
    response = client.get(route, params=payload)
    assert response.status_code == 200
    assert 'Bing' in response.json()['disclosure']
    assert response.json()['eligible']
    payload['consent_version'] = response.json()['consent_version']
    started = client.post(route, json=payload)
    assert started.status_code == 202
    reopened = ProjectRepository(repo.db_path)
    plan = reopened.list_monitoring_plans(project.id)[0]
    assert plan.target == bing_news_target(payload['term'])
    assert ExternalFocusStore(reopened).get(plan.id)['provider'] == 'bing'


def test_provider_migration_preserves_history_and_immutability(tmp_path):
    import sqlite3

    from project_watch.external_focus import ensure_external_focus_schema
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE monitoring_plans(id TEXT PRIMARY KEY)')
    db.execute("INSERT INTO monitoring_plans VALUES ('plan')")
    db.execute("""CREATE TABLE external_focus(plan_id TEXT PRIMARY KEY REFERENCES monitoring_plans(id),
        category TEXT, term TEXT, provider TEXT CHECK(provider IN ('gdelt','brave')),
        consent_version TEXT, created_at TEXT, actor_ref TEXT)""")
    row = ('plan','company','Synthetic','gdelt','public-focus-v1','2026-10-01','local_user')
    db.execute('INSERT INTO external_focus VALUES(?,?,?,?,?,?,?)', row)
    ensure_external_focus_schema(db)
    ensure_external_focus_schema(db)
    assert db.execute('SELECT * FROM external_focus').fetchone() == row
    assert not db.execute('PRAGMA foreign_key_check').fetchall()
    for statement in ['DELETE FROM external_focus', "UPDATE external_focus SET term='changed'"]:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(statement)


def test_provider_migration_rolls_back_on_incompatible_row():
    import sqlite3

    from project_watch.external_focus import ensure_external_focus_schema
    db=sqlite3.connect(':memory:')
    db.execute('CREATE TABLE monitoring_plans(id TEXT PRIMARY KEY)')
    db.execute("""CREATE TABLE external_focus(plan_id TEXT PRIMARY KEY,category TEXT,term TEXT,
        provider TEXT CHECK(provider IN ('gdelt','brave')),consent_version TEXT,created_at TEXT,actor_ref TEXT)""")
    db.execute("INSERT INTO external_focus VALUES('id','unknown','term','gdelt','v','date','actor')")
    with pytest.raises(sqlite3.IntegrityError):
        ensure_external_focus_schema(db)
    assert db.execute('SELECT category FROM external_focus').fetchone()[0] == 'unknown'
    assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='external_focus_bing'").fetchone()


def test_competitor_focus_survives_restart_without_project_name(tmp_path):
    repo = ProjectRepository(tmp_path / 'watch.sqlite3')
    project = repo.create_project('Synthetic Investor Project')
    store = ExternalFocusStore(repo)
    plan = store.create(project.id, category='competitors', term='Synthetic Peer', provider='gdelt')
    assert not plan.enabled
    assert store.create(project.id, category='competitors', term='Synthetic Peer', provider='gdelt').id == plan.id
    repo.set_monitoring_plan_enabled(plan.id, True)
    reopened = ProjectRepository(repo.db_path)
    now = datetime.now(UTC)
    items = (
        FeedItem('1', 'Synthetic Peer product launch', 'https://example.org/1', '', now),
        FeedItem('2', 'Synthetic Peerless launch', 'https://example.org/2', '', now),
        FeedItem('3', 'Unrelated company launch', 'https://example.org/3', '', now),
        FeedItem('4', 'Synthetic Peer old announcement', 'https://example.org/4', '', now-timedelta(days=60)),
        FeedItem('5', 'Synthetic Peer future announcement', 'https://example.org/5', '', now+timedelta(days=60)),
    )
    runner = MonitoringRunner(reopened, {'rss': ManualSnapshotAdapter({plan.target: items})})
    receipt = runner.run(plan.id, idempotency_key='first')
    assert receipt.matched_count == 3
    assert receipt.new_count == 1
    assert runner.run(plan.id, idempotency_key='first').id == receipt.id
    reading = external_reading(reopened, project.id)
    competitor = next(c for c in reading['categories'] if c['key'] == 'competitors')
    assert len(competitor['items']) == 1
    assert competitor['items'][0]['match_term'] == 'Synthetic Peer'
    assert '名称匹配' in competitor['items'][0]['relevance']
    assert len(reading['categories']) == 5
    assert next(c for c in reading['categories'] if c['key'] == 'policy')['coverage'] == 'not_configured'


def test_scope_cannot_relabel_existing_plan_or_cross_project(tmp_path):
    repo = ProjectRepository(tmp_path / 'watch.sqlite3')
    project = repo.create_project('Synthetic Company')
    other = repo.create_project('Other Company')
    store = ExternalFocusStore(repo)
    plan = store.create(project.id, category='policy', term='Medical device regulation', provider='gdelt')
    with pytest.raises(ReviewConflictError):
        store.create(project.id, category='industry', term='Medical device regulation', provider='gdelt')
    assert external_reading(repo, other.id)['categories'][3]['items'] == []
    assert store.get(plan.id)['category'] == 'policy'
    assert repo.list_monitoring_plans(other.id) == []


def test_preview_and_consent_do_not_leak_profile_or_start_before_confirmation(tmp_path):
    repo = ProjectRepository(tmp_path / 'watch.sqlite3')
    project = repo.create_project('Synthetic Company')
    app = create_app(repo.db_path)
    client = TestClient(app)
    path = f'/api/projects/{project.id}/monitoring/focus'
    preview = client.get(path, params={'category':'competitors', 'term':'Synthetic Peer', 'provider':'gdelt'})
    assert preview.status_code == 200
    data = preview.json()
    assert data['term'] == 'Synthetic Peer'
    assert repo.list_monitoring_plans(project.id) == []
    payload = {'category':'competitors', 'term':'Synthetic Peer', 'provider':'gdelt'}
    assert client.post(path, json=payload).status_code == 422
    bad = {**payload, 'consent_version':'old'}
    assert client.post(path, json=bad).status_code == 409
    payload['consent_version'] = data['consent_version']
    started = client.post(path, json=payload)
    assert started.status_code == 202
    assert started.json()['plan']['enabled']
    assert client.post(path, json=payload).json() == started.json()
    assert len(repo.list_monitoring_jobs(project.id)) == 1
    assert repo.list_monitoring_plans(project.id)[0].target == gdelt_rss_target('Synthetic Peer')
    assert client.post(path, json=payload, headers={'Origin':'https://evil.example'}).status_code == 403
    assert client.get(f'/api/projects/{project.id}/monitoring/external').status_code == 200


@pytest.mark.parametrize('term', ['A', '"injected" OR query', 'private\nnotes'])
def test_bad_public_terms_never_create_plans(tmp_path, term):
    repo = ProjectRepository(tmp_path / 'watch.sqlite3')
    project = repo.create_project('Synthetic Company')
    with pytest.raises(ValueError):
        ExternalFocusStore(repo).create(project.id, category='company', term=term, provider='gdelt')
    assert repo.list_monitoring_plans(project.id) == []


def test_policy_relevance_is_not_applicability_and_failures_keep_last_success(tmp_path):
    repo = ProjectRepository(tmp_path / 'watch.sqlite3')
    project = repo.create_project('Synthetic Company')
    plan = ExternalFocusStore(repo).create(project.id, category='policy', term='Device rules', provider='gdelt')
    repo.set_monitoring_plan_enabled(plan.id, True)
    news = FeedItem('1', 'Device rules apply only to region X', 'https://example.org/policy',
                    'The region and eligibility must be checked.', datetime.now(UTC))
    runner = MonitoringRunner(repo, {'rss': ManualSnapshotAdapter({plan.target:(news,)})})
    success = runner.run(plan.id, idempotency_key='success')
    runner.adapters = {'rss': ManualSnapshotAdapter({})}
    runner.run(plan.id, idempotency_key='failed')
    policy = external_reading(repo, project.id)['categories'][3]
    assert policy['coverage'] == 'failed'
    assert policy['plans'][0]['last_success_at'] == success.finished_at
    assert '尚需核实' in policy['items'][0]['relevance']
    repo.set_monitoring_plan_enabled(plan.id, False)
    assert external_reading(repo, project.id)['categories'][3]['coverage'] == 'paused'


def test_team_mode_does_not_expose_local_focus_routes(tmp_path):
    database = tmp_path / 'watch.sqlite3'
    repo = ProjectRepository(database)
    project = repo.create_project('Synthetic Company')
    client = TestClient(create_app(database, team_auth=TeamAuthStore(database), team_origin='https://testserver'), base_url='https://testserver')
    for suffix in ['external', 'focus?category=company&term=Synthetic&provider=gdelt']:
        assert client.get(f'/api/projects/{project.id}/monitoring/{suffix}').status_code in {401,404}
    assert repo.list_monitoring_plans(project.id) == []


def test_existing_database_upgrade_and_immutable_scope(tmp_path):
    import sqlite3

    database = tmp_path / 'watch.sqlite3'
    repo = ProjectRepository(database)
    project = repo.create_project('Synthetic Company')
    old = repo.create_monitoring_plan(project.id, kind='rss', target=gdelt_rss_target(project.name), cadence_minutes=1440)
    with sqlite3.connect(database) as connection:
        connection.execute('DROP TABLE external_focus')
    upgraded = ProjectRepository(database)
    assert upgraded.get_monitoring_plan(old.id) == old
    focus = ExternalFocusStore(upgraded).create(project.id, category='company', term=project.name, provider='gdelt')
    assert focus.id == old.id
    with sqlite3.connect(database) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute('UPDATE external_focus SET term=? WHERE plan_id=?', ('Other Company', old.id))
    assert len(upgraded.list_monitoring_plans(project.id)) == 1


def test_pause_cancels_queued_first_focus_query_before_network(tmp_path):
    from project_watch.scheduler import MonitoringScheduler

    repo = ProjectRepository(tmp_path / 'watch.sqlite3')
    project = repo.create_project('Synthetic Company')
    plan = ExternalFocusStore(repo).create(project.id, category='industry', term='Synthetic Sector', provider='gdelt')
    repo.set_monitoring_plan_enabled(plan.id, True)
    job = repo.request_monitoring_run(plan.id, idempotency_key='initial')
    repo.set_monitoring_plan_enabled(plan.id, False)

    class NeverFetch:
        def fetch(self, target):
            pytest.fail('paused public focus must not contact any source')

    result = MonitoringScheduler(repo, MonitoringRunner(repo, {'rss': NeverFetch()})).process_one()
    assert result.id == job.id
    assert repo.list_source_runs(plan.id)[0].status == 'not_run'

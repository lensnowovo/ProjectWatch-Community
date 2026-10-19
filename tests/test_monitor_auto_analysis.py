"""One consent journey, durable interpretation bridge; synthetic sources only."""
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
from test_external_analysis import output
from test_monitor_autopilot import sample
from test_public_article_store import Reader

from project_watch.external_analysis import OUTPUT_TOKENS, validate_external_analysis
from project_watch.external_analysis_jobs import ExternalAnalysisWorker
from project_watch.monitor_autopilot import MonitorAutopilot
from project_watch.monitoring import FeedItem, ManualSnapshotAdapter, MonitoringRunner
from project_watch.reading_jobs import ReadingDenied


def setup(tmp_path, *, auto=True):
    repo, packet, _strategy, model, settings, pilot = sample(tmp_path)
    model.analysis_calls = 0
    model.analysis_before = model.analysis_after = None
    def analyze(packet, *, before_send):
        if model.analysis_before:
            model.analysis_before()
        before_send(json.dumps(dict(model='fake', max_tokens=OUTPUT_TOKENS, stream=True, messages=[])).encode())
        model.analysis_calls += 1
        if model.analysis_after:
            model.analysis_after()
        return validate_external_analysis(packet, output(packet))
    model.analyze_external = analyze
    started = pilot.authorize(packet, model_consent=True, public_consent=True, automatic_analysis=auto)
    pilot.process_one()
    worker = ExternalAnalysisWorker(pilot.analysis.store, settings, article_reader=Reader())
    # Plan timestamps can tie (especially on Windows); UUID order is not scope.
    company = next(scope for scope in pilot.activation.read(packet['project_id'])['scopes']
                   if scope['category'] == 'company')
    plan = repo.get_monitoring_plan(company['plan_id'])
    feed = FeedItem('one', 'Synthetic public company product trial', 'https://example.org/one', '', datetime.now(UTC))
    MonitoringRunner(repo, {'rss': ManualSnapshotAdapter({plan.target: (feed,)})}).run(plan.id, idempotency_key='one')
    return repo, packet, model, settings, pilot, worker, started


def test_single_consent_to_durable_news_analysis_and_restart(tmp_path):
    repo, packet, model, settings, pilot, worker, started = setup(tmp_path)
    assert worker.process_one()['state'] == 'succeeded'
    assert model.calls == 1 and model.analysis_calls == 1
    assert pilot.read(packet['project_id'])[0]['analysis']['requests_used'] == 1
    restored = MonitorAutopilot(repo, settings)
    restored.process_one()
    assert ExternalAnalysisWorker(restored.analysis.store, settings).process_one() is None
    assert len(restored.analysis.store.list_authorizations()) == 1
    assert not repo.list_observations(packet['project_id'])
    assert restored.authorize(packet, model_consent=True, public_consent=True, automatic_analysis=True)['id'] == started['id']
    assert model.calls == model.analysis_calls == 1


def test_fixture_news_uses_company_scope_even_when_plan_order_changes(tmp_path, monkeypatch):
    from project_watch.repository import ProjectRepository

    original = ProjectRepository.list_monitoring_plans
    monkeypatch.setattr(ProjectRepository, 'list_monitoring_plans',
                        lambda self, project_id: list(reversed(original(self, project_id))))
    repo, packet, _, _, pilot, worker, _ = setup(tmp_path)
    company = next(scope for scope in pilot.activation.read(packet['project_id'])['scopes']
                   if scope['category'] == 'company')
    assert repo.list_signals(packet['project_id'])[0].plan_id == company['plan_id']
    assert worker.process_one()['state'] == 'succeeded'


def test_old_consent_requires_explicit_upgrade_without_replanning(tmp_path):
    _, packet, model, _, pilot, worker, _ = setup(tmp_path, auto=False)
    assert worker.process_one() is None
    assert not pilot.analysis.store.list_authorizations()
    pilot.authorize(packet, model_consent=True, public_consent=True, automatic_analysis=True)
    pilot.process_one()
    assert worker.process_one()['state'] == 'succeeded'
    assert model.calls == 1 and model.analysis_calls == 1


@pytest.mark.parametrize('change', ['pause', 'source', 'model', 'revoke_child'])
def test_parent_scope_checked_at_actual_dispatch(tmp_path, change):
    repo, packet, model, settings, pilot, worker, started = setup(tmp_path)
    def before():
        if change == 'pause':
            pilot.pause(started['id'])
        elif change == 'source':
            repo.add_source(packet['project_id'], kind='document', reference=packet['reference'], content_sha256='c'*64)
        elif change == 'model':
            settings.model = None
        else:
            grant = pilot.analysis.store.list_authorizations()[0]
            pilot.analysis.store.set_status(grant['id'], 'revoked', actor='synthetic')
    model.analysis_before = before
    assert worker.process_one()['state'] == 'queued'
    assert model.analysis_calls == 0
    assert pilot.analysis.store.list_authorizations()[0]['requests_used'] == 0


def test_pause_does_not_reactivate_child_or_human_plan(tmp_path):
    repo, packet, _, _, pilot, worker, started = setup(tmp_path)
    pilot.pause(started['id'])
    pilot.process_one()
    assert worker.process_one() is None
    assert pilot.analysis.store.list_authorizations()[0]['status'] == 'paused'
    with pytest.raises(ReadingDenied):
        pilot.authorize(packet, model_consent=True, public_consent=True, automatic_analysis=True)
    assert all(not p.enabled for p in repo.list_monitoring_plans(packet['project_id']))


def test_sent_response_loss_is_not_rebilled(tmp_path):
    _, packet, model, settings, pilot, worker, _ = setup(tmp_path)
    def fail():
        raise TimeoutError('synthetic response loss')
    model.analysis_after = fail
    assert worker.process_one()['state'] == 'uncertain'
    pilot.process_one()
    assert ExternalAnalysisWorker(pilot.analysis.store, settings).process_one() is None
    assert model.analysis_calls == 1
    assert pilot.read(packet['project_id'])[0]['analysis']['requests_used'] == 1


@pytest.mark.parametrize('change', ['none', 'pause', 'source', 'model'])
def test_explicit_retry_is_independent_of_brief_selection(tmp_path, monkeypatch, change):
    repo, packet, model, settings, pilot, worker, started = setup(tmp_path)
    def fail():
        raise TimeoutError('synthetic response loss')
    model.analysis_after = fail
    parent = worker.process_one()
    preview = pilot.analysis.store.retry_preview(packet['project_id'], parent['id'], model.authorization_id())
    child = pilot.analysis.store.retry(packet['project_id'], parent['id'], model.authorization_id(),
                                       preview['disclosure_digest'])
    model.analysis_after = None
    # A manual retry must not depend on the next automatically ranked homepage.
    monkeypatch.setattr('project_watch.external_analysis_jobs.external_reading',
                        lambda *a: {'briefing': {'items': []}, 'categories': []})
    if change == 'pause':
        pilot.pause(started['id'])
    elif change == 'source':
        repo.add_source(packet['project_id'], kind='document', reference=packet['reference'], content_sha256='c'*64)
    elif change == 'model':
        settings.model = None
    result = worker.process_one()
    if change == 'none':
        assert result['id'] == child['id'] and result['state'] == 'succeeded'
        assert result['packet'] == parent['packet']
        assert model.analysis_calls == 2
        assert worker.process_one() is None
    else:
        assert result is None and model.analysis_calls == 1
    assert pilot.analysis.store.get(parent['id'])['state'] == 'uncertain'


def test_preview_contains_only_explicit_subset_and_is_read_only(tmp_path):
    repo, packet, _, _, pilot, _, _ = setup(tmp_path, auto=False)
    info = pilot.analysis.preview(packet)
    assert len(info['material_refs']) == 1
    assert info['material_refs'][0]['text'] == packet['excerpts'][0]['text']
    assert info['max_requests'] == 20
    assert not pilot.analysis.store.list_authorizations()
    with closing(repo._connect()) as db:
        assert db.execute('SELECT count(*) FROM monitor_autopilot_analysis').fetchone()[0] == 0


def test_api_binds_analysis_choice_and_existing_upgrade(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from project_watch.api import create_app
    repo, packet, model, _, _, _, started = setup(tmp_path, auto=False)
    monkeypatch.setenv('PROJECT_WATCH_SETTINGS_TOKEN', 'x'*40)
    app = create_app(repo.db_path, proposal_model=model)
    client = TestClient(app, base_url='http://127.0.0.1')
    path = f"/api/workspace/projects/{packet['project_id']}/monitor-autopilot"
    headers = {'origin':'http://127.0.0.1', 'x-settings-token':'x'*40}
    assert client.post(path+'/'+started['id']+'/analysis-preview').status_code == 403
    view = client.post(path+'/'+started['id']+'/analysis-preview', headers=headers).json()
    assert view['automatic_analysis'] is True and view['analysis']['max_requests'] == 20
    body = dict(packet=view['packet'], disclosure_digest=view['disclosure_digest'], model_consent=True,
                public_consent=True, automatic_analysis=False)
    assert client.post(path, headers=headers, json=body).status_code == 409
    body['automatic_analysis'] = True
    assert client.post(path, headers=headers, json=body).status_code == 202
    assert model.calls == 1 and model.analysis_calls == 0


def test_additive_schema_backup_restore_and_immutable_consent(tmp_path):
    from project_watch.repository import ProjectRepository
    repo, packet, _, settings, _, worker, _ = setup(tmp_path)
    worker.process_one()
    with closing(repo._connect()) as db, closing(sqlite3.connect(tmp_path/'restored.sqlite3')) as dest:
        db.backup(dest)
    restored = ProjectRepository(tmp_path/'restored.sqlite3')
    pilot = MonitorAutopilot(restored, settings)
    assert len(pilot.analysis.store.read(packet['project_id'])['items']) == 1
    with closing(restored._connect()) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE monitor_autopilot_analysis_v2 SET consent_json='{}'")


def test_new_bp_version_hides_prior_interpretation(tmp_path):
    repo, packet, _, _, pilot, worker, _ = setup(tmp_path)
    worker.process_one()
    repo.add_source(packet['project_id'], kind='document', reference=packet['reference'], content_sha256='d'*64)
    assert pilot.analysis.store.read(packet['project_id'])['items'] == []
    assert pilot.analysis.store.read(packet['project_id'])['stale_count'] == 1
    assert worker.status(packet['project_id'])['reason'] == 'scope_changed'


def test_no_analysis_for_deferred_market_commentary(tmp_path):
    repo, packet, model, _, _, worker, _ = setup(tmp_path)
    plan = repo.list_monitoring_plans(packet['project_id'])[0]
    feed = FeedItem('market', '工业视觉股价上涨20%', 'https://example.org/market', '', datetime.now(UTC))
    MonitoringRunner(repo, {'rss': ManualSnapshotAdapter({plan.target: (feed,)})}).run(plan.id, idempotency_key='market')
    assert worker.process_one()['state'] == 'succeeded'
    assert worker.process_one() is None
    assert model.analysis_calls == 1


def test_historical_category_conflict_does_not_block_remaining_scopes(tmp_path):
    from project_watch.external_focus import ExternalFocusStore
    repo, packet, _strategy, model, _, pilot = sample(tmp_path)
    old = ExternalFocusStore(repo).create(packet['project_id'], category='industry',
                                         term='Industrial vision systems', provider='bing')
    pilot.authorize(packet, model_consent=True, public_consent=True)
    pilot.process_one()
    result = pilot.activation.read(packet['project_id'])
    assert result['coverage'] == 'partial'
    assert result['skipped'][0]['anchor'] == 'Industrial vision systems'
    assert len(result['scopes']) == 1 and model.calls == 1
    assert ExternalFocusStore(repo).get(old.id)['category'] == 'industry'
    assert repo.get_monitoring_plan(old.id).enabled is False


def test_manual_activation_retry_keeps_single_model_dispatch(tmp_path, monkeypatch):
    _, packet, _, model, _, pilot = sample(tmp_path)
    started = pilot.authorize(packet, model_consent=True, public_consent=True)
    original = pilot.activation.activate
    def fail(*a, **kw):
        raise RuntimeError('local failure')
    monkeypatch.setattr(pilot.activation, 'activate', fail)
    pilot.process_one()
    pilot.retry_activation(started['id'])
    monkeypatch.setattr(pilot.activation, 'activate', original)
    pilot.process_one()
    assert model.calls == 1
    assert pilot.read(packet['project_id'])[0]['state'] == 'collecting'
    pilot.pause(started['id'])
    with pytest.raises(ReadingDenied):
        pilot.retry_activation(started['id'])


def test_saved_v1_consent_stays_v1_until_explicit_body_upgrade(tmp_path):
    import time
    from dataclasses import asdict
    repo,packet,model,_,pilot,worker,started=setup(tmp_path,auto=False)
    legacy=pilot.analysis.preview(packet)
    legacy.pop('read_articles')
    legacy.pop('public_source_note')
    legacy.update(config_id=model.authorization_id(),recipient=asdict(model.config))
    with closing(repo._connect()) as db, db:
        db.execute('INSERT INTO monitor_autopilot_analysis VALUES(?,?,?)',
                   (started['id'],json.dumps(legacy),time.time()))
    pilot.process_one()
    grant=pilot.analysis.store.list_authorizations()[0]
    assert grant['rule_version']=='external-analysis-v1'
    assert worker.process_one()['state']=='succeeded'
    assert worker.article_reader.calls==0
    pilot.authorize(packet,model_consent=True,public_consent=True,automatic_analysis=True)
    pilot.process_one()
    assert worker.process_one()['state']=='succeeded'
    assert worker.article_reader.calls==1
    assert pilot.analysis.store.status(grant['id'])['requests_used']==1
    assert len(pilot.analysis.store.read(packet['project_id'])['items'])==2
    assert worker.process_one() is None
    assert model.calls==1 and model.analysis_calls==2

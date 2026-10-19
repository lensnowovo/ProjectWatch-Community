"""Durable external-analysis integration, built-in synthetic evidence only."""
import json
from contextlib import closing
from dataclasses import asdict, replace

import pytest
from test_external_analysis import inputs, output

from project_watch.external_analysis import OUTPUT_TOKENS, validate_external_analysis
from project_watch.external_analysis_jobs import ExternalAnalysisStore, ExternalAnalysisWorker
from project_watch.model_provider import ModelProviderConfig
from project_watch.model_settings import ModelSettings
from project_watch.reading_jobs import ReadingDenied, ReadingLimits


class Model:
    def __init__(self):
        self.config = ModelProviderConfig('https://example.test', 'anthropic-messages', 'fake')
        self.calls = 0
        self.before = self.after = None

    def authorization_id(self):
        return 'b'*64

    def disclosure(self):
        return {'remaining_runs': 100}

    def analyze_external(self, packet, *, before_send):
        if self.before:
            self.before()
        before_send(json.dumps({'model': 'fake', 'stream': True, 'max_tokens': OUTPUT_TOKENS, 'messages': []}).encode())
        self.calls += 1
        if self.after:
            self.after()
        return validate_external_analysis(packet, output(packet, relationship=any(
            r['kind'] == 'internal' for r in packet['references'])))


def sample(tmp_path):
    repo, project, source, packet = inputs(tmp_path)
    model = Model()
    settings = ModelSettings(model)
    store = ExternalAnalysisStore(repo)
    worker = ExternalAnalysisWorker(store, settings)
    plans = [p.id for p in repo.list_monitoring_plans(project.id)]
    selection = [{'source_id': source.id, 'ordinal': 1, 'offset': 0,
                  'length': len(repo.list_fragments(source.id)[0].text)}]
    return repo, project, source, packet, model, settings, store, worker, plans, selection


def allow(s, *, future=True, context=True, calls=5, key='synthetic-external-grant'):
    _, project, _, _, model, _, store, _, plans, selected = s
    scope = store.preview_scope(project.id, plan_ids=plans, selections=selected if context else [],
                                include_future_news=future)
    return store.authorize(project.id, scope=scope, config_id=model.authorization_id(),
                           recipient=asdict(model.config), actor='synthetic', request_key=key,
                           limits=ReadingLimits(max_requests=calls, max_output_tokens=OUTPUT_TOKENS))


def test_durable_authorization_success_duplicate_restart_and_human_boundary(tmp_path):
    s = sample(tmp_path)
    repo, project, _, _, model, settings, store, worker, _, _ = s
    assert worker.process_one() is None
    grant = allow(s)
    assert worker.process_one()['state'] == 'succeeded'
    assert model.calls == 1
    results = store.read(project.id)
    assert len(results['items']) == 1
    assert results['items'][0]['analysis']['review_status'] == 'unreviewed'
    assert store.status(grant['id'])['requests_used'] == 1
    assert worker.process_one() is None
    renewed = allow(s, key='synthetic-renewed-consent')
    reboot = ExternalAnalysisWorker(ExternalAnalysisStore(repo), settings)
    assert reboot.process_one() is None and model.calls == 1
    assert store.status(renewed['id'])['requests_used'] == 0
    assert store.read(project.id) == results
    assert repo.list_observations(project.id) == []
    assert allow(s)['id'] == grant['id']


@pytest.mark.parametrize('change', ['pause', 'revoke', 'model', 'source', 'plan'])
def test_scope_rechecked_immediately_before_dispatch(tmp_path, change):
    s = sample(tmp_path)
    repo, _, source, _, model, settings, store, worker, plans, _ = s
    grant = allow(s)
    def before():
        if change in {'pause', 'revoke'}:
            store.set_status(grant['id'], 'paused' if change == 'pause' else 'revoked', actor='synthetic')
        elif change == 'model':
            settings.model = None
        elif change == 'source':
            repo.get_source = lambda sid: replace(source, content_sha256='c'*64)
        else:
            repo.set_monitoring_plan_enabled(plans[0], False)
    model.before = before
    assert worker.process_one()['state'] == 'queued'
    assert model.calls == 0 and store.status(grant['id'])['requests_used'] == 0


def test_budget_restart_and_uncertain_response_never_rebill(tmp_path):
    s = sample(tmp_path)
    _, project, _, _, model, settings, store, worker, _, _ = s
    grant = allow(s, calls=1)
    def fail():
        raise RuntimeError('synthetic response lost')
    model.after = fail
    assert worker.process_one()['state'] == 'uncertain'
    assert model.calls == 1 and store.status(grant['id'])['requests_used'] == 1
    assert store.read(project.id)['items'] == []
    model.after = None
    allow(s, key='synthetic-renewed-consent')
    assert ExternalAnalysisWorker(store, settings).process_one() is None
    assert model.calls == 1


def test_publish_rechecks_source_and_preserves_uncertain_reservation(tmp_path):
    s = sample(tmp_path)
    repo, project, source, _, model, _, store, worker, _, _ = s
    allow(s)
    fragment = repo.list_fragments(source.id)[0]
    model.after = lambda: setattr(repo, 'list_fragments', lambda sid: [replace(fragment, text='已修改的内容。')])
    assert worker.process_one()['state'] == 'uncertain'
    assert model.calls == 1 and not store.read(project.id)['items']


def test_saved_analysis_is_hidden_when_evidence_changes(tmp_path):
    s = sample(tmp_path)
    repo, project, source, _, _, _, store, worker, _, _ = s
    allow(s)
    worker.process_one()
    repo.get_source = lambda sid: replace(source, content_sha256='d'*64)
    assert store.read(project.id) == {'project_id': project.id, 'items': [], 'stale_count': 1}
    with closing(repo._connect()) as db:
        assert db.execute('SELECT count(*) FROM external_analysis_results').fetchone()[0] == 1


def test_existing_reading_consent_cannot_authorize_external_analysis(tmp_path):
    from project_watch.reading_jobs import ReadingJobs
    s = sample(tmp_path)
    _, project, source, _, model, _, _, worker, _, _ = s
    ReadingJobs(s[0]).authorize(project.id, config_id=model.authorization_id(), model=model.config.model,
                               rule_version='company-reading-v2', source_hashes=[source.content_sha256],
                               include_future=True, limits=ReadingLimits(max_output_tokens=3072),
                               actor='synthetic', recipient=asdict(model.config))
    assert worker.process_one() is None and model.calls == 0


def test_revoke_is_final_and_input_scope_cannot_change_with_same_request_key(tmp_path):
    s = sample(tmp_path)
    store = s[6]
    grant = allow(s)
    with pytest.raises(ReadingDenied):
        allow(s, context=False)
    store.set_status(grant['id'], 'revoked', actor='synthetic')
    with pytest.raises(ReadingDenied):
        store.set_status(grant['id'], 'active', actor='synthetic')


def test_atomic_claim_and_expired_sent_lease_recovery(tmp_path):
    s = sample(tmp_path)
    _, _, _, packet, model, _, store, _, _, _ = s
    grant = allow(s)
    job = store.enqueue(grant['id'], packet)
    first = store.claim(job['id'])
    assert first and store.claim(job['id']) is None
    store.reserve(job['id'], first['lease_token'], model.authorization_id(),
                  json.dumps({'model': 'fake', 'max_tokens': OUTPUT_TOKENS, 'stream': True, 'messages': []}).encode())
    store.recover(now=first['lease_until'] + 1)
    assert store.get(job['id'])['state'] == 'uncertain'
    assert store.claim(job['id']) is None


def test_public_only_scope_never_copies_material_into_job(tmp_path):
    s = sample(tmp_path)
    _, project, source, _, _, _, store, worker, _, _ = s
    allow(s, context=False)
    assert worker.process_one()['state'] == 'succeeded'
    result = store.read(project.id)['items'][0]
    assert all(r['kind'] == 'external' for r in result['packet']['references'])
    assert source.id not in json.dumps(result)
    assert result['analysis']['relationship']['assessment'] == 'unclear'


@pytest.mark.parametrize('status,state', [('paused', 'succeeded'), ('revoked', 'uncertain')])
def test_pause_allows_already_sent_completion_but_revoke_does_not(tmp_path, status, state):
    s = sample(tmp_path)
    model, store, worker = s[4], s[6], s[7]
    grant = allow(s)
    model.after = lambda: store.set_status(grant['id'], status, actor='synthetic')
    assert worker.process_one()['state'] == state
    assert model.calls == 1


def test_upgrade_backup_restore_and_repository_compatibility(tmp_path):
    import sqlite3

    from project_watch.repository import ProjectRepository
    s = sample(tmp_path)
    repo, project, source, _, _, _, store, worker, _, _ = s
    allow(s)
    worker.process_one()
    with closing(repo._connect()) as db, closing(sqlite3.connect(tmp_path/'restored.sqlite3')) as restored:
        db.backup(restored)
    restored_repo = ProjectRepository(tmp_path/'restored.sqlite3')
    before = store.read(project.id)
    assert ExternalAnalysisStore(restored_repo).read(project.id) == before
    assert restored_repo.get_source(source.id) == source
    # Pre-feature repository operations remain valid; history cannot be rewritten.
    assert restored_repo.get_project(project.id) == project
    assert restored_repo.list_observations(project.id) == []
    with closing(restored_repo._connect()) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE external_analysis_results SET output_json='{}'")


def test_status_is_read_only_and_distinguishes_missing_consent_pause_budget(tmp_path):
    s = sample(tmp_path)
    _, project, _, _, model, settings, store, worker, _, _ = s
    assert worker.status(project.id)['reason'] == 'consent_required'
    grant = allow(s, calls=1)
    assert worker.status(project.id)['reason'] == 'ready'
    assert model.calls == 0
    store.set_status(grant['id'], 'paused', actor='synthetic')
    assert worker.status(project.id)['reason'] == 'paused'
    store.set_status(grant['id'], 'active', actor='synthetic')
    worker.process_one()
    assert worker.status(project.id)['reason'] == 'budget_reached'
    settings.model = None
    assert worker.status(project.id)['reason'] == 'model_missing'
    assert len(store.read(project.id)['items']) == 1


def add_story(s, key='two'):
    from datetime import UTC, datetime

    from project_watch.external_analysis import build_external_packet
    from project_watch.external_focus import external_reading
    from project_watch.monitoring import FeedItem, ManualSnapshotAdapter, MonitoringRunner

    repo, project, _, _, _, _, _, _, plans, selections = s
    plan = next(p for p in repo.list_monitoring_plans(project.id) if p.id == plans[0])
    feed = FeedItem(key, 'Synthetic Peer product trial '+key, 'https://example.org/'+key, '', datetime.now(UTC))
    MonitoringRunner(repo, {'rss': ManualSnapshotAdapter({plan.target: (feed,)})}).run(plan.id, idempotency_key=key)
    item = next(i for g in external_reading(repo, project.id)['categories'] for i in g['items']
                if i['url'] == feed.url)
    return build_external_packet(repo, project.id, item['id'], selections=selections)


@pytest.mark.parametrize('future', [True, False])
def test_future_story_scope_is_explicit(tmp_path, future):
    s = sample(tmp_path)
    grant = allow(s, future=future)
    packet = add_story(s)
    if future:
        assert s[6].enqueue(grant['id'], packet)['state'] == 'queued'
    else:
        with pytest.raises(ReadingDenied):
            s[6].enqueue(grant['id'], packet)


def test_two_claimed_jobs_share_durable_request_budget(tmp_path):
    s = sample(tmp_path)
    store, model = s[6], s[4]
    grant = allow(s, calls=1)
    first = store.claim(store.enqueue(grant['id'], s[3])['id'])
    second = store.claim(store.enqueue(grant['id'], add_story(s))['id'])
    body = json.dumps({'model': 'fake', 'max_tokens': OUTPUT_TOKENS, 'stream': True, 'messages': []}).encode()
    store.reserve(first['id'], first['lease_token'], model.authorization_id(), body)
    with pytest.raises(ReadingDenied):
        store.reserve(second['id'], second['lease_token'], model.authorization_id(), body)
    assert store.status(grant['id'])['requests_used'] == 1
    assert store.get(second['id'])['state'] == 'running'


def test_cumulative_cost_budget_blocks_second_dispatch(tmp_path):
    s = sample(tmp_path)
    _, project, _, packet, model, _, store, _, plans, selections = s
    scope = store.preview_scope(project.id, plan_ids=plans, selections=selections, include_future_news=True)
    grant = store.authorize(project.id, scope=scope, config_id=model.authorization_id(),
                            recipient=asdict(model.config), actor='synthetic', request_key='cost-budget-external',
                            limits=ReadingLimits(max_requests=5, max_output_tokens=OUTPUT_TOKENS,
                                                 max_cost_cny='0.005', input_cny_per_million='1',
                                                 output_cny_per_million='1'))
    body = json.dumps({'model': 'fake', 'max_tokens': OUTPUT_TOKENS, 'stream': True, 'messages': []}).encode()
    first = store.claim(store.enqueue(grant['id'], packet)['id'])
    store.reserve(first['id'], first['lease_token'], model.authorization_id(), body)
    second = store.claim(store.enqueue(grant['id'], add_story(s))['id'])
    with pytest.raises(ReadingDenied):
        store.reserve(second['id'], second['lease_token'], model.authorization_id(), body)
    assert store.status(grant['id'])['requests_used'] == 1


def test_unsent_retry_is_bounded_and_never_charged(tmp_path):
    s = sample(tmp_path)
    model, store, worker = s[4], s[6], s[7]
    grant = allow(s)
    def unavailable():
        raise RuntimeError('synthetic local transport unavailable')
    model.before = unavailable
    for expected in ['queued', 'queued', 'failed']:
        job = worker.process_one()
        assert job['state'] == expected
        with closing(s[0]._connect()) as db, db:
            db.execute('UPDATE external_analysis_jobs SET lease_until=0 WHERE id=?', (job['id'],))
    assert worker.process_one() is None
    assert store.status(grant['id'])['requests_used'] == 0 and model.calls == 0


def test_paused_news_and_revoked_consent_are_not_reported_ready(tmp_path):
    s = sample(tmp_path)
    grant = allow(s)
    s[0].set_monitoring_plan_enabled(s[8][0], False)
    assert s[7].status(s[1].id)['reason'] == 'news_paused'
    s[6].set_status(grant['id'], 'revoked', actor='synthetic')
    assert s[7].status(s[1].id)['reason'] == 'consent_required'


def test_shared_reading_loop_runs_external_after_internal_and_stops(tmp_path):
    from project_watch.reading_jobs import ReadingJobs
    from project_watch.reading_worker import ReadingWorker

    s = sample(tmp_path)
    reading = ReadingWorker(s[0], ReadingJobs(s[0]), s[5])
    reading.external_worker = s[7]
    allow(s)
    # Exercise the actual shared loop without a live thread or external network.
    reading.process_one = lambda: None
    def completed():
        reading._stop.set()
    s[4].after = completed
    reading._run()
    assert s[4].calls == 1 and len(s[6].read(s[1].id)['items']) == 1
    reading.stop()
    assert s[7]._stop.is_set()

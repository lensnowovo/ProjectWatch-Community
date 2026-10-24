"""Synthetic reproduction of a legacy site retry blocked before DSH startup."""
import hashlib
from contextlib import closing
from types import SimpleNamespace

import pytest
import test_studio_dsh_monitor as fixtures

from project_watch.studio import dsh_runtime, recovery
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError, Response

URL = 'https://example.com/robots.txt'


def key(url=URL):
    return hashlib.sha256(('site:' + url).encode()).hexdigest()


def lost():
    raise HttpError('connection_failed')


@pytest.fixture
def site(tmp_path):
    loop = fixtures.loop.__wrapped__(tmp_path)
    loop.pipeline.store.upsert_project(loop.project_id, {'website': 'https://example.com/'}, consent=True)
    loop.execution.checkpoint('dsh_binding', None)
    loop.execution.checkpoint('recovery_inputs', {'binding': recovery.input_binding(loop.pipeline, loop.project_id)})
    with pytest.raises(HttpError):
        loop.execution.call('site', key(), lost, provider='guarded_site:example.com')
    loop.store.finish(loop.execution.job['id'], error='request_outcome_unknown', lease_token=loop.execution.job['lease_token'])
    return loop


def approve(loop, nonce='synthetic-site-once'):
    p = recovery.preview(loop.pipeline, loop.project_id, loop.execution.job['id'])
    assert p['can_recover']
    recovery.authorize(loop.pipeline, loop.project_id, loop.execution.job['id'], expected_version=p['version'],
                       idempotency_key=nonce, accept_duplicate_cost=True)
    return JobExecution(loop.store, loop.store.claim())


def run(loop, execution, monkeypatch, send):
    monkeypatch.setattr(loop.pipeline, 'tools', lambda: (SimpleNamespace(model=None), None, None, loop.collectors))
    monkeypatch.setattr(loop.pipeline, 'reader', lambda: SimpleNamespace(transport=SimpleNamespace(get_response=send)))
    monkeypatch.setattr(dsh_runtime, 'Gateway', lambda *args: None)
    monkeypatch.setattr(dsh_runtime, 'run_dsh', lambda research, *args: (research.guard(), 'entered-dsh')[1])
    return dsh_runtime.run_monitor(loop.pipeline, execution, None)


def test_approved_legacy_site_replay_enters_dsh_once(site, monkeypatch):
    execution = approve(site)
    calls = []
    def send(url):
        from project_watch.studio.runtime_health import stage_phase
        stage = next(j['stage'] for j in site.store.jobs(site.project_id) if j['id'] == execution.job['id'])
        assert stage_phase(stage) == 'read'
        calls.append(url)
        return Response(200, {'content-type':'text/plain'}, b'User-agent: *\nAllow: /')
    assert run(site, execution, monkeypatch, send) == 'entered-dsh'
    assert run(site, execution, monkeypatch, send) == 'entered-dsh'
    assert calls == [URL]
    with closing(site.store._connect()) as db:
        assert [r[0] for r in db.execute('SELECT status FROM studio_job_calls ORDER BY rowid')] == ['unknown', 'completed']
    assert execution.usage()['site']['unknown'] == 0


def test_old_guard_reproduces_failure_before_transport(site, monkeypatch):
    execution = approve(site)
    monkeypatch.setattr(dsh_runtime, 'recover_authorized_site', lambda *a: None)
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        run(site, execution, monkeypatch, lambda *a: pytest.fail('must not dispatch'))


def test_retry_of_retry_uses_base_key_and_preserves_both_unknowns(site, monkeypatch):
    first = approve(site)
    with pytest.raises(HttpError):
        first.call('site', key(), lost, provider='guarded_site:example.com')
    site.store.finish(first.job['id'], error='request_outcome_unknown', lease_token=first.job['lease_token'])
    second = approve(site, 'synthetic-second-explicit-approval')
    # A previous DSH refusal must not leave the old hold stuck after a known reply.
    second.checkpoint('dsh_hold', {'code':'request_outcome_unknown'})
    assert run(site, second, monkeypatch, lambda *a: Response(200, {}, b'User-agent: *\nAllow: /')) == 'entered-dsh'
    assert second.read_checkpoint('dsh_hold') == {}
    with closing(site.store._connect()) as db:
        assert db.execute("SELECT count(*) FROM studio_job_calls WHERE status='unknown'").fetchone()[0] == 2
    assert second.usage()['site']['unknown'] == 0


def test_new_timeout_is_not_retried(site, monkeypatch):
    execution = approve(site)
    calls = []
    def send(*a):
        calls.append(1)
        raise HttpError('connection_failed')
    for _ in range(2):
        with pytest.raises(HttpError):
            run(site, execution, monkeypatch, send)
    assert calls == [1]
    assert execution.usage()['site']['unknown'] == 1
    assert recovery.dsh_site_recovery(site.pipeline, execution) is None


@pytest.mark.parametrize('boundary', ['no_consent','config','paused','other_unknown','foreign_job','lost_lease'])
def test_no_bypass_of_boundaries(site, monkeypatch, boundary):
    if boundary == 'no_consent':
        with site.store._connect() as db, db:
            db.execute("UPDATE studio_jobs SET status='queued' WHERE id=?", (site.execution.job['id'],))
        execution = JobExecution(site.store, site.store.claim())
    else:
        execution = approve(site)
    if boundary == 'config':
        settings = site.pipeline.settings_store.load()
        settings.model.model = 'changed-model'
        site.pipeline.settings_store.save(settings)
    elif boundary == 'paused':
        site.store.set_status(site.project_id, 'paused')
    elif boundary == 'other_unknown':
        with pytest.raises(HttpError):
            execution.call('search', 'other', lost)
    elif boundary == 'foreign_job':
        site.store.finish(execution.job['id'], error='held', lease_token=execution.job['lease_token'])
        site.store.enqueue(site.project_id, 'insight', input_version='different-job')
        execution = JobExecution(site.store, site.store.claim())
    elif boundary == 'lost_lease':
        with site.store._connect() as db, db:
            db.execute("UPDATE studio_jobs SET lease_token='different' WHERE id=?", (execution.job['id'],))
    with pytest.raises((HttpError, ValueError, RuntimeError)):
        run(site, execution, monkeypatch, lambda *a: pytest.fail('boundary must stop dispatch'))


@pytest.mark.parametrize('mismatch', ['url','provider','kind','configuration_after_admission'])
def test_dispatch_scope_checks_exact_request_atomically(site, mismatch):
    execution = approve(site)
    slot = recovery.dsh_site_recovery(site.pipeline, execution)
    args = ['site', key()]
    provider = 'guarded_site:example.com'
    if mismatch == 'url':
        args[1] = key('https://example.com/other')
    if mismatch == 'provider':
        provider = 'other-provider'
    if mismatch == 'kind':
        args[0] = 'model'
    if mismatch == 'configuration_after_admission':
        settings = site.pipeline.settings_store.load()
        settings.model.model = 'later-change'
        site.pipeline.settings_store.save(settings)
    with execution.site_recovery(site.pipeline, slot), pytest.raises(HttpError, match='recovery_request_mismatch'):
        execution.call(*args, lambda: pytest.fail('mismatch must not dispatch'), provider=provider)
    with closing(site.store._connect()) as db:
        assert db.execute('SELECT consumed_at FROM studio_call_recoveries').fetchone()[0] is None


def test_unknown_target_not_reconstructed_from_untrusted_guess(site, monkeypatch):
    execution = approve(site)
    slot = recovery.dsh_site_recovery(site.pipeline, execution)
    monkeypatch.setattr(recovery, 'dsh_site_recovery', lambda *a, **k: {**slot,'base_key':'not-a-saved-url'})
    with pytest.raises(HttpError, match='recovery_request_unavailable'):
        run(site, execution, monkeypatch, lambda *a: pytest.fail('no guessed URL'))


def test_known_unreadable_reply_is_not_collection_success(site, monkeypatch):
    from project_watch.feeds import FeedError
    execution = approve(site)
    def send(*a):
        raise FeedError('http_failure', status=403)
    assert run(site, execution, monkeypatch, send) == 'entered-dsh'
    receipt = execution.read_checkpoint('dsh_site_recovery')
    assert receipt['response_status'] == 403
    assert not site.pipeline.monitor.pages(site.project_id)


def test_concurrent_recovery_consumes_slot_only_once(site):
    import threading
    execution = approve(site)
    slot = recovery.dsh_site_recovery(site.pipeline, execution)
    entered, release = threading.Event(), threading.Event()
    errors = []
    calls = []
    def send():
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return Response(200, {}, b'User-agent: *\nAllow: /')
    def first():
        try:
            with execution.site_recovery(site.pipeline, slot):
                execution.call('site', key(), send, provider='guarded_site:example.com')
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert entered.wait(5)
        second = JobExecution(site.store, execution.job)
        with second.site_recovery(site.pipeline, slot), pytest.raises(HttpError):
            second.call('site', key(), lambda: pytest.fail('duplicate'), provider='guarded_site:example.com')
    finally:
        release.set()
        thread.join(timeout=6)
    assert not thread.is_alive() and not errors and calls == [1]


def other_model_failure(site):
    queued = site.store.enqueue(site.project_id, 'insight', input_version='other-model-failure')
    other = JobExecution(site.store, site.store.claim())
    assert other.job['id'] == queued['id']
    other.call('model', 'synthetic-model-500', lambda: Response(500, {}, b'{}'))
    site.store.finish(other.job['id'], error='model_http_500', lease_token=other.job['lease_token'])
    return other


def test_other_received_model_failure_is_not_covered_by_site_consent(site, monkeypatch):
    other_model_failure(site)
    # Reproduce the formerly accepted site-only authorization before DSH migration.
    execution = approve(site)
    calls = []
    def send(url):
        calls.append(url)
        return Response(200, {}, b'User-agent: *\nAllow: /')
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        run(site, execution, monkeypatch, send)
    assert calls == [URL]  # Exact authorized GET settles; model/tool loop stays blocked.
    with closing(site.store._connect()) as db:
        assert db.execute('SELECT consumed_at FROM studio_call_recoveries').fetchone()[0]
        assert db.execute("SELECT count(*) FROM studio_job_calls WHERE kind='model'").fetchone()[0] == 1


def test_dsh_preview_explains_other_blocker_before_requeue(site, monkeypatch):
    other_model_failure(site)
    monkeypatch.setenv('PROJECT_WATCH_DSH_RUNTIME', 'synthetic-runtime')
    monkeypatch.setenv('PROJECT_WATCH_DSH_MONITOR', '1')
    p = recovery.preview(site.pipeline, site.project_id, site.execution.job['id'])
    assert p['can_recover'] and '模型错误仍需另行确认' in p['notice']
    assert p['requests'] == {'site':1}
    recovery.authorize(site.pipeline, site.project_id, site.execution.job['id'], expected_version=p['version'],
                       idempotency_key='site-only', accept_duplicate_cost=True)


def test_recovery_budget_refusal_does_not_claim_request_settled(site, monkeypatch):
    execution = approve(site)
    execution.budgets['site'] = 1
    with pytest.raises(HttpError, match='request_budget'):
        run(site, execution, monkeypatch, lambda *a: pytest.fail('budget refusal must not dispatch'))
    assert execution.read_checkpoint('dsh_site_recovery') is None
    with closing(site.store._connect()) as db:
        assert db.execute('SELECT consumed_at FROM studio_call_recoveries').fetchone()[0] is None


@pytest.mark.parametrize('robots_cached', [True, False])
def test_page_recovery_only_replays_already_known_robots(tmp_path, monkeypatch, robots_cached):
    loop = fixtures.loop.__wrapped__(tmp_path)
    loop.pipeline.store.upsert_project(loop.project_id, {'website': 'https://example.com/'}, consent=True)
    loop.execution.checkpoint('dsh_binding', None)
    loop.execution.checkpoint('recovery_inputs', {'binding': recovery.input_binding(loop.pipeline, loop.project_id)})
    if robots_cached:
        loop.execution.call('site', key(), lambda: Response(200, {}, b'User-agent: *\nAllow: /'),
                            provider='guarded_site:example.com')
    with pytest.raises(HttpError):
        loop.execution.call('site', key('https://example.com/'), lost, provider='guarded_site:example.com')
    loop.store.finish(loop.execution.job['id'], error='request_outcome_unknown', lease_token=loop.execution.job['lease_token'])
    execution = approve(loop)
    calls = []
    def send(url):
        calls.append(url)
        return Response(200, {'content-type':'text/html'}, b'<p>Synthetic company</p>')
    if robots_cached:
        assert run(loop, execution, monkeypatch, send) == 'entered-dsh'
        assert calls == ['https://example.com/']
    else:
        with pytest.raises(HttpError):
            run(loop, execution, monkeypatch, send)
        assert not calls  # No grant for the missing prerequisite request.

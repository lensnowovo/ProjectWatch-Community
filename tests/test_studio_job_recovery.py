"""Synthetic recovery: no live provider or private runtime is used."""

from contextlib import closing

import pytest
from test_studio_agent_instance import make_service

from project_watch.studio.http import HttpError, Response
from project_watch.studio.store import StudioStore


def expire(store, job):
    with closing(store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?", (job["id"],))


def test_restart_does_not_steal_active_lease_and_enqueue_dedupes_running(tmp_path):
    service, pid = make_service(tmp_path / "jobs.sqlite3")
    queued = service.store.enqueue(pid, "external")
    running = service.store.claim()
    other = StudioStore(service.store.db_path)
    assert other.jobs(pid)[0]["status"] == "running"
    assert other.claim() is None
    assert other.enqueue(pid, "external")["id"] == queued["id"]
    assert running["status"] == "running" and running["lease_token"]


def test_expired_completed_calls_replay_without_network(tmp_path):
    from project_watch.studio.execution import JobExecution

    service, pid = make_service(tmp_path / "jobs.sqlite3")
    service.store.enqueue(pid, "external")
    job = service.store.claim()
    calls = []
    execution = JobExecution(service.store, job)
    response = execution.call("model", "synthetic-key", lambda: (calls.append(1) or Response(200, {}, b'{"ok":true}')))
    execution.checkpoint("profile", {"saved": True})
    expire(service.store, job)
    recovered = StudioStore(service.store.db_path).claim()
    assert recovered["id"] == job["id"] and recovered["lease_token"] != job["lease_token"]
    new = JobExecution(service.store, recovered)
    assert new.call("model", "synthetic-key", lambda: pytest.fail("must not repeat paid call")) == response
    assert new.read_checkpoint("profile") == {"saved": True}
    with pytest.raises(RuntimeError):
        execution.checkpoint("late", {})


def test_article_read_is_replayed_after_crash_before_body_save(tmp_path):
    from project_watch.studio.app import create_studio_app
    pipeline = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    project = pipeline.repository.create_project('Synthetic Reader')
    store = pipeline.store
    store.upsert_project(project.id, {}, consent=True)
    store.add_items(project.id, 'company', [{'key': 'article', 'title': 'Synthetic Reader update',
        'url': 'https://synthetic.example/news', 'source': 'Synthetic', 'snippet': '', 'published_at': None}])
    calls = []
    class Reader:
        def read(self, url):
            calls.append(url)
            return {'status': 'article_body', 'text': 'Synthetic Reader original public update.'}
    pipeline.article_reader = Reader()
    pipeline.run_external = lambda job: pipeline.fetch_bodies(job['project_id'], lambda _: None)
    save_body = store.save_body
    store.save_body = lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt('synthetic crash'))
    store.enqueue(project.id, 'external')
    first = store.claim()
    with pytest.raises(KeyboardInterrupt):
        pipeline.run(first)
    expire(store, first)
    second = store.claim()
    assert second is not None
    store.save_body = save_body
    pipeline.run(second)
    assert len(calls) == 1
    assert store.items_with_body(project.id)[0]['body'] == 'Synthetic Reader original public update.'


def test_unexpected_reader_failure_does_not_complete_leased_dispatch(tmp_path):
    from project_watch.studio.app import create_studio_app
    from project_watch.studio.execution import JobExecution
    pipeline = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    project = pipeline.repository.create_project('Synthetic Reader')
    store = pipeline.store
    store.upsert_project(project.id, {}, consent=True)
    store.add_items(project.id, 'company', [{'key': 'article', 'title': 'Synthetic Reader update',
        'url': 'https://synthetic.example/news', 'source': 'Synthetic', 'snippet': '', 'published_at': None}])
    class Reader:
        def read(self, url):
            raise ValueError('synthetic unexpected reader error')
    pipeline.article_reader = Reader()
    pipeline.run_external = lambda job: pipeline.fetch_bodies(job['project_id'], lambda _: None)
    store.enqueue(project.id, 'external')
    job = store.claim()
    with pytest.raises(ValueError):
        pipeline.run(job)
    assert JobExecution(store, job).read_checkpoint('dispatch_completed') is None
    assert JobExecution(store, job).usage()['page']['unknown'] == 1
    assert store.items_with_body(project.id) == []


def test_inflight_call_is_not_implicitly_retried_and_automatic_enqueue_holds(tmp_path):
    from project_watch.studio.execution import JobExecution

    service, pid = make_service(tmp_path / "jobs.sqlite3")
    service.store.enqueue(pid, "external")
    job = service.store.claim()
    execution = JobExecution(service.store, job)
    with pytest.raises(KeyboardInterrupt):
        execution.call("search", "inflight", lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
    expire(service.store, job)
    assert service.store.claim() is None
    interrupted = service.store.jobs(pid)[0]
    assert interrupted["status"] == "failed" and "interrupted" in interrupted["error"]
    assert service.store.enqueue(pid, "external", automatic=True)["id"] == job["id"]
    assert service.store.enqueue(pid, "external")["id"] != job["id"]  # explicit new run


def test_request_budget_and_provider_denial_stop_before_transport(tmp_path):
    from project_watch.studio.execution import JobExecution

    service, pid = make_service(tmp_path / "jobs.sqlite3")
    service.store.enqueue(pid, "external")
    job = service.store.claim()
    execution = JobExecution(service.store, job, budgets={"search": 1})
    execution.call("search", "one", lambda: Response(200, {}, b"ok"))
    with pytest.raises(HttpError, match="request_budget"):
        execution.call("search", "two", lambda: pytest.fail("budget must stop transport"))
    execution.call("model", "denial", lambda: Response(429, {}, b"do not expose this raw response"), provider="synthetic-model")
    with pytest.raises(HttpError, match="provider_backoff"):
        execution.call("model", "another", lambda: pytest.fail("denial stops further requests"), provider="synthetic-model")
    assert execution.usage()["search"]["requests"] == 1
    assert "do not expose" not in str(execution.usage())


def test_job_finish_is_fenced_and_recovery_is_bounded(tmp_path):
    service, pid = make_service(tmp_path / "jobs.sqlite3")
    service.store.enqueue(pid, "internal")
    first = service.store.claim()
    expire(service.store, first)
    second = service.store.claim()
    assert not service.store.finish(first["id"], lease_token=first["lease_token"])
    expire(service.store, second)
    third = service.store.claim()
    expire(service.store, third)
    assert service.store.claim() is None
    assert service.store.jobs(pid)[0]["status"] == "failed"


def test_worker_dispatch_reuses_model_response_and_does_not_double_count_usage(tmp_path):
    import json

    from project_watch.studio.app import create_studio_app
    from project_watch.studio.http import Http

    app = create_studio_app(tmp_path, start_worker=False)
    pipeline, store = app.state.studio_pipeline, app.state.studio_store
    project = pipeline.repository.create_project("Synthetic recovery")
    store.upsert_project(project.id, {}, consent=True)
    pipeline.settings_store.update({"model": {"protocol": "openai", "base_url": "https://model.example/v1",
        "model": "synthetic", "api_key": "secret-not-persisted"}})
    calls, results = [], []

    def transport(*_args):
        calls.append(1)
        return Response(200, {"set-cookie": "never-save"}, json.dumps({"choices": [{"message": {"content": "result"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2}}).encode())

    pipeline.http_factory = lambda _settings: Http(transport)

    def run(job):
        results.append(pipeline.tools()[2].complete("synthetic", "data"))
        if len(results) == 1:
            raise KeyboardInterrupt("synthetic crash after response")

    pipeline.run_external = run
    store.enqueue(project.id, "external")
    first = store.claim()
    with pytest.raises(KeyboardInterrupt):
        pipeline.run(first)
    expire(store, first)
    second = store.claim()
    pipeline.run(second)
    assert calls == [1] and results == ["result", "result"]
    assert store.usage()[0]["calls"] == 1
    with closing(store._connect()) as db:
        persisted = str([tuple(r) for r in db.execute("SELECT * FROM studio_job_calls")])
        assert "secret-not-persisted" not in persisted and "never-save" not in persisted
    pipeline.run(second)  # completed dispatch checkpoint avoids doing stages again
    assert len(results) == 2


def test_model_request_hold_does_not_sleep_or_retry(monkeypatch):
    from project_watch.studio.http import Http
    from project_watch.studio.llm import LLM, ModelError
    from project_watch.studio.settings import ModelConfig

    monkeypatch.setattr("project_watch.studio.llm.time.sleep", lambda _seconds: pytest.fail("held requests must not retry"))
    def blocked(*_args):
        raise HttpError("provider_backoff")
    llm = LLM(ModelConfig("openai", "https://synthetic.example/v1", "model", "key"), Http(blocked))
    with pytest.raises(ModelError) as failure:
        llm.complete("synthetic", "data")
    assert failure.value.code == "provider_backoff"

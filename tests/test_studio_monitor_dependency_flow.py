from contextlib import closing
from types import SimpleNamespace

from test_studio_agent_instance import make_service
from test_studio_evidence_versions import sample

from project_watch.studio.pipeline import StudioWorker


def worker_for(service):
    for project in service.store.list_projects():
        service.store.upsert_project(project["id"], project, consent=True)
    return StudioWorker(SimpleNamespace(store=service.store))


def test_revision_dispatches_only_affected_profile_once_and_failure_preserves_warning(tmp_path):
    service, pid = make_service(tmp_path / "flow.sqlite3")
    deps = service.evidence.dependencies
    old = service.evidence.save(sample(pid, "Revenue 100"))
    deps.track(pid, "profile", "profile", [old["id"]])
    service.evidence.save(sample(pid, "Revenue 80"))
    worker = worker_for(service)
    worker.dispatch_rebuilds(model_ready=True)
    queued = service.store.jobs(pid)
    assert len(queued) == 1 and queued[0]["kind"] == "internal"
    worker.dispatch_rebuilds(model_ready=True)
    assert len(service.store.jobs(pid)) == 1
    service.store.finish(queued[0]["id"], error="synthetic model failure")
    worker.dispatch_rebuilds(model_ready=True)
    assert len(service.store.jobs(pid)) == 1
    assert deps.get(pid, "profile")["status"] == "stale"
    assert deps.pending(pid)[0]["status"] == "blocked_failed"


def test_disabled_model_or_paused_project_does_not_dispatch(tmp_path):
    service, pid = make_service(tmp_path / "flow.sqlite3")
    deps = service.evidence.dependencies
    old = service.evidence.save(sample(pid, "Old"))
    deps.track(pid, "profile", "profile", [old["id"]])
    service.evidence.save(sample(pid, "New"))
    worker = worker_for(service)
    worker.dispatch_rebuilds(model_ready=False)
    assert not service.store.jobs(pid)
    service.store.set_status(pid, "paused")
    worker.dispatch_rebuilds(model_ready=True)
    assert not service.store.jobs(pid)


def test_new_generation_after_failed_rebuild_can_queue_and_unrelated_facts_do_not_queue_models(tmp_path):
    service, pid = make_service(tmp_path / "flow.sqlite3")
    deps = service.evidence.dependencies
    old = service.evidence.save(sample(pid, "Old"))
    deps.track(pid, "profile", "profile", [old["id"]])
    deps.track(pid, "fact:immutable", "fact", [old["id"]])
    service.evidence.save(sample(pid, "New"))
    worker = worker_for(service)
    worker.dispatch_rebuilds(model_ready=True)
    job = service.store.jobs(pid)[0]
    service.store.finish(job["id"], error="failed")
    worker.dispatch_rebuilds(model_ready=True)
    service.evidence.save(sample(pid, "Newest"))
    worker.dispatch_rebuilds(model_ready=True)
    assert len(service.store.jobs(pid)) == 2
    requests = {r["artifact_key"]: r["status"] for r in deps.pending(pid)}
    assert requests["fact:immutable"] == "awaiting_source_update"
    with closing(service.store._connect()) as db:
        assert db.execute("SELECT count(*) FROM studio_jobs WHERE kind<>'internal'").fetchone()[0] == 0


def test_late_brief_cannot_overwrite_after_relationship_correction(tmp_path):
    service, pid = make_service(tmp_path / "flow.sqlite3")
    deps = service.evidence.dependencies
    generation = deps.generation(pid)
    service.store.put_compiled_brief(pid, {"headline": "Previously saved"}, [], expected_generation=generation)
    with closing(service.store._connect()) as db, db:
        deps.on_relationship_corrected(pid, db=db)
    result = service.store.put_compiled_brief(pid, {"headline": "Old late result"}, [], expected_generation=generation)
    assert result["status"] == "stale"
    assert service.store.get_brief(pid)["headline"] == "Previously saved"
    assert service.store.get_brief(pid)["dependency"]["status"] == "stale"

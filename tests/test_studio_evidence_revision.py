from test_studio_agent_instance import make_service
from test_studio_evidence_versions import sample

from project_watch.studio.dependencies import DependencyStore


def test_revision_invalidates_only_dependents_and_coalesces_latest_rebuild(tmp_path):
    service, pid = make_service(tmp_path / "revision.sqlite3")
    deps = DependencyStore(service.store)
    first = service.evidence.save(sample(pid, "Revenue 100"))
    unrelated = service.evidence.save(sample(pid, "Other source").model_copy(update={"source_key": "other"}))
    deps.track(pid, "report", "profile", [first["id"]])
    deps.track(pid, "unrelated", "profile", [unrelated["id"]])
    service.evidence.save(sample(pid, "Revenue 80"))
    assert deps.get(pid, "report")["status"] == "stale"
    assert deps.get(pid, "unrelated")["status"] == "current"
    service.evidence.save(sample(pid, "Revenue 60"))
    work = deps.pending(pid)
    assert len(work) == 1 and work[0]["generation"] == deps.generation(pid)
    assert service.evidence.get(pid, first["id"])["parts"][0]["text"] == "Revenue 100"


def test_cosmetic_change_not_analysis_and_http_failure_not_withdrawal(tmp_path):
    service, pid = make_service(tmp_path / "cosmetic.sqlite3")
    deps = DependencyStore(service.store)
    original = service.evidence.save(sample(pid, "Revenue 100"))
    deps.track(pid, "report", "profile", [original["id"]])
    service.evidence.save(sample(pid, "Revenue\n100"))
    assert deps.get(pid, "report")["status"] == "current" and not deps.pending(pid)
    deps.record_read_failure(pid, "synthetic-source", "http_403")
    assert deps.get(pid, "report")["status"] == "current"
    assert deps.changes(pid)["items"][-1]["kind"] == "source_read_failure"


def test_withdrawal_and_stale_completion_are_durable(tmp_path):
    service, pid = make_service(tmp_path / "withdrawal.sqlite3")
    deps = DependencyStore(service.store)
    original = service.evidence.save(sample(pid, "Revenue 100"))
    generation = deps.generation(pid)
    deps.track(pid, "report", "profile", [original["id"]], expected_generation=generation)
    deps.withdraw(pid, original["id"], "publisher correction", actor="user")
    assert deps.get(pid, "report")["status"] == "stale"
    late = deps.track(pid, "report", "profile", [original["id"]], expected_generation=generation)
    assert late["status"] == "stale"
    assert DependencyStore(service.store).get(pid, "report")["status"] == "stale"
    assert deps.pending(pid)


def test_incremental_changes_bound_to_project_and_visibility(tmp_path):
    service, pid = make_service(tmp_path / "changes.sqlite3")
    deps = DependencyStore(service.store)
    service.evidence.save(sample(pid))
    service.evidence.save(sample(pid, "new value"))
    assert len(deps.changes(pid)["items"]) == 2
    assert deps.changes(pid, include_internal=False)["items"] == []
    first = deps.changes(pid, limit=1)
    assert len(deps.changes(pid, after=first["next_after"])["items"]) == 1
    assert deps.changes("another-project")["items"] == []


def test_late_new_material_keeps_previous_profile_and_failed_rebuild_warning(tmp_path):
    service, pid = make_service(tmp_path / "late-profile.sqlite3")
    deps = DependencyStore(service.store)
    first = service.evidence.save(sample(pid, "Revenue 100"))
    old_generation = deps.generation(pid)
    service.store.put_compiled_profile(pid, {"summary": "previous saved profile"}, [first["id"]],
                                       expected_generation=old_generation)
    service.evidence.save(sample(pid, "new document").model_copy(update={"source_key": "additional-document"}))
    result = service.store.put_compiled_profile(pid, {"summary": "late model result"}, [first["id"]],
                                               expected_generation=old_generation)
    assert result["status"] == "stale"
    assert service.store.get_profile(pid)["summary"] == "previous saved profile"
    assert service.store.get_profile(pid)["dependency"]["status"] == "stale"
    assert deps.pending(pid)


def test_source_can_restore_prior_content_without_duplicate_version(tmp_path):
    service, pid = make_service(tmp_path / "restored.sqlite3")
    deps = DependencyStore(service.store)
    first = service.evidence.save(sample(pid, "Revenue 100"))
    second = service.evidence.save(sample(pid, "Revenue 60"))
    deps.track(pid, "report", "profile", [second["id"]])
    restored = service.evidence.save(sample(pid, "Revenue 100"))
    assert restored["id"] == first["id"]
    assert deps.get(pid, "report")["status"] == "stale"
    assert deps.track(pid, "restored-report", "profile", [first["id"]])["status"] == "current"
    assert len(service.evidence.versions(pid, "synthetic-source")) == 2

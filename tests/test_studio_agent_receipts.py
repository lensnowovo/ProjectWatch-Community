from concurrent.futures import ThreadPoolExecutor

import pytest
from test_studio_agent_instance import make_service

from project_watch.studio.application import VersionConflict


def test_status_idempotency_versions_and_receipts(tmp_path):
    service, project_id = make_service(tmp_path / "receipts.sqlite3")
    initial = service.project(project_id)["config_version"]
    receipt = service.change_status(
        project_id, "paused", expected_version=initial,
        idempotency_key="pause-1", actor="synthetic-agent",
    )
    assert receipt["saved"] is True
    assert receipt["operation_status"] == "completed"
    assert receipt["job_status"] == "not_requested"
    assert receipt["version"] == initial + 1
    assert receipt == service.change_status(
        project_id, "paused", expected_version=initial,
        idempotency_key="pause-1", actor="synthetic-agent",
    )
    assert service.receipt(project_id, receipt["operation_id"]) == receipt
    with pytest.raises(VersionConflict):
        service.change_status(project_id, "active", expected_version=initial,
                              idempotency_key="resume-2", actor="synthetic-agent")
    with pytest.raises(VersionConflict):
        service.change_status(project_id, "active", expected_version=initial,
                              idempotency_key="pause-1", actor="synthetic-agent")
    assert not service.store.jobs(project_id)


def test_web_changes_conflict_with_old_agent_version(tmp_path):
    service, project_id = make_service(tmp_path / "web.sqlite3")
    version = service.project(project_id)["config_version"]
    service.store.set_note(project_id, "Human note")
    with pytest.raises(VersionConflict):
        service.change_status(project_id, "paused", expected_version=version,
                              idempotency_key="stale", actor="synthetic-agent")


def test_atomic_concurrent_requests_only_one_wins(tmp_path):
    service, project_id = make_service(tmp_path / "race.sqlite3")
    version = service.project(project_id)["config_version"]

    def attempt(key):
        try:
            return service.change_status(project_id, "paused", expected_version=version,
                                         idempotency_key=key, actor="synthetic-agent")["saved"]
        except VersionConflict:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(attempt, ["one", "two"])) == [False, True]


def test_pause_reports_running_work_instead_of_claiming_cancelled(tmp_path):
    service, project_id = make_service(tmp_path / "work.sqlite3")
    job = service.store.enqueue(project_id, "external")
    service.store.claim()
    receipt = service.change_status(project_id, "paused", expected_version=1,
                                    idempotency_key="pause-running", actor="synthetic-agent")
    assert receipt["in_flight_job_ids"] == [job["id"]]
    assert service.store.jobs(project_id)[0]["status"] == "running"


def test_paused_monitoring_queue_waits_until_resume(tmp_path):
    service, project_id = make_service(tmp_path / "paused-queue.sqlite3")
    job = service.store.enqueue(project_id, "external")
    receipt = service.change_status(project_id, "paused", expected_version=1,
                                    idempotency_key="pause-queued", actor="synthetic-agent")
    assert service.store.claim() is None
    assert service.store.jobs(project_id)[0]["status"] == "queued"
    service.change_status(project_id, "active", expected_version=receipt["version"],
                          idempotency_key="resume-queued", actor="synthetic-agent")
    assert service.store.claim()["id"] == job["id"]

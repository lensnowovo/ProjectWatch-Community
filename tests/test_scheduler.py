from datetime import UTC, datetime, timedelta
from pathlib import Path

from project_watch.monitoring import (
    AdapterResult,
    FeedItem,
    ManualSnapshotAdapter,
    MonitoringRunner,
)
from project_watch.repository import ProjectRepository
from project_watch.scheduler import MonitoringScheduler


class FakeAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, _target: str) -> AdapterResult:
        self.calls += 1
        return AdapterResult(
            items=(
                FeedItem(
                    external_id="one",
                    title="Synthetic Monitor update",
                    url="https://example.org/one",
                    summary="A synthetic announcement.",
                    published_at=datetime.now(UTC),
                ),
            )
        )


def _setup(tmp_path: Path) -> tuple[ProjectRepository, str, str]:
    repo = ProjectRepository(tmp_path / "watch.sqlite3")
    project = repo.create_project("Synthetic Monitor")
    plan = repo.create_monitoring_plan(
        project.id, kind="rss", target="https://example.org/feed.xml", cadence_minutes=60
    )
    return repo, project.id, plan.id


def test_manual_snapshot_adapter_is_offline_and_distinguishes_missing_from_empty() -> None:
    adapter = ManualSnapshotAdapter({"https://example.org/empty.xml": ()})

    assert adapter.fetch("https://example.org/empty.xml") == AdapterResult(items=())
    assert adapter.fetch("https://example.org/missing.xml").unavailable_error == "snapshot_missing"


def test_disabled_plan_does_not_schedule_but_manual_request_runs(tmp_path: Path) -> None:
    repo, project_id, plan_id = _setup(tmp_path)
    assert repo.schedule_due_monitoring() == []

    job = repo.request_monitoring_run(plan_id, idempotency_key="manual-1")
    assert job.status == "queued" and job.trigger == "manual"
    assert repo.request_monitoring_run(plan_id, idempotency_key="manual-1").id == job.id
    adapter = FakeAdapter()
    completed = MonitoringScheduler(repo, MonitoringRunner(repo, {"rss": adapter})).process_one()

    assert completed.id == job.id and completed.status == "done"
    assert adapter.calls == 1
    assert repo.list_monitoring_jobs(project_id) == [completed]
    assert repo.list_source_runs(plan_id)[0].new_count == 1


def test_enabled_plan_schedules_once_and_waits_for_cadence(tmp_path: Path) -> None:
    repo, _project_id, plan_id = _setup(tmp_path)
    repo.set_monitoring_plan_enabled(plan_id, True)
    now = datetime.now(UTC)

    jobs = repo.schedule_due_monitoring(now=now)
    assert len(jobs) == 1 and jobs[0].trigger == "scheduled"
    assert repo.schedule_due_monitoring(now=now) == []
    adapter = FakeAdapter()
    MonitoringScheduler(repo, MonitoringRunner(repo, {"rss": adapter})).process_one()

    assert repo.schedule_due_monitoring(now=now + timedelta(minutes=30)) == []
    assert len(repo.schedule_due_monitoring(now=now + timedelta(minutes=61))) == 1


def test_expired_lease_is_reclaimed_without_duplicate_run(tmp_path: Path) -> None:
    repo, _project_id, plan_id = _setup(tmp_path)
    job = repo.request_monitoring_run(plan_id, idempotency_key="manual-2")
    now = datetime.now(UTC)
    claimed = repo.claim_next_monitoring_job(now=now, lease_seconds=1)
    assert claimed.id == job.id and claimed.status == "processing"
    assert repo.claim_next_monitoring_job(now=now) is None

    adapter = FakeAdapter()
    completed = MonitoringScheduler(repo, MonitoringRunner(repo, {"rss": adapter})).process_one(
        now=now + timedelta(seconds=2)
    )

    assert completed.status == "done" and completed.attempts == 2
    assert len(repo.list_source_runs(plan_id)) == 1
    assert adapter.calls == 1


def test_disabling_queued_scheduled_plan_records_not_run(tmp_path: Path) -> None:
    repo, _project_id, plan_id = _setup(tmp_path)
    repo.set_monitoring_plan_enabled(plan_id, True)
    job = repo.schedule_due_monitoring()[0]
    repo.set_monitoring_plan_enabled(plan_id, False)

    completed = MonitoringScheduler(
        repo, MonitoringRunner(repo, {"rss": FakeAdapter()})
    ).process_one()

    assert completed.id == job.id and completed.status == "done"
    assert repo.list_source_runs(plan_id)[0].status == "not_run"


def test_job_survives_repository_restart(tmp_path: Path) -> None:
    repo, project_id, plan_id = _setup(tmp_path)
    queued = repo.request_monitoring_run(plan_id, idempotency_key="manual-restart")

    after_restart = ProjectRepository(tmp_path / "watch.sqlite3")

    assert after_restart.list_monitoring_jobs(project_id) == [queued]
    assert after_restart.claim_next_monitoring_job().id == queued.id


def test_failed_internal_job_can_be_retried_with_same_key(tmp_path: Path) -> None:
    repo, _project_id, plan_id = _setup(tmp_path)
    job = repo.request_monitoring_run(plan_id, idempotency_key="retry-me")
    repo.claim_next_monitoring_job()
    failed = repo.fail_monitoring_job(job.id, error_code="internal_error")

    assert failed.status == "failed"
    retried = repo.retry_failed_monitoring_job(job.id)

    assert retried.id == job.id and retried.status == "queued"
    assert retried.error_code is None
    assert repo.claim_next_monitoring_job().attempts == 2

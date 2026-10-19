from pathlib import Path

import pytest

from project_watch.repository import NotFoundError, ProjectRepository


def test_monitoring_plan_defaults_to_disabled_and_not_run(tmp_path: Path) -> None:
    repository = ProjectRepository(tmp_path / "watch.sqlite3")
    project = repository.create_project("Synthetic Device Maker")

    plan = repository.create_monitoring_plan(
        project.id, kind="rss", target="https://example.org/news.xml", cadence_minutes=1440
    )

    assert plan.project_id == project.id
    assert plan.enabled is False
    assert plan.last_run_status == "not_run"
    assert repository.list_monitoring_plans(project.id) == [plan]
    assert ProjectRepository(tmp_path / "watch.sqlite3").get_monitoring_plan(plan.id) == plan


def test_monitoring_plan_is_project_scoped_and_idempotent(tmp_path: Path) -> None:
    repository = ProjectRepository(tmp_path / "watch.sqlite3")
    first = repository.create_project("Synthetic One")
    second = repository.create_project("Synthetic Two")

    plan = repository.create_monitoring_plan(
        first.id, kind="rss", target="https://example.org/news.xml", cadence_minutes=60
    )
    repeat = repository.create_monitoring_plan(
        first.id, kind="rss", target="https://example.org/news.xml", cadence_minutes=60
    )
    other = repository.create_monitoring_plan(
        second.id, kind="rss", target="https://example.org/news.xml", cadence_minutes=60
    )

    assert repeat.id == plan.id
    assert other.id != plan.id
    assert repository.list_monitoring_plans(first.id) == [plan]
    with pytest.raises(NotFoundError):
        repository.list_monitoring_plans("missing")


@pytest.mark.parametrize(
    ("kind", "target", "cadence"),
    [
        ("rss", "http://example.org/feed", 60),
        ("rss", "https://localhost/feed", 60),
        ("rss", "https://example.org/feed", 0),
        ("rss", "https://example.org/feed", 10081),
        ("other", "https://example.org/feed", 60),
    ],
)
def test_invalid_monitoring_plan_rejected(
    tmp_path: Path, kind: str, target: str, cadence: int
) -> None:
    repository = ProjectRepository(tmp_path / "watch.sqlite3")
    project = repository.create_project("Synthetic Company")

    with pytest.raises(ValueError, match="monitoring plan"):
        repository.create_monitoring_plan(
            project.id, kind=kind, target=target, cadence_minutes=cadence
        )

    assert repository.list_monitoring_plans(project.id) == []


def test_run_receipt_distinguishes_no_result_from_unreachable(tmp_path: Path) -> None:
    repository = ProjectRepository(tmp_path / "watch.sqlite3")
    project = repository.create_project("Synthetic Company")
    plan = repository.create_monitoring_plan(
        project.id, kind="rss", target="https://example.org/feed.xml", cadence_minutes=60
    )

    empty = repository.record_source_run(plan.id, status="no_results", error_code=None)
    unreachable = repository.record_source_run(plan.id, status="unreachable", error_code="timeout")

    assert empty.status == "no_results"
    assert unreachable.status == "unreachable"
    assert unreachable.error_code == "timeout"
    assert repository.list_source_runs(plan.id) == [empty, unreachable]
    assert repository.list_latest_source_runs(project.id) == [unreachable]
    assert repository.get_monitoring_plan(plan.id).last_run_status == "unreachable"

    other = repository.create_project("Unrelated Synthetic Company")
    other_plan = repository.create_monitoring_plan(
        other.id, kind="rss", target="https://example.org/other.xml", cadence_minutes=60
    )
    other_run = repository.record_source_run(other_plan.id, status="no_results", error_code=None)
    assert repository.list_latest_source_runs(project.id) == [unreachable]
    assert repository.list_latest_source_runs(other.id) == [other_run]

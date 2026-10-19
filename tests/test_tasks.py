"""Versioned, evidence-bounded execution tasks use only synthetic projects."""

import hashlib
from pathlib import Path

import pytest

from project_watch.repository import ProjectRepository
from project_watch.tasks import TaskService


def _source(repo: ProjectRepository, project_id: str, label: str) -> str:
    return repo.add_source(
        project_id,
        kind="document",
        reference=f"{label}.txt",
        content_sha256=hashlib.sha256(label.encode()).hexdigest(),
    ).id


def test_native_coverage_task_is_idempotent_and_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "watch.sqlite3"
    repo = ProjectRepository(path)
    project = repo.create_project("Synthetic Task Project")
    source_id = _source(repo, project.id, "bp")

    task = TaskService(repo).run_coverage(
        project.id, source_ids=(source_id,), idempotency_key="coverage-1"
    )

    assert task.contract_version == 1
    assert task.kind == "coverage" and task.mode == "native"
    assert task.status == "completed" and task.source_ids == (source_id,)
    assert task.outcome["source_count"] == 1
    assert "经营情况" in task.outcome["missing_topics"]
    assert "business_claims" not in task.outcome
    assert (
        TaskService(repo)
        .run_coverage(project.id, source_ids=(source_id,), idempotency_key="coverage-1")
        .id
        == task.id
    )
    assert ProjectRepository(path).get_execution_task(project.id, task.id) == task


def test_coverage_task_rejects_foreign_source_and_reports_empty_evidence(tmp_path: Path) -> None:
    repo = ProjectRepository(tmp_path / "watch.sqlite3")
    first = repo.create_project("Synthetic First")
    second = repo.create_project("Synthetic Second")
    foreign_id = _source(repo, second.id, "foreign")

    with pytest.raises(ValueError, match="another project"):
        TaskService(repo).run_coverage(
            first.id, source_ids=(foreign_id,), idempotency_key="foreign"
        )
    empty = TaskService(repo).run_coverage(first.id, source_ids=(), idempotency_key="empty")
    assert empty.outcome["source_count"] == 0
    assert len(empty.outcome["missing_topics"]) == 6
    assert empty.outcome["confirmed_count"] == 0

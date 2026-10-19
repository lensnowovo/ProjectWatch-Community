"""A selected set of candidates is reviewed together or not at all."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from project_watch.api import create_app
from project_watch.repository import NotFoundError, ProjectRepository, ReviewConflictError


def _candidates(repository: ProjectRepository):
    project = repository.create_project("Batch review synthetic")
    source = repository.add_source(
        project.id, kind="document", reference="synthetic.txt", content_sha256="a" * 64
    )
    first = repository.add_observation(
        project.id,
        source_id=source.id,
        subject="经营情况",
        statement="已获订单",
        source_locator="line 1",
    )
    second = repository.add_observation(
        project.id,
        source_id=source.id,
        subject="经营情况",
        statement="计划扩产",
        source_locator="line 2",
    )
    return project, first, second


def test_batch_review_is_atomic_and_audited(tmp_path: Path) -> None:
    repository = ProjectRepository(tmp_path / "watch.sqlite3")
    project, first, second = _candidates(repository)
    reviewed = repository.review_observations_batch(
        project.id,
        ((first.id, 1, "confirm"), (second.id, 1, "reject")),
        reviewer="synthetic-reviewer",
    )
    assert [(item.id, item.status, item.version) for item in reviewed] == [
        (first.id, "confirmed", 2),
        (second.id, "rejected", 2),
    ]
    assert [item.action for item in repository.review_history(first.id)] == ["confirm"]
    assert [item.action for item in repository.review_history(second.id)] == ["reject"]
    assert {event.action for event in repository.list_audit_events(project.id)} >= {
        "candidate.confirmed",
        "candidate.rejected",
    }


def test_batch_review_rejects_stale_cross_project_and_duplicate_without_partial_write(
    tmp_path: Path,
) -> None:
    repository = ProjectRepository(tmp_path / "watch.sqlite3")
    project, first, second = _candidates(repository)
    other = repository.create_project("Outside synthetic")
    outside_source = repository.add_source(
        other.id, kind="document", reference="outside.txt", content_sha256="b" * 64
    )
    outside = repository.add_observation(
        other.id,
        source_id=outside_source.id,
        subject="经营情况",
        statement="外部记录",
        source_locator="line 1",
    )
    with pytest.raises(ReviewConflictError):
        repository.review_observations_batch(
            project.id,
            ((first.id, 1, "confirm"), (second.id, 2, "confirm")),
            reviewer="synthetic-reviewer",
        )
    with pytest.raises(NotFoundError):
        repository.review_observations_batch(
            project.id,
            ((first.id, 1, "confirm"), (outside.id, 1, "confirm")),
            reviewer="synthetic-reviewer",
        )
    with pytest.raises(ValueError, match="duplicate"):
        repository.review_observations_batch(
            project.id,
            ((first.id, 1, "confirm"), (first.id, 1, "reject")),
            reviewer="synthetic-reviewer",
        )
    assert [item.status for item in repository.list_observations(project.id)] == [
        "candidate",
        "candidate",
    ]
    assert repository.review_history(first.id) == []


def test_preview_batch_endpoint_exposes_atomic_result(tmp_path: Path) -> None:
    path = tmp_path / "watch.sqlite3"
    repository = ProjectRepository(path)
    project, first, second = _candidates(repository)
    client = TestClient(create_app(path))
    response = client.post(
        f"/api/projects/{project.id}/observations/batch-review",
        json={
            "reviewer": "synthetic-reviewer",
            "items": [
                {"observation_id": first.id, "expected_version": 1, "action": "confirm"},
                {"observation_id": second.id, "expected_version": 1, "action": "reject"},
            ],
        },
    )
    assert response.status_code == 200
    assert [item["status"] for item in response.json()] == ["confirmed", "rejected"]
    stale = client.post(
        f"/api/projects/{project.id}/observations/batch-review",
        json={
            "reviewer": "synthetic-reviewer",
            "items": [
                {"observation_id": first.id, "expected_version": 1, "action": "confirm"},
            ],
        },
    )
    assert stale.status_code == 409

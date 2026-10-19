from decimal import Decimal
from pathlib import Path

import pytest

from project_watch.repository import (
    NotFoundError,
    ProjectRepository,
    ReviewConflictError,
)


def _repository(tmp_path: Path) -> ProjectRepository:
    return ProjectRepository(tmp_path / "watch.sqlite3")


def test_source_is_idempotent_by_content_hash(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    project = repository.create_project("Synthetic Company")

    first = repository.add_source(
        project.id,
        kind="document",
        reference="example.pdf",
        content_sha256="a" * 64,
    )
    duplicate = repository.add_source(
        project.id,
        kind="document",
        reference="renamed-example.pdf",
        content_sha256="a" * 64,
    )

    assert duplicate.id == first.id
    assert len(repository.list_sources(project.id)) == 1


def test_observation_is_pending_until_reviewed(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    project = repository.create_project("Synthetic Company")
    source = repository.add_source(
        project.id, kind="document", reference="example.pdf", content_sha256="a" * 64
    )

    observation = repository.add_observation(
        project.id,
        source_id=source.id,
        subject="revenue",
        statement="Reported revenue for the synthetic period",
        number=Decimal("120.50"),
        unit="CNY million",
        period="2025 FY",
        scope="company",
        source_locator="p. 3, table 2",
    )

    assert observation.status == "candidate"
    assert observation.version == 1
    assert repository.list_observations(project.id, status="confirmed") == []
    assert repository.list_observations(project.id, status="candidate") == [observation]

    confirmed = repository.confirm_observation(
        observation.id, reviewer="analyst-1", expected_version=1
    )

    assert confirmed.status == "confirmed"
    assert confirmed.version == 2
    assert repository.list_observations(project.id, status="confirmed") == [confirmed]
    history = repository.review_history(observation.id)
    assert len(history) == 1
    assert history[0].reviewer == "analyst-1"
    assert history[0].action == "confirm"
    assert repository.review_decisions_for_report(project.id, [observation.id]) == history
    other = repository.create_project("Other Synthetic Company")
    other_source = repository.add_source(
        other.id, kind="document", reference="other.pdf", content_sha256="b" * 64
    )
    other_observation = repository.add_observation(
        other.id,
        source_id=other_source.id,
        subject="revenue",
        statement="Unrelated figure",
        source_locator="p. 1",
    )
    repository.confirm_observation(
        other_observation.id, reviewer="other-reviewer", expected_version=1
    )
    assert repository.review_decisions_for_report(
        project.id, [observation.id, other_observation.id]
    ) == history
    audit = repository.list_audit_events(project.id)
    assert [item.action for item in audit] == ["candidate.created", "candidate.confirmed"]
    assert audit[1].actor_kind == "unverified_reviewer_claim"
    assert audit[1].actor_ref == "analyst-1"
    assert audit[1].subject_id == observation.id
    assert ProjectRepository(repository.db_path).list_audit_events(project.id) == audit


def test_source_cannot_support_observation_for_another_project(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    first = repository.create_project("Company One")
    second = repository.create_project("Company Two")
    source = repository.add_source(
        first.id, kind="document", reference="synthetic.pdf", content_sha256="b" * 64
    )

    with pytest.raises(ValueError, match="another project"):
        repository.add_observation(
            second.id,
            source_id=source.id,
            subject="product",
            statement="Synthetic product statement",
            source_locator="p. 1",
        )


def test_numeric_observation_needs_period_and_unit(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    project = repository.create_project("Synthetic Company")
    source = repository.add_source(
        project.id, kind="document", reference="example.pdf", content_sha256="c" * 64
    )

    with pytest.raises(ValueError, match="period and unit"):
        repository.add_observation(
            project.id,
            source_id=source.id,
            subject="revenue",
            statement="Unscoped figure",
            number=Decimal("10"),
            source_locator="p. 2",
        )


def test_stale_or_duplicate_review_does_not_overwrite(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    project = repository.create_project("Synthetic Company")
    source = repository.add_source(
        project.id, kind="document", reference="example.pdf", content_sha256="d" * 64
    )
    observation = repository.add_observation(
        project.id,
        source_id=source.id,
        subject="business",
        statement="Synthetic business statement",
        source_locator="p. 4",
    )
    repository.confirm_observation(observation.id, reviewer="analyst-1", expected_version=1)

    with pytest.raises(ReviewConflictError):
        repository.confirm_observation(observation.id, reviewer="analyst-2", expected_version=1)
    assert len(repository.review_history(observation.id)) == 1


def test_missing_project_is_rejected(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(NotFoundError):
        repository.add_source(
            "missing", kind="document", reference="example.pdf", content_sha256="e" * 64
        )

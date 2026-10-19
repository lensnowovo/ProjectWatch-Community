"""Model-shaped executors cannot promote unsupported text into confirmed facts."""

from pathlib import Path

import pytest

from project_watch.domain import FragmentInput
from project_watch.executors import ExecutorUnavailable, ModelCandidateExecutor
from project_watch.repository import ProjectRepository


class FakeModel:
    def __init__(self, response: dict[str, str] | None) -> None:
        self.response = response
        self.received = None

    def propose(self, evidence):
        self.received = evidence
        if self.response is None:
            raise ExecutorUnavailable("synthetic model unavailable")
        return self.response


def _prepared(tmp_path: Path):
    repo = ProjectRepository(tmp_path / "watch.sqlite3")
    project = repo.create_project("Synthetic Executor")
    source = repo.add_source(
        project.id, kind="document", reference="sample.txt", content_sha256="a" * 64
    )
    repo.enqueue_intake(project.id, source.id)
    claimed = repo.claim_next_intake()
    repo.complete_intake(
        claimed.id,
        [FragmentInput(ordinal=1, locator="line 1", text="Synthetic revenue " * 200)],
    )
    return repo, project, source


def test_model_candidate_uses_bounded_evidence_and_stays_candidate(tmp_path: Path) -> None:
    repo, project, source = _prepared(tmp_path)
    model = FakeModel(
        {
            "source_id": source.id,
            "source_sha256": source.content_sha256,
            "source_locator": "line 1",
            "subject": "经营情况",
            "statement": "资料称收入变化，仍待人工核对。",
        }
    )

    observation = ModelCandidateExecutor(repo, model).propose(
        project.id, source_id=source.id, source_locator="line 1"
    )

    assert model.received.project_id == project.id
    assert model.received.text == ("Synthetic revenue " * 200)[:1500]
    assert not hasattr(model.received, "path")
    assert observation.status == "candidate"
    assert repo.list_observations(project.id, status="confirmed") == []


def test_model_unavailable_or_fabricated_locator_creates_nothing(tmp_path: Path) -> None:
    repo, project, source = _prepared(tmp_path)
    unavailable = ModelCandidateExecutor(repo, FakeModel(None))
    with pytest.raises(ExecutorUnavailable):
        unavailable.propose(project.id, source_id=source.id, source_locator="line 1")

    liar = FakeModel(
        {
            "source_id": source.id,
            "source_sha256": source.content_sha256,
            "source_locator": "invented page",
            "subject": "经营情况",
            "statement": "Unsupported claim",
        }
    )
    with pytest.raises(ValueError, match="evidence"):
        ModelCandidateExecutor(repo, liar).propose(
            project.id, source_id=source.id, source_locator="line 1"
        )
    assert repo.list_observations(project.id) == []


def test_model_shaped_numeric_proposal_is_only_a_candidate(tmp_path: Path) -> None:
    repo = ProjectRepository(tmp_path / "numeric.sqlite3")
    project = repo.create_project("Synthetic Numeric Model")
    source = repo.add_source(
        project.id, kind="document", reference="finance.txt", content_sha256="f" * 64
    )
    repo.enqueue_intake(project.id, source.id)
    claimed = repo.claim_next_intake()
    repo.complete_intake(
        claimed.id,
        [FragmentInput(ordinal=1, locator="line 1", text="2025 revenue 1,200.5 万元")],
    )
    model = FakeModel(
        {
            "source_id": source.id,
            "source_sha256": source.content_sha256,
            "source_locator": "line 1",
            "subject": "财务表现",
            "statement": "营业收入",
            "number": "1200.5",
            "unit": "万元",
            "period": "2025",
            "scope": "合并",
        }
    )
    result = ModelCandidateExecutor(repo, model).propose(
        project.id, source_id=source.id, source_locator="line 1"
    )
    assert result.status == "candidate"
    assert str(result.number) == "1200.5"
    assert result.scope == "合并"
    assert repo.list_observations(project.id, status="confirmed") == []

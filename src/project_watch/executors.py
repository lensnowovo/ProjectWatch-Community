"""Opt-in model boundary; only bounded excerpts may enter an executor."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from project_watch.domain import RESEARCH_TOPICS, Observation
from project_watch.evidence_proposals import TaskService
from project_watch.literal_metrics import validate_candidate_metric
from project_watch.repository import ProjectRepository


class ExecutorUnavailable(RuntimeError):
    """An optional model could not provide a candidate; no fact was created."""


@dataclass(frozen=True, slots=True)
class EvidencePacket:
    project_id: str
    source_id: str
    source_sha256: str
    source_locator: str
    text: str


class ProposalModel(Protocol):
    def propose(self, evidence: EvidencePacket) -> dict[str, str]: ...


class ModelCandidateExecutor:
    def __init__(self, repository: ProjectRepository, model: ProposalModel) -> None:
        self.repository = repository
        self.model = model
        self.tasks = TaskService(repository)

    def propose(self, project_id: str, *, source_id: str, source_locator: str) -> Observation:
        source = self.repository.get_source(source_id)
        if source is None or source.project_id != project_id:
            raise ValueError("source does not belong to the selected project")
        fragment = next(
            (
                fragment
                for fragment in self.repository.list_fragments(source_id)
                if fragment.locator == source_locator
            ),
            None,
        )
        if fragment is None:
            raise ValueError("evidence locator was not found")
        evidence = EvidencePacket(
            project_id=project_id,
            source_id=source.id,
            source_sha256=source.content_sha256,
            source_locator=fragment.locator,
            text=fragment.text[:1500],
        )
        draft = self.model.propose(evidence)
        if not isinstance(draft, dict) or any(
            draft.get(key) != value
            for key, value in (
                ("source_id", evidence.source_id),
                ("source_sha256", evidence.source_sha256),
                ("source_locator", evidence.source_locator),
            )
        ):
            raise ValueError("model proposal did not match selected evidence")
        subject = draft.get("subject")
        statement = draft.get("statement")
        if subject not in RESEARCH_TOPICS or not isinstance(statement, str):
            raise ValueError("model proposal has invalid topic or statement")
        for key in ("number", "unit", "period", "scope", "basis"):
            if draft.get(key) is not None and not isinstance(draft[key], str):
                raise ValueError("model proposal has invalid field types")
        validate_candidate_metric(
            number=draft.get("number"),
            unit=draft.get("unit"),
            period=draft.get("period"),
            scope=draft.get("scope"),
            statement=statement,
            fragment_text=evidence.text,
        )
        return self.tasks.propose_candidate(
            project_id,
            source_id=evidence.source_id,
            expected_source_sha256=evidence.source_sha256,
            source_locator=evidence.source_locator,
            subject=subject,
            statement=statement,
            number=draft.get("number"),
            unit=draft.get("unit"),
            period=draft.get("period"),
            scope=draft.get("scope"),
            basis=draft.get("basis", "unknown"),
        )

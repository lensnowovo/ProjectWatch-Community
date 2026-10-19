"""Evidence-bounded task service shared by native UI and future executor bridges."""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from project_watch.domain import RESEARCH_TOPICS, ExecutionTask, Observation
from project_watch.literal_metrics import validate_candidate_metric
from project_watch.repository import ProjectRepository


class EvidenceConflictError(RuntimeError):
    """The proposed evidence no longer matches the selected source version."""


def coverage_outcome(
    observations: Iterable[Observation], source_ids: tuple[str, ...]
) -> dict[str, object]:
    """Deterministic coverage result shared by both storage backends."""
    scoped = [
        item for item in observations if item.source_id in source_ids and item.status != "rejected"
    ]
    seen_topics = {item.subject for item in scoped}
    return {
        "source_count": len(source_ids),
        "candidate_count": sum(item.status == "candidate" for item in scoped),
        "confirmed_count": sum(item.status == "confirmed" for item in scoped),
        "covered_topics": [topic for topic in RESEARCH_TOPICS if topic in seen_topics],
        "missing_topics": [topic for topic in RESEARCH_TOPICS if topic not in seen_topics],
        "note": "Coverage reflects recorded observations only; unrecorded material is not a conclusion.",
    }


class TaskService:
    def __init__(self, repository: ProjectRepository) -> None:
        self.repository = repository

    def run_coverage(
        self, project_id: str, *, source_ids: tuple[str, ...], idempotency_key: str
    ) -> ExecutionTask:
        if len(source_ids) > 100 or len(set(source_ids)) != len(source_ids):
            raise ValueError("coverage task accepts at most 100 distinct source IDs")
        outcome = coverage_outcome(self.repository.list_observations(project_id), source_ids)
        return self.repository.save_coverage_task(
            project_id,
            source_ids=source_ids,
            idempotency_key=idempotency_key,
            outcome=outcome,
        )

    def propose_candidate(
        self,
        project_id: str,
        *,
        source_id: str,
        expected_source_sha256: str,
        source_locator: str,
        subject: str,
        statement: str,
        number: Decimal | str | None = None,
        unit: str | None = None,
        period: str | None = None,
        scope: str | None = None,
        basis: str = "unknown",
        submission_channel: str = "http",
        actor_id: str | None = None,
    ) -> Observation:
        source = self.repository.get_source(source_id)
        if source is None or source.project_id != project_id:
            raise ValueError("source belongs to another project or does not exist")
        if source.content_sha256 != expected_source_sha256.lower():
            raise EvidenceConflictError("source version changed")
        if source.kind != "document":
            raise ValueError("only extracted documents support executor proposals")
        fragment = next(
            (
                item
                for item in self.repository.list_fragments(source_id)
                if item.locator == source_locator
            ),
            None,
        )
        if fragment is None:
            raise ValueError("source locator is not in the extracted document")
        number, unit, period, scope = validate_candidate_metric(
            number=number,
            unit=unit,
            period=period,
            scope=scope,
            statement=statement,
            fragment_text=fragment.text,
        )
        return self.repository.add_observation(
            project_id,
            source_id=source_id,
            subject=subject,
            statement=statement,
            source_locator=source_locator,
            number=number,
            unit=unit,
            period=period,
            scope=scope,
            basis=basis,
            submission_channel=submission_channel,
            actor_id=actor_id,
        )

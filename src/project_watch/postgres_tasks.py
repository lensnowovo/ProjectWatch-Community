"""Project-scoped deterministic tasks for the PostgreSQL team repository."""

from __future__ import annotations

from decimal import Decimal

from project_watch.candidate_metrics import validate_candidate_metric
from project_watch.domain import ExecutionTask, Observation
from project_watch.postgres_core import PostgresCoreRepository
from project_watch.tasks import EvidenceConflictError, coverage_outcome


class PostgresTaskService:
    def __init__(self, repository: PostgresCoreRepository) -> None:
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
        verified_actor_id: str,
        number: Decimal | str | None = None,
        unit: str | None = None,
        period: str | None = None,
        scope: str | None = None,
        basis: str = "unknown",
        submission_channel: str = "team_web",
    ) -> Observation:
        source = self.repository.get_source(project_id, source_id)
        if source is None:
            raise ValueError("source belongs to another project or does not exist")
        if source.content_sha256 != expected_source_sha256.lower():
            raise EvidenceConflictError("source version changed")
        if source.kind != "document":
            raise ValueError("only extracted documents support executor proposals")
        fragment = next(
            (
                item
                for item in self.repository.list_fragments(project_id, source_id)
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
            verified_actor_id=verified_actor_id,
            submission_channel=submission_channel,
        )

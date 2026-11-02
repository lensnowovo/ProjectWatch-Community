"""Project-bound stdio MCP bridge; the executor never receives a database path."""

from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path

from project_watch.api import MetricSeriesResponse
from project_watch.evidence_pages import page_excerpts
from project_watch.metric_series import build_metric_series
from project_watch.repository import NotFoundError, ProjectRepository
from project_watch.tasks import EvidenceConflictError, TaskService


def build_mcp_server(
    db_path: Path | str,
    project_id: str,
):
    try:
        from mcp.server import MCPServer
        from mcp.server.mcpserver.exceptions import ToolError
    except ImportError as exc:
        raise RuntimeError('Install the optional "mcp" extra to run the MCP bridge') from exc

    repository = ProjectRepository(db_path)
    project = repository.get_project(project_id)
    if project is None:
        raise ValueError("bound project does not exist")
    tasks = TaskService(repository)
    server = MCPServer(
        "Project Watch",
        instructions=(
            "This server is bound to one project. Read evidence before proposing a candidate. "
            "Candidate observations are not confirmed institutional facts."
        ),
    )

    @server.tool()
    def project_overview() -> dict[str, object]:
        """Show this project's identity status, source versions and review counts."""
        sources = repository.list_sources(project_id)
        observations = repository.list_observations(project_id)
        return {
            "project_id": project.id,
            "name": project.name,
            "identity_status": project.identity_status,
            "source_count": len(sources),
            "sources": [
                {
                    "id": source.id,
                    "reference": source.reference,
                    "content_sha256": source.content_sha256,
                }
                for source in sources[:50]
            ],
            "sources_truncated": len(sources) > 50,
            "candidate_count": sum(item.status == "candidate" for item in observations),
            "confirmed_count": sum(item.status == "confirmed" for item in observations),
        }

    @server.tool()
    def source_excerpts(
        source_id: str, limit: int = 8, cursor: str | None = None
    ) -> dict[str, object]:
        """Read a bounded page of located excerpts from one source."""
        source = repository.get_source(source_id)
        if source is None or source.project_id != project_id:
            raise ToolError("source not found in bound project")
        fragments = repository.list_fragments(source_id)
        try:
            page = page_excerpts(
                fragments, source_sha256=source.content_sha256, cursor=cursor, limit=limit
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "source_id": source.id,
            "content_sha256": source.content_sha256,
            **page,
        }

    @server.tool()
    def monitoring_status(limit: int = 20, include_metrics: bool = False) -> dict[str, object]:
        """Read bounded source receipts and candidate signals; never run a source."""
        if not 1 <= limit <= 20:
            raise ToolError("limit must be between 1 and 20")
        plans = repository.list_monitoring_plans(project_id)
        signals = repository.list_signals(project_id)
        result = {
            "plans": [
                {
                    "id": plan.id,
                    "kind": plan.kind,
                    "enabled": plan.enabled,
                    "last_run_status": plan.last_run_status,
                }
                for plan in plans[:20]
            ],
            "plans_truncated": len(plans) > 20,
            "signals": [
                {
                    "id": signal.id,
                    "title": signal.title,
                    "url": signal.url,
                    "freshness": signal.freshness,
                    "status": signal.status,
                    "first_seen_at": signal.first_seen_at.isoformat(),
                }
                for signal in signals[:limit]
            ],
            "signals_truncated": len(signals) > limit,
        }
        if include_metrics:
            result["metrics"] = MetricSeriesResponse.model_validate(
                build_metric_series(
                    repository.list_observations(project_id, status="confirmed"),
                    repository.list_sources(project_id),
                    points_per_series=4,
                )
            ).model_dump(mode="json")
        return result

    def run_coverage(source_ids: list[str], idempotency_key: str) -> dict[str, object]:
        """Record deterministic coverage and gaps without inventing project facts."""
        try:
            task = tasks.run_coverage(
                project_id, source_ids=tuple(source_ids), idempotency_key=idempotency_key
            )
        except (ValueError, NotFoundError) as exc:
            raise ToolError(str(exc)) from exc
        return {**asdict(task), "created_at": task.created_at.isoformat()}

    server.tool()(run_coverage)

    @server.tool()
    def propose_candidate(
        source_id: str,
        expected_source_sha256: str,
        source_locator: str,
        subject: str,
        statement: str,
        number: str | None = None,
        unit: str | None = None,
        period: str | None = None,
        scope: str | None = None,
        basis: str = "unknown",
    ) -> dict[str, object]:
        """Propose an evidence-linked candidate for human review; never confirm it.

        Copy source_id, expected_source_sha256 and source_locator from source_excerpts.
        For a numeric metric, statement is its short name (at most 80 characters),
        number is an exact decimal string copied literally from the excerpt (do not
        rescale it), unit preserves the original scale (e.g. 'million yuan'), and
        period is YYYY, YYYY-Qn or YYYY-MM. Supply number/unit/period together.
        basis must be actual, forecast, budget or unknown; leave unknown when unclear.
        A qualitative candidate omits number/unit/period. Candidates remain unconfirmed.
        """
        try:
            observation = tasks.propose_candidate(
                project_id,
                source_id=source_id,
                expected_source_sha256=expected_source_sha256,
                source_locator=source_locator,
                subject=subject,
                statement=statement,
                number=number,
                unit=unit,
                period=period,
                scope=scope,
                basis=basis,
                submission_channel="mcp",
                actor_id=None,
            )
        except (ValueError, NotFoundError, EvidenceConflictError) as exc:
            raise ToolError(str(exc)) from exc
        return {
            "id": observation.id,
            "project_id": observation.project_id,
            "source_id": observation.source_id,
            "source_locator": observation.source_locator,
            "status": observation.status,
            "version": observation.version,
            "number": str(observation.number) if observation.number is not None else None,
            "unit": observation.unit,
            "period": observation.period,
            "scope": observation.scope,
            "basis": observation.basis,
        }

    return server


def main() -> None:
    db_path = os.environ.get("PROJECT_WATCH_DB_PATH")
    project_id = os.environ.get("PROJECT_WATCH_PROJECT_ID")
    if not db_path or not project_id:
        raise RuntimeError("PROJECT_WATCH_DB_PATH and PROJECT_WATCH_PROJECT_ID are required")
    build_mcp_server(Path(db_path), project_id).run(transport="stdio")


if __name__ == "__main__":
    main()

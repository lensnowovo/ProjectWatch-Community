"""A source-bound record timeline, not an inferred business event feed."""

from __future__ import annotations

from typing import Any

from project_watch.domain import Observation, SourceVersion

ACTIVITY_SUBJECTS = frozenset({"经营情况", "产品与技术", "风险与合规"})
MAX_ENTRIES = 12
MAX_EXCERPT = 240


def project_activity(
    observations: list[Observation], sources: dict[str, SourceVersion]
) -> dict[str, Any]:
    """Separate reviewed records from candidates without inventing change dates."""
    relevant = [
        row
        for row in observations
        if row.subject in ACTIVITY_SUBJECTS and row.source_id in sources and row.source_locator
    ]
    confirmed = sorted(
        (row for row in relevant if row.status == "confirmed"),
        key=lambda row: (row.created_at, row.id),
        reverse=True,
    )
    entries = []
    for row in confirmed[:MAX_ENTRIES]:
        statement = row.statement.strip()
        entries.append(
            {
                "observation_id": row.id,
                "subject": row.subject,
                "statement_excerpt": statement[:MAX_EXCERPT]
                + ("…" if len(statement) > MAX_EXCERPT else ""),
                "statement_truncated": len(statement) > MAX_EXCERPT,
                "number": str(row.number) if row.number is not None else None,
                "unit": row.unit,
                "basis": row.basis,
                "period": row.period,
                "scope": row.scope,
                "recorded_at": row.created_at.isoformat(),
                "source_id": row.source_id,
                "source_sha256": sources[row.source_id].content_sha256,
                "source_locator": row.source_locator,
            }
        )
    return {
        "confirmed_count": len(confirmed),
        "pending_count": sum(row.status == "candidate" for row in relevant),
        "entries": entries,
        "entries_truncated": len(confirmed) > len(entries),
        "empty_note": "尚无经审核的经营、产品或风险记录；这不代表企业没有变化或风险。",
    }


__all__ = ["project_activity"]

"""Evidence-bound, non-ranking comparison of a reviewed competitor set.

The caller supplies only approved peer IDs and project-authorized source metadata.
This pure read model never confirms a candidate or infers that unlike metrics rank.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from project_watch.domain import MeasurementBasis, Observation, SourceVersion


@dataclass(frozen=True, slots=True)
class CompetitiveMetric:
    id: str
    entity_id: str
    name: str
    value: Decimal
    unit: str
    period: str
    scope: str
    basis: MeasurementBasis
    source_id: str
    source_sha256: str
    source_locator: str
    review_status: Literal["candidate", "confirmed", "rejected"]


def _key(row: CompetitiveMetric) -> tuple[str, str, str, str, str]:
    return (" ".join(row.name.split()), row.period, row.unit, row.scope, row.basis)


def _has_source(row: CompetitiveMetric) -> bool:
    return bool(
        row.source_id
        and row.source_locator.strip()
        and re.fullmatch(r"[0-9a-f]{64}", row.source_sha256)
    )


def _usable_number(row: CompetitiveMetric) -> bool:
    return (
        isinstance(row.value, Decimal)
        and row.value.is_finite()
        and bool(row.name.strip())
        and bool(row.period.strip())
        and bool(row.unit.strip())
        and bool(row.scope.strip())
        and row.basis in {"actual", "forecast", "budget"}
    )


def _point(row: CompetitiveMetric) -> dict[str, str]:
    return {
        "entity_id": row.entity_id,
        "value": str(row.value),
        "source_id": row.source_id,
        "source_sha256": row.source_sha256,
        "source_locator": row.source_locator,
        "metric_id": row.id,
    }


def project_competitive_metrics(
    entity_id: str,
    project_id: str,
    observations: list[Observation],
    sources: list[SourceVersion],
) -> list[CompetitiveMetric]:
    """Bind project-scoped observations to their exact stored source versions."""
    source_by_id = {(source.project_id, source.id): source for source in sources}
    result: list[CompetitiveMetric] = []
    for row in observations:
        if row.project_id != project_id or row.number is None:
            continue
        source = source_by_id.get((project_id, row.source_id))
        if source is None:
            continue
        result.append(
            CompetitiveMetric(
                id=row.id,
                entity_id=entity_id,
                name=row.statement,
                value=row.number,
                unit=row.unit or "",
                period=row.period or "",
                scope=row.scope or "",
                basis=row.basis,
                source_id=source.id,
                source_sha256=source.content_sha256,
                source_locator=row.source_locator,
                review_status=row.status,
            )
        )
    return result


def compare_competitive_metrics(
    subject_id: str,
    approved_peer_ids: list[str],
    metrics: list[CompetitiveMetric],
) -> dict[str, object]:
    """Compare like-for-like reviewed numbers; never calculate a rank from sparse peers."""
    peers = list(dict.fromkeys(peer for peer in approved_peer_ids if peer and peer != subject_id))
    if not peers:
        return {"status": "missing_peer_set", "comparisons": [], "gaps": [], "rank": None}

    allowed = {subject_id, *peers}
    rows = [row for row in metrics if row.entity_id in allowed and _usable_number(row)]
    subject_keys = sorted({_key(row) for row in rows if row.entity_id == subject_id})
    if not subject_keys:
        return {
            "status": "insufficient_evidence",
            "comparisons": [],
            "gaps": [{"entity_id": subject_id, "reason": "missing_subject_metric"}],
            "rank": None,
        }

    comparisons: list[dict[str, object]] = []
    gaps: list[dict[str, str]] = []
    for key in subject_keys:
        name, period, unit, scope, basis = key
        basis_label = {
            "name": name,
            "period": period,
            "unit": unit,
            "scope": scope,
            "basis": basis,
        }
        own_rows = [
            row
            for row in rows
            if row.entity_id == subject_id and _key(row) == key and row.review_status == "confirmed"
        ]
        valid_own = [row for row in own_rows if _has_source(row)]
        if not valid_own or len({row.value for row in valid_own}) > 1:
            reason = (
                "conflicting_subject_values"
                if len({row.value for row in valid_own}) > 1
                else "missing_subject_evidence"
            )
            gaps.extend({**basis_label, "entity_id": peer, "reason": reason} for peer in peers)
            continue

        selected_own = min(valid_own, key=lambda row: row.id)
        comparable_peers: list[dict[str, str]] = []
        for peer in peers:
            peer_rows = [row for row in rows if row.entity_id == peer]
            exact = [row for row in peer_rows if _key(row) == key]
            confirmed = [row for row in exact if row.review_status == "confirmed"]
            valid = [row for row in confirmed if _has_source(row)]
            if len({row.value for row in valid}) > 1:
                reason = "conflicting_values"
            elif valid:
                comparable_peers.append(_point(min(valid, key=lambda row: row.id)))
                continue
            elif confirmed:
                reason = "missing_source"
            elif any(row.review_status == "candidate" for row in exact):
                reason = "pending_review"
            elif peer_rows:
                reason = (
                    "different_basis"
                    if any(" ".join(row.name.split()) == name for row in peer_rows)
                    else "different_metric"
                )
            else:
                reason = "missing_metric"
            gaps.append({**basis_label, "entity_id": peer, "reason": reason})

        if comparable_peers:
            comparisons.append(
                {
                    **basis_label,
                    "subject": _point(selected_own),
                    "peers": comparable_peers,
                }
            )

    return {
        "status": "comparable" if comparisons else "insufficient_evidence",
        "comparisons": comparisons,
        "gaps": gaps,
        "rank": None,
    }


__all__ = ["CompetitiveMetric", "compare_competitive_metrics", "project_competitive_metrics"]

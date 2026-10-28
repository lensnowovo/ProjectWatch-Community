"""Bounded, evidence-linked display projection of confirmed numeric observations.

The narrow latest-year change compares only adjacent, reviewed annual actual
financial values. It never reconciles conflicting values or creates a fact.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext

from project_watch.domain import Observation, SourceVersion
from project_watch.financial_vocabulary import FINANCIAL_METRIC_NAMES

_FINANCIAL_UNITS = frozenset({"元", "万元", "亿元"})
_CONSOLIDATED_SCOPES = frozenset({"合并", "合并口径"})


def _latest_annual_change(
    rows: list[tuple[tuple[int, int], Observation, SourceVersion]],
    *,
    subject: str,
    name: str,
    unit: str,
    scope: str,
    basis: str,
    cadence: str,
) -> dict[str, object]:
    """Only adjacent annual actual financial periods may produce a derived number."""
    if basis != "actual":
        return {"status": "non_actual"}
    if (
        subject not in {"财务表现", "financials"}
        or name not in FINANCIAL_METRIC_NAMES
        or unit not in _FINANCIAL_UNITS
        or scope not in _CONSOLIDATED_SCOPES
        or cadence != "year"
    ):
        return {"status": "unsupported_basis"}
    periods: dict[int, list[tuple[Observation, SourceVersion]]] = {}
    for (year, _), observation, source in rows:
        periods.setdefault(year, []).append((observation, source))
    latest_year = max(periods)
    previous_year = latest_year - 1
    if previous_year not in periods:
        return {"status": "missing_previous"}
    if len(periods[latest_year]) != 1 or len(periods[previous_year]) != 1:
        return {"status": "same_period_conflict"}
    current, current_source = periods[latest_year][0]
    previous, previous_source = periods[previous_year][0]
    if any(
        not row.source_locator
        or len(source.content_sha256) != 64
        or any(char not in "0123456789abcdef" for char in source.content_sha256)
        for row, source in ((current, current_source), (previous, previous_source))
    ):
        return {"status": "source_unavailable"}
    assert current.number is not None and previous.number is not None
    if previous.number <= 0:
        return {"status": "nonpositive_baseline"}
    if any(
        value and not -12 <= value.adjusted() <= 18 for value in (current.number, previous.number)
    ):
        return {"status": "uncomputable"}
    try:
        with localcontext() as context:
            context.prec = 64
            absolute = current.number - previous.number
            percent = ((absolute / previous.number) * 100).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
    except InvalidOperation:
        return {"status": "uncomputable"}
    return {
        "status": "comparable",
        "previous_period": previous.period,
        "period": current.period,
        "absolute": absolute,
        "percent": percent,
        "previous_observation_id": previous.id,
        "observation_id": current.id,
    }


def _period_key(period: str | None) -> tuple[str, tuple[int, int]] | None:
    if period is None:
        return None
    if match := re.fullmatch(r"(\d{4})", period):
        return "year", (int(match.group(1)), 0)
    if match := re.fullmatch(r"(\d{4})-Q([1-4])", period):
        return "quarter", (int(match.group(1)), int(match.group(2)))
    if match := re.fullmatch(r"(\d{4})-(0[1-9]|1[0-2])", period):
        return "month", (int(match.group(1)), int(match.group(2)))
    return None


def build_metric_series(
    observations: Iterable[Observation],
    sources: Iterable[SourceVersion],
    *,
    limit: int = 6,
    points_per_series: int = 12,
) -> dict[str, object]:
    """Group only comparable confirmed values; retain same-period disagreement."""
    if not 1 <= limit <= 50 or not 1 <= points_per_series <= 100:
        raise ValueError("metric series bounds must be positive and limited")
    source_by_id = {(source.project_id, source.id): source for source in sources}
    groups: dict[
        tuple[str, str, str, str, str, str, str],
        list[tuple[tuple[int, int], Observation, SourceVersion]],
    ] = {}
    unsequenced_count = 0
    for observation in observations:
        if observation.status != "confirmed" or observation.number is None:
            continue
        source = source_by_id.get((observation.project_id, observation.source_id))
        parsed = _period_key(observation.period)
        if (
            source is None
            or parsed is None
            or not isinstance(observation.number, Decimal)
            or not observation.number.is_finite()
            or not observation.unit
            or not observation.statement.strip()
        ):
            unsequenced_count += 1
            continue
        cadence, order = parsed
        key = (
            observation.project_id,
            observation.subject,
            " ".join(observation.statement.split()),
            observation.unit,
            observation.scope or "",
            observation.basis,
            cadence,
        )
        groups.setdefault(key, []).append((order, observation, source))

    ordered_groups = sorted(
        groups.items(),
        key=lambda item: (
            -max(row[0][0] * 100 + row[0][1] for row in item[1]),
            item[0],
        ),
    )
    series: list[dict[str, object]] = []
    for (_, subject, name, unit, scope, basis, cadence), rows in ordered_groups[:limit]:
        rows.sort(key=lambda row: (row[0], row[1].created_at, row[1].id))
        period_counts = Counter(row[1].period for row in rows)
        points: list[dict[str, object]] = []
        for _, observation, source in rows[-points_per_series:]:
            points.append(
                {
                    "observation_id": observation.id,
                    "observation_version": observation.version,
                    "period": observation.period,
                    "number": observation.number,
                    "source_id": source.id,
                    "source_reference": source.reference,
                    "source_sha256": source.content_sha256,
                    "source_locator": observation.source_locator,
                    "same_period_conflict": period_counts[observation.period] > 1,
                }
            )
        series.append(
            {
                "subject": subject,
                "name": name,
                "unit": unit,
                "scope": scope or None,
                "basis": basis,
                "cadence": cadence,
                "total_points": len(rows),
                "truncated_points": len(rows) > points_per_series,
                "points": points,
                "latest_change": _latest_annual_change(
                    rows,
                    subject=subject,
                    name=name,
                    unit=unit,
                    scope=scope,
                    basis=basis,
                    cadence=cadence,
                ),
            }
        )
    return {
        "series": series,
        "total_series": len(groups),
        "truncated": len(groups) > limit,
        "unsequenced_count": unsequenced_count,
    }

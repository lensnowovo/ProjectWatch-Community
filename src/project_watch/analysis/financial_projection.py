"""Feed only comparable, confirmed financial facts into the DB parity screen.

The source-bound observations remain authoritative. Screening is a derived,
rebuildable view; it never changes a review decision or the original value.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from project_watch.analysis.financial_integrity import (
    ALIASES,
    build_financial_integrity_analysis,
)
from project_watch.domain import Observation, SourceVersion
from project_watch.financial_vocabulary import CHINESE_FINANCIAL_NAMES as _CHINESE_NAMES
from project_watch.financial_vocabulary import FINANCIAL_METRIC_NAMES

_UNIT_TO_WAN = {"元": Decimal("0.0001"), "万元": Decimal("1"), "亿元": Decimal("10000")}
_ALIASES = {alias: canonical for canonical, names in ALIASES.items() for alias in names}


@dataclass(frozen=True, slots=True)
class _FinancialRow:
    kpi_name: str
    period: str
    value: Decimal
    unit: str = "万元"
    as_of_date: None = None


def _canonical_name(statement: str) -> str | None:
    cleaned = " ".join(statement.strip().split())
    if cleaned in _CHINESE_NAMES:
        return _CHINESE_NAMES[cleaned]
    return _ALIASES.get(cleaned.lower())


def screen_confirmed_financials(
    observations: list[Observation], sources: list[SourceVersion]
) -> dict[str, Any]:
    """Screen annual consolidated RMB actuals; quarantine anything ambiguous.

    The DB engine expects comparable normalized values. Unsupported periods,
    units and scopes stay outside it instead of being silently coerced.
    """

    source_by_id = {(source.project_id, source.id): source for source in sources}
    excluded: Counter[str] = Counter()
    eligible: dict[tuple[str, str], list[tuple[_FinancialRow, Observation, SourceVersion]]] = (
        defaultdict(list)
    )
    for observation in observations:
        if observation.status != "confirmed":
            excluded["unreviewed"] += 1
            continue
        if observation.basis != "actual":
            excluded["non_actual"] += 1
            continue
        source = source_by_id.get((observation.project_id, observation.source_id))
        if source is None or not observation.source_locator:
            excluded["source_missing"] += 1
            continue
        if observation.subject not in {"财务表现", "financials"}:
            excluded["non_financial"] += 1
            continue
        if observation.scope not in {"合并", "合并口径"}:
            excluded["scope_mismatch"] += 1
            continue
        if not observation.period or re.fullmatch(r"\d{4}", observation.period) is None:
            excluded["period_unmapped"] += 1
            continue
        if observation.unit not in _UNIT_TO_WAN:
            excluded["unit_unmapped"] += 1
            continue
        name = _canonical_name(observation.statement)
        if name is None:
            excluded["metric_unmapped"] += 1
            continue
        if observation.number is None or not observation.number.is_finite():
            excluded["number_invalid"] += 1
            continue
        row = _FinancialRow(
            kpi_name=name,
            period=observation.period,
            value=observation.number * _UNIT_TO_WAN[observation.unit],
        )
        eligible[(row.period, row.kpi_name)].append((row, observation, source))

    rows: list[_FinancialRow] = []
    evidence: list[dict[str, str]] = []
    for key in sorted(eligible):
        matches = eligible[key]
        if len(matches) > 1:
            excluded["same_period_conflict"] += len(matches)
            continue
        row, observation, source = matches[0]
        rows.append(row)
        evidence.append(
            {
                "metric": row.kpi_name,
                "period": row.period,
                "observation_id": observation.id,
                "source_id": source.id,
                "source_sha256": source.content_sha256,
                "source_locator": observation.source_locator,
                "normalized_unit": "万元",
                "original_unit": observation.unit,
            }
        )

    return {
        "analysis": build_financial_integrity_analysis(rows),
        "accepted_count": len(rows),
        "excluded": dict(sorted(excluded.items())),
        "evidence": evidence,
    }


__all__ = ["FINANCIAL_METRIC_NAMES", "screen_confirmed_financials"]

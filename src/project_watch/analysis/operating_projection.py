"""Source-bound operating indicators; no inferred capacity or utilization."""

from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal
from typing import Any

from project_watch.domain import Observation, SourceVersion

_PERIOD = re.compile(r"\d{4}(?:-Q[1-4]|-(?:0[1-9]|1[0-2]))?\Z")
_SUBJECTS = frozenset({"经营情况", "产品与技术"})
_METRICS = frozenset({"设计产能", "实际产量", "产能利用率"})
_BASES = frozenset({"actual", "forecast", "budget"})
_MONEY_UNITS = frozenset({"元", "千元", "万元", "亿元", "美元", "万美元", "人民币"})


def _usable(row: Observation) -> bool:
    if row.status != "confirmed" or row.basis not in _BASES:
        return False
    if row.statement == "实际产量" and row.basis != "actual":
        return False
    if (
        not row.period
        or not _PERIOD.fullmatch(row.period)
        or not row.scope
        or not row.scope.strip()
    ):
        return False
    if not row.unit or not row.unit.strip():
        return False
    value = row.number
    if value is None or not value.is_finite() or value < Decimal(0):
        return False
    if row.statement == "产能利用率":
        return row.unit == "%" and value <= Decimal(100)
    return row.unit != "%" and row.unit not in _MONEY_UNITS


def screen_operating_metrics(
    observations: list[Observation], sources: dict[str, SourceVersion]
) -> dict[str, Any]:
    """Separate actuals from plans and quarantine conflicting same-period values."""
    scoped = [
        row
        for row in observations
        if row.subject in _SUBJECTS
        and row.statement in _METRICS
        and row.source_id in sources
        and row.source_locator
    ]
    grouped: dict[tuple[str, str, str, str], list[Observation]] = defaultdict(list)
    pending = 0
    for row in scoped:
        if _usable(row):
            grouped[(row.statement, row.period or "", row.scope or "", row.basis)].append(row)
        else:
            pending += 1
    accepted: list[Observation] = []
    conflicts = 0
    for rows in grouped.values():
        if len(rows) == 1:
            accepted.extend(rows)
        else:
            conflicts += len(rows)
            pending += len(rows)
    accepted.sort(
        key=lambda row: (row.period or "", row.statement, row.scope or "", row.id), reverse=True
    )
    actual_count = sum(row.basis == "actual" for row in accepted)
    planned_count = len(accepted) - actual_count
    evidence = [
        {
            "observation_id": row.id,
            "metric": row.statement,
            "value": str(row.number),
            "unit": row.unit,
            "period": row.period,
            "scope": row.scope,
            "basis": row.basis,
            "source_id": row.source_id,
            "source_sha256": sources[row.source_id].content_sha256,
            "source_locator": row.source_locator,
        }
        for row in accepted[:12]
    ]
    if actual_count:
        note = "已登记可定位的实际经营指标；不同产品、期间和单位不合并，预测另列。"
    elif planned_count:
        note = "只有预测或预算口径，不等于实际经营基线。"
    elif pending:
        note = "有经营指标记录，但尚需核对范围、期间、单位、口径或冲突值。"
    else:
        note = "尚无可核对的产能、产量或利用率记录。"
    return {
        "status": "evidence_available"
        if actual_count
        else "needs_review"
        if planned_count or pending
        else "missing",
        "actual_count": actual_count,
        "planned_count": planned_count,
        "pending_count": pending,
        "conflict_count": conflicts,
        "note": note,
        "evidence": evidence,
        "evidence_truncated": len(accepted) > len(evidence),
    }


__all__ = ["screen_operating_metrics"]

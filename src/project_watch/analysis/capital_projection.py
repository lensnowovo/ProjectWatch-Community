"""Conservative capital-record screen over reviewed, source-bound observations.

This is not a cap table. A company financing amount never implies that the
institution owns shares, and conflicting same-period values stay unresolved.
"""

from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal
from typing import Any

from project_watch.domain import Observation, SourceVersion

_PERIOD = re.compile(r"\d{4}(?:-Q[1-4]|-(?:0[1-9]|1[0-2]))?\Z")
_MONEY_UNITS = frozenset({"元", "万元", "亿元"})
_METRICS = {
    "本机构投资金额": ("本机构", _MONEY_UNITS),
    "本机构持股比例": ("本机构", frozenset({"%"})),
    "公司融资额": ("项目公司", _MONEY_UNITS),
    "投后估值": ("项目公司", _MONEY_UNITS),
}


def _usable(row: Observation) -> bool:
    specification = _METRICS.get(row.statement)
    if specification is None or row.status != "confirmed" or row.basis != "actual":
        return False
    scope, units = specification
    if (
        row.scope != scope
        or row.unit not in units
        or not row.period
        or not _PERIOD.fullmatch(row.period)
    ):
        return False
    value = row.number
    if value is None or not value.is_finite() or value <= Decimal(0):
        return False
    return row.statement != "本机构持股比例" or value <= Decimal(100)


def screen_capital_records(
    observations: list[Observation], sources: dict[str, SourceVersion]
) -> dict[str, Any]:
    """Expose only scoped actuals; keep every unqualified/conflicting item pending."""
    scoped = [
        row
        for row in observations
        if row.subject == "资本与持仓" and row.source_id in sources and row.source_locator
    ]
    grouped: dict[tuple[str, str, str], list[Observation]] = defaultdict(list)
    unresolved = 0
    for row in scoped:
        if _usable(row):
            grouped[(row.statement, row.period or "", row.scope or "")].append(row)
        else:
            unresolved += 1
    accepted: list[Observation] = []
    for rows in grouped.values():
        if len(rows) == 1:
            accepted.extend(rows)
        else:
            unresolved += len(rows)
    accepted.sort(key=lambda row: (row.period or "", row.statement, row.id), reverse=True)
    evidence = [
        {
            "observation_id": row.id,
            "metric": row.statement,
            "value": str(row.number),
            "unit": row.unit,
            "period": row.period,
            "scope": row.scope,
            "source_id": row.source_id,
            "source_sha256": sources[row.source_id].content_sha256,
            "source_locator": row.source_locator,
        }
        for row in accepted[:12]
    ]
    if accepted:
        note = (
            "已登记本机构资本指标；投资金额、持股比例及估值仍需逐项核对来源。"
            if any(row.scope == "本机构" for row in accepted)
            else "已登记公司融资信息，不等于本机构持仓；请核对投资金额和持股比例。"
        )
    elif unresolved:
        note = "有资本相关记录，但口径或数值尚不足以形成可用指标。"
    else:
        note = "尚未建立可核对的融资或持仓记录。"
    return {
        "status": "evidence_available" if accepted else "needs_review" if unresolved else "missing",
        "evidence_count": len(accepted),
        "pending_count": unresolved,
        "note": note,
        "evidence": evidence,
        "evidence_truncated": len(accepted) > len(evidence),
    }


__all__ = ["screen_capital_records"]

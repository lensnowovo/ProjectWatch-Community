"""Per-fund holding positions from reviewed snapshots, with no blind aggregation."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from project_watch.domain import HoldingSnapshot, SourceVersion


def screen_holding_snapshots(
    project_id: str,
    snapshots: list[HoldingSnapshot],
    sources: list[SourceVersion],
    *,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Choose only the newest uncontested reviewed date for each fund."""
    source_by_id = {row.id: row for row in sources if row.project_id == project_id}
    own = [
        row
        for row in snapshots
        if row.project_id == project_id and row.source_id in source_by_id and row.source_locator
    ]
    pending = sum(row.status == "candidate" for row in own)
    grouped: dict[str, list[HoldingSnapshot]] = defaultdict(list)
    for row in own:
        if row.status == "confirmed":
            grouped[row.fund_name].append(row)
    positions: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for fund_name, rows in sorted(grouped.items()):
        latest = max(row.as_of for row in rows)
        current = [row for row in rows if row.as_of == latest]
        if len(current) != 1:
            conflicts.append(
                {
                    "fund_name": fund_name,
                    "as_of": latest.isoformat(),
                    "snapshot_ids": sorted(row.id for row in current),
                }
            )
            continue
        row = current[0]
        cost, value = row.invested_cost_wan, row.current_value_wan
        moc = value / cost if cost is not None and value is not None and cost > 0 else None
        new_material_count = sum(
            source.kind == "document"
            and source.id != row.source_id
            and source.created_at > row.updated_at
            and (as_of is None or source.created_at <= as_of)
            for source in source_by_id.values()
        )
        positions.append(
            {
                "snapshot_id": row.id,
                "fund_name": fund_name,
                "as_of": latest.isoformat(),
                "invested_cost_wan": str(cost) if cost is not None else None,
                "ownership_pct": str(row.ownership_pct) if row.ownership_pct is not None else None,
                "current_value_wan": str(value) if value is not None else None,
                "moc": str(moc.normalize()) if moc is not None else None,
                "new_material_count_since_review": new_material_count,
                "source_id": row.source_id,
                "source_sha256": source_by_id[row.source_id].content_sha256,
                "source_locator": row.source_locator,
                "reviewed_by": row.reviewed_by,
                "version": row.version,
            }
        )
    positions.sort(key=lambda item: (item["as_of"], item["fund_name"]), reverse=True)
    return {
        "status": (
            "needs_review"
            if conflicts
            else "evidence_available"
            if positions
            else "needs_review"
            if pending
            else "missing"
        ),
        "positions": positions,
        "conflicts": conflicts,
        "pending_count": pending + sum(len(row["snapshot_ids"]) for row in conflicts),
        "note": (
            "同基金最新时点存在多条已确认快照，需复核后才能展示持仓。"
            if conflicts
            else "逐基金展示已确认持仓，不跨基金汇总。"
            if positions
            else "有待审核的持仓快照。"
            if pending
            else "未建立本机构持仓；项目公司的融资记录不等于我方持仓。"
        ),
    }


__all__ = ["screen_holding_snapshots"]

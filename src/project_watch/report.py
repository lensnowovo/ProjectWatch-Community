"""Evidence-bound, printable internal monitoring brief projection.

This read model never writes a business fact or treats an external lead as one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlsplit

from project_watch.analysis.holding_projection import screen_holding_snapshots
from project_watch.analysis.monitoring_projection import build_monitoring_projection
from project_watch.domain import (
    HoldingSnapshot,
    MonitoringPlan,
    Observation,
    Project,
    ReviewDecision,
    ReviewImpact,
    Signal,
    SourceRun,
    SourceVersion,
    WatchItem,
    WatchItemChange,
)
from project_watch.metric_series import build_metric_series
from project_watch.signal_projection import deduplicate_project_signals
from project_watch.watch_items import watch_item_snapshot


def build_current_brief(
    project: Project,
    observations: list[Observation],
    sources: list[SourceVersion],
    plans: list[MonitoringPlan],
    signals: list[Signal],
    impacts: list[ReviewImpact],
    *,
    as_of: datetime | None = None,
    runs: list[SourceRun] | None = None,
    watch_items: list[WatchItem] | None = None,
    watch_changes: list[WatchItemChange] | None = None,
    holding_snapshots: list[HoldingSnapshot] | None = None,
) -> dict[str, Any]:
    """Compose a bounded snapshot; source creation defines the 30-day intake window."""
    as_of = as_of or datetime.now(UTC)
    if as_of.tzinfo is None:
        raise ValueError("as_of must have a timezone")
    cutoff = as_of - timedelta(days=30)
    own_sources = [row for row in sources if row.project_id == project.id]
    recent_sources = sorted(
        (row for row in own_sources if cutoff <= row.created_at <= as_of),
        key=lambda row: (row.created_at, row.id),
        reverse=True,
    )
    source_by_id = {row.id: row for row in own_sources}
    own_observations = [
        row
        for row in observations
        if row.project_id == project.id and row.source_id in source_by_id and row.source_locator
    ]
    confirmed_by_id = {row.id: row for row in own_observations if row.status == "confirmed"}
    domains = build_monitoring_projection(
        project.id, own_observations, own_sources, plans, runs=runs,
        holding_snapshots=holding_snapshots,
    )
    metric_projection = build_metric_series(own_observations, own_sources)
    confirmed = sorted(
        (row for row in own_observations if row.status == "confirmed"),
        key=lambda row: (row.created_at, row.id),
        reverse=True,
    )
    pending = [row for row in own_observations if row.status == "candidate"]
    own_impacts = [
        row
        for row in impacts
        if row.project_id == project.id
        and row.trigger_source_id in source_by_id
        and row.observation_id in confirmed_by_id
        and (
            row.status == "pending"
            or (row.reviewed_at is not None and cutoff <= row.reviewed_at <= as_of)
        )
    ]
    pending_impacts = [row for row in own_impacts if row.status == "pending"]
    visible_impacts = sorted(
        own_impacts,
        key=lambda row: (row.status != "pending", -row.created_at.timestamp(), row.id),
    )
    own_signals = deduplicate_project_signals(
        sorted(
            (row for row in signals if row.project_id == project.id and row.status == "candidate"),
            key=lambda row: (row.first_seen_at, row.id),
            reverse=True,
        )
    )
    own_plans = [row for row in plans if row.project_id == project.id]
    reviewed_watch = sorted(
        (
            row
            for row in (watch_items or [])
            if row.project_id == project.id
            and row.status in {"watching", "action_required"}
            and row.resolved_at is None
            and row.current_observation_id in confirmed_by_id
        ),
        key=lambda row: (row.status != "action_required", row.due_on is None, row.due_on, row.id),
    )
    resolved_watch = sorted(
        (
            row
            for row in (watch_items or [])
            if row.project_id == project.id
            and row.resolved_at is not None
            and row.resolved_at >= cutoff
            and row.current_observation_id in confirmed_by_id
        ),
        key=lambda row: (row.resolved_at, row.id),
        reverse=True,
    )
    resolution_by_item = {
        change.watch_item_id: change
        for change in sorted(
            (
                row
                for row in (watch_changes or [])
                if row.project_id == project.id
                and row.status == "accepted"
                and row.target_status == "resolved"
            ),
            key=lambda row: (row.updated_at, row.id),
        )
    }
    domain_rows = domains["domains"]
    holdings = screen_holding_snapshots(
        project.id, holding_snapshots or [], own_sources, as_of=as_of
    )
    return {
        "project_id": project.id,
        "project_name": project.name,
        "identity_status": project.identity_status,
        "status": "internal_draft",
        "as_of": as_of.isoformat(),
        "window_start": cutoff.isoformat(),
        "source_count": len(own_sources),
        "holdings": holdings,
        "new_source_count": len(recent_sources),
        "new_sources_truncated": len(recent_sources) > 10,
        "new_sources": [
            {
                "source_id": row.id,
                "reference": row.reference,
                "source_sha256": row.content_sha256,
                "created_at": row.created_at.isoformat(),
            }
            for row in recent_sources[:10]
        ],
        "candidate_count": len(pending),
        "pending_review_count": len(pending_impacts),
        "review_impacts_truncated": len(visible_impacts) > 20,
        "review_impacts": [
            {
                "impact_id": row.id,
                "status": row.status,
                "topic": row.topic,
                "old_observation_id": row.observation_id,
                "old_statement": confirmed_by_id[row.observation_id].statement,
                "old_source_id": confirmed_by_id[row.observation_id].source_id,
                "old_source_reference": source_by_id[
                    confirmed_by_id[row.observation_id].source_id
                ].reference,
                "old_source_sha256": source_by_id[
                    confirmed_by_id[row.observation_id].source_id
                ].content_sha256,
                "old_source_locator": confirmed_by_id[row.observation_id].source_locator,
                "old_observation_version": confirmed_by_id[row.observation_id].version,
                "new_source_id": row.trigger_source_id,
                "new_source_reference": source_by_id[row.trigger_source_id].reference,
                "new_source_sha256": source_by_id[row.trigger_source_id].content_sha256,
                "created_at": row.created_at.isoformat(),
                "resolution": row.resolution,
                "note": row.note,
                "reviewer_ref": row.reviewer_ref,
                "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
            }
            for row in visible_impacts[:20]
        ],
        "confirmed_count": len(confirmed),
        "watch_items_truncated": len(reviewed_watch) > 20,
        "watch_items": [
            {
                **watch_item_snapshot(row, confirmed_by_id, source_by_id),
                "due_on": row.due_on.isoformat() if row.due_on else None,
                "created_at": row.created_at.isoformat(),
                "updated_at": row.updated_at.isoformat(),
            }
            for row in reviewed_watch[:20]
        ],
        "resolved_watch_items_truncated": len(resolved_watch) > 20,
        "resolved_watch_items": [
            {
                **watch_item_snapshot(row, confirmed_by_id, source_by_id),
                "resolution_reason": resolution_by_item[row.id].reason
                if row.id in resolution_by_item
                else None,
                "due_on": row.due_on.isoformat() if row.due_on else None,
                "resolved_at": row.resolved_at.isoformat(),
                "created_at": row.created_at.isoformat(),
                "updated_at": row.updated_at.isoformat(),
            }
            for row in resolved_watch[:20]
        ],
        "metric_trends_truncated": metric_projection["truncated"],
        "metric_trends": [
            {
                "subject": row["subject"],
                "name": row["name"],
                "unit": row["unit"],
                "scope": row["scope"],
                "basis": row["basis"],
                "cadence": row["cadence"],
                "truncated_points": row["truncated_points"],
                "latest_change": {
                    key: str(value) if isinstance(value, Decimal) else value
                    for key, value in row["latest_change"].items()
                },
                "points": [
                    {
                        "observation_id": point["observation_id"],
                        "observation_version": point["observation_version"],
                        "period": point["period"],
                        "value": str(point["number"]),
                        "source_id": point["source_id"],
                        "source_sha256": point["source_sha256"],
                        "source_locator": point["source_locator"],
                        "same_period_conflict": point["same_period_conflict"],
                        "review_proof": None,
                        "locator_status": "verification_unavailable",
                    }
                    for point in row["points"]
                ],
            }
            for row in metric_projection["series"]
        ],
        "confirmed_facts_truncated": len(confirmed) > 30,
        "confirmed_facts": [
            {
                "observation_id": row.id,
                "subject": row.subject,
                "statement": row.statement,
                "value": str(row.number) if row.number is not None else None,
                "unit": row.unit,
                "period": row.period,
                "scope": row.scope,
                "basis": row.basis,
                "source_id": row.source_id,
                "source_reference": source_by_id[row.source_id].reference,
                "source_sha256": source_by_id[row.source_id].content_sha256,
                "source_locator": row.source_locator,
                "observation_version": row.version,
                "created_at": row.created_at.isoformat(),
                "review_proof": None,
                "locator_status": "verification_unavailable",
            }
            for row in confirmed[:30]
        ],
        "domain_status": {row["key"]: row["status"] for row in domain_rows},
        "domains": domain_rows,
        "gaps": [
            {"domain": row["title"], "status": row["status"], "note": row["note"]}
            for row in domain_rows
            if row["status"] != "evidence_available"
        ],
        "external_leads_truncated": len(own_signals) > 10,
        "source_checks_truncated": len(own_plans) > 20,
        "source_checks": [
            {
                "plan_id": row.id,
                "provider": urlsplit(row.target).hostname or "未知来源",
                "enabled": row.enabled,
                "status": row.last_run_status,
                "error_code": row.last_run_error_code,
                "item_count": (
                    row.last_item_count
                    if row.last_run_status in {"completed", "no_results"}
                    else None
                ),
                "matched_count": (
                    row.last_matched_count
                    if row.last_run_status in {"completed", "no_results"}
                    else None
                ),
            }
            for row in own_plans[:20]
        ],
        "external_leads": [
            {
                "signal_id": row.id,
                "title": row.title,
                "url": row.url,
                "first_seen_at": row.first_seen_at.isoformat(),
                "freshness": row.freshness,
                "status": "candidate",
            }
            for row in own_signals[:10]
        ],
        "disclaimer": "内部工作底稿；只列已确认且登记了来源定位的记录。系统只核对定位是否命中已提取片段，不核验陈述内容。公开线索、AI 草拟与待复核项不是正式事实或投资结论。",
    }


def brief_observation_ids(brief: dict[str, Any]) -> list[str]:
    """Return only observations actually printed in the bounded brief."""
    ids = {row["observation_id"] for row in brief["confirmed_facts"]}
    ids.update(
        point["observation_id"] for series in brief["metric_trends"] for point in series["points"]
    )
    return sorted(ids)


def brief_source_locators(brief: dict[str, Any]) -> list[tuple[str, str]]:
    """Return only source positions printed in the bounded brief."""
    positions = {(row["source_id"], row["source_locator"]) for row in brief["confirmed_facts"]}
    positions.update(
        (point["source_id"], point["source_locator"])
        for series in brief["metric_trends"]
        for point in series["points"]
    )
    return sorted(positions)


def attach_locator_status(
    brief: dict[str, Any], statuses: dict[tuple[str, str], str]
) -> dict[str, Any]:
    """Annotate whether a source locator exists in extracted text, not whether a claim is true."""

    def status(row: dict[str, Any]) -> str:
        result = statuses.get((row["source_id"], row["source_locator"]))
        allowed = {"indexed_exact", "locator_not_found", "not_indexed"}
        return result if result in allowed else "verification_unavailable"

    for fact in brief["confirmed_facts"]:
        fact["locator_status"] = status(fact)
    for series in brief["metric_trends"]:
        for point in series["points"]:
            point["locator_status"] = status(point)
    return brief


def attach_review_proof(
    brief: dict[str, Any],
    decisions: list[ReviewDecision],
    *,
    reviewer_authentication: Literal["authenticated_team", "unverified_preview"],
) -> dict[str, Any]:
    """Annotate visible confirmed records with their exact review decision, if present."""
    if reviewer_authentication not in {"authenticated_team", "unverified_preview"}:
        raise ValueError("invalid reviewer authentication")
    visible_ids = set(brief_observation_ids(brief))
    as_of = datetime.fromisoformat(brief["as_of"])
    current: dict[tuple[str, int], ReviewDecision] = {}
    for decision in decisions:
        if (
            decision.observation_id not in visible_ids
            or decision.action != "confirm"
            or decision.decided_at.tzinfo is None
            or decision.decided_at > as_of
        ):
            continue
        key = (decision.observation_id, decision.expected_version)
        previous = current.get(key)
        if previous is None or (decision.decided_at, decision.id) > (
            previous.decided_at,
            previous.id,
        ):
            current[key] = decision

    def proof(observation_id: str, version: int) -> dict[str, str] | None:
        decision = current.get((observation_id, version - 1))
        if decision is None:
            return None
        return {
            "decision_id": decision.id,
            "reviewer_ref": decision.reviewer,
            "reviewer_authentication": reviewer_authentication,
            "decided_at": decision.decided_at.isoformat(),
        }

    for fact in brief["confirmed_facts"]:
        fact["review_proof"] = proof(fact["observation_id"], fact["observation_version"])
    for series in brief["metric_trends"]:
        for point in series["points"]:
            point["review_proof"] = proof(point["observation_id"], point["observation_version"])
    return brief


__all__ = [
    "attach_locator_status",
    "attach_review_proof",
    "brief_observation_ids",
    "brief_source_locators",
    "build_current_brief",
]

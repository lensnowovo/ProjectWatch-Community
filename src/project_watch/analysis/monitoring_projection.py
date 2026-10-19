"""Project-bound six-domain monitor; a lead is never a confirmed business fact."""

from __future__ import annotations

from collections import Counter
from typing import Any

from project_watch.analysis.activity_projection import project_activity
from project_watch.analysis.capital_projection import screen_capital_records
from project_watch.analysis.financial_projection import screen_confirmed_financials
from project_watch.analysis.holding_projection import screen_holding_snapshots
from project_watch.analysis.operating_projection import screen_operating_metrics
from project_watch.domain import (
    HoldingSnapshot,
    MonitoringPlan,
    Observation,
    SourceRun,
    SourceVersion,
)

DOMAINS = (
    ("capital", "资本与持仓"),
    ("financials", "财务表现"),
    ("financial_integrity", "财务完整性"),
    ("operations_products", "经营与产品"),
    ("external_signals", "外部情报"),
    ("competitive_position", "竞争位置"),
)


def build_monitoring_projection(
    project_id: str,
    observations: list[Observation],
    sources: list[SourceVersion],
    plans: list[MonitoringPlan],
    *,
    runs: list[SourceRun] | None = None,
    holding_snapshots: list[HoldingSnapshot] | None = None,
) -> dict[str, Any]:
    """Return honest coverage states without writing facts or touching external services."""
    project_sources = [source for source in sources if source.project_id == project_id]
    source_ids = {source.id for source in project_sources}
    records = [
        item
        for item in observations
        if item.project_id == project_id
        and item.source_id in source_ids
        and item.source_locator
        and item.status != "rejected"
    ]
    finance = screen_confirmed_financials(records, project_sources)
    capital = screen_capital_records(records, {source.id: source for source in project_sources})
    holdings = screen_holding_snapshots(project_id, holding_snapshots or [], project_sources)
    own_holdings = [
        item for item in (holding_snapshots or []) if item.project_id == project_id
    ]
    activity = project_activity(records, {source.id: source for source in project_sources})
    operating_metrics = screen_operating_metrics(
        records, {source.id: source for source in project_sources}
    )
    financial_evidence_ids = {item["observation_id"] for item in finance["evidence"]}
    domain_records = {
        "capital": [item for item in records if item.subject == "资本与持仓"],
        "financials": [item for item in records if item.subject in {"财务表现", "financials"}],
        "financial_integrity": [item for item in records if item.id in financial_evidence_ids],
        "operations_products": [
            item for item in records if item.subject in {"经营情况", "产品与技术"}
        ],
        "competitive_position": [item for item in records if item.subject == "行业与竞争"],
    }
    confirmed = Counter(item.subject for item in records if item.status == "confirmed")
    candidates = Counter(item.subject for item in records if item.status == "candidate")
    project_plans = [plan for plan in plans if plan.project_id == project_id and plan.enabled]
    active_plan_ids = {plan.id for plan in project_plans}
    project_runs = [
        run
        for run in (runs or [])
        if run.project_id == project_id and run.plan_id in active_plan_ids
    ]
    last_checked_at = max((run.finished_at for run in project_runs), default=None)
    matched = sum(
        plan.last_matched_count or 0
        for plan in project_plans
        if plan.last_run_status == "completed"
    )
    failures = sum(
        plan.last_run_status in {"unreachable", "rate_limited"} for plan in project_plans
    )
    legacy_unknown = sum(
        plan.last_run_status == "completed" and plan.last_matched_count is None
        for plan in project_plans
    )
    completed = sum(
        plan.last_run_status == "no_results"
        or (plan.last_run_status == "completed" and plan.last_matched_count == 0)
        for plan in project_plans
    )

    def domain(
        key: str,
        status: str,
        evidence_count: int,
        pending_count: int,
        note: str,
        *,
        source_failure_count: int = 0,
    ) -> dict[str, Any]:
        relevant = domain_records.get(key, [])
        return {
            "key": key,
            "title": dict(DOMAINS)[key],
            "status": status,
            "evidence_count": evidence_count,
            "pending_count": pending_count,
            "source_failure_count": source_failure_count,
            "source_count": (
                len(project_plans)
                if key == "external_signals"
                else len({item.source_id for item in relevant} | (
                    {item.source_id for item in own_holdings if item.status == "confirmed"}
                    if key == "capital" else set()
                ))
            ),
            "last_recorded_at": (
                max(
                    [item.created_at for item in relevant] +
                    ([item.created_at for item in own_holdings] if key == "capital" else [])
                ).isoformat()
                if relevant or (key == "capital" and own_holdings) else None
            ),
            "last_checked_at": (
                last_checked_at.isoformat()
                if key == "external_signals" and last_checked_at
                else None
            ),
            "note": note,
        }

    financial_status = (
        "evidence_available"
        if finance["accepted_count"]
        else "needs_review"
        if confirmed["财务表现"] or candidates["财务表现"]
        else "missing"
    )
    checks = finance["analysis"]
    integrity_status = (
        "not_run"
        if not finance["accepted_count"]
        else "evidence_available"
        if checks["status"] == "screened"
        and not checks["summary"]["warning_count"]
        and not checks["summary"]["data_gap_count"]
        else "needs_review"
    )
    operations_count = confirmed["经营情况"] + confirmed["产品与技术"]
    operations_pending = candidates["经营情况"] + candidates["产品与技术"]
    operations_status = (
        "evidence_available"
        if operations_count
        else "needs_review"
        if operations_pending
        else "missing"
    )
    external_status = (
        "evidence_available"
        if matched
        else "source_failure"
        if failures
        else "needs_review"
        if legacy_unknown
        else "no_results"
        if completed
        else "not_run"
        if project_plans
        else "missing"
    )
    competition_count = confirmed["行业与竞争"]
    competition_pending = candidates["行业与竞争"]

    domains = [
        {
            **domain(
                "capital",
                "needs_review" if holdings["conflicts"] else
                "evidence_available" if holdings["positions"] or capital["evidence_count"] else
                "needs_review" if holdings["pending_count"] or capital["pending_count"] else "missing",
                capital["evidence_count"] + len(holdings["positions"]),
                capital["pending_count"] + holdings["pending_count"],
                holdings["note"] if holding_snapshots else capital["note"],
            ),
            "evidence": capital["evidence"],
            "evidence_truncated": capital["evidence_truncated"],
            "holdings": holdings,
        },
        domain(
            "financials",
            financial_status,
            finance["accepted_count"],
            candidates["财务表现"],
            "只把已确认、同口径且可定位的年度实际数纳入财务核验。",
        ),
        domain(
            "financial_integrity",
            integrity_status,
            finance["accepted_count"],
            checks.get("summary", {}).get("warning_count", 0),
            "勾稽异常只提示复核，不作舞弊定性。",
        ),
        domain(
            "operations_products",
            operations_status,
            operations_count,
            operations_pending,
            "已确认记录和待核对摘录分开显示；产能与产量仅在明确口径下展示。",
        ),
        domain(
            "external_signals",
            external_status,
            matched,
            matched,
            "名称匹配只产生公开线索，不能视为企业事实。",
            source_failure_count=failures,
        ),
        domain(
            "competitive_position",
            "needs_review" if competition_count or competition_pending else "missing",
            competition_count,
            competition_pending,
            "行业材料不等于可比竞争集；同口径比较仍需人工核对。",
        ),
    ]
    return {
        "project_id": project_id,
        "domains": domains,
        "activity": activity,
        "operating_metrics": operating_metrics,
        "evidence_available_count": sum(item["status"] == "evidence_available" for item in domains),
        "total_count": len(domains),
    }


__all__ = ["DOMAINS", "build_monitoring_projection"]

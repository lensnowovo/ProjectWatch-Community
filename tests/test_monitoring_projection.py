"""Six-domain projection uses only project-bound, source-backed records."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from project_watch.analysis.monitoring_projection import build_monitoring_projection
from project_watch.domain import MonitoringPlan, Observation, SourceRun, SourceVersion

NOW = datetime(2026, 9, 27, tzinfo=UTC)
SOURCE = SourceVersion("s1", "p1", "document", "synthetic.xlsx", "a" * 64, NOW)


def observation(
    subject: str, *, status: str = "confirmed", project_id: str = "p1", source_id: str = "s1"
) -> Observation:
    return Observation(
        id=f"{subject}-{status}-{project_id}",
        project_id=project_id,
        source_id=source_id,
        subject=subject,
        statement="营业收入" if subject == "财务表现" else "合成测试摘录",
        number=Decimal("120") if subject == "财务表现" else None,
        unit="万元" if subject == "财务表现" else None,
        period="2025" if subject == "财务表现" else None,
        scope="合并" if subject == "财务表现" else None,
        source_locator="合成表:A1",
        status=status,
        version=2 if status == "confirmed" else 1,
        created_at=NOW,
        basis="actual" if subject == "财务表现" else "unknown",
    )


def plan(status: str, matched: int | None, *, project_id: str = "p1") -> MonitoringPlan:
    return MonitoringPlan(
        id=f"plan-{status}",
        project_id=project_id,
        kind="rss",
        target="https://example.test/rss",
        cadence_minutes=1440,
        enabled=True,
        last_run_status=status,
        created_at=NOW,
        last_item_count=matched,
        last_matched_count=matched,
    )


def test_six_domains_separate_evidence_candidate_gap_and_source_failure() -> None:
    result = build_monitoring_projection(
        "p1",
        [observation("财务表现"), observation("经营情况", status="candidate")],
        [SOURCE],
        [plan("completed", 1), plan("unreachable", 0)],
    )
    domains = {item["key"]: item for item in result["domains"]}
    assert len(domains) == 6
    assert domains["financials"]["status"] == "evidence_available"
    assert domains["financials"]["evidence_count"] == 1
    assert domains["financial_integrity"]["status"] == "needs_review"
    assert domains["operations_products"]["status"] == "needs_review"
    assert domains["operations_products"]["evidence_count"] == 0
    assert domains["external_signals"]["status"] == "evidence_available"
    assert domains["external_signals"]["source_failure_count"] == 1
    assert domains["external_signals"]["source_count"] == 2
    assert domains["external_signals"]["last_recorded_at"] is None
    assert domains["capital"]["status"] == "missing"
    assert domains["competitive_position"]["status"] == "missing"


def test_foreign_or_missing_source_cannot_create_project_coverage() -> None:
    result = build_monitoring_projection(
        "p1",
        [
            observation("财务表现", project_id="p2"),
            observation("财务表现", source_id="missing"),
        ],
        [SOURCE],
        [plan("completed", 1, project_id="p2")],
    )
    domains = {item["key"]: item for item in result["domains"]}
    assert domains["financials"]["status"] == "missing"
    assert domains["external_signals"]["status"] == "missing"


def test_unreachable_is_not_zero_news_and_competition_is_not_inferred() -> None:
    result = build_monitoring_projection(
        "p1",
        [observation("行业与竞争")],
        [SOURCE],
        [plan("unreachable", 0)],
    )
    domains = {item["key"]: item for item in result["domains"]}
    assert domains["external_signals"]["status"] == "source_failure"
    assert domains["competitive_position"]["status"] == "needs_review"
    assert domains["competitive_position"]["evidence_count"] == 1


def test_rate_limit_is_source_failure_not_zero_news() -> None:
    result = build_monitoring_projection("p1", [], [SOURCE], [plan("rate_limited", 0)])
    external = {item["key"]: item for item in result["domains"]}["external_signals"]
    assert external["status"] == "source_failure"
    assert external["source_failure_count"] == 1
    assert external["evidence_count"] == 0


def test_external_check_time_uses_latest_project_bound_run_not_plan_creation() -> None:
    active = plan("rate_limited", 0)
    old = SourceRun(
        "old",
        "p1",
        active.id,
        "no_results",
        None,
        NOW - timedelta(days=2),
        NOW - timedelta(days=2),
    )
    latest = replace(
        old,
        id="latest",
        status="rate_limited",
        error_code="rate_limited",
        started_at=NOW - timedelta(hours=1),
        finished_at=NOW,
    )
    foreign = replace(latest, id="foreign", project_id="p2", finished_at=NOW + timedelta(days=1))
    projection = build_monitoring_projection("p1", [], [], [active], runs=[old, latest, foreign])
    external = {item["key"]: item for item in projection["domains"]}["external_signals"]
    assert external["status"] == "source_failure"
    assert external["last_checked_at"] == NOW.isoformat()
    assert external["last_recorded_at"] is None


def test_domain_record_times_and_source_counts_are_project_bound() -> None:
    older = replace(observation("财务表现"), created_at=NOW - timedelta(days=3))
    newer = replace(
        observation("财务表现"),
        id="second-financial",
        statement="营业成本",
        source_id="s2",
        source_locator="合成表:B2",
        created_at=NOW - timedelta(days=1),
    )
    pending = replace(
        observation("经营情况", status="candidate"),
        created_at=NOW - timedelta(hours=1),
    )
    foreign = replace(observation("财务表现", project_id="p2"), created_at=NOW)
    result = build_monitoring_projection(
        "p1",
        [older, newer, pending, foreign, observation("行业与竞争", source_id="missing")],
        [SOURCE, replace(SOURCE, id="s2", content_sha256="b" * 64)],
        [],
    )
    domains = {item["key"]: item for item in result["domains"]}
    assert domains["financials"]["source_count"] == 2
    assert domains["financials"]["last_recorded_at"] == newer.created_at.isoformat()
    assert domains["financial_integrity"]["source_count"] == 2
    assert domains["operations_products"]["source_count"] == 1
    assert domains["operations_products"]["last_recorded_at"] == pending.created_at.isoformat()
    assert domains["competitive_position"]["source_count"] == 0
    assert domains["competitive_position"]["last_recorded_at"] is None
    assert domains["external_signals"]["last_recorded_at"] is None


def test_legacy_completed_run_without_match_count_is_not_reported_as_no_news() -> None:
    result = build_monitoring_projection("p1", [], [SOURCE], [plan("completed", None)])
    domains = {item["key"]: item for item in result["domains"]}
    assert domains["external_signals"]["status"] == "needs_review"


def test_activity_timeline_uses_only_reviewed_located_project_records() -> None:
    operations = replace(
        observation("经营情况"),
        statement="已登记订单交付进度",
        period="2026-08",
        created_at=NOW - timedelta(days=2),
    )
    product = replace(
        observation("产品与技术"),
        statement="已登记产品测试结果",
        number=Decimal("120"),
        unit="台",
        basis="forecast",
        period="2026-09",
        created_at=NOW - timedelta(days=1),
    )
    risk = replace(
        observation("风险与合规"),
        statement="已登记合同履约疑点",
        created_at=NOW,
    )
    result = build_monitoring_projection(
        "p1",
        [
            operations,
            product,
            risk,
            observation("经营情况", status="candidate"),
            observation("风险与合规", status="candidate"),
            observation("产品与技术", project_id="p2"),
            observation("产品与技术", source_id="missing"),
        ],
        [SOURCE],
        [],
    )
    activity = result["activity"]
    assert activity["confirmed_count"] == 3
    assert activity["pending_count"] == 2
    assert [item["subject"] for item in activity["entries"]] == [
        "风险与合规",
        "产品与技术",
        "经营情况",
    ]
    assert activity["entries"][2]["period"] == "2026-08"
    assert activity["entries"][1]["basis"] == "forecast"
    assert activity["entries"][1]["number"] == "120"
    assert all(item["source_id"] == SOURCE.id for item in activity["entries"])
    assert all(item["source_sha256"] == SOURCE.content_sha256 for item in activity["entries"])
    assert all(item["source_locator"] == "合成表:A1" for item in activity["entries"])


def test_empty_activity_does_not_mean_zero_risk() -> None:
    activity = build_monitoring_projection("p1", [], [], [])["activity"]
    assert activity["entries"] == []
    assert activity["confirmed_count"] == 0
    assert activity["pending_count"] == 0
    assert "不代表" in activity["empty_note"]

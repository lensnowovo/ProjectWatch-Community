"""An internal brief must remain evidence-bound and honest about gaps."""

import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from openpyxl import Workbook

from project_watch.api import create_app
from project_watch.domain import (
    MonitoringPlan,
    Observation,
    Project,
    ReviewDecision,
    ReviewImpact,
    Signal,
    SourceVersion,
)
from project_watch.report import (
    attach_locator_status,
    attach_review_proof,
    brief_observation_ids,
    brief_source_locators,
    build_current_brief,
)

NOW = datetime(2026, 9, 28, tzinfo=UTC)
PROJECT = Project("p1", "合成甲公司", "unverified", NOW - timedelta(days=90))
SOURCE = SourceVersion("s1", "p1", "document", "synthetic-finance.xlsx", "a" * 64, NOW)
OTHER = SourceVersion("s2", "p2", "document", "foreign.xlsx", "b" * 64, NOW)


def observation(status: str, *, source_id: str = "s1", project_id: str = "p1") -> Observation:
    return Observation(
        f"o-{status}-{project_id}-{source_id}",
        project_id,
        source_id,
        "财务表现",
        "营业收入",
        Decimal("120") if status == "confirmed" else Decimal("999"),
        "万元",
        "2025",
        "合并",
        "资产负债表:B2",
        status,
        2,
        NOW,
        "actual",
    )


def test_brief_never_promotes_candidates_or_cross_project_sources() -> None:
    brief = build_current_brief(
        PROJECT,
        [
            observation("confirmed"),
            observation("candidate"),
            observation("confirmed", source_id="s2"),
        ],
        [SOURCE, OTHER],
        [],
        [],
        [],
        as_of=NOW,
    )
    assert brief["project_id"] == "p1"
    assert brief["status"] == "internal_draft"
    assert brief["identity_status"] == "unverified"
    assert brief["candidate_count"] == 1
    assert len(brief["confirmed_facts"]) == 1
    assert brief["confirmed_facts"][0]["value"] == "120"
    assert brief["confirmed_facts"][0]["source_sha256"] == "a" * 64
    assert brief["confirmed_facts"][0]["source_locator"] == "资产负债表:B2"
    assert brief["confirmed_facts"][0]["observation_version"] == 2
    assert "999" not in str(brief["confirmed_facts"])


def test_brief_prints_current_confirmation_record_for_fact_and_metric() -> None:
    confirmed = observation("confirmed")
    brief = build_current_brief(PROJECT, [confirmed], [SOURCE], [], [], [], as_of=NOW)
    assert brief_observation_ids(brief) == [confirmed.id]
    decision = ReviewDecision("review-1", confirmed.id, "reviewer-1", "confirm", 1, NOW)
    stale = ReviewDecision("review-old", confirmed.id, "reviewer-old", "confirm", 0, NOW)
    attach_review_proof(brief, [stale, decision], reviewer_authentication="authenticated_team")
    proof = {
        "decision_id": "review-1",
        "reviewer_ref": "reviewer-1",
        "reviewer_authentication": "authenticated_team",
        "decided_at": NOW.isoformat(),
    }
    assert brief["confirmed_facts"][0]["review_proof"] == proof
    assert brief["metric_trends"][0]["points"][0]["review_proof"] == proof
    assert brief["metric_trends"][0]["points"][0]["observation_id"] == confirmed.id


def test_brief_does_not_invent_missing_or_stale_confirmation_record() -> None:
    confirmed = observation("confirmed")
    brief = build_current_brief(PROJECT, [confirmed], [SOURCE], [], [], [], as_of=NOW)
    stale = ReviewDecision("review-old", confirmed.id, "reviewer-old", "confirm", 0, NOW)
    attach_review_proof(brief, [stale], reviewer_authentication="unverified_preview")
    assert brief["confirmed_facts"][0]["review_proof"] is None
    assert brief["metric_trends"][0]["points"][0]["review_proof"] is None
    assert brief["domain_status"]["capital"] == "missing"


def test_brief_marks_extracted_locator_without_claiming_fact_verification() -> None:
    brief = build_current_brief(PROJECT, [observation("confirmed")], [SOURCE], [], [], [], as_of=NOW)
    key = ("s1", "资产负债表:B2")
    assert brief_source_locators(brief) == [key]
    attach_locator_status(brief, {key: "indexed_exact"})
    assert brief["confirmed_facts"][0]["locator_status"] == "indexed_exact"
    assert brief["metric_trends"][0]["points"][0]["locator_status"] == "indexed_exact"
    attach_locator_status(brief, {key: "locator_not_found"})
    assert brief["confirmed_facts"][0]["locator_status"] == "locator_not_found"
    attach_locator_status(brief, {})
    assert brief["confirmed_facts"][0]["locator_status"] == "verification_unavailable"


def test_brief_keeps_new_material_review_and_external_lead_separate() -> None:
    impact = ReviewImpact(
        "i1", "p1", "s1", "o-confirmed-p1-s1", "财务表现", "pending", None, None, None, NOW, None
    )
    lead = Signal(
        "n1",
        "p1",
        "plan1",
        "run1",
        "external1",
        "合成新闻",
        "https://example.test/news",
        "未经核验",
        NOW,
        NOW,
        "c" * 64,
        "current",
        "candidate",
    )
    brief = build_current_brief(
        PROJECT, [observation("confirmed")], [SOURCE], [], [lead], [impact], as_of=NOW
    )
    assert brief["new_source_count"] == 1
    assert brief["new_sources"][0]["reference"] == "synthetic-finance.xlsx"
    assert brief["pending_review_count"] == 1
    assert brief["external_leads"][0]["status"] == "candidate"
    assert brief["external_leads"][0]["title"] == "合成新闻"
    assert brief["disclaimer"].startswith("内部工作底稿")
    assert "只核对定位是否命中已提取片段" in brief["disclaimer"]


def test_brief_links_new_material_to_old_judgment_without_rewriting_fact() -> None:
    old_source = SourceVersion(
        "old-source", "p1", "document", "prior-finance.xlsx", "1" * 64, NOW - timedelta(days=45)
    )
    new_source = SourceVersion(
        "new-source", "p1", "document", "updated-finance.xlsx", "2" * 64, NOW
    )
    old_fact = Observation(
        "old-fact",
        "p1",
        "old-source",
        "财务表现",
        "营业收入",
        Decimal("100"),
        "万元",
        "2025",
        "合并",
        "利润表!B4",
        "confirmed",
        2,
        NOW - timedelta(days=45),
        "actual",
    )
    pending = ReviewImpact(
        "impact-1",
        "p1",
        "new-source",
        "old-fact",
        "财务表现",
        "pending",
        None,
        None,
        None,
        NOW,
        None,
    )
    foreign_impact = ReviewImpact(
        "foreign-impact",
        "p2",
        "new-source",
        "old-fact",
        "财务表现",
        "pending",
        None,
        None,
        None,
        NOW,
        None,
    )
    brief = build_current_brief(
        PROJECT, [old_fact], [old_source, new_source], [], [], [pending, foreign_impact], as_of=NOW
    )
    assert brief["pending_review_count"] == 1
    assert brief["review_impacts"] == [
        {
            "impact_id": "impact-1",
            "status": "pending",
            "topic": "财务表现",
            "old_observation_id": "old-fact",
            "old_statement": "营业收入",
            "old_source_id": "old-source",
            "old_source_reference": "prior-finance.xlsx",
            "old_source_sha256": "1" * 64,
            "old_source_locator": "利润表!B4",
            "old_observation_version": 2,
            "new_source_id": "new-source",
            "new_source_reference": "updated-finance.xlsx",
            "new_source_sha256": "2" * 64,
            "created_at": NOW.isoformat(),
            "resolution": None,
            "note": None,
            "reviewer_ref": None,
            "reviewed_at": None,
        }
    ]
    assert brief["confirmed_facts"][0]["value"] == "100"
    reviewed = ReviewImpact(
        "impact-1",
        "p1",
        "new-source",
        "old-fact",
        "财务表现",
        "reviewed",
        "follow_up",
        "两期口径待核对",
        "reviewer-1",
        NOW,
        NOW,
    )
    after = build_current_brief(
        PROJECT, [old_fact], [old_source, new_source], [], [], [reviewed], as_of=NOW
    )
    assert after["pending_review_count"] == 0
    assert after["review_impacts"][0]["resolution"] == "follow_up"
    assert after["review_impacts"][0]["note"] == "两期口径待核对"
    assert after["confirmed_facts"][0]["value"] == "100"


def test_brief_empty_project_does_not_claim_no_risk_or_no_news() -> None:
    brief = build_current_brief(PROJECT, [], [], [], [], [], as_of=NOW)
    assert brief["confirmed_facts"] == []
    assert brief["external_leads"] == []
    assert brief["domain_status"]["external_signals"] == "missing"
    assert brief["new_source_count"] == 0
    assert brief["gaps"]


def test_brief_exposes_source_failures_without_claiming_no_news() -> None:
    plans = [
        MonitoringPlan(
            "rate-limited",
            "p1",
            "rss",
            "https://api.gdeltproject.org/api/v2/doc/doc?query=synthetic",
            1440,
            True,
            "unreachable",
            NOW,
            last_item_count=0,
            last_matched_count=0,
            last_run_error_code="rate_limited",
        ),
        MonitoringPlan(
            "no-results",
            "p1",
            "rss",
            "https://example.org/feed.xml",
            1440,
            True,
            "no_results",
            NOW,
            last_item_count=0,
            last_matched_count=0,
        ),
        MonitoringPlan(
            "other-project",
            "p2",
            "rss",
            "https://other.example.org/feed.xml",
            1440,
            True,
            "completed",
            NOW,
            last_item_count=5,
            last_matched_count=5,
        ),
    ]
    brief = build_current_brief(PROJECT, [], [], plans, [], [], as_of=NOW)
    assert brief["external_leads"] == []
    assert brief["domain_status"]["external_signals"] == "source_failure"
    assert brief["source_checks"] == [
        {
            "plan_id": "rate-limited",
            "provider": "api.gdeltproject.org",
            "enabled": True,
            "status": "unreachable",
            "error_code": "rate_limited",
            "item_count": None,
            "matched_count": None,
        },
        {
            "plan_id": "no-results",
            "provider": "example.org",
            "enabled": True,
            "status": "no_results",
            "error_code": None,
            "item_count": 0,
            "matched_count": 0,
        },
    ]


def test_preview_api_returns_no_store_internal_brief(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "brief.sqlite3"))
    project = client.post("/api/projects", json={"name": "Synthetic Brief"}).json()
    path = f"/api/projects/{project['id']}/report/current"
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["confirmed_facts"] == []
    assert response.json()["status"] == "internal_draft"
    assert client.get("/api/projects/missing/report/current").status_code == 404


def test_brief_shows_only_comparable_confirmed_periods_with_evidence() -> None:
    earlier = Observation(
        "o-2024",
        "p1",
        "s1",
        "财务表现",
        "营业收入",
        Decimal("100"),
        "万元",
        "2024",
        "合并",
        "利润表:B3",
        "confirmed",
        2,
        NOW - timedelta(days=1),
        "actual",
    )
    current = observation("confirmed")
    brief = build_current_brief(
        PROJECT, [earlier, current, observation("candidate")], [SOURCE], [], [], [], as_of=NOW
    )
    series = brief["metric_trends"]
    assert len(series) == 1
    assert series[0]["name"] == "营业收入"
    assert [(point["period"], point["value"]) for point in series[0]["points"]] == [
        ("2024", "100"),
        ("2025", "120"),
    ]
    assert series[0]["points"][0]["source_locator"] == "利润表:B3"
    assert series[0]["latest_change"] == {
        "status": "comparable",
        "previous_period": "2024",
        "period": "2025",
        "absolute": "20",
        "percent": "20.00",
        "previous_observation_id": "o-2024",
        "observation_id": current.id,
    }
    assert "999" not in str(series)


def test_brief_marks_same_period_conflict_without_averaging() -> None:
    conflicting = Observation(
        "o-conflict",
        "p1",
        "s1",
        "财务表现",
        "营业收入",
        Decimal("125"),
        "万元",
        "2025",
        "合并",
        "利润表:C4",
        "confirmed",
        2,
        NOW,
        "actual",
    )
    brief = build_current_brief(
        PROJECT, [observation("confirmed"), conflicting], [SOURCE], [], [], [], as_of=NOW
    )
    points = brief["metric_trends"][0]["points"]
    assert {point["value"] for point in points} == {"120", "125"}
    assert all(point["same_period_conflict"] for point in points)


def test_two_uploaded_workbooks_flow_into_brief_only_after_review(tmp_path: Path) -> None:
    app = create_app(tmp_path / "brief-cycle.sqlite3")
    client = TestClient(app)
    project_id = client.post("/api/projects", json={"name": "Synthetic Cycle"}).json()["id"]
    source_ids: list[str] = []
    for year, amount in [(2024, 100), (2025, 120)]:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "合并利润表"
        sheet.append(["单位：万元"])
        sheet.append(["项目", f"{year}年实际"])
        sheet.append(["营业收入", amount])
        payload = io.BytesIO()
        workbook.save(payload)
        uploaded = client.post(
            f"/api/projects/{project_id}/files",
            files={
                "file": (
                    f"synthetic-{year}.xlsx",
                    payload.getvalue(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        assert uploaded.status_code == 202
        source_id = uploaded.json()["source"]["id"]
        source_ids.append(source_id)
        assert app.state.intake_worker.process_one().status == "completed"

    path = f"/api/projects/{project_id}/report/current"
    before = client.get(path).json()
    assert before["confirmed_count"] == 0
    assert before["metric_trends"] == []
    assert before["candidate_count"] >= 2

    observations = client.get(f"/api/projects/{project_id}/observations").json()
    finance = [item for item in observations if item["statement"] == "营业收入"]
    assert len(finance) == 2
    for item in finance:
        approved = client.post(
            f"/api/projects/{project_id}/observations/{item['id']}/confirm",
            json={"reviewer": "synthetic-reviewer", "expected_version": item["version"]},
        )
        assert approved.status_code == 200

    after = client.get(path).json()
    assert after["confirmed_count"] == 2
    assert len(after["metric_trends"]) == 1
    assert [(point["period"], point["value"]) for point in after["metric_trends"][0]["points"]] == [
        ("2024", "100"),
        ("2025", "120"),
    ]
    for point in after["metric_trends"][0]["points"]:
        assert point["source_id"] in source_ids
        assert point["locator_status"] == "indexed_exact"
        assert point["review_proof"]["reviewer_ref"] == "synthetic-reviewer"
        assert point["review_proof"]["reviewer_authentication"] == "unverified_preview"
        fragments = client.get(f"/api/sources/{point['source_id']}/fragments").json()
        assert any(row["locator"] == point["source_locator"] for row in fragments)

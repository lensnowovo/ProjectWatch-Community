"""A financing disclosure is not an investment position."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from project_watch.analysis.holding_projection import screen_holding_snapshots
from project_watch.domain import HoldingSnapshot, Project, SourceVersion
from project_watch.report import build_current_brief

NOW = datetime(2026, 9, 29, tzinfo=UTC)
SOURCE = SourceVersion("source-1", "project-1", "document", "synthetic-holdings.pdf", "a" * 64, NOW)


def snapshot(
    fund: str,
    as_of: date,
    cost: str | None,
    value: str | None,
    *,
    snapshot_id: str = "position-1",
    status: str = "confirmed",
    project_id: str = "project-1",
    ownership: str | None = None,
) -> HoldingSnapshot:
    return HoldingSnapshot(
        id=snapshot_id,
        project_id=project_id,
        fund_name=fund,
        as_of=as_of,
        source_id=SOURCE.id,
        source_locator="page 2",
        invested_cost_wan=Decimal(cost) if cost else None,
        ownership_pct=Decimal(ownership) if ownership else None,
        current_value_wan=Decimal(value) if value else None,
        status=status,
        version=2 if status == "confirmed" else 1,
        proposed_by="analyst",
        reviewed_by="reviewer" if status == "confirmed" else None,
        review_note=None,
        created_at=NOW,
        updated_at=NOW,
    )


def test_latest_reviewed_position_is_per_fund_and_source_bound() -> None:
    rows = [
        snapshot("甲基金", date(2025, 12, 31), "1000", "1200", snapshot_id="old"),
        snapshot("甲基金", date(2026, 6, 30), "1100", "1430", snapshot_id="new", ownership="8"),
        snapshot("乙基金", date(2026, 3, 31), "500", None, snapshot_id="other"),
    ]
    result = screen_holding_snapshots("project-1", rows, [SOURCE])
    assert result["status"] == "evidence_available"
    assert [item["snapshot_id"] for item in result["positions"]] == ["new", "other"]
    assert result["positions"][0]["moc"] == "1.3"
    assert result["positions"][0]["source_sha256"] == SOURCE.content_sha256
    assert result["positions"][0]["reviewed_by"] == "reviewer"
    assert result["positions"][1]["moc"] is None
    assert "total" not in result


def test_same_fund_date_conflict_does_not_choose_a_value_or_fall_back() -> None:
    rows = [
        snapshot("甲基金", date(2025, 12, 31), "1000", "1200", snapshot_id="old"),
        snapshot("甲基金", date(2026, 6, 30), "1100", "1430", snapshot_id="first"),
        snapshot("甲基金", date(2026, 6, 30), "1100", "1500", snapshot_id="second"),
    ]
    result = screen_holding_snapshots("project-1", rows, [SOURCE])
    assert result["status"] == "needs_review"
    assert result["positions"] == []
    assert result["conflicts"][0]["fund_name"] == "甲基金"
    assert set(result["conflicts"][0]["snapshot_ids"]) == {"first", "second"}


def test_candidates_rejections_foreign_projects_and_sources_are_not_positions() -> None:
    rows = [
        snapshot("甲基金", date(2026, 6, 30), "1100", "1430", status="candidate"),
        snapshot(
            "甲基金", date(2026, 6, 30), "1100", "1430", status="rejected", snapshot_id="rejected"
        ),
        snapshot(
            "乙基金", date(2026, 6, 30), "500", "600", project_id="foreign", snapshot_id="foreign"
        ),
    ]
    result = screen_holding_snapshots("project-1", rows, [SOURCE])
    assert result["status"] == "needs_review"
    assert result["pending_count"] == 1
    assert result["positions"] == []
    assert screen_holding_snapshots("project-1", [], [SOURCE])["status"] == "missing"


def test_brief_exposes_reviewed_holdings_without_promoting_pending_rows() -> None:
    reviewed = snapshot("甲基金", date(2026, 6, 30), "1100", "1430", ownership="8")
    pending = snapshot("乙基金", date(2026, 6, 30), "500", "600", snapshot_id="pending", status="candidate")
    brief = build_current_brief(
        Project("project-1", "合成项目", "verified", NOW),
        [], [SOURCE], [], [], [], as_of=NOW,
        holding_snapshots=[reviewed, pending],
    )
    assert [row["fund_name"] for row in brief["holdings"]["positions"]] == ["甲基金"]
    assert brief["holdings"]["positions"][0]["moc"] == "1.3"
    assert brief["holdings"]["pending_count"] == 1


def test_new_project_documents_after_review_prompt_recheck_without_replacing_position() -> None:
    reviewed = snapshot("甲基金", date(2026, 6, 30), "1100", "1430")
    sources = [
        SOURCE,
        SourceVersion("new-doc", "project-1", "document", "later.pdf", "b" * 64, NOW + timedelta(days=1)),
        SourceVersion("new-note", "project-1", "note", "later note", "c" * 64, NOW + timedelta(days=1)),
        SourceVersion("foreign", "another-project", "document", "other.pdf", "d" * 64, NOW + timedelta(days=1)),
    ]
    result = screen_holding_snapshots("project-1", [reviewed], sources)
    assert result["status"] == "evidence_available"
    assert result["positions"][0]["invested_cost_wan"] == "1100"
    assert result["positions"][0]["new_material_count_since_review"] == 1
    historical = screen_holding_snapshots(
        "project-1", [reviewed], sources, as_of=NOW
    )
    assert historical["positions"][0]["new_material_count_since_review"] == 0

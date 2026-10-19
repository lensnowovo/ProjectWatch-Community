"""Only comparable, confirmed source-bound observations enter the DB screen."""

from datetime import UTC, datetime
from decimal import Decimal

from project_watch.analysis.financial_projection import screen_confirmed_financials
from project_watch.domain import Observation, SourceVersion

NOW = datetime(2026, 9, 27, tzinfo=UTC)
SOURCE = SourceVersion("s1", "p1", "document", "synthetic.xlsx", "a" * 64, NOW)


def metric(
    name: str,
    period: str,
    value: str,
    *,
    status: str = "confirmed",
    basis: str = "actual",
    unit: str = "万元",
    scope: str = "合并",
    source_id: str = "s1",
) -> Observation:
    return Observation(
        id=f"{name}-{period}-{value}",
        project_id="p1",
        source_id=source_id,
        subject="财务表现",
        statement=name,
        number=Decimal(value),
        unit=unit,
        period=period,
        scope=scope,
        source_locator=f"财务表:{period}!B4",
        status=status,
        version=2 if status == "confirmed" else 1,
        created_at=NOW,
        basis=basis,
    )


def test_screen_reuses_db_rule_without_candidates_forecasts_or_other_scopes() -> None:
    result = screen_confirmed_financials(
        [
            metric("营业收入", "2024", "100"),
            metric("营业收入", "2025", "120"),
            metric("应收账款", "2024", "200000", unit="元"),
            metric("应收账款", "2025", "48"),
            metric("现金", "2025", "999", status="candidate"),
            metric("营业收入", "2026", "200", basis="forecast"),
            metric("营业收入", "2025", "500", scope="单体"),
        ],
        [SOURCE],
    )
    check = next(
        item
        for item in result["analysis"]["checks"]
        if item["key"] == "revenue_receivable_divergence"
    )
    assert check["status"] == "warning"
    assert check["values"]["receivable_growth_pct"] == 140.0
    assert result["accepted_count"] == 4
    assert result["excluded"] == {
        "unreviewed": 1,
        "non_actual": 1,
        "scope_mismatch": 1,
    }
    assert len(result["evidence"]) == 4
    assert all(item["source_sha256"] == "a" * 64 for item in result["evidence"])


def test_conflicting_same_period_is_withheld_not_silently_overwritten() -> None:
    result = screen_confirmed_financials(
        [
            metric("营业收入", "2024", "100"),
            metric("营业收入", "2025", "120"),
            metric("营业收入", "2025", "130"),
        ],
        [SOURCE],
    )
    assert result["accepted_count"] == 1
    assert result["excluded"]["same_period_conflict"] == 2
    assert result["analysis"]["status"] == "partial"


def test_missing_source_and_nonstandard_unit_never_enter_screen() -> None:
    result = screen_confirmed_financials(
        [
            metric("营业收入", "2025", "120", source_id="other"),
            metric("营业收入", "2025", "120", unit="美元"),
        ],
        [SOURCE],
    )
    assert result["analysis"]["status"] == "insufficient_data"
    assert result["excluded"] == {"source_missing": 1, "unit_unmapped": 1}

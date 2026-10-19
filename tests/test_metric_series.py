from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from project_watch.domain import Observation, SourceVersion
from project_watch.metric_series import build_metric_series

NOW = datetime(2026, 9, 27, tzinfo=UTC)
SOURCE = SourceVersion("s1", "p1", "document", "财务报表.xlsx", "a" * 64, NOW)


def observation(
    observation_id: str,
    period: str,
    value: str,
    *,
    status: str = "confirmed",
    unit: str = "万元",
    scope: str | None = "合并",
    subject: str = "财务表现",
    statement: str = "营业收入",
    basis: str = "unknown",
) -> Observation:
    return Observation(
        id=observation_id,
        project_id="p1",
        source_id="s1",
        subject=subject,
        statement=statement,
        number=Decimal(value),
        unit=unit,
        period=period,
        scope=scope,
        source_locator="工作表:利润表!B4",
        status=status,
        version=2 if status == "confirmed" else 1,
        created_at=NOW,
        basis=basis,
    )


def test_only_confirmed_numeric_observations_form_series() -> None:
    result = build_metric_series(
        [
            observation("old", "2024", "0"),
            observation("new", "2025", "-5"),
            observation("candidate", "2026", "99", status="candidate"),
        ],
        [SOURCE],
    )

    assert result["total_series"] == 1
    assert result["unsequenced_count"] == 0
    assert [point["observation_id"] for point in result["series"][0]["points"]] == [
        "old",
        "new",
    ]
    assert [point["number"] for point in result["series"][0]["points"]] == [
        Decimal("0"),
        Decimal("-5"),
    ]
    assert result["series"][0]["points"][0]["source_sha256"] == "a" * 64


def test_cadence_unit_scope_and_topic_never_merge() -> None:
    result = build_metric_series(
        [
            observation("year", "2025", "1"),
            observation("quarter", "2025-Q1", "2"),
            observation("month", "2025-03", "3"),
            observation("unit", "2025", "4", unit="元"),
            observation("scope", "2025", "5", scope="单体"),
            observation("topic", "2025", "6", subject="经营情况"),
        ],
        [SOURCE],
    )

    assert result["total_series"] == 6
    assert {series["cadence"] for series in result["series"]} == {
        "year",
        "quarter",
        "month",
    }


def test_actual_forecast_budget_and_unknown_never_share_a_series() -> None:
    result = build_metric_series(
        [
            observation("a", "2025", "120", basis="actual"),
            observation("f", "2026", "180", basis="forecast"),
            observation("b", "2026", "160", basis="budget"),
            observation("u", "2024", "100", basis="unknown"),
        ],
        [SOURCE],
    )
    assert result["total_series"] == 4
    assert {series["basis"] for series in result["series"]} == {
        "actual",
        "forecast",
        "budget",
        "unknown",
    }
    assert all(series["total_points"] == 1 for series in result["series"])


def test_duplicate_period_is_visible_as_conflict_not_silently_selected() -> None:
    result = build_metric_series(
        [observation("a", "2025-Q2", "10"), observation("b", "2025-Q2", "12")],
        [SOURCE],
    )

    points = result["series"][0]["points"]
    assert len(points) == 2
    assert all(point["same_period_conflict"] for point in points)


def test_nonstandard_periods_remain_counted_but_are_not_charted() -> None:
    result = build_metric_series(
        [
            observation("bad-quarter", "2025-Q5", "1"),
            observation("bad-month", "2025-13", "2"),
            observation("free", "2025年度", "3"),
            observation("not-number", "2025", "4", unit=""),
        ],
        [SOURCE],
    )

    assert result["series"] == []
    assert result["unsequenced_count"] == 4


def test_series_and_points_are_bounded_without_losing_counts() -> None:
    rows = [observation(str(year), str(year), str(year)) for year in range(2010, 2027)]
    rows.append(observation("profit", "2025", "1", statement="净利润"))
    result = build_metric_series(rows, [SOURCE], limit=1, points_per_series=3)

    assert result["total_series"] == 2
    assert result["truncated"] is True
    revenue = next(series for series in result["series"] if series["name"] == "营业收入")
    assert [point["period"] for point in revenue["points"]] == ["2024", "2025", "2026"]
    assert revenue["total_points"] == 17
    assert revenue["truncated_points"] is True


def test_latest_annual_actual_change_requires_adjacent_located_financial_periods() -> None:
    result = build_metric_series(
        [
            observation("old", "2024", "80", basis="actual"),
            observation("new", "2025", "100", basis="actual"),
        ],
        [SOURCE],
    )
    change = result["series"][0]["latest_change"]
    assert change == {
        "status": "comparable",
        "previous_period": "2024",
        "period": "2025",
        "absolute": Decimal("20"),
        "percent": Decimal("25.00"),
        "previous_observation_id": "old",
        "observation_id": "new",
    }


def test_latest_change_never_uses_forecast_or_skips_a_year() -> None:
    forecast = build_metric_series(
        [
            observation("a", "2024", "80", basis="forecast"),
            observation("b", "2025", "100", basis="forecast"),
        ],
        [SOURCE],
    )
    assert forecast["series"][0]["latest_change"]["status"] == "non_actual"
    gap = build_metric_series(
        [
            observation("a", "2023", "80", basis="actual"),
            observation("b", "2025", "100", basis="actual"),
        ],
        [SOURCE],
    )
    assert gap["series"][0]["latest_change"]["status"] == "missing_previous"


def test_latest_change_quarantines_same_period_conflict_and_nonpositive_base() -> None:
    conflict = build_metric_series(
        [
            observation("a", "2024", "80", basis="actual"),
            observation("b", "2024", "90", basis="actual"),
            observation("c", "2025", "100", basis="actual"),
        ],
        [SOURCE],
    )
    assert conflict["series"][0]["latest_change"]["status"] == "same_period_conflict"
    zero = build_metric_series(
        [
            observation("a", "2024", "0", basis="actual"),
            observation("b", "2025", "100", basis="actual"),
        ],
        [SOURCE],
    )
    assert zero["series"][0]["latest_change"]["status"] == "nonpositive_baseline"


def test_latest_change_requires_financial_currency_and_consolidated_scope() -> None:
    rows = [
        observation("a", "2024", "80", basis="actual"),
        observation("b", "2025", "100", basis="actual"),
    ]
    assert (
        build_metric_series(rows, [SOURCE])["series"][0]["latest_change"]["status"] == "comparable"
    )
    assert (
        build_metric_series(
            [
                observation("a", "2024", "80", basis="actual", unit="%"),
                observation("b", "2025", "100", basis="actual", unit="%"),
            ],
            [SOURCE],
        )["series"][0]["latest_change"]["status"]
        == "unsupported_basis"
    )
    assert (
        build_metric_series(
            [
                observation("a", "2024", "80", basis="actual", scope="单体"),
                observation("b", "2025", "100", basis="actual", scope="单体"),
            ],
            [SOURCE],
        )["series"][0]["latest_change"]["status"]
        == "unsupported_basis"
    )


def test_cross_project_observations_cannot_form_a_derived_change() -> None:
    other_source = replace(SOURCE, id="s2", project_id="p2")
    other_observation = replace(
        observation("p2-2025", "2025", "100", basis="actual"),
        project_id="p2",
        source_id="s2",
    )
    result = build_metric_series(
        [observation("p1-2024", "2024", "80", basis="actual"), other_observation],
        [SOURCE, other_source],
    )
    assert result["total_series"] == 2
    assert all(row["latest_change"]["status"] == "missing_previous" for row in result["series"])

"""Operating metrics never mix capacity, output, plans, or product lines."""

from datetime import UTC, datetime
from decimal import Decimal

from project_watch.analysis.monitoring_projection import build_monitoring_projection
from project_watch.domain import Observation, SourceVersion

NOW = datetime(2026, 9, 29, tzinfo=UTC)
SOURCE = SourceVersion("source-1", "project-1", "document", "synthetic-ops.xlsx", "f" * 64, NOW)


def metric(
    name: str,
    value: str,
    unit: str,
    scope: str | None,
    *,
    basis: str = "actual",
    period: str = "2026-09",
    status: str = "confirmed",
    observation_id: str | None = None,
    source_id: str = "source-1",
    project_id: str = "project-1",
) -> Observation:
    return Observation(
        id=observation_id or f"{name}-{basis}-{scope}-{period}",
        project_id=project_id,
        source_id=source_id,
        subject="经营情况",
        statement=name,
        number=Decimal(value),
        unit=unit,
        period=period,
        scope=scope,
        source_locator="sheet 1!B4",
        status=status,
        version=2 if status == "confirmed" else 1,
        created_at=NOW,
        basis=basis,
    )


def screen(*records: Observation) -> dict:
    return build_monitoring_projection("project-1", list(records), [SOURCE], [])[
        "operating_metrics"
    ]


def test_actual_capacity_output_and_forecast_remain_separate_with_evidence() -> None:
    result = screen(
        metric("设计产能", "1200", "台/年", "A 产品线"),
        metric("实际产量", "80", "台", "A 产品线"),
        metric("设计产能", "1500", "台/年", "A 产品线", basis="forecast"),
        metric("产能利用率", "67", "%", "A 产品线"),
    )
    assert result["status"] == "evidence_available"
    assert result["actual_count"] == 3
    assert result["planned_count"] == 1
    assert result["pending_count"] == 0
    assert {item["basis"] for item in result["evidence"]} == {"actual", "forecast"}
    assert all(item["source_sha256"] == SOURCE.content_sha256 for item in result["evidence"])
    assert all(item["source_locator"] == "sheet 1!B4" for item in result["evidence"])


def test_candidate_unknown_basis_invalid_period_scope_and_ratio_wait_for_review() -> None:
    result = screen(
        metric("实际产量", "12", "台", "A 产品线", status="candidate"),
        metric("设计产能", "12", "台/年", "A 产品线", basis="unknown"),
        metric("实际产量", "12", "台", "A 产品线", period="本月"),
        metric("实际产量", "12", "台", None),
        metric("产能利用率", "105", "%", "A 产品线"),
        metric("实际产量", "12", "台", "A 产品线", source_id="foreign"),
        metric("实际产量", "12", "台", "A 产品线", project_id="foreign"),
    )
    assert result["status"] == "needs_review"
    assert result["actual_count"] == 0
    assert result["pending_count"] == 5
    assert result["evidence"] == []


def test_same_metric_period_scope_and_basis_conflict_even_if_units_differ() -> None:
    result = screen(
        metric("实际产量", "80", "台", "A 产品线", observation_id="first"),
        metric("实际产量", "8", "万台", "A 产品线", observation_id="second"),
    )
    assert result["status"] == "needs_review"
    assert result["conflict_count"] == 2
    assert result["evidence"] == []


def test_other_products_and_unmapped_words_are_not_summed_into_capacity() -> None:
    result = screen(
        metric("实际产量", "80", "台", "A 产品线"),
        metric("实际产量", "20", "台", "B 产品线"),
        metric("产品型号", "30", "种", "A 产品线"),
    )
    assert result["actual_count"] == 2
    assert {item["scope"] for item in result["evidence"]} == {"A 产品线", "B 产品线"}
    assert all(item["metric"] == "实际产量" for item in result["evidence"])


def test_plan_only_is_not_reported_as_actual_baseline() -> None:
    result = screen(metric("设计产能", "1500", "台/年", "A 产品线", basis="forecast"))
    assert result["status"] == "needs_review"
    assert result["actual_count"] == 0
    assert result["planned_count"] == 1
    assert "不等于实际" in result["note"]


def test_monetary_unit_cannot_be_used_as_output() -> None:
    result = screen(metric("实际产量", "80", "万元", "A 产品线"))
    assert result["actual_count"] == 0
    assert result["pending_count"] == 1

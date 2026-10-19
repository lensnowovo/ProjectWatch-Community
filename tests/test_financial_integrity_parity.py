"""Synthetic parity case for the DB's deterministic integrity screen."""

from dataclasses import dataclass

from project_watch.analysis.financial_integrity import build_financial_integrity_analysis


@dataclass(frozen=True)
class Row:
    kpi_name: str
    period: str
    value: float
    unit: str = "万元"
    as_of_date: None = None


def test_receivable_growth_outpacing_revenue_matches_db_rule() -> None:
    result = build_financial_integrity_analysis(
        [
            Row("revenue", "2024", 100),
            Row("revenue", "2025", 120),
            Row("accounts_receivable", "2024", 20),
            Row("accounts_receivable", "2025", 48),
        ]
    )
    assert result["engine_version"] == "financial-integrity-v1.1.0"
    check = next(
        item for item in result["checks"] if item["key"] == "revenue_receivable_divergence"
    )
    assert check["status"] == "warning"
    assert check["values"] == {
        "revenue_growth_pct": 20.0,
        "receivable_growth_pct": 140.0,
        "divergence_pct": 120.0,
        "receivable_to_revenue_pct": 40.0,
    }
    assert check["formula"] and check["evidence_needed"]


def test_missing_financial_statement_fields_are_gaps_not_clean_bills() -> None:
    result = build_financial_integrity_analysis([Row("revenue", "2025", 120)])
    checks = {item["key"]: item for item in result["checks"]}
    assert checks["balance_sheet_equation"]["status"] == "gap"
    assert checks["balance_sheet_equation"]["evidence_needed"]

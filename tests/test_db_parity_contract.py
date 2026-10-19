"""Contract frozen from the DB monitoring design using synthetic values only."""

import json
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "monitoring-golden.json"
DOMAINS = {
    "capital",
    "financials",
    "financial_integrity",
    "operations_products",
    "external_signals",
    "competitive_position",
}


def test_golden_case_covers_six_db_monitoring_domains_without_private_data() -> None:
    case = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert case["fixture"] == "synthetic-watch-two-periods"
    assert case["project"] == "示例甲公司"
    assert set(case["expected"]["domains"]) == DOMAINS
    assert case["expected"]["coverage"]["total"] == len(DOMAINS)
    assert case["expected"]["coverage"]["covered"] == 2
    assert case["expected"]["candidate_in_confirmed_series"] is False
    assert all(
        metric["source"].startswith("synthetic-") for metric in case["inputs"]["confirmed_metrics"]
    )


def test_golden_case_distinguishes_unavailable_source_from_empty_source() -> None:
    case = json.loads(FIXTURE.read_text(encoding="utf-8"))
    statuses = {row["source"]: row["status"] for row in case["inputs"]["external_source_runs"]}
    assert statuses == case["expected"]["external_source_status"]
    assert statuses["regulator"] == "unreachable"

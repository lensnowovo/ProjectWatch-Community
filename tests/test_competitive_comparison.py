"""Competitor numbers must not look comparable without a reviewed common basis."""

from dataclasses import replace
from decimal import Decimal

from project_watch.analysis.competitive_comparison import (
    CompetitiveMetric,
    compare_competitive_metrics,
)


def metric(entity: str, value: str, *, source_id: str) -> CompetitiveMetric:
    return CompetitiveMetric(
        id=f"{entity}-{source_id}",
        entity_id=entity,
        name="营业收入",
        value=Decimal(value),
        unit="万元",
        period="2025",
        scope="合并",
        basis="actual",
        source_id=source_id,
        source_sha256="a" * 64,
        source_locator="利润表:B3",
        review_status="confirmed",
    )


def test_only_reviewed_same_basis_numbers_are_compared_with_both_sources() -> None:
    own = metric("subject", "120", source_id="own-source")
    peer = metric("peer-a", "100", source_id="peer-source")
    draft = replace(metric("peer-b", "80", source_id="draft-source"), review_status="candidate")

    result = compare_competitive_metrics("subject", ["peer-a", "peer-b"], [own, peer, draft])

    assert result["status"] == "comparable"
    assert len(result["comparisons"]) == 1
    comparison = result["comparisons"][0]
    assert comparison["name"] == "营业收入"
    assert comparison["period"] == "2025"
    assert comparison["unit"] == "万元"
    assert comparison["scope"] == "合并"
    assert comparison["basis"] == "actual"
    assert comparison["subject"]["value"] == "120"
    assert comparison["subject"]["source_id"] == "own-source"
    assert comparison["peers"] == [
        {
            "entity_id": "peer-a",
            "value": "100",
            "source_id": "peer-source",
            "source_sha256": "a" * 64,
            "source_locator": "利润表:B3",
            "metric_id": "peer-a-peer-source",
        }
    ]
    assert result["rank"] is None
    assert [(gap["entity_id"], gap["reason"]) for gap in result["gaps"]] == [
        ("peer-b", "pending_review")
    ]


def test_period_unit_scope_basis_and_name_mismatches_never_compare() -> None:
    own = metric("subject", "120", source_id="own")
    mismatches = [
        replace(metric("peer-period", "100", source_id="p"), period="2024"),
        replace(metric("peer-unit", "100", source_id="u"), unit="元"),
        replace(metric("peer-scope", "100", source_id="s"), scope="单体"),
        replace(metric("peer-basis", "100", source_id="b"), basis="forecast"),
        replace(metric("peer-name", "100", source_id="n"), name="净利润"),
    ]

    result = compare_competitive_metrics(
        "subject", [row.entity_id for row in mismatches], [own, *mismatches]
    )

    assert result["status"] == "insufficient_evidence"
    assert result["comparisons"] == []
    assert {(gap["entity_id"], gap["reason"]) for gap in result["gaps"]} == {
        (row.entity_id, "different_metric" if row.entity_id == "peer-name" else "different_basis")
        for row in mismatches
    }


def test_conflicting_values_or_missing_locator_block_that_entity_without_hiding_others() -> None:
    own = metric("subject", "120", source_id="own")
    peer = metric("peer-a", "100", source_id="peer-a-1")
    conflicting = replace(metric("peer-a", "105", source_id="peer-a-2"), id="conflict")
    missing_locator = replace(metric("peer-b", "110", source_id="peer-b"), source_locator="")
    clean = metric("peer-c", "90", source_id="peer-c")

    result = compare_competitive_metrics(
        "subject",
        ["peer-a", "peer-b", "peer-c"],
        [own, peer, conflicting, missing_locator, clean],
    )

    assert result["status"] == "comparable"
    assert [row["entity_id"] for row in result["comparisons"][0]["peers"]] == ["peer-c"]
    assert [(gap["entity_id"], gap["reason"]) for gap in result["gaps"]] == [
        ("peer-a", "conflicting_values"),
        ("peer-b", "missing_source"),
    ]


def test_unapproved_peers_and_other_entities_cannot_enter_comparison() -> None:
    result = compare_competitive_metrics(
        "subject",
        ["peer-a"],
        [
            metric("subject", "120", source_id="own"),
            metric("peer-a", "100", source_id="allowed"),
            metric("peer-b", "80", source_id="foreign"),
        ],
    )

    assert [row["entity_id"] for row in result["comparisons"][0]["peers"]] == ["peer-a"]
    assert "peer-b" not in str(result)


def test_no_reviewed_peer_set_or_subject_number_cannot_claim_comparability() -> None:
    no_peers = compare_competitive_metrics(
        "subject", [], [metric("subject", "120", source_id="own")]
    )
    assert no_peers["status"] == "missing_peer_set"
    assert no_peers["comparisons"] == []

    no_subject = compare_competitive_metrics(
        "subject", ["peer-a"], [metric("peer-a", "100", source_id="peer")]
    )
    assert no_subject["status"] == "insufficient_evidence"
    assert no_subject["comparisons"] == []
    assert no_subject["gaps"] == [{"entity_id": "subject", "reason": "missing_subject_metric"}]


def test_subject_conflict_blocks_all_peers_and_untrusted_hash_is_not_evidence() -> None:
    own = metric("subject", "120", source_id="own-a")
    conflicting = replace(metric("subject", "125", source_id="own-b"), id="own-conflict")
    peer = metric("peer-a", "100", source_id="peer")
    blocked = compare_competitive_metrics("subject", ["peer-a"], [own, conflicting, peer])
    assert blocked["status"] == "insufficient_evidence"
    assert blocked["comparisons"] == []
    assert [(row["entity_id"], row["reason"]) for row in blocked["gaps"]] == [
        ("peer-a", "conflicting_subject_values")
    ]

    untrusted = replace(peer, source_sha256="invalid")
    result = compare_competitive_metrics("subject", ["peer-a"], [own, untrusted])
    assert result["status"] == "insufficient_evidence"
    assert result["gaps"][0]["reason"] == "missing_source"


def test_multiple_metrics_keep_gaps_attached_to_their_own_period_and_name() -> None:
    revenue = metric("subject", "120", source_id="revenue-own")
    profit = replace(
        metric("subject", "12", source_id="profit-own"), name="净利润", id="subject-profit"
    )
    peer_revenue = metric("peer-a", "100", source_id="revenue-peer")
    result = compare_competitive_metrics("subject", ["peer-a"], [revenue, profit, peer_revenue])

    assert [row["name"] for row in result["comparisons"]] == ["营业收入"]
    assert result["gaps"][0]["name"] == "净利润"
    assert result["gaps"][0]["reason"] == "different_metric"

"""Capital coverage must not invent a holding from generic financing notes."""

from datetime import UTC, datetime
from decimal import Decimal

from project_watch.analysis.monitoring_projection import build_monitoring_projection
from project_watch.domain import Observation, SourceVersion

NOW = datetime(2026, 9, 29, tzinfo=UTC)
SOURCE = SourceVersion("source-1", "project-1", "document", "synthetic-round.pdf", "a" * 64, NOW)


def capital_observation(
    name: str,
    number: str | None,
    unit: str | None,
    scope: str | None,
    *,
    status: str = "confirmed",
    source_id: str = "source-1",
    project_id: str = "project-1",
    observation_id: str | None = None,
    basis: str = "actual",
) -> Observation:
    return Observation(
        id=observation_id or f"{name}-{status}",
        project_id=project_id,
        source_id=source_id,
        subject="资本与持仓",
        statement=name,
        number=Decimal(number) if number is not None else None,
        unit=unit,
        period="2026-09",
        scope=scope,
        source_locator="page 3",
        status=status,
        version=2 if status == "confirmed" else 1,
        created_at=NOW,
        basis=basis,
    )


def capital_domain(*observations: Observation) -> dict:
    result = build_monitoring_projection("project-1", list(observations), [SOURCE], [])
    return next(row for row in result["domains"] if row["key"] == "capital")


def test_confirmed_investment_and_holding_have_clickable_evidence() -> None:
    capital = capital_domain(
        capital_observation("本机构投资金额", "1200", "万元", "本机构"),
        capital_observation("本机构持股比例", "8.5", "%", "本机构"),
    )
    assert capital["status"] == "evidence_available"
    assert capital["evidence_count"] == 2
    assert capital["pending_count"] == 0
    assert {row["metric"] for row in capital["evidence"]} == {"本机构投资金额", "本机构持股比例"}
    assert all(row["source_id"] == SOURCE.id for row in capital["evidence"])
    assert all(row["source_sha256"] == SOURCE.content_sha256 for row in capital["evidence"])
    assert all(row["source_locator"] == "page 3" for row in capital["evidence"])


def test_candidate_or_unstructured_note_does_not_become_a_holding() -> None:
    capital = capital_domain(
        capital_observation("本机构持股比例", "9", "%", "本机构", status="candidate"),
        capital_observation("融资情况", None, None, None),
    )
    assert capital["status"] == "needs_review"
    assert capital["evidence_count"] == 0
    assert capital["pending_count"] == 2
    assert capital["evidence"] == []


def test_company_round_is_not_labeled_institutional_holding() -> None:
    capital = capital_domain(capital_observation("公司融资额", "5000", "万元", "项目公司"))
    assert capital["status"] == "evidence_available"
    assert capital["evidence_count"] == 1
    assert "不等于本机构持仓" in capital["note"]


def test_invalid_scope_unit_basis_and_cross_project_source_are_not_adopted() -> None:
    capital = capital_domain(
        capital_observation("本机构投资金额", "1200", "万元", "项目公司"),
        capital_observation("本机构持股比例", "110", "%", "本机构"),
        capital_observation("投后估值", "3", "倍", "项目公司"),
        capital_observation("公司融资额", "5000", "万元", "项目公司", basis="forecast"),
        capital_observation("本机构投资金额", "800", "万元", "本机构", source_id="foreign"),
        capital_observation("本机构投资金额", "900", "万元", "本机构", project_id="foreign"),
    )
    assert capital["status"] == "needs_review"
    assert capital["evidence_count"] == 0
    assert capital["pending_count"] == 4


def test_conflicting_same_period_position_values_wait_for_review() -> None:
    capital = capital_domain(
        capital_observation("本机构持股比例", "8", "%", "本机构", observation_id="first"),
        capital_observation("本机构持股比例", "9", "%", "本机构", observation_id="second"),
    )
    assert capital["status"] == "needs_review"
    assert capital["evidence_count"] == 0
    assert capital["pending_count"] == 2
    assert capital["evidence"] == []

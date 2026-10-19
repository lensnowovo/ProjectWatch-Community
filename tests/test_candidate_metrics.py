"""Numeric executor proposals stay literal, bounded and human-reviewable."""

from decimal import Decimal

import pytest

from project_watch.candidate_metrics import validate_candidate_metric


def test_text_proposal_keeps_old_contract() -> None:
    assert validate_candidate_metric(
        number=None,
        unit=None,
        period=None,
        scope=None,
        statement="资料称已经投产。",
        fragment_text="资料称已经投产。",
    ) == (None, None, None, None)


@pytest.mark.parametrize(
    ("evidence", "number"),
    [
        ("营业收入 1,200.50 万元", "1200.50"),
        ("净利润 -12.5 万元", "-12.5"),
        ("亏损 (1,200) 万元", "-1200"),
        ("客户数量0家", "0"),
    ],
)
def test_literal_numeric_value_round_trips(evidence: str, number: str) -> None:
    result = validate_candidate_metric(
        number=number,
        unit=" 万元 ",
        period=" 2025-Q4 ",
        scope=" 合并 ",
        statement="营业收入",
        fragment_text=evidence,
    )
    assert result == (Decimal(number), "万元", "2025-Q4", "合并")


@pytest.mark.parametrize(
    ("number", "unit", "period", "statement", "evidence"),
    [
        (None, "万元", "2025", "营业收入", "1200 万元"),
        ("1200", None, "2025", "营业收入", "1200 万元"),
        ("1200", "万元", None, "营业收入", "1200 万元"),
        ("NaN", "万元", "2025", "营业收入", "NaN"),
        ("Infinity", "万元", "2025", "营业收入", "Infinity"),
        ("1e100000", "万元", "2025", "营业收入", "1e100000"),
        ("1200", "万元", "2025年", "营业收入", "1200 万元"),
        ("1200", "万元", "2025-Q5", "营业收入", "1200 万元"),
        ("1200", "万元", "2025-13", "营业收入", "1200 万元"),
        ("1200", "万元", "2025", "营业收入", "2025 年收入未披露"),
        ("1200", "万元", "2025", "X" * 81, "1200 万元"),
    ],
)
def test_invalid_metric_is_rejected_without_guessing(
    number: str | None,
    unit: str | None,
    period: str | None,
    statement: str,
    evidence: str,
) -> None:
    with pytest.raises(ValueError):
        validate_candidate_metric(
            number=number,
            unit=unit,
            period=period,
            scope=None,
            statement=statement,
            fragment_text=evidence,
        )
